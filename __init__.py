"""ComfyUI nodes that run the MOSS-Image prompt-enhancer skill inside a workflow.

Wiring:  [MOSS Enhancer System Prompt] -> system_prompt of any LLM node
         (Generate Text / Claude / Gemini / OpenAI / OpenRouter)
         LLM output -> [MOSS Compile Prompt] -> prompt -> CLIP Text Encode
                                             -> width/height -> Empty latent
"""

import json
import math
import os
import re

from .compile_prompt import centered_box, compile_prompt, reject_constant, unique_object

_HERE = os.path.dirname(os.path.abspath(__file__))

_MODE_HINTS = {
    "auto": "",
    "caption": "\n\nFor this request, return ONLY a caption object: {\"caption\": \"...\"}.",
    "layout": "\n\nFor this request, return ONLY a layout object (root fields width, height, "
              "bbox_coord_space, bbox_format, summary, items). Do not wrap it in another key "
              "and do not add a root-level caption.",
}


def _keep_section(text, keep, drop):
    """Keep the <!--keep--> section, delete the <!--drop--> one, strip markers."""
    text = re.sub(rf"<!--{drop}-->.*?<!--/{drop}-->\n?", "", text, flags=re.S)
    return re.sub(rf"<!--/?{keep}-->\n?", "", text)


class MOSSEnhancerSystemPrompt:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "mode": (["auto", "layout", "caption"], {"default": "auto"}),
                "width": ("INT", {"default": 1024, "min": 16, "max": 16384, "step": 16}),
                "height": ("INT", {"default": 1024, "min": 16, "max": 16384, "step": 16}),
                "lock_size": ("BOOLEAN", {"default": True,
                              "tooltip": "On: the LLM must use exactly this width x height. Off: it may pick a size suited to the medium."}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("system_prompt",)
    FUNCTION = "build"
    CATEGORY = "MOSS-Image/prompt enhancer"

    def build(self, mode, width, height, lock_size):
        with open(os.path.join(_HERE, "system_prompt.md"), encoding="utf-8") as f:
            text = f.read()
        if lock_size:
            rule = f"use exactly width {width} and height {height}."
        else:
            rule = (f"honor any size the user gives; with only an aspect ratio choose compatible "
                    f"pixel dimensions; otherwise default to {width}x{height} unless the medium "
                    f"suggests another shape.")
        if mode == "layout":
            text = _keep_section(text, "LAYOUT", "CAPTION")
        elif mode == "caption":
            text = _keep_section(text, "CAPTION", "LAYOUT")
        else:
            text = re.sub(r"<!--/?(CAPTION|LAYOUT)-->\n?", "", text)
        text = text.replace("{{CANVAS_RULE}}", rule).replace("{{MODE_HINT}}", _MODE_HINTS[mode])
        return (text,)


def _first_wins(pairs):
    result = {}
    for key, value in pairs:
        result.setdefault(key, value)
    return result


def _extract_json(raw, strict=False):
    """Pull the JSON object out of an LLM reply (tolerates code fences / chatter).

    Relaxed mode keeps the first value of a duplicated key; strict mode rejects it.
    """
    s = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    start = s.find("{")
    if start < 0:
        raise ValueError("LLM output contains no JSON object:\n" + raw[:500])
    try:
        obj, _ = json.JSONDecoder(object_pairs_hook=unique_object if strict else _first_wins,
                                  parse_constant=reject_constant).raw_decode(s[start:])
    except json.JSONDecodeError as e:
        raise ValueError(
            f"LLM output is not valid JSON ({e}). If it stops mid-way, the reply was cut off: "
            f"raise max_length on the LLM node (layouts often need 2000-4000 tokens)."
        ) from e
    return obj


_LAYOUT_KEYS = {"width", "height", "bbox_coord_space", "bbox_format", "summary", "items"}


_CHARACTER_WORDS = {"character", "person", "people", "human", "figure", "creature", "animal",
                    "monster", "man", "woman", "child", "girl", "boy", "subject"}


def _normalize_item(item):
    """Map invented labels onto object/character/text."""
    if not isinstance(item, dict) or item.get("label") in ("object", "character", "text"):
        return item
    item = dict(item)
    label = str(item.get("label", "")).strip().lower()
    if "text" in item:
        item["label"] = "text"
    elif label in _CHARACTER_WORDS:
        item["label"] = "character"
    else:
        item["label"] = "object"
    return item


def _normalize_layout(draft, width, height):
    """Repair common LLM slips in a layout draft before strict compilation.

    - A root-level "caption" (compile_prompt would treat the draft as caption-only):
      move it into summary.caption if that is missing, else drop it.
    - Missing width/height: use the node's size (the boxes are still bounds-checked,
      so a draft planned for a different canvas fails loudly instead of misplacing).
    - Missing bbox_coord_space/bbox_format: the system prompt only allows abs/xyxy.
    """
    if not isinstance(draft, dict):
        return draft
    # Unwrap {"caption": ..., "layout": {...}} and similar wrappers.
    for key in ("layout", "draft", "result", "output"):
        inner = draft.get(key)
        if isinstance(inner, dict) and _LAYOUT_KEYS & inner.keys():
            outer_caption = draft.get("caption")
            draft = dict(inner)
            if outer_caption is not None and "caption" not in draft:
                draft["caption"] = outer_caption
            break
    if not _LAYOUT_KEYS & draft.keys():
        return draft
    draft = dict(draft)
    if isinstance(draft.get("items"), list):
        draft["items"] = [_normalize_item(it) for it in draft["items"]]
    draft.setdefault("width", width)
    draft.setdefault("height", height)
    draft.setdefault("bbox_coord_space", "abs")
    draft.setdefault("bbox_format", "xyxy")
    if "caption" in draft:
        cap = draft.pop("caption")
        summary = draft.get("summary")
        if summary is None:
            draft["summary"] = {"caption": cap}
        elif isinstance(summary, dict) and not (isinstance(summary.get("caption"), str) and summary["caption"].strip()):
            draft["summary"] = {**summary, "caption": cap}
    return draft


def _ratio_to_size(ratio, area=1024 * 1024):
    """'16:9' -> (w, h): ~area pixels, multiples of 16 (MOSS requirement)."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[:x/]\s*(\d+(?:\.\d+)?)\s*", str(ratio))
    if not m or float(m.group(2)) == 0:
        raise ValueError(f"Unrecognised wh_ratio: {ratio!r}")
    r = float(m.group(1)) / float(m.group(2))
    w = max(16, round((area * r) ** 0.5 / 16) * 16)
    h = max(16, round((area / r) ** 0.5 / 16) * 16)
    return w, h


def _text_list(value):
    return [v for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


def _repair_layout(draft, warnings):
    """Make a layout draft acceptable to compile_prompt by dropping or fixing what is wrong.

    Returns the repaired draft, or None if no usable items are left.
    Each repair is appended to warnings.
    """
    width, height = draft["width"], draft["height"]
    if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0):
        return None
    new_w, new_h = max(16, round(width / 16) * 16), max(16, round(height / 16) * 16)
    if (new_w, new_h) != (width, height):
        warnings.append(f"size {width}x{height} is not a multiple of 16, using {new_w}x{new_h}")
    sx, sy = new_w / width, new_h / height

    items = []
    for i, item in enumerate(draft.get("items") or []):
        box = item.get("bbox") if isinstance(item, dict) else None
        caption = item.get("caption") if isinstance(item, dict) else None
        if (not isinstance(box, list) or len(box) != 4 or not all(type(v) in (int, float) and math.isfinite(v) for v in box)
                or not isinstance(caption, str) or not caption.strip()):
            warnings.append(f"dropped items[{i}]: needs a caption and a bbox of four numbers")
            continue
        x1, y1, x2, y2 = box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy
        x1, x2 = sorted((min(max(x1, 0), new_w), min(max(x2, 0), new_w)))
        y1, y2 = sorted((min(max(y1, 0), new_h), min(max(y2, 0), new_h)))
        fixed = {"label": item["label"], "caption": caption, "bbox": [x1, y1, x2, y2]}
        if not all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(fixed["bbox"], (box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy))):
            warnings.append(f"items[{i}]: bbox moved inside the canvas")
        if isinstance(item.get("text"), str) and item["text"].strip():
            fixed["text"] = item["text"]
        elif fixed["label"] == "text":
            fixed["label"] = "object"
            warnings.append(f"items[{i}]: text item has no text, treated as object")
        for key in ("tags", "palette"):
            if _text_list(item.get(key)):
                fixed[key] = _text_list(item[key])
        try:
            centered_box(fixed["bbox"], new_w, new_h, f"items[{i}].bbox")
        except ValueError as e:
            warnings.append(f"dropped items[{i}]: {e}")
            continue
        extra = item.keys() - fixed.keys() - {"text", "tags", "palette"}
        if extra:
            warnings.append(f"items[{i}]: removed unknown fields {sorted(extra)}")
        items.append(fixed)
    if not items:
        return None

    summary = draft.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    fixed_summary = {"caption": summary.get("caption") if isinstance(summary.get("caption"), str) and summary["caption"].strip() else items[0]["caption"]}
    if summary.get("canvas") in ("single", "multi_panel", "collage"):
        fixed_summary["canvas"] = summary["canvas"]
    for key in ("meta", "palette"):
        if _text_list(summary.get(key)):
            fixed_summary[key] = _text_list(summary[key])
    return {"width": new_w, "height": new_h, "bbox_coord_space": "abs", "bbox_format": "xyxy",
            "summary": fixed_summary, "items": items}


class MOSSCompilePrompt:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "llm_output": ("STRING", {"forceInput": True}),
                "caption_width": ("INT", {"default": 1024, "min": 16, "max": 16384, "step": 16,
                                  "tooltip": "Fallback canvas size: used for captions, and for layouts where the LLM left out width/height (set it to the same size as the System Prompt node). For Qwen-PE output, width x height sets the pixel budget and wh_ratio sets the shape."}),
                "caption_height": ("INT", {"default": 1024, "min": 16, "max": 16384, "step": 16}),
            },
            "optional": {
                "strict": ("BOOLEAN", {"default": False,
                           "tooltip": "Off: fix or drop what is wrong in the draft (sizes snapped to 16, boxes clamped to the canvas, bad items removed, caption used if no layout is left) and print each repair. On: reject any draft that is not exactly valid."}),
            },
        }

    RETURN_TYPES = ("STRING", "INT", "INT", "STRING")
    RETURN_NAMES = ("prompt", "width", "height", "draft")
    FUNCTION = "compile"
    CATEGORY = "MOSS-Image/prompt enhancer"

    def compile(self, llm_output, caption_width, caption_height, strict=False):
        draft = _extract_json(llm_output, strict)
        # Qwen-Image-2.1-PE-T2I output: {"rewritten_prompt": "...", "wh_ratio": "16:9"}
        if "rewritten_prompt" in draft:
            prompt = draft["rewritten_prompt"]
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("rewritten_prompt is empty")
            if draft.get("wh_ratio"):
                w, h = _ratio_to_size(draft["wh_ratio"], caption_width * caption_height)
            else:
                w, h = caption_width, caption_height
            return (prompt, w, h, json.dumps(draft, ensure_ascii=False, indent=2))
        caption = draft.get("caption")
        draft = _normalize_layout(draft, caption_width, caption_height)
        if not strict and "caption" not in draft:
            warnings = []
            repaired = _repair_layout(draft, warnings) if isinstance(draft.get("items"), list) else None
            if repaired is None:
                caption = caption if isinstance(caption, str) and caption.strip() else (draft.get("summary") or {}).get("caption")
                if not isinstance(caption, str) or not caption.strip():
                    raise ValueError("MOSS draft has no usable layout items or caption. Re-run the LLM (new seed).")
                warnings.append("no usable layout, using the caption only")
                draft = {"caption": caption}
            else:
                draft = repaired
            for w in warnings:
                print(f"MOSS Compile Prompt: {w}")
        try:
            prompt = compile_prompt(draft)
        except ValueError as e:
            raise ValueError(f"MOSS layout draft rejected: {e}\nRe-run the LLM (new seed) or fix the draft.") from e
        if "caption" in draft and len(draft) == 1:
            w, h = caption_width, caption_height
        else:
            w, h = draft["width"], draft["height"]
        pretty = json.dumps(draft, ensure_ascii=False, indent=2)
        return (prompt, w, h, pretty)


NODE_CLASS_MAPPINGS = {
    "MOSSEnhancerSystemPrompt": MOSSEnhancerSystemPrompt,
    "MOSSCompilePrompt": MOSSCompilePrompt,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "MOSSEnhancerSystemPrompt": "MOSS Enhancer System Prompt",
    "MOSSCompilePrompt": "MOSS Compile Prompt",
}

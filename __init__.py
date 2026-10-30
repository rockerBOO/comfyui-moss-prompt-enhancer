"""ComfyUI nodes that run the MOSS-Image prompt-enhancer skill inside a workflow.

Wiring:  [MOSS Enhancer System Prompt] -> system_prompt of any LLM node
         (Generate Text / Claude / Gemini / OpenAI / OpenRouter)
         LLM output -> [MOSS Compile Prompt] -> prompt -> CLIP Text Encode
                                             -> width/height -> Empty latent
"""

import json
import os
import re

from .compile_prompt import compile_prompt, reject_constant, unique_object

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


def _extract_json(raw):
    """Pull the JSON object out of an LLM reply (tolerates code fences / chatter)."""
    s = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    start = s.find("{")
    if start < 0:
        raise ValueError("LLM output contains no JSON object:\n" + raw[:500])
    try:
        obj, _ = json.JSONDecoder(object_pairs_hook=unique_object,
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


class MOSSCompilePrompt:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "llm_output": ("STRING", {"forceInput": True}),
                "caption_width": ("INT", {"default": 1024, "min": 16, "max": 16384, "step": 16,
                                  "tooltip": "Fallback canvas size: used for captions, and for layouts where the LLM left out width/height (set it to the same size as the System Prompt node). For Qwen-PE output, width x height sets the pixel budget and wh_ratio sets the shape."}),
                "caption_height": ("INT", {"default": 1024, "min": 16, "max": 16384, "step": 16}),
            }
        }

    RETURN_TYPES = ("STRING", "INT", "INT", "STRING")
    RETURN_NAMES = ("prompt", "width", "height", "draft")
    FUNCTION = "compile"
    CATEGORY = "MOSS-Image/prompt enhancer"

    def compile(self, llm_output, caption_width, caption_height):
        draft = _extract_json(llm_output)
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
        draft = _normalize_layout(draft, caption_width, caption_height)
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

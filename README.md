# MOSS-Image prompt enhancer for ComfyUI

Two ComfyUI nodes that turn a short image idea into a MOSS-Image prompt. The result is either a caption, or a JSON layout with exact text and pixel bounding boxes.

The nodes run the [moss-image-prompt-enhancer](https://github.com/OpenMOSS/MOSS-Image/tree/main/skills/moss-image-prompt-enhancer) skill from OpenMOSS/MOSS-Image inside a workflow. Any LLM node writes the draft. The skill's own `compile_prompt.py` (included unmodified) checks it and converts the boxes to MOSS coordinates. There are no extra Python dependencies.

## Install

You need a ComfyUI build with native MOSS-Image support: master/nightly at or after commit `e087d8f6`. Set up the MOSS-Image model files first, using the [ComfyUI-format models](https://huggingface.co/Comfy-Org/MOSS-Image) and the loaders described in the [MOSS-Image ComfyUI guide](https://github.com/OpenMOSS/MOSS-Image/blob/main/comfyui/README.md).

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/rockerBOO/comfyui-moss-prompt-enhancer.git
```

Restart ComfyUI. The nodes appear under **MOSS-Image/prompt enhancer**. To update, run `git pull` in the node folder and restart.

## Quick start

```
MOSS Enhancer System Prompt ──system_prompt──▶ LLM node ──text──▶ MOSS Compile Prompt
  (mode, width, height)                          ▲                   ├─ prompt ──────▶ CLIP Text Encode (+)
                                                 │                   ├─ width/height ─▶ Empty latent
your idea ──────────────────────────── prompt ───┘                   └─ draft ───────▶ Preview Any
```

1. Add **MOSS Enhancer System Prompt**. Set `mode` to `layout` for posters, infographics or anything with text, and to `caption` for photos and illustrations. Set the canvas `width` and `height`.
2. Add an LLM node: **Generate Text** (local), or the Claude, Gemini, OpenAI or OpenRouter nodes. Connect `system_prompt` and type your idea as the prompt.
   - On **Generate Text**, raise `max_length` to about 4096. The default of 512 cuts layouts off.
   - Many API nodes show `system_prompt` as a text box. Convert it to an input to connect it.
3. Connect the LLM's text output to **MOSS Compile Prompt**. Give it the same width and height as step 1.
4. Send `prompt` to **CLIP Text Encode** (positive), using a CLIP Loader set to `moss_image`. Send `width` and `height` to your empty latent.
5. Optional: send `draft` to **Preview Any** to read the plan. To tweak a layout without calling the LLM again, paste an edited draft into a text node and connect that to the compile node.

Always drive the latent from the compile node's `width` and `height`. A layout is only correct at the size it was planned for.

To start from a working graph, open `example_workflows/moss_prompt_enhancer_t2i.json` (also under Workflow > Browse Templates). Pick your own UNet, text encoder and VAE in the loader nodes, write your idea, and run it.

## Nodes

**MOSS Enhancer System Prompt** builds the system prompt for the LLM.

| Input | Default | What it does |
| --- | --- | --- |
| `mode` | `auto` | `layout` or `caption` forces that format, and the prompt then describes only that format. `auto` lets the LLM choose and shows both, which small models sometimes blend. |
| `width`, `height` | 1024 | The canvas in pixels, in multiples of 16. |
| `lock_size` | on | On: the LLM must use exactly this size. Off: it may pick a size suited to the medium. |

**MOSS Compile Prompt** turns the LLM's reply into the model prompt.

| Input or output | What it is |
| --- | --- |
| `llm_output` (input) | The LLM's reply. Code fences and text around the JSON are ignored. |
| `caption_width`, `caption_height` (input) | The fallback canvas: used for captions and for layouts that leave out width or height. Set them to the System Prompt node's size. For Qwen-Image-PE output, they set the pixel budget. |
| `strict` (optional input) | Off by default. Off: the node repairs the draft instead of failing (see Troubleshooting). On: any draft that is not exactly valid is rejected. |
| `prompt` (output) | The caption text, or the compiled one-line layout JSON. |
| `width`, `height` (outputs) | The size to generate at. |
| `draft` (output) | The parsed draft, pretty-printed for inspection. |

## Choosing an LLM

Layout quality depends mostly on the LLM. The skill was written for GPT-6-class models.

- **Layouts:** use a strong API model (Claude, Gemini, OpenAI). Small local models often break the format: a stray top-level `caption`, a wrapped layout, missing size fields or invented labels. The compile node repairs the common slips, but a model that can't follow the format also tends to plan weaker layouts.
- **Captions, running locally:** [Qwen-Image-2.1-PE-T2I](https://huggingface.co/Qwen/Qwen-Image-2.1-PE-T2I) is a 9B prompt enhancer trained for design quality ([paper](https://arxiv.org/abs/2610.33472)). Load `qwen3.5_9b_qwen_image_2.1_pe_t2i` from [Comfy-Org/Qwen-Image-2.1](https://huggingface.co/Comfy-Org/Qwen-Image-2.1) in **Generate Text**, and connect its output straight to **MOSS Compile Prompt**.
  - Use the model's own `system_prompt.txt`, not this pack's.
  - The compile node reads its `{"rewritten_prompt", "wh_ratio"}` output and turns the ratio into a MOSS size.
  - It writes prose only, never layouts.
  - It is a separate 9B model loaded alongside the MOSS text encoder, so plan for the extra VRAM.
  - It is under the Qwen Research License, which is non-commercial.

For several options, run the LLM with 3–4 seeds and generate a batch.

## Sampling

From the [MOSS-Image user guide](https://github.com/OpenMOSS/MOSS-Image/blob/main/USERGUIDE.md): 50 steps, CFG 3, shift 5, Euler with `sgm_uniform`.

- Enhanced prompts are long, so CFG 3 is usually enough. Raise it for short prompts.
- The native model already defaults to shift 5. To change it, use **ModelSamplingAuraFlow**, not ModelSamplingSD3.

## Troubleshooting

By default (`strict` off) the compile node repairs the draft and prints each repair to the console as `MOSS Compile Prompt: ...`:

- A top-level `caption` beside a layout moves into `summary.caption`, or is dropped if that is already set.
- A layout wrapped in `layout`, `draft`, `result` or `output` is unwrapped.
- A missing `width` or `height` falls back to `caption_width` and `caption_height`. A missing `bbox_coord_space` or `bbox_format` becomes `abs` or `xyxy`.
- Invented labels are mapped. Words such as `creature`, `person` or `animal` become `character`. Anything with a `text` field becomes `text`. Everything else becomes `object`.
- A width or height that is not a multiple of 16 is snapped to the nearest multiple, and the boxes are scaled with it. `ImageResizeKJv2` rounds to 8 by default, so set its `divisible_by` to 16 or the sizes get snapped.
- Boxes that fall outside the canvas are clamped to it.
- A duplicate JSON key keeps its first value.
- Unknown fields are removed. Items without a caption or a usable box are dropped. A `text` item with no `text` becomes an `object`.
- If no usable items are left, the caption is used as the prompt.

Always take `width` and `height` from the compile node, since it may have changed them.

With `strict` on, none of that happens and the node stops with an error:

| Error | Fix |
| --- | --- |
| `LLM output is not valid JSON` | The reply was cut off. Raise the LLM's `max_length` or output limit. |
| `Duplicate JSON key` | The LLM repeated a key. Rerun with a new seed. |
| `bbox exceeds the WxH canvas` | Boxes don't fit the canvas. Check that both nodes use the same size, then rerun with a new seed. |
| `width must be a positive integer multiple of 16` | Use a size divisible by 16. |
| `text items require literal text` | A text item has no `text`. Rerun with a new seed. |
| `unknown fields [...]` | The LLM added fields the schema doesn't allow. Rerun with a new seed, or use a stronger model. |

Each error names the field at fault. To see the raw reply, connect the LLM's text output to **Preview Any**. The `draft` output only appears when compiling succeeds.

## Credits and license

- [MOSS-Image](https://github.com/OpenMOSS/MOSS-Image) by OpenMOSS: the model, the prompt-enhancer skill and `compile_prompt.py` (Apache-2.0).
- [Qwen-Image-PE](https://arxiv.org/abs/2610.33472) by the Qwen team: the design checklist in caption mode follows this paper.

This pack is released under Apache-2.0. See `LICENSE`.

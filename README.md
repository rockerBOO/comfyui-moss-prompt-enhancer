# MOSS-Image prompt enhancer for ComfyUI

Runs the `moss-image-prompt-enhancer` skill from OpenMOSS/MOSS-Image as two nodes.
There are no extra dependencies. The skill's `compile_prompt.py` is included unmodified (Apache-2.0).

## Install
You need a ComfyUI build with native MOSS-Image support (master/nightly at or after `e087d8f6`).

```
cd ComfyUI/custom_nodes
git clone git@github.com:rockerBOO/comfyui-moss-prompt-enhancer.git
```

Or over HTTPS: `git clone https://github.com/rockerBOO/comfyui-moss-prompt-enhancer.git`

Restart ComfyUI. The nodes appear under **MOSS-Image/prompt enhancer**. To update, run `git pull` in the
node folder and restart.

## Wiring
```
MOSS Enhancer System Prompt ──system_prompt──▶ LLM node ──text──▶ MOSS Compile Prompt
   (mode, width, height)        (Generate Text / Claude /             │ prompt ─▶ CLIP Text Encode (+)
                                 Gemini / OpenAI / OpenRouter)        │ width/height ─▶ Empty latent
user idea ──────────────────────────────▶ LLM prompt                  │ draft ─▶ Preview Any (to inspect/edit)
```
- Many API nodes show `system_prompt` as a text box. Right-click it and convert it to an input, or paste in the text yourself.
- Always feed `width` and `height` into the latent. A layout is only correct at the size it was planned for.
- If the compile node rejects a draft (for example, a box falls outside the canvas), run the LLM again with a new seed.
  The node never clips boxes silently.
- **Qwen-Image-2.1-PE-T2I as the LLM:** load `qwen3.5_9b_qwen_image_2.1_pe_t2i` (from Comfy-Org/Qwen-Image-2.1) in **Generate Text**,
  then connect its output to **MOSS Compile Prompt**. It does not need this pack's system prompt; use the
  `system_prompt.txt` that ships with the model. The node reads `{"rewritten_prompt", "wh_ratio"}` and turns
  the ratio into MOSS dimensions at the pixel budget set by caption_width × caption_height. It produces prose
  only, with no layouts. The model is under the Qwen Research License, which is non-commercial.
- Sampling, per the MOSS user guide: 50 steps, CFG 3 (raise it for short prompts), shift 5, Euler / sgm_uniform.

# ComfyUI workflow templates

Each subdirectory is one versioned template:

```
<template_id>/
  manifest.json   # what it is, which capabilities it satisfies, and its interface
  workflow.json   # the ComfyUI API-format graph, with literal placeholder values
```

The adapter never guesses an interface. `manifest.json` declares, explicitly,
which semantic input maps to which graph node field, and what the graph outputs.
`workflow.json` is sent to ComfyUI unchanged except for the declared inputs.

## manifest.json

```json
{
  "template_id": "image_txt2img",
  "version": "1",
  "label": "Text to image (SD/SDXL checkpoint)",
  "capabilities": ["image_generation"],
  "inputs": {
    "prompt":          { "node": "6", "field": "text", "required": true,  "type": "string" },
    "steps":           { "node": "3", "field": "steps", "default": 20,     "type": "integer" },
    "ckpt_name":       { "node": "4", "field": "ckpt_name", "required": true, "type": "string" }
  },
  "outputs": {
    "images": { "node": "9", "kind": "image" }
  },
  "notes": "Anything a human needs to know before using it."
}
```

* `capabilities` must use the capability names from
  `app/services/engine_providers.py` (`CAPABILITIES`). An unknown name makes the
  template unusable rather than silently wrong.
* `inputs` is authoritative. If a caller passes a parameter the manifest does not
  declare, `bind_template` **fails** — it will not ignore it and quietly run the
  workflow with a default instead.
* `outputs` is descriptive; the adapter reports whatever ComfyUI's `/history`
  actually returns, keyed by the node that produced it.

## How a template is verified

`engine_providers` and the health endpoint only advertise a capability when
`template_supported()` confirms that **every `class_type` used by the graph is
present in the connected server's `/object_info`**. A template whose node classes
are missing is listed under `unsupported_templates` with the exact missing class
names, and it contributes no capability.

This is why the adapter can be trusted to say "ComfyUI can do image generation
here": it is the server itself that reports the node classes, not our hope.

## Adding a template

1. Build the graph in ComfyUI and use **Save (API format)** to export
   `workflow.json`. API format is the flat `{"<node id>": {"class_type", "inputs"}}`
   object, not the editor's `nodes`/`links` format.
2. Replace the values you want to control with literals, then declare each one in
   `inputs` with the node id and field name.
3. Drop it in a new directory. No code change is required — `list_templates()`
   picks it up.
4. Check the health endpoint: your template must disappear from
   `unsupported_templates` before it can be used.

## Shipped templates

| Template | Capability | Status |
| --- | --- | --- |
| `image_txt2img` | `image_generation` | Canonical ComfyUI text-to-image graph (`CheckpointLoaderSimple` → `CLIPTextEncode` ×2 → `EmptyLatentImage` → `KSampler` → `VAEDecode` → `SaveImage`). It works with whatever checkpoints the connected server exposes. |
| `wan22_ti2v_5b_txt2video` | `text_to_video` | Wan 2.2 TI2V-5B text→video (`UNETLoader` / `CLIPLoader` / `VAELoader` / `ModelSamplingSD3` → `CLIPTextEncode` ×2 → `Wan22ImageToVideoLatent` → `KSampler` → `VAEDecode` → `CreateVideo` → `SaveVideo`). Defaults 1280×704, 121 frames, 24 fps; length, resolution, steps and sampler are declared inputs, so the app tunes them without editing the graph. Needs the three Wan 2.2 files on the server — see [`docs/COMFYUI-LOCAL.md`](../../../docs/COMFYUI-LOCAL.md). |

Every graph here is checked class-by-class against the connected server before use, and
`wan22_ti2v_5b_txt2video` has **not been executed against a live ComfyUI by this
repository**: it is derived from the official Wan 2.2 5B example, so a server whose build
differs will reject it loudly (ComfyUI returns `node_errors` naming the node) rather than
quietly render something else. The setup screen's functional test is what proves the
engine end to end.

Audio and speech templates are **not** shipped: their graphs depend on the exact model
package installed, and shipping an unrun graph would be claiming an integration we did
not test. Add them from your own ComfyUI instance following the steps above — the
adapter will verify them for you.

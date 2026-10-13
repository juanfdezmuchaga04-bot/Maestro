# The local engine: self-hosted ComfyUI + Wan 2.2

This is Maestro's default and only initial generation engine, and it is deliberately
the cheapest one to own: a **self-hosted ComfyUI on your own computer**, running
**Wan 2.2 TI2V-5B** (Apache-2.0). Generation costs nothing per clip — no account,
no API key, no credit balance, no cloud round trip. You pay for electricity and for
the time your GPU spends working.

| Piece | What it is | Licence |
| --- | --- | --- |
| **ComfyUI** | the local server that executes a workflow graph, spoken to over its own HTTP API | separate program with its own licence — see its repository; it is cloned into your own folder, never vendored into Maestro |
| **Wan 2.2 TI2V-5B** | the video model (text→video and image→video) | **Apache-2.0** |
| Maestro's part | the setup, the adapter, the queue handler, the UI | this repository's licence |

Implemented in `app/services/{local_hardware,comfy_models,comfy_bootstrap,comfy_jobs,local_engine_api}.py`,
with the workflow in `app/services/comfy_workflows/wan22_ti2v_5b_txt2video/`.

---

## 1. Why this model and not LTX-2

Both were evaluated. The choice is recorded as data in
`comfy_models.evaluated_but_not_selected()` and shown in the app, not buried here:

| | **Wan 2.2 TI2V-5B** (selected) | **LTX-2 / 2.5** (evaluated, not selected) |
| --- | --- | --- |
| Licence | Apache-2.0 (no commercial restriction found in the licence file or the model cards) | LTX-2.x **Community License** — open weights, but a custom licence: entities with ≥ US$10M annual revenue need a paid Commercial Use Agreement for commercial use |
| Hardware | ComfyUI documents its 5B workflow fitting **8 GB VRAM** with native offloading (Wan's own reference command says "at least 24 GB" for the same model — a different implementation, quoted separately) | Lightricks' published requirement is **32 GB+ VRAM** (their Desktop path documents ≥15 GiB) |
| Weights | 18.2 GB of files | larger still, and not the constraint that decides it |
| Credits | none for local generation | none for local generation either — only their hosted API is paid; note that one official text-encoding node calls an API, so a fully local run must use local encoding |

**LTX-2 is not excluded because it is bad; it is excluded because its licence is not an
open licence and its official requirement is above the consumer GPUs this engine
targets.** On a 32 GB+ GPU, and after you accept the licence terms for your
organisation, its ComfyUI workflows can be added the same way as any other template:
export the API graph from your ComfyUI and let the adapter verify it.

The 14B Wan variants are in the catalogue for the same licence and better fidelity,
with Wan's own quoted requirement (≥ 80 GB VRAM at 720p). No 14B graph is shipped:
publishing a workflow nobody has run would be claiming an untested integration.

## 2. What the machine needs (measured, not assumed)

`GET /api/v1/engines/local/hardware` measures this computer and reports what it could
**not** measure as unknown, with the reason. Nothing is inferred from a hostname or a
promise.

| Need | Why | Status if missing |
| --- | --- | --- |
| NVIDIA GPU with **≥ 8 GB VRAM** | the model runs on the GPU | reported as a blocker; the app keeps working for import, storyboard and assembly |
| `nvidia-smi` (driver installed) | it is how VRAM is measured | reported as unmeasured — never guessed |
| `git` | ComfyUI is cloned, not vendored | reported as a blocker for the install step only |
| ~**30 GB** free disk | 18.2 GB of weights plus ComfyUI, dependencies and outputs | a documented blocker when the free space is short |
| ~32 GB system RAM *(planning estimate, not an official figure)* | offloading moves pressure from VRAM to RAM | a documented blocker, labelled as an estimate |
| One-time Internet access | clone + `pip` + the weight files | afterwards everything runs offline |

A driver reports slightly less than a card's nominal size (an "8 GB" card answers
~8188 MiB), so a 3% tolerance is applied to a stated requirement — the reason is
returned alongside the verdict.

## 3. Installing it

Everything is planned first and shown before anything runs:

```
GET  /api/v1/engines/local/setup              # the plan: commands, files, sizes, blockers
POST /api/v1/engines/local/setup/install      # runs it — needs confirm: true
```

The plan is the exact sequence, in order: clone ComfyUI into its own folder → create
its own virtual environment → install PyTorch → install ComfyUI's requirements →
download the weights → *you* start the server → functional test → register.

Rules the installer keeps:

* **Nothing runs without confirmation** (`confirm: true`). Without it the response
  says so and no command is executed.
* **Weights are never downloaded automatically** (spec §22.4). They need
  `download_models: true` on top of the confirmation, because they are ~18 GB.
* **A step that did not run is never reported as done**: each step comes back as
  `done`, `skipped`, `failed`, `manual`, `pending_action` or `not_reached`, and the run
  stops at the first failure with its exit code and output.
* **ComfyUI's environment is its own** (`.venv` inside its folder); Maestro's Python
  environment is not touched.
* **The server binds to loopback** (`--listen 127.0.0.1`) in the command the plan gives
  you. Exposing it beyond that machine is your decision, and the safe way to do it is
  the existing Tailscale path in [`STUDIO-NODES.md`](STUDIO-NODES.md), not a public bind.

The screen that does this (and the equivalent calls) is **Studio → Compute & transfer →
Local engine**. It also prints the exact command to start the server on the computer
that has the GPU.

### The weights it fetches

From `Comfy-Org/Wan_2.2_ComfyUI_Repackaged` (the ComfyUI-repackaged files), into
ComfyUI's own model folders:

| File | Size | Goes to |
| --- | --- | --- |
| `wan2.2_ti2v_5B_fp16.safetensors` | 10 GB | `ComfyUI/models/diffusion_models/` |
| `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | 6.74 GB | `ComfyUI/models/text_encoders/` |
| `wan2.2_vae.safetensors` | 1.41 GB | `ComfyUI/models/vae/` |

Official references: [ComfyUI's Wan 2.2 guide](https://docs.comfy.org/tutorials/video/wan/wan2_2),
[the official Wan 2.2 examples](https://comfyanonymous.github.io/ComfyUI_examples/wan22/),
[Wan's own repository](https://github.com/Wan-Video/Wan2.2).

## 4. Proving it before believing it

```
POST /api/v1/engines/local/functional-test     # generate a real clip and fetch it back
POST /api/v1/engines/local/register            # only with a passed test on record
```

The functional test, in order, and it stops at the first failure:

1. Is ComfyUI reachable? (no → the reason is returned)
2. Does this server have **every node class** the template uses, according to its own
   `/object_info`? (no → the missing class names are returned, plus "update ComfyUI")
3. Does it have **the model files**, according to its own loader dropdowns? (no → the
   exact filenames, sizes and destination folders are returned)
4. Submit a deliberately **tiny** clip — 256×256, 9 frames, 4 steps — so the proof costs
   seconds rather than the ~5 s at 704p the model can really do.
5. Wait for the server's own history to report it finished. If it is still working when
   the poll budget ends, that is **not** a pass: the response says it is still running
   and the engine stays unregistered.
6. Fetch the produced file through `/view` and **verify the container by its magic
   bytes**, not by its name. An HTML error page saved as `.mp4` is a failure.

Only then does registration happen, and the node carries the proof: prompt id,
filename, byte count and SHA-256 of the clip that proved it. The test result is
remembered for 24 hours so registering does not have to render a second clip, but it is
re-validated every time (the file must still exist) — a test result is evidence, not a
flag.

## 5. Generating the first clip from the phone

1. Open Maestro on the phone → **Studio → Compute & transfer → Local engine**.
2. **Re-detect** to see the measured hardware, and the model with its licence.
3. **Install on this computer** (tick the weights box if you want them fetched).
4. Start ComfyUI on the GPU machine with the printed command.
5. **Run the test** → the clip plays right there in the panel, with its path and hash.
   That is the verification you asked for, from the phone, without a terminal.
6. **Register engine**, then **Add to the queue**: type a prompt, choose the seconds,
   and the job appears in the **Job queue** panel below.

From then on, generation is ordinary queue work (`engine: "comfyui"`), with one
important property: **if the computer is off, the job is deferred, not failed.** It goes
back to `pending` with a delay, no attempt is consumed and no error is recorded, and it
runs when the engine answers again. See [`STUDIO-QUEUE.md`](STUDIO-QUEUE.md) for the
queue itself.

`POST /api/v1/engines/local/queue-job` accepts `prompt`, `seconds`, `width`, `height`,
`steps`, `seed`, `priority`, `project_id`, `shot_id`, `node_id`, `base_url` and
`dedupe_key`, and needs no running engine: the phone can queue work from anywhere on the
same network.

## 6. What is verified, and what is not

**Verified by tests** (`tests/test_local_engine.py`, `tests/test_comfy_queue.py`,
`tests/test_local_engine_api.py`): hardware parsing including malformed output and the
absence of a GPU; the fit/selection logic and its refusals; the plan's contents; install
confirmation, the weights flag, the failure path; the functional test against a stub
ComfyUI speaking the real protocol — passing, unreachable, missing node classes, missing
model files, still-running, no video reported, and a file that is not a video;
registration gated on a passed test; deferral semantics; and the end-to-end path
(API → queue → handler → real file on disk → playable URL).

**Not verified here, stated plainly:** this sandbox has no NVIDIA GPU, no CUDA and no
ComfyUI, so **no model has been run and no clip generated by a real engine in this
environment**. The ComfyUI side of every test is a **stub server** implementing the
documented HTTP API — it proves the protocol handling and the wiring, not generation.

Two consequences you should read before the first real run:

* **The shipped Wan 2.2 graph has not been executed against a live ComfyUI by this
  repository.** It follows the official example's structure and uses node classes
  confirmed in that example, and the adapter refuses to use it unless the server
  reports every class it needs. If a field name differs in your build (`clip_type`,
  `sampler_name`, the latent node's `width`/`length`), ComfyUI rejects the graph loudly
  with `node_errors` naming the node — it does not silently produce something else —
  and every one of those values is a declared template input you can override without
  editing the JSON.
* **Video length and resolution defaults are the official ones** (1280×704, 121 frames,
  24 fps). The test uses tiny values only to keep the proof fast.

## 7. Offline, online and what costs money

* **Needs Internet once**: cloning ComfyUI, `pip install`, and the ~18 GB of weights.
* **Works with no Internet afterwards**: generation, the queue, the project, the
  storyboard and the assembly.
* **Never involved**: a commercial generation provider, a credit balance, an API key.
  Maestro has no mandatory paid provider anywhere, and this path is the default one.
* **What it costs**: your hardware, your electricity and your time. Nothing per clip.
* **The intelligence layer** (planning, the Director) can use a local model or an
  optional provider you configure explicitly; it is not required by this engine.

## 8. What is missing on *your* computer (checklist)

Work down this list; each line is reported by the app with a measured value, not a
guess:

1. **A CUDA GPU with ≥ 8 GB VRAM and its driver.** The panel names the card it found, or
   says `nvidia-smi` is not on PATH. No GPU → generation cannot run on that machine;
   import, storyboard and assembly still work.
2. **`git` on PATH** — needed once, for the checkout.
3. **~30 GB free disk** where ComfyUI will live (the plan prints the required and the
   free figure).
4. **The three weight files** in the right folders (the plan lists them with URLs and
   sizes; the functional test names any that are missing).
5. **A current ComfyUI build** — it must report `CreateVideo`, `SaveVideo`,
   `Wan22ImageToVideoLatent` and the rest of the template's classes in `/object_info`.
   An older build fails the check with the exact class names to update for.
6. **ComfyUI running** at `127.0.0.1:8188` (or at a Tailscale address you register
   instead).
7. **A passed functional test** — then, and only then, the engine is registered.

Nothing else is required from you, and nothing is assumed about your machine.

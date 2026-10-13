# Maestro — AI Film Studio Layer: Architecture Audit & Plan

Audit performed against `main` @ `31d7396` ("Release failed INT8 launch buffers before retry on
Python 3.10"). This document is the Phase 1 deliverable: what already exists, what is missing,
what can run where, and the order in which the film-studio layer is built.

## 1. Repository shape

| Area | Location | Notes |
| --- | --- | --- |
| Launcher / Python API + Gradio | `app/launch.py` (~30k lines), `app/wgp.py` (WanGP core) | One process: FastAPI `api = FastAPI(...)` on `:7860` plus the legacy Gradio/classic UI |
| React UI (Maestro UI) | `ui/` — Vite 7 + React 19 + TS + Tailwind 4 + Zustand | `npm run dev` serves `:3000` and proxies `/api` → `127.0.0.1:7860` |
| Generation engine | `app/models/*` (ltx2, minimax_h3, qwen21, hidream, wan, TTS/*) | Local diffusion/TTS pipelines, GPU (NVIDIA CUDA) |
| Director Mode | `app/services/director/**`, `app/services/director_pipeline.py`, `ui/src/components/DirectorDashboard/**` | Planners per format: `short_film`, `music_video`, `podcast`, `viral_video`, `long_form_story` |
| Editor Mode | `app/services/editor_projects.py`, `ui/src/editor/**` | Non-destructive multipista timeline, FFmpeg render, export dialog |
| Media / queue / export | `app/services/gallery_*`, `job_lifecycle`, `services/hdi*` API, FFmpeg via `ffmpeg-python` | Real job queue with cancel/retry/OOM recovery |
| Tests | `tests/` (275 files), CI `.github/workflows/ci.yml` | unittest discovery + targeted pytest + UI lint/build |

## 2. What already exists (reuse, do not rebuild)

- **Director Mode is real and deep.** `services/director/planners/{short_film,music_video,podcast,
  viral_video,long_form_story}.py` turn a brief + references into a `ProductionPlan` of `ShotPlan`
  objects (`services/director/schema.py`): shot_id, scene_goal, subjects_on_screen, spatial_setup,
  environment, visual_style, lighting, mood, action_beats, dialogue_beats, camera_plan, audio_plan,
  continuity_refs. Exposed via `/api/v1/director/plan-*` and `directorV2Plan()` in the UI client.
- **Reference cards already carry intent.** `services/director/reference_inputs.py` maps typed
  reference cards (character / location / object) into planner inputs, and `VISUAL_IDENTITY_GUIDANCE`
  already encodes "a reference photo is inspiration, not a literal frame".
- **Audio analysis exists.** `services/audio_analysis.py` (1437 lines) does BPM, beats, sections,
  energy, optional transcription and diarization locally via librosa/faster-whisper.
- **Timeline persistence + FFmpeg assembly exist.** `services/editor_projects.py` writes
  workspace-scoped project JSON atomically, validates paths, compiles a render graph and renders with
  FFmpeg; `/api/v1/editor/projects*` and `/api/v1/editor/export` expose it. The UI Editor
  (`ui/src/editor/*`) is a full multipista editor with media bin, inspector, transitions, export dialog.
- **Job queue exists.** `job_lifecycle` + `_jobs` + `/api/v1/jobs*`, `/api/v1/director/queue*` with
  progress, cancel, retry, stall recovery, OOM recovery banner.
- **Workspaces + uploads exist.** `/api/v1/workspaces*`, `/api/v1/upload`, `/api/v1/upload-audio`,
  per-workspace `outputs/<name>/` media library.
- **Licensing posture already documented.** `THIRD_PARTY_NOTICES.md`, `app/LICENSES/*`,
  `app/models/*/NOTICE.md`, per-model license files.

## 3. Real gaps (what this work adds)

1. **No production-project object.** `EditorProject` is a *timeline*; there is no persistent
   "film project" holding brief, genre, visual style, cast/continuity bible, scenes, soundtrack plan,
   job state and creative decisions. Nothing survives a reopen as a production.
2. **No asset intake with a creative role.** Uploads land as anonymous files. Nothing records
   "this photo is a *character reference*" / "this track is the *soundtrack*" / "this video must be
   *conserved*", and the user cannot correct the machine's guess.
3. **"Storyboard" in the codebase is a prompt format** (`minimax_h3_window_storyboard`), not an
   editable artifact. There is no ordered, per-shot, re-orderable/regenerable storyboard document.
4. **No continuity bible** persisted across shots (characters, wardrobe, locations, lighting, palette).
5. **No bridge from an approved plan to real generation jobs** scoped per shot, and no per-shot take
   history.
6. **No assembly bridge** from storyboard → Editor timeline (tracks, cuts, transitions, music sync).

## 4. Environment reality (Freebuff Cloud, measured in this sandbox)

| Capability | Available? | Evidence |
| --- | --- | --- |
| Node 22.23.2 / Bun 1.4.2 | yes | `node -v`, `bun -v` |
| UI typecheck / lint / build | yes | `ui/package.json` scripts; CI job `ui-build` |
| Python 3.10.12 | yes | `python3 -V` |
| pytest / numpy / torch / fastapi | **no** (not installed) | `import torch` → ModuleNotFoundError |
| `ffmpeg` / `ffprobe` | **no** | `ffmpeg -version` → command not found |
| NVIDIA GPU | **no** | `nvidia-smi` → command not found |
| RAM / disk | 3 GB / 20 GB free | `free -g`, `df -h` |

Consequences, stated plainly:

- The **production engine (models, CUDA, FFmpeg rendering) cannot run in this cloud sandbox.** It
  requires a local machine with an NVIDIA GPU (see `app/docs/INSTALLATION.md`). Nothing in this work
  claims otherwise and nothing fakes a generation.
- What *can* run here: the **application layer** — project persistence, asset intake/classification,
  brief composition, plan→storyboard conversion, soundtrack planning, assembly planning — because it is
  Python-stdlib-first and degrades explicitly when a native tool is absent. That is the same posture
  `editor_projects.py` already uses (`ffprobe` is invoked, and failure is surfaced, not invented).

## 5. Licensing / cost posture

- Default path stays **local-only**: no new API keys, no paid services, no uploads to third parties.
- Model licenses are read from files that ship with the repo (`app/LICENSES/*`, `app/models/*/NOTICE.md`,
  `THIRD_PARTY_NOTICES.md`) and surfaced per asset/model requirement. No statement is made about
  commercial usability unless the license file is present and read.
- No cost estimates are published: local generation cost = user's own electricity/hardware.

## 6. Prioritized implementation plan

| # | Stage | Deliverable | Verification |
| --- | --- | --- | --- |
| 1 | Audit | this document | — |
| 2 | Film project persistence | `app/services/film_projects.py` + `/api/v1/film/projects*` | `tests/test_film_projects.py` |
| 3 | Asset intake & roles | `app/services/film_assets.py` + `/api/v1/film/assets*` | `tests/test_film_assets.py` |
| 4 | Brief + reference association | `app/services/film_brief.py` (approach modes, role→planner mapping) | `tests/test_film_brief.py` |
| 5 | Director integration | plan ingestion → storyboard (`film_planning.py`) | `tests/test_film_planning.py` |
| 6 | Editable storyboard + bible | shot ops in `film_projects.py`, `film_planning.py` | `tests/test_film_storyboard.py` |
| 7 | Real generation queue per shot | `film_planning.production_requests()` + `/api/v1/film/produce` using the existing job queue | `tests/test_film_produce.py` |
| 8 | Assembly via FFmpeg | `app/services/film_assembly.py` → real `EditorProject` → `render_editor_project` | `tests/test_film_assembly.py` |
| 9 | Export & reopen | export records + project round-trip | `tests/test_film_projects.py` |
| 10 | Studio UI | `ui/src/studio/**`, mode toggle entry, api client | `ui`: lint + `tsc -b` + build |
| 11 | Docs | this file + `docs/FILM-STUDIO.md` (GPU machine procedure) | — |

Stages 1–11 land in this change set, in that order.

## 7. Second increment — infrastructure gaps the studio still had

Re-audited after stages 1–11. Five further gaps were confirmed against the code
(not assumed), and each was closed in a following change set:

7. **The inference engine was reachable directly.** Nothing abstracted "which
   engine can do this". A capability could not be swapped between the local
   engine, a ComfyUI server and another computer. → `engine_providers.py`
   (12 individually registered capabilities, four provider kinds, local-first
   resolution, commercial adapters optional and never required).
8. **No ComfyUI support at all** (`grep -ri comfyui app/` → 0 hits). →
   `comfyui_adapter.py` + a versioned template library whose capabilities are
   verified against the server's own `/object_info`.
9. **A phone could not use a computer's GPU.** No node concept existed
   (`compute_node` / `worker_node` → 0 hits). → `compute_nodes.py`: registration
   with a token shown once, heartbeat-gated online state, capability routing,
   concurrency limits, and work that is **parked and handed back** when a node
   returns.
10. **Large uploads went through RAM.** `/api/v1/upload` calls
    `await file.read()` (whole body in memory, 500 MB cap), with no resume and no
    integrity check. → `upload_chunks.py`: chunked sessions written straight to
    disk, resume by missing index, per-chunk and whole-file SHA-256, atomic
    assembly, dedup, quota check, safe cleanup.
11. **Only a CivitAI allowlist guarded outbound fetches** (`launch.py:4347`),
    so there was no general URL import and no general SSRF guard. →
    `reference_urls.py`: scheme and network allow-lists with pre-resolution IP
    validation, per-hop redirect re-validation, streaming byte cap, and a plain
    refusal for platforms that gate their media.

| # | Stage | Deliverable | Verification |
| --- | --- | --- | --- |
| 12 | Engine registry | `app/services/engine_providers.py` | `tests/test_engine_providers.py` |
| 13 | Compute nodes | `app/services/compute_nodes.py` | `tests/test_compute_nodes.py` |
| 14 | ComfyUI adapter | `app/services/comfyui_adapter.py`, `app/services/comfy_workflows/**` | `tests/test_comfyui_adapter.py` (real HTTP against a stub server) |
| 15 | Reference URLs | `app/services/reference_urls.py` | `tests/test_reference_urls.py` (real HTTP against a local origin, plus the guard) |
| 16 | Chunked uploads | `app/services/upload_chunks.py` | `tests/test_upload_chunks.py` |
| 17 | Routes | 26 endpoints under `/api/v1/{engines,compute,reference,uploads}` in `app/launch.py` | AST check: no duplicate method+path, every service call exists |
| 18 | Studio UI | `ui/src/studio/{StudioComputePanel,StudioTransferPanel,useChunkedUpload}`, `ui/src/{api,types}/engines.ts`, **Compute** phase | `tsc -b` + `eslint` + `vite build` |
| 19 | Docs | `docs/STUDIO-NODES.md` (engines, nodes, ComfyUI, transfers, costs) | — |

Still open, and deliberately not faked: text-to-video/audio ComfyUI templates
(they need a real server to verify against), spreading **one** model across
several devices (needs a validated solution), and any measurement of generation
speed or VRAM — those require a GPU that this sandbox does not have.

## 8. Third increment — the job queue as a queue of record

The film project already tracked "a job id per shot", but the studio still had no
queue of its own: nothing survived a restart with its state, nothing bounded
concurrency, memory, retries or duplicates, and progress was whatever the last
HTTP poll said. Re-audited against the code, and closed as follows:

12. **No persistent queue.** Jobs lived in the in-process table only. →
    `generation_queue.py`: eight states, the full per-job record (project, scene,
    shot, task, engine, model, parameters, inputs, node, state, progress, times,
    outputs, errors, retries), one atomically written JSON document, priority
    order, configurable concurrency and a declared memory ceiling, bounded
    retries, duplicate refusal by `dedupe_key`, cancellation that only reports a
    stop when it happened, recovery of interrupted work, history trimming and an
    event log.
13. **Nothing executed a claimable job**, so a queue would have been a data
    structure with no behaviour. → `queue_worker.py`: claims the next runnable
    job, runs the handler registered for its engine, and **fails the job with
    "no runner is registered"** when there is none. Completion requires absolute
    paths that exist on disk.
14. **Progress was conflated with the transport.** → `report_progress` records
    only what an engine measured; a job whose engine reports nothing keeps
    `percent = null` and `source = "none"` for its whole life, and the UI shows
    the state instead of a bar.
15. **A restart lost in-flight work.** → `recover()` runs in a FastAPI startup
    handler (and on demand): silent running jobs are requeued while retries
    remain, failed when they are spent, and nothing is deleted.

| # | Stage | Deliverable | Verification |
| --- | --- | --- | --- |
| 20 | Queue of record | `app/services/generation_queue.py` | `tests/test_generation_queue.py` (51 tests) |
| 21 | Executor + handler registry | `app/services/queue_worker.py` | `tests/test_queue_worker.py` (29 tests) |
| 22 | HTTP shell | `app/services/production_queue_api.py`, mounted in `app/launch.py`, recovery wired to startup | `tests/test_production_queue_api.py` (25 tests over HTTP + 6 wiring checks: router mounted, startup recovery, no shadowed route, UI paths exist, call order) |
| 23 | Studio UI | `ui/src/studio/StudioQueuePanel.tsx`, `ui/src/api/queue.ts`, `ui/src/types/queue.ts` | `tsc -b` + `eslint` + `vite build` |
| 24 | Docs | `docs/STUDIO-QUEUE.md` | — |

Still open, and not faked: **no real engine handler is registered by default**
(the local generation path and the ComfyUI adapter are the next phase's wiring;
until then those engines honestly answer `no runner`), and cross-node scheduling
of *independent* jobs remains the compute-node layer's job. Spreading a single
model across devices is still out of scope.

## 9. Fourth increment — the local engine, installed and proved

The studio could describe a local engine but never turn one on: the ComfyUI
adapter spoke the protocol, the queue could execute work, and nothing joined
them, so the default configuration still had no engine at all. Five gaps were
confirmed in the code before being closed:

16. **No hardware reality.** A machine's suitability for a model could not be
    measured from the app. → `local_hardware.py` detects the CPU, RAM, disk,
    each GPU (name, memory, driver) and the toolchain, from sources that are
    parsed as data and whose absence is reported as **unknown with a reason** —
    never inferred from a hostname.
17. **No model catalogue with licences and requirements.** → `comfy_models.py`:
    per-model licence, weight files with sizes, quoted hardware requirements, and
    a fit verdict that names the binding constraint. LTX-2 is listed and
    *refused* with its real reason (custom community licence; 32 GB+ VRAM
    officially) instead of being quietly omitted.
18. **Nothing installed anything.** → `comfy_bootstrap.py`: a plan that is shown
    before it runs, single-step execution with a real outcome per step, weight
    downloads that require a second explicit flag, and a ComfyUI environment
    separate from Maestro's.
19. **"Reachable" was treated as "working".** → the installer registers an
    engine only after a **functional test** that generates a tiny clip, waits for
    ComfyUI's own history to report it, fetches the file and verifies its
    container by magic bytes. Still-running is not a pass; an HTML page named
    `.mp4` is not a pass.
20. **No video template existed**, so there was nothing to run. →
    `comfy_workflows/wan22_ti2v_5b_txt2video/` (declared inputs, so the app can
    tune length, resolution and steps) plus a queue handler and a deferral rule:
    a job whose engine is offline is **deferred, not failed** — it returns to
    `pending`, consumes no attempt and records no error.

| # | Stage | Deliverable | Verification |
| --- | --- | --- | --- |
| 25 | Hardware reality | `app/services/local_hardware.py` | `tests/test_local_engine.py` |
| 26 | Model catalogue | `app/services/comfy_models.py` (Wan 2.2 selected; LTX-2 evaluated and refused with reasons) | idem |
| 27 | Wan 2.2 template | `app/services/comfy_workflows/wan22_ti2v_5b_txt2video/{manifest,workflow}.json` | idem |
| 28 | Installer + functional test | `app/services/comfy_bootstrap.py` | idem, against a stub ComfyUI speaking the real protocol |
| 29 | Queue integration | `app/services/comfy_jobs.py`, defer support in `generation_queue.py` / `queue_worker.py` | `tests/test_comfy_queue.py` |
| 30 | Routes | 6 endpoints under `/api/v1/engines/local` in `app/launch.py`, handler registered at startup | `tests/test_local_engine_api.py` (HTTP, in-process) + AST wiring checks |
| 31 | Studio UI | `ui/src/studio/StudioLocalEnginePanel.tsx`, `ui/src/{api,types}/localEngine.ts`, mounted in *Compute & transfer* | `tsc -b` + `eslint` + `vite build` |
| 32 | Docs | `docs/COMFYUI-LOCAL.md` | — |

Still open, and stated as such: **no engine has been run in this environment** (no
GPU, no CUDA, no ComfyUI here — every server-side test is a stub), the shipped Wan
graph has not been executed against a live ComfyUI by this repository, and no
speed or VRAM figure is claimed for either model. `docs/COMFYUI-LOCAL.md` §8 lists
exactly what is missing on a real machine.

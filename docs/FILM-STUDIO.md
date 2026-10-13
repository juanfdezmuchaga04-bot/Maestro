# Maestro Films — AI production studio

Maestro already had a Director (planning), a Studio (single generations) and an
Editor (multipista timeline + FFmpeg export). **Films** adds the missing layer
between them: a *production* that keeps the brief, the imported material with the
creative role the user assigned to it, the continuity bible, the editable
storyboard, the soundtrack plan, the generation queue bookkeeping and the final
cut together — and survives closing the app.

The architecture audit that led here is in [`FILM-STUDIO-AUDIT.md`](FILM-STUDIO-AUDIT.md).

## What it does

The workspace follows one flow, and every step reports its real state:

1. **Project** — create/reopen/delete a production (name, genre, style, status).
2. **Idea** — brief (logline, objective, treatment, tone, visual style, aspect
   ratio, target duration, must-keep/avoid) plus the creative approach:
   *faithful*, *hybrid* or *creative*.
3. **Files** — drag photographs, video, your own music, voices, SFX, scripts and
   PDFs. Each file is catalogued locally and given a suggested creative role
   (character / location / set / wardrobe / lighting / palette / camera /
   style / prop / soundtrack / voice / dialogue / SFX / ambience / source video /
   script). **You can override every role**, and mark a reference as
   *reproduce literally* or *inspiration only* — nothing is assumed to be a
   frame-for-frame copy.
4. **Approach** — part of the brief; it becomes an explicit instruction to the
   Director.
5. **Script & storyboard** — the composed brief (plus the matched reference
   images) is handed to Director Mode's own story planner
   (`/api/v1/director/plan-short-film-script`). The Studio does not write the
   script itself: it converts the planner's real output into scenes, shots and a
   continuity bible.
6. **Review the plan** — ordered, editable storyboard: narrative objective,
   description, shot size, camera movement, duration, transition, dialogue,
   lighting, final prompt, model, status and takes. Reorder, edit or delete a
   single shot; re-planning a production **keeps the takes of shots whose prompt
   did not change** instead of throwing finished work away.
   - **Assigning references.** For any shot you can pick which imported images are
     its visual references, and double-click one to make it the **start frame**.
     “Add to every shot of this scene” applies the same images to a whole scene.
     This explicit decision always wins over the planner's guess, and it is exactly
     what is sent to the model (`image_start` / `image_refs`).
   - **Visual continuity.** “Apply visual continuity” reads the continuity bible and
     gives every shot that features a character *that character's* reference photos,
     so the same face conditions every shot instead of only the ones you tagged.
     A character with no reference photo is **reported**, never substituted.
7. **Produce** — each runnable shot is submitted to the real generation queue
   using the Studio's *current* model settings, with the shot's own prompt and
   references. Jobs are recorded per shot; “Check queue” reconciles the
   storyboard with the live job table.
8. **Review & regenerate** — regenerate a single shot without re-rendering the
   film; takes are kept and one can be approved.

### Queue states, cancellation and recovery

Each shot job moves through the shared queue's real states
(`held → queued → running → completed | failed | cancelled`) and the production
reflects exactly what the worker reports:

| State | Effect on the shot |
| --- | --- |
| `queued` / `running` | shown as in-flight, with a **Cancel** button |
| `completed` + a written file | a take is recorded, the shot becomes `generated` |
| `failed` | the error is kept on the take, the shot becomes `failed` **and stays retryable** |
| `cancelled` | the shot returns to `planned` (not a failure), ready to submit again |

- **Cancelling** uses Maestro's existing job machinery
  (`POST /api/v1/cancel/{job_id}`); the production only reflects the outcome, so
  shot generation and the final render cancel the same way.
- **Retrying** is simply submitting that shot again: naming a shot explicitly
  always submits it, even if it already has a render — that is what makes
  “Regenerate” regenerate. (Only a failed or cancelled take never counts as a
  finished render, so the shot is never silently skipped.)
- Shots are never deleted or blanked by a failure: the prompt, references and take
  history survive so the next attempt starts from the same place.
9. **Mix** — soundtrack lanes (music / dialogue / narration / SFX / ambience)
   built from your files, with gain and fades. Your music is marked *preserved*
   and is never replaced or muted automatically. If the track has been analysed
   locally, the Studio proposes cut points on the beat, previews them, and only
   moves the storyboard when you apply.
10. **Assemble & export** — the storyboard compiles into a **real Editor
    project** (video track with transitions, audio lanes, optional subtitle and
    title/credit cards, markers on every shot boundary and on every beat). The
    final MP4 is rendered by the Editor's existing FFmpeg pipeline, so one render
    path serves both features and the assembled film stays editable.
    The export is tracked by its job id: the record starts as `queued`, and
    “Refresh render state” attaches the finished MP4's filename (or the failure, or
    the cancellation) to the export and only then marks the production `exported`.
    A queued render can be cancelled from the same panel.

### The engine that generates the shots

Behind the **Produce** step there has to be an engine, and the studio's default is
the free one you own: **self-hosted ComfyUI on your own GPU, running Wan 2.2
TI2V-5B (Apache-2.0)**. No account, no API key, no credits, nothing per clip.

It is installed, detected and proved from the app itself — hardware measured, an
install plan shown before it runs, and a real test clip generated and verified
before the engine is allowed to take work — and the whole procedure, including
exactly what is missing on a machine that cannot run it yet, is in
[`COMFYUI-LOCAL.md`](COMFYUI-LOCAL.md).

Two behaviours worth knowing when directing from a phone:

* **A shot sent to an engine that is offline is deferred, not failed.** It goes
  back to `pending` with a delay, consumes no retry and records no error; it runs
  when the computer answers again. Nothing is lost by closing the laptop.
* **No commercial provider is ever used implicitly.** Local is the default and the
  registry reports `local_only` when nothing else is configured.

## Where things are stored

Everything lives in your Maestro workspace (`save_path`, i.e. `outputs/`):

```
outputs/<workspace>/.maestro_film/<project_id>.json   # the production
outputs/<workspace>/.maestro_editor/<project_id>.json  # the assembled cut
outputs/<workspace>/…                                  # rendered clips and MP4s
uploads/                                               # files you imported
```

Writes are atomic and path-validated; a crash mid-save cannot truncate a
production.

## Running it

The Films workspace is part of the normal Maestro app — start Maestro as usual
(Pinokio launcher, or `app/launch.py`), open the **Films** tab in the top
navigation. Its API is under `/api/v1/film/*`; the service layer behind it is
`app/services/film_api.py` and the routes are thin wrappers in `app/launch.py`.

### Endpoints

`{id}` is a production id and `{sid}` a shot id. All of them take an optional
`workspace` (query or body) and default to the active workspace.

| Method | Path | Does |
| --- | --- | --- |
| GET | `/api/v1/film/engine` | which engines are actually available here (GPU, FFmpeg, analysers) |
| GET / POST | `/api/v1/film/projects` | list / create productions |
| GET / PUT / DELETE | `/api/v1/film/projects/{id}` | load / save / delete a production |
| GET | `/api/v1/film/projects/{id}/overview` | production + engine status + job reconciliation |
| GET / PUT | `/api/v1/film/projects/{id}/brief` | composed brief (director input, character sheets, references) / update it |
| GET / POST / PATCH / DELETE | `/api/v1/film/projects/{id}/assets` … `/{asset_id}` | import files, change an asset's creative role, remove it |
| POST | `/api/v1/film/projects/{id}/assets/analyze` | local analysis (dimensions/palette, BPM/beats, duration, PDF text) |
| POST | `/api/v1/film/projects/{id}/plan/import` | turn a real Director plan into scenes, shots and the continuity bible |
| POST | `/api/v1/film/projects/{id}/references` | assign reference images to shots, or to every shot of a scene |
| POST | `/api/v1/film/projects/{id}/continuity` | bind each character's photos to the shots where they appear |
| GET | `/api/v1/film/projects/{id}/storyboard` | storyboard + totals + the runnable production requests (automation surface) |
| POST / PATCH / DELETE | `/api/v1/film/projects/{id}/shots` … `/{sid}` | add / edit / delete a shot |
| POST | `/api/v1/film/projects/{id}/shots/{sid}/move` | reorder |
| POST | `/api/v1/film/projects/{id}/shots/{sid}/takes` | record a take (automation surface) |
| POST | `/api/v1/film/projects/{id}/shots/{sid}/approve` | approve a take |
| GET | `/api/v1/film/projects/{id}/production` | the runnable shots and their real submission parameters |
| POST | `/api/v1/film/projects/{id}/produce` | submit shots to the shared generation queue |
| POST | `/api/v1/film/projects/{id}/production/sync` | reconcile shots with the live job table |
| GET / POST | `/api/v1/film/projects/{id}/soundtrack` … `/sync` | plan the lanes / cut to the song's real beats |
| GET | `/api/v1/film/projects/{id}/assembly` | the assembled Editor project + markers + report |
| POST | `/api/v1/film/projects/{id}/export` | compile and queue the final MP4 through the Editor's FFmpeg renderer |

`/storyboard` and the per-shot `/takes` route are the automation surface: the
Studio web UI reads the storyboard from the overview and records takes while
syncing the queue, so it does not call those two. Everything else in the table is
used by the UI.

### Engines, compute nodes and transfers

The studio never addresses an inference engine directly. It asks for a
*capability* and the registry resolves it to whichever engine can run it — the
local one, a ComfyUI server, or another computer. That layer, the ComfyUI
adapter, the SSRF-guarded URL import and the resumable chunked uploads have their
own endpoints under `/api/v1/{engines,compute,reference,uploads}` and are
documented in **[STUDIO-NODES.md](STUDIO-NODES.md)** — including what each
capability is measured from, the security posture, how to raise upload limits,
and the real costs (hardware, electricity, storage, connectivity and optional
services) that a local-only setup still has.

### The production queue

Work that is going to run — a shot, an export, a piece of processing — is
recorded in a **persistent queue** before anything starts, so closing the app or
crashing the backend never loses it. The queue has the eight states the film
pipeline needs (`draft` → `pending` → `preparing` → `running` → `finalizing` →
`completed` / `failed` / `cancelled`), configurable concurrency and a declared
memory ceiling, priorities, bounded retries, duplicate protection, cancellation
that distinguishes "requested" from "really stopped", and automatic recovery of
interrupted jobs when the backend starts.

Its rule for progress is the important part: **a percentage is only ever shown
when an engine reported one.** A job whose engine reports nothing is described by
its state, because a finishing HTTP request is not generation progress. The
queue's own endpoints live under `/api/v1/production/queue` and it is documented,
with its limits and what is *not* wired yet, in
**[STUDIO-QUEUE.md](STUDIO-QUEUE.md)**. You watch and control it from the phone in
Studio → *Compute & transfer* → **Job queue**.

### Requirements on the production machine

| Need | Why | How to get it |
| --- | --- | --- |
| NVIDIA GPU (CUDA) | Image/video/audio generation | See `app/docs/INSTALLATION.md`; the launcher picks the right Python environment for your card |
| FFmpeg + FFprobe | Media metadata, final render | Installed with Maestro (`app/docs/INSTALLATION.md`) |
| Pillow | Image dimensions + colour palette | Already in `app/requirements.txt` |
| pypdf | Reading PDF scripts | `pip install pypdf` (optional; PDFs are catalogued without it) |
| librosa | Tempo, beats, sections for cut-to-music | Already in `app/requirements.txt` |
| The generation models | Actually rendering shots | Downloaded on demand by the launcher, per model |

The **Films → Project** screen lists exactly which of these are missing on the
machine you are running on, and the import screen explains what each missing
tool costs you. The Studio never claims a capability it does not have:
if a file cannot be analysed locally it says so, and if a model is not
installed the generation is refused with the model's requirements instead of
being simulated.

### In a CPU-only sandbox (e.g. a cloud container)

Available: creating productions, importing and classifying files, writing the
brief, storyboarding a plan you generated elsewhere, editing the storyboard,
planning the soundtrack lanes and compiling the Editor project.

Not available: generating shots, rendering the final MP4, and any analysis that
needs FFmpeg/Pillow/librosa. The UI shows this in the engine badge (GPU / FFmpeg)
and the API reports it from `GET /api/v1/film/engine`.

## Models and licences

Films adds **no** new models, no API keys and no external services. It drives the
models that are already installed, and the licence position is unchanged from the
rest of Maestro:

- Maestro itself is under the **WanGP Non-Commercial Evaluation License 1.1**
  (`LICENSE`, `app/LICENSE.txt`). Outputs you generate are yours to use
  commercially with attribution; commercial use of the software itself needs a
  separate licence from the WanGP licensor.
- Third-party models and components keep their own terms:
  `THIRD_PARTY_NOTICES.md`, `app/LICENSES/*`, each `app/models/*/NOTICE.md`
  (for example MiniMax H3 weights, ACE-Step, IndexTTS2, YuE, seed-vc GPL-3.0
  which is cloned separately, not shipped).
- Nothing in this feature claims that a model is commercially usable. Check the
  model's own licence before you distribute anything you render.

Cost: local generation costs only your own hardware and electricity. No usage
estimates are published because none of it runs on infrastructure we control.

Privacy: production files stay on the machine that runs Maestro. The only network
traffic is what Maestro already does (optional model downloads, optional
CivitAI/Hugging Face browsing, optional notifications). Films itself never
uploads your media anywhere.

## Tests

```bash
# backend (no GPU, no FFmpeg needed)
cd /path/to/Maestro && PYTHONPATH=app python3 -m unittest \
  tests.test_film_projects tests.test_film_assets tests.test_film_brief \
  tests.test_film_planning tests.test_film_assembly tests.test_film_api

# frontend
cd ui && npm ci && npm run lint && npx tsc -b && npm run build
```

`python -m unittest discover -s tests -p "test_*.py"` (the full CI suite) also
picks them up.

## Known limitations

- **The story planner needs the local LLM.** Step 5 calls Director Mode's story
  planner, which uses the local LLM server (Settings → Services). If it is not
  running you get a clear error and can still add shots manually.
- **Music sync depends on analysis.** Without librosa (or before you analyse the
  track) the Studio reports why it cannot align cuts instead of guessing times.
- **Subtitle burn-in uses the Editor's text track.** Their styling options are the
  Editor's; richer caption styling is future work.
- **Character identity matching is name-based.** A plan character is linked to a
  character-reference file when the names overlap; otherwise the Studio tells you
  which characters still have no reference photo instead of attaching a wrong one.
  The manual reference picker exists precisely for the cases where the names do not
  match, and removing a file unlinks it from the bible too, so a deleted photo can
  never be handed back to the model.
- **Per-shot generation reuses the Studio's parameter set.** Model, resolution,
  LoRAs and steps come from the Studio sidecar (they are the user's real
  settings); the Films layer overrides the prompt and the shot's references.

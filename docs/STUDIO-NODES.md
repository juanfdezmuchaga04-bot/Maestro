# Engines, compute nodes and large-file transfer

This is the infrastructure layer of the film studio: **which engine runs a piece
of work, on which computer, and how large media gets there without breaking.**
It is what lets the phone direct a project while a desktop renders it, and it is
what keeps the application independent of any particular model or vendor.

Everything described here is implemented in:

| Module | Responsibility |
| --- | --- |
| `app/services/engine_providers.py` | Capabilities and provider registry (the studio never addresses an engine directly) |
| `app/services/compute_nodes.py` | Node registry: registration, heartbeat, capability routing, parked work |
| `app/services/remote_engine.py` | Delegating a queued job to another Maestro that has the GPU, and bringing the result back |
| `app/services/comfyui_adapter.py` | ComfyUI client + versioned workflow templates |
| `app/services/reference_urls.py` | SSRF-guarded reference URL import |
| `app/services/upload_chunks.py` | Chunked, resumable uploads for large media |

---

## 1. Capabilities, not engines

The studio asks for a **capability**, never for a model or a pipeline. Twelve are
registered individually (`CAPABILITIES`):

`image_generation`, `text_to_video`, `image_to_video`, `video_to_video`,
`video_extension`, `frame_interpolation`, `upscaling`, `inpainting`,
`voice_generation`, `audio_generation`, `transcription`, `auxiliary_processing`.

A **provider** declares the subset it can actually do. Providers come in four
kinds, ordered by preference:

1. `local_engine` — the Maestro engine on this machine
2. `compute_node` — another computer running Maestro
3. `comfyui` — a ComfyUI server on its local HTTP API
4. `commercial_api` — optional, **never required**

Rules enforced by the registry:

* A capability with no reachable provider raises a clear error naming what is
  missing. There is **no silent fallback**.
* A commercial adapter is only chosen when the caller explicitly allows it. A
  local-only installation never depends on a key.
* When a computer disconnects, `resolve(..., node_ids=[...])` stops giving it
  work without breaking the local engine.

### Where the local capabilities come from

`GET /api/v1/engines` reports each capability with its **evidence**. Nothing is
asserted from a model's name:

* `auxiliary_processing`, `upscaling`, `frame_interpolation` — only when
  `ffmpeg`/`ffprobe` are present (the real local assembly and retiming path).
* `transcription` — only when `faster_whisper` is importable.
* `text_to_video` / `image_to_video` / `video_to_video` — only for installed
  model definitions that set `t2v_class`, `i2v_class` or `vace_class`, and the
  model type that justified it is returned in `local_engine.evidence`.
* `image_generation` / `inpainting` — only for model definitions whose
  `image_mode` is 1 / 2.
* `audio_generation` — only for model definitions declaring `audio_only`;
  `voice_generation` additionally requires the model to be a speech model.

---

## 2. Directing from the phone, rendering on a computer

A **compute node** announces what it can do; the studio sends it only work it
declared.

```
phone (control)  ──HTTP──▶  Maestro on the desktop (renders)
     │                              ▲
     └── same workspace, same projects, authenticated ──┘
```

* `POST /api/v1/compute/nodes` registers a node and returns a **token once**.
  Only its SHA-256 hash is stored; there is no endpoint that can reveal it again.
* `POST /api/v1/compute/nodes/{id}/heartbeat` requires that token and refreshes
  `last_seen`. A node is **online only while its heartbeat is fresh**
  (default 90 s, overridable per request). Nothing is inferred from a name or a
  hardware string.
* `POST /api/v1/compute/assignments` assigns a job to a node. If the node is
  away the assignment is recorded as `waiting_node` — **the work is kept**.
* `GET /api/v1/compute/assignments` lists parked work (`orphaned_jobs`).
* `POST /api/v1/compute/nodes/{id}/recover` hands the parked jobs back and
  increments their attempt counter, so a returning computer resumes where the
  queue left off. Finished work is never resurrected.
* A node at its `max_concurrent_jobs` is **skipped, not overloaded**.
* `DELETE /api/v1/compute/nodes/{id}` removes a node but parks its jobs rather
  than dropping them.

### Delegating to another Maestro (a computer that *pulls* vs. one that is *asked*)

The registry above is bookkeeping: it records which node can do what. When the
computer doing the directing has no GPU of its own — a modest laptop — the render
has to happen on another Maestro, and that is `services.remote_engine`:

```
this computer (queue, projects, interface)         the peer (GPU, ComfyUI)
  job with engine "node:<peer_id>"
    ├─ GET  /api/v1/engines                    → what that computer can really do
    ├─ POST /api/v1/production/queue/jobs      → the job, with a dedupe_key
    ├─ GET  /api/v1/production/queue/jobs/{id} → follow it; forward its real progress
    ├─ GET  …/jobs/{id}/outputs/{index}        → download the result (resumable)
    └─ POST …/jobs/{id}/cancel                 → stop it, and wait for confirmation
  completed, with the file in this workspace
```

```
GET    /api/v1/engines/remote/peers              # configured peers + measured state
POST   /api/v1/engines/remote/peers              # add a peer, route its engine, probe it
PATCH  /api/v1/engines/remote/peers/{id}         # address, token, engine, capabilities
DELETE /api/v1/engines/remote/peers/{id}         # forget it here (nothing changes there)
POST   /api/v1/engines/remote/peers/{id}/probe   # ask it again, right now
POST   /api/v1/engines/remote/queue-job          # queue work for a peer on this machine
```

Rules that hold on the wire:

* The peer authenticates the caller with **its own** `MAESTRO_ACCESS_TOKEN`, sent as
  an `Authorization: Bearer` header. Nothing but the token is stored here (0600,
  never returned by the API), and the header is only honoured by a gate in token
  mode — in password mode the stored hash is *not* a login credential.
* A peer that does not answer **defers** the job (no attempt consumed, no error
  recorded); it runs when that computer answers again.
* A retry **adopts** the job the peer already has (`dedupe_key = maestro:<local job
  id>`), so a dropped connection re-downloads the result instead of rendering it
  twice.
* Progress is the peer's own measurement, forwarded. A peer reporting no
  percentage keeps `percent = null` here too.
* The peer is asked for output *N*; it is never asked for a path. Transfer is
  `Range`-resumable, which is what a multi-gigabyte clip over a VPN needs.
* Capabilities of a peer are declared by the user or measured from that peer's own
  report — never inferred from its name or hardware.

### Which endpoints the Studio UI calls

The **Compute & transfer** phase calls everything above except three endpoints,
which belong to the *node agent* (the process running on the computer that
renders) and to API clients rather than to the control surface:

| Endpoint | Caller |
| --- | --- |
| `POST /api/v1/compute/nodes/{id}/heartbeat` | the node itself, with its token |
| `POST /api/v1/compute/assignments/{job_id}/release` | the node reporting a job's outcome |
| `GET /api/v1/engines/capabilities` | the capability catalogue, for API clients and tests |

### Security posture

* Node ids are validated (`[A-Za-z0-9][A-Za-z0-9._-]{0,63}`) and are never used
  as filesystem paths.
* Heartbeats without the token are rejected with `403`.
* The registry is written atomically; a corrupt file starts clean instead of
  taking the app down.
* **This layer opens no port and publishes nothing.** Exposing the API safely is
  the job of the existing `services.remote_access` (Tailscale Serve) plus the
  app's own authentication. Do not put an unauthenticated generation API on the
  public internet.
* A phone cannot wake a powered-off computer. Nothing here claims it can.

### Adding more computers later

Each node announces and accepts work independently. Nothing in this design
spreads *one* model across several devices — that needs a validated solution and
is deliberately out of scope. What is supported today is distributing
**independent** work: shots, images, audio and processing across nodes.

---

## 3. ComfyUI

`app/services/comfyui_adapter.py` speaks ComfyUI's documented HTTP API:
`/system_stats`, `/object_info`, `/prompt`, `/history/{id}`, `/queue`,
`/interrupt`.

Workflows are **versioned templates** under
`app/services/comfy_workflows/<template_id>/` (`manifest.json` + `workflow.json`).
The manifest declares which semantic input maps to which graph node field — the
adapter never guesses, and it **rejects a parameter the manifest does not
declare** instead of silently running the workflow with a default.

A template only counts as supported when **every `class_type` it uses is present
in the server's own `/object_info`**. Otherwise it is listed under
`unsupported_templates` with the exact missing class names.

```
POST /api/v1/engines/comfyui/probe          # what can this server really do?
GET  /api/v1/engines/comfyui/templates      # the versioned library
POST /api/v1/engines/comfyui/run            # submit {template_id, params}
GET  /api/v1/engines/comfyui/run/{id}       # progress, then results
POST /api/v1/engines/comfyui/run/{id}/cancel
```

Cancelling is honest: a queued prompt is deleted from ComfyUI's queue; a running
prompt uses `/interrupt`, which is **global** in ComfyUI and is reported as such.

**Shipped templates:** `image_txt2img` (the canonical text-to-image graph) and
`wan22_ti2v_5b_txt2video` (Wan 2.2 TI2V-5B text→video, every value the app needs
to set declared as an input). Audio and speech templates are deliberately **not**
shipped: their graphs depend on the exact model package installed, and shipping an
unrun graph would be claiming an integration that was never tested. See
`app/services/comfy_workflows/README.md` to add your own; the adapter verifies it
for you.

### Installing and proving the local engine

The adapter above can *talk* to a server; [`COMFYUI-LOCAL.md`](COMFYUI-LOCAL.md)
is how one is **installed, detected and proved** on a real machine, and it is the
studio's default engine:

```
GET  /api/v1/engines/local/hardware        # measured CPU/RAM/disk/GPU + toolchain
GET  /api/v1/engines/local/models          # catalogue: licence, sizes, requirements
GET  /api/v1/engines/local/setup           # the install plan, before anything runs
POST /api/v1/engines/local/setup/install   # run it (confirm: true; weights need a 2nd flag)
POST /api/v1/engines/local/functional-test # generate a real clip and fetch it back
POST /api/v1/engines/local/register        # only with a passed test on record
POST /api/v1/engines/local/queue-job       # send a real generation to the queue
```

An engine is registered **only after** a functional test produced a file whose
container was verified. Generation is then ordinary queue work with one special
rule: **an offline computer defers the job instead of failing it**.

---

## 4. Large-file transfer

`/api/v1/upload` reads the whole body into memory and is fine for a reference
still. Large media goes through a **session** instead:

```
GET    /api/v1/uploads/limits                          # per-kind limits, configurable
POST   /api/v1/uploads/sessions                        # {filename, total_bytes, sha256?}
POST   /api/v1/uploads/sessions/{id}/chunks?index=&sha256=
GET    /api/v1/uploads/sessions/{id}                   # what arrived, what is missing
POST   /api/v1/uploads/sessions/{id}/complete          # assemble, streamed, on disk
DELETE /api/v1/uploads/sessions/{id}                   # cancel + delete temp data
POST   /api/v1/uploads/sessions/cleanup                # finished, and stale if asked
```

* Chunks are written straight to disk. **Nothing large is ever held in RAM**, and
  the request body is read with a hard cap, so a client cannot make the server
  buffer more than one chunk.
* Interrupting is not a restart: `GET` the session, send only the
  `missing_chunks`, and complete.
* Each chunk can carry its own SHA-256; the whole file's SHA-256 is computed
  **while assembling**. A mismatch fails the session and keeps the chunks so they
  can be re-sent.
* Assembly writes a temporary file and renames it atomically, so a crash cannot
  leave a truncated file where a complete one is expected. Identical content is
  reused (deduplication).
* Space is checked **before** the session is accepted; a full disk fails early.
* Cancelling, and a corrupt session file, are handled explicitly. `cleanup`
  never deletes an in-progress upload unless you pass `include_open: true`.

### Limits

| Kind | Default limit |
| --- | --- |
| image | 64 MB |
| audio | 1 GB |
| video | 16 GB |
| document | 256 MB |
| other | 1 GB |

Chunk size defaults to 8 MB (min 64 KB, max 256 MB). Raise a limit by calling
`kind_limits({"video": 64 * 1024**3})` from an integration, or by passing
`limits=` to `init_session`. The API reports the effective values from
`GET /api/v1/uploads/limits`; **there is no unlimited upload**.

---

## 5. Reference URLs

```
POST /api/v1/reference/url/validate   # verdict only, no request is made
POST /api/v1/reference/url/import     # download, optionally attach to a production
```

The guard fails closed:

* Only `http`/`https`. `file://`, `gopher://`, `ftp://`, `data:` … are refused
  **by name**.
* The host is resolved **before** any request and **every** resulting address
  must be public. Loopback, private (RFC1918), link-local, unique-local IPv6,
  multicast, reserved and unspecified addresses are refused, including
  IPv4-mapped loopback (`::ffff:127.0.0.1`).
* Cloud metadata endpoints are refused by hostname and by IP
  (`169.254.169.254`, `169.254.170.2`, `100.100.100.200`, `fd00:ec2::254`,
  `metadata.google.internal` …).
* **Redirects are re-validated per hop** (cap 5), so a public URL cannot bounce
  into `localhost`.
* Ports are restricted to 80/443 unless configured; embedded credentials are
  refused; the byte cap is enforced **while streaming** rather than trusting
  `Content-Length`.
* Content types are limited to media, PDF, plain text/markdown and subtitles.
  HTML is refused: the studio imports files, it does not scrape pages.
* Known platforms that gate media behind a login (YouTube, Instagram, TikTok, …)
  are **reported, not circumvented**. Downloads are stored, never executed.

Residual risk, stated rather than hidden: validation resolves the name and the
HTTP client resolves it again when connecting, so a hostile DNS server could
answer differently the second time (DNS rebinding). Requiring *all* resolved
addresses to be public narrows that window; the documented hardening is to run
the backend behind the authenticated network path (`services.remote_access`).

---

## 6. What runs where

| Capability | This sandbox / a CPU-only container | A machine with an NVIDIA GPU |
| --- | --- | --- |
| Engine registry, node registry, capability routing | yes | yes |
| ComfyUI protocol client (needs a reachable server) | client only | client + server |
| Chunked/resumable uploads, URL import | yes | yes |
| Production projects, brief, storyboard, bibles | yes | yes |
| Audio analysis (librosa), image metadata (Pillow) | only with those wheels installed | yes |
| Hardware/model detection, install plan, functional test | yes (reports no GPU) | yes |
| Shot generation, MP4 render | **no** | yes |
| ComfyUI generation | **no** (nothing to connect to) | yes, once the local engine is installed |

The Films workspace reports this live (`GET /api/v1/film/engine`, the engine badge
and the **Compute** phase) instead of pretending a missing capability exists.

---

## 7. Costs — read this before assuming "free"

Nothing in the studio requires a paid API, a credit balance or a subscription:
the local-only path needs no keys at all. That is **not** the same as costing
nothing. Separately:

* **Hardware.** A CUDA GPU with enough VRAM for the models you choose, plus
  storage for weights (tens of GB, sometimes more). This is the dominant cost and
  it is a purchase, not a subscription.
* **Electricity.** A GPU under sustained load draws hundreds of watts for the
  duration of a render. On a laptop it also means shorter battery life and more
  heat.
* **Storage.** Reference media, rendered clips and exports accumulate. The
  upload limits above bound a single file, not the total footprint; you manage
  deletion yourself.
* **Connectivity.** Directing from a phone over the LAN costs nothing extra;
  reaching the desktop from another network needs a VPN or an authenticated
  tunnel, which may be a paid service.
* **Per generation, nothing.** The default engine (self-hosted ComfyUI on your
  own GPU) has no per-clip cost: no account, no credits, no paid provider in the
  path. See [`COMFYUI-LOCAL.md`](COMFYUI-LOCAL.md).
* **Optional services.** Commercial engines and hosted storage can be added as
  adapters, and they cost money. They are never required, and the registry
  reports `local_only` when none is configured.
* **Model licences.** Running a model is not the same as being licensed to use
  its output commercially. Check each model's own terms
  (`app/LICENSES/*`, `app/models/*/NOTICE.md`, `THIRD_PARTY_NOTICES.md`); the
  studio makes no claim on their behalf.

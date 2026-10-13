# The production queue

Maestro's existing job table drives one generation at a time in the Studio. The
**production queue** is the queue *of record* for a film: it survives a restart,
it can be paused and cancelled honestly, it decides what may run when, and it
tells the phone what is really happening on the computer.

It is implemented in:

| Module | Responsibility |
| --- | --- |
| `app/services/generation_queue.py` | The queue itself: states, record, priorities, retries, cancellation, recovery, event log |
| `app/services/queue_worker.py` | The executor: claims a job, runs the handler registered for its engine, writes the outcome back |
| `app/services/production_queue_api.py` | HTTP shell (a router factory, so it is testable without booting the app) |

The UI panel is `ui/src/studio/StudioQueuePanel.tsx` (Studio → *Compute &
transfer*).

---

## 1. Every job is recorded before it runs

A job carries the whole story of the work: **project, scene, shot, task kind,
engine, model, parameters, input files, execution node, state, progress, times,
outputs, errors and retries**.

```
project_id  scene_id  shot_id        # where it belongs in the film
task        engine    model          # what it is and who runs it
params      inputs                   # the exact request and its source files
node_id     worker                   # which computer took it
state       priority  attempts/max_retries
progress    {percent, step, total_steps, source, note, reported_at}
created_at  started_at  finished_at  duration_seconds
outputs[]   errors[]   events[]      # artefacts, failures, its own trail
```

Nothing here stores file *contents*: jobs hold paths and metadata, never media.

**Persistence.** The queue is one JSON document (`settings/generation_queue/queue.json`)
written atomically (`fsync` + `os.replace`) under a process lock, so a crash
cannot leave half a queue. Finished history is trimmed to a configurable bound
(`keep_terminal`), and the event log to `event_limit`, so the file cannot grow
without limit. A corrupt file starts clean rather than taking the app down —
and because a fresh queue runs nothing, starting clean is the safe failure.

## 2. States, and the transitions that are legal

`draft → pending → preparing → running → finalizing → completed | failed | cancelled`

| State | Means |
| --- | --- |
| `draft` | Held on purpose (e.g. `start: "held"`). Not claimable until moved to `pending`. |
| `pending` | Waiting for a runner; its `available_at` may defer it (retry delay). |
| `preparing` | Claimed. The runner is loading a model / preparing inputs. |
| `running` | The engine is working. |
| `finalizing` | Post-processing, muxing, saving. |
| `completed` | Finished **with artefacts that exist on disk**. |
| `failed` | The error is recorded and the attempt counted. |
| `cancelled` | Really stopped, or stopped before it started. |

Only these moves are accepted; anything else raises instead of being tolerated,
so a bug in a runner surfaces as an error rather than as a silently
inconsistent job. Both retries out of `failed`/`cancelled` and the automatic
retry path are explicit steps in the job's own event trail.

## 3. Honest progress

Two different things are kept strictly apart:

- **The job's state** — where the job is. Always known.
- **Progress** — a percentage. Only known if the engine reports one.

`progress.percent` starts as `null` and stays `null` unless something measured
it: a ComfyUI queue position, a diffusion step counter, an FFmpeg frame counter.
An engine that reports nothing produces a job that is described by its state,
with `source: "none"` and a note saying so, for its entire life.

**A finishing HTTP request is not generation progress.** Submitting a job,
polling it, or claiming it never changes `percent`; only `report_progress` can,
and it is called by the thing doing the work. Completion sets 100% only for a
job that was reporting percentages in the first place.

`report_progress` also derives a percentage from a step count the engine gave
(`step` of `total_steps`) — that is a measurement, not an estimate.

## 4. Running work: concurrency, memory, priorities

| Setting | Effect |
| --- | --- |
| `max_concurrent` | How many jobs may be in a running state at once (minimum 1). |
| `memory_ceiling_mb` | Ceiling over the **declared** memory of running jobs. `0` = no ceiling. A job that would not fit is **skipped**, not started and starved. |
| `max_retries` | Automatic retries after a retryable failure, on top of the first attempt. |
| `auto_retry` | Whether a retryable failure is requeued automatically. |
| `stale_after_seconds` | How long a silent running job may stay "running" before recovery treats it as abandoned. |
| `retry_delay_seconds` | Delay before a retried job becomes claimable again. |
| `keep_terminal` / `event_limit` | History bounds. |

Selection order is **highest priority first, then oldest submission**. Memory is
*declared* by the caller (a job says "I need 9000 MB"); the queue never measures
per-job VRAM, because it cannot. The machine's own total RAM is reported by the
API (`observed_system_memory_mb`) so the ceiling can be chosen with a real number
in front of you.

### Duplicate protection

A job may carry a `dedupe_key` (typically `shot-<shot_id>`). Submitting that key
while an earlier job with it is still active returns **that** job instead of
creating a second one, and records a `duplicate_ignored` event. A double tap on
a phone cannot render the same shot twice.

### Cancellation: requested vs. stopped

- A job that has not started is cancelled immediately — this is the common case
  and it is reported as `cancelled_before_start`.
- A job a runner is holding is only reported as `cancelled` when the engine
  declared it **can** be stopped (`cancel_supported`). Otherwise the request is
  recorded (`cancel_requested`) and the API answers, explicitly, that the job is
  *still running* until the runner acknowledges it.
- The runner acknowledges with `acknowledge-cancel`, or by having a worker that
  observed the flag and stopped. Partial output from a stopped job is **not**
  published as a result.

## 5. Recovery, abandonment and cleanup

`recover()` is the crash path, and it runs **automatically at backend start**
(FastAPI startup handler) as well as on demand:

- A job whose runner stopped talking for `stale_after_seconds` is requeued with
  its attempt counted, or marked `failed` when its retries are spent.
- Nothing is deleted. A job is never marked `completed` because it merely looks
  finished.
- Drafts that were never started are listed as `abandoned_drafts`.
- `prune()` trims finished history to `keep_terminal`; active jobs are never
  touched.

Reopening the app after a crash therefore shows the real queue: what finished,
what failed, what went back to waiting.

## 6. The runner

`queue_worker` claims the next runnable job and hands it to the handler
registered for that job's **engine string**:

```python
from services import queue_worker

def local_generate(job, context):
    context.set_state("running")
    context.report(step=1, total_steps=20, note="denoising")
    ...
    return ["/abs/path/to/shot.mp4"]     # real files, absolute paths

queue_worker.register_handler("local:maestro", local_generate)
```

What a handler can do is deliberately narrow: move its own job along
(`set_state`), attach the engine's job id (`link`), report what it measured
(`report`), fail with a message, or finish with real files. It cannot claim other
jobs, change queue settings, or complete a job with a path that does not exist.

Three honesty guarantees hold no matter what a handler returns:

1. **No handler → the job fails** with `No runner is registered for engine …`.
   It is not retried (retrying cannot conjure an engine) and it is never
   reported as generated.
2. **No artefacts → failure.** Completing without outputs, or with a relative
   path, or with a path that is not on disk, is refused by the queue itself.
3. **Cancelled → cancelled.** If a cancel was requested and the handler stopped,
   the job is recorded as cancelled and its partial files are not published.

The background runner is **opt-in** (`POST /api/v1/production/queue/worker/start`).
Nothing generates in the background because the app started; and the handler
registry is empty by default, because in an installation with no usable engine
the honest answer is "no runner", not a fabricated file.

## 7. HTTP API

All of it is under `/api/v1/production/queue`.

| Method | Path | Does |
| --- | --- | --- |
| GET | `/api/v1/production/queue` | jobs (filters: `state`, `project_id`, `shot_id`, `include_terminal`, `limit`) + counts + settings + runner status |
| GET / PUT | `/api/v1/production/queue/settings` | read / update the limits (unknown keys are rejected) |
| POST | `/api/v1/production/queue/jobs` | submit (`start: "held"` or `draft: true` creates a draft; `dedupe_key` prevents duplicates) |
| GET | `/api/v1/production/queue/jobs/{id}` | one job (`?events=true` adds its own trail) |
| POST | `/api/v1/production/queue/jobs/{id}/state` | move it along (`running`, `finalizing`, `pending`…) |
| POST | `/api/v1/production/queue/jobs/{id}/progress` | record progress the engine reported |
| POST | `/api/v1/production/queue/jobs/{id}/complete` | finish with existing files |
| POST | `/api/v1/production/queue/jobs/{id}/fail` | record a real failure (`retryable`) |
| POST | `/api/v1/production/queue/jobs/{id}/retry` | requeue a failed/cancelled job |
| POST | `/api/v1/production/queue/jobs/{id}/cancel` | cancel, or record the request and say so |
| POST | `/api/v1/production/queue/jobs/{id}/acknowledge-cancel` | the runner confirms a stopped job |
| POST | `/api/v1/production/queue/jobs/{id}/link` | attach the engine's own job id |
| GET | `/api/v1/production/queue/jobs/{id}/outputs` | what a finished job produced, by **index** (name, media type, size, present) |
| GET | `/api/v1/production/queue/jobs/{id}/outputs/{index}` | stream one produced file, resumable with a `Range` request |
| POST | `/api/v1/production/queue/claim` | claim the next runnable job (node agent / local worker) |
| POST | `/api/v1/production/queue/run` | run what is claimable now, up to `limit` |
| POST | `/api/v1/production/queue/recover` | requeue interrupted work now |
| POST | `/api/v1/production/queue/prune` | trim finished history |
| GET | `/api/v1/production/queue/events` | queue-wide event log, or one job's |
| POST | `/api/v1/production/queue/worker/start` \| `/stop` | start/stop the built-in runner |

**Who calls what.** The UI panel reads the queue, submits, cancels, retries,
runs claimable work, recovers and prunes. The remaining endpoints
(`state`, `progress`, `complete`, `fail`, `link`, `acknowledge-cancel`) are the
**runner's** contract: they are what a node agent or an engine adapter calls while
it executes a job. They are public because the phone may direct a computer that
runs them; they are not hidden behind the UI to look "integrated".

The two `outputs` endpoints exist for **delegated work**: when another Maestro runs
a job for this one (`services.remote_engine`), it hands the artefacts back through
them. A caller asks for output *N* of a job id — it never names a path, because the
path comes from this queue's own record (already verified to exist on disk when the
job completed), and only the file name, its type and its size cross the boundary.
Streaming with a `Range` header is what makes a multi-gigabyte clip resumable over
a VPN instead of restarted.

## 8. What is verified, and what is not

Verified by tests (`tests/test_generation_queue.py`, `tests/test_queue_worker.py`,
`tests/test_production_queue_api.py`):

- every legal and illegal transition; priorities; the memory ceiling (including
  refusing an over-ceiling submission); bounded retries; duplicate refusal;
  cancellation in all four shapes (before start, requested-only, acknowledged,
  on a finished job); recovery of interrupted work; history trimming; the event
  log; and the API end to end over HTTP (submit → claim → progress → complete,
  plus the refusal cases).
- the honesty rules above, as tests: a missing runner fails the job, a handler
  with no output fails, a path that does not exist is refused, and a job whose
  engine reports nothing keeps `percent = null` from submission to completion.

Not done yet, stated plainly:

- **No real engine handler is registered by default.** Wiring the local Maestro
  generation path and the ComfyUI adapter into `register_handler` is the next
  phase's work; until then the queue will honestly answer `no_handler` for those
  engines instead of running them.
- **No cross-node scheduling here.** Distributing *independent* jobs across
  computers is the compute-node layer's job (`docs/STUDIO-NODES.md`); spreading a
  *single* model across devices is out of scope.
- **No progress from engines that do not report it.** That is by design, not a
  gap to be filled with an invented bar.

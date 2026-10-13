"""Persistent generation queue (spec §10).

A queue of record for production work, with the eight states the spec requires,
the per-job record it requires, and honest progress.

Two rules shape the whole module:

1. **Progress is the engine's, not the transport's.**  A job's ``progress``
   starts as ``None`` and only ever carries a percentage that an engine reported
   through :func:`report_progress`.  A job whose engine does not report progress
   keeps ``percent = None`` and ``source = "none"`` for its whole life — the UI
   shows the *state* instead of a fabricated bar.  An HTTP request finishing is
   not generation progress, and nothing here derives one from the other.
2. **Nothing is lost silently.**  A job interrupted by a crash is recovered on
   the next start (requeued with its attempt counted, or failed when its retries
   are spent), duplicate submissions are rejected instead of duplicated, and
   cancellation is only reported as done when it really happened.

Storage is a single JSON document written atomically, guarded by a process lock;
terminal history is trimmed to a configurable bound so the file cannot grow
without limit.  Large artefacts are never stored here — jobs hold *paths* and
metadata, never file contents.

The queue drives the existing engine path: a real Studio job id can be attached
with :func:`link_external_job`, and that job's own progress callback can forward
into :func:`report_progress`, so this layer never becomes a second source of
truth about what the engine is doing.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from typing import Any, Mapping, Optional

__all__ = [
    "ACTIVE_STATES",
    "DEFAULT_SETTINGS",
    "QUEUE_SCHEMA_VERSION",
    "RUNNING_STATES",
    "STATES",
    "TERMINAL_STATES",
    "GenerationQueueError",
    "acknowledge_cancel",
    "cancel",
    "claim_next",
    "complete",
    "configure",
    "defer",
    "events",
    "fail",
    "job",
    "link_external_job",
    "list_jobs",
    "prune",
    "read_queue",
    "recover",
    "report_progress",
    "retry",
    "set_state",
    "settings",
    "submit",
    "summary",
    "system_memory_mb",
]

QUEUE_SCHEMA_VERSION = 1

STATES = (
    "draft",
    "pending",
    "preparing",
    "running",
    "finalizing",
    "completed",
    "failed",
    "cancelled",
)
# States a job can be *worked on* from.
ACTIVE_STATES = ("pending", "preparing", "running", "finalizing")
# States that mean "a worker is holding this job right now".
RUNNING_STATES = ("preparing", "running", "finalizing")
TERMINAL_STATES = ("completed", "failed", "cancelled")

# Only these transitions are legal; anything else is a bug, not a state to
# tolerate.  ``preparing -> preparing`` is allowed so a worker can re-announce
# itself without losing the job.
_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("pending", "cancelled", "failed"),
    "pending": ("preparing", "cancelled", "failed", "pending"),
    "preparing": ("running", "finalizing", "completed", "failed", "cancelled", "pending"),
    "running": ("finalizing", "completed", "failed", "cancelled", "running", "pending"),
    "finalizing": ("completed", "failed", "cancelled", "finalizing", "pending"),
    "completed": (),
    "failed": ("pending",),  # only via an explicit retry()
    "cancelled": ("pending",),  # retrying a cancelled job is a deliberate action
}

TASK_KINDS = (
    "image",
    "video",
    "audio",
    "speech",
    "transcription",
    "assembly",
    "export",
    "analysis",
    "other",
)

DEFAULT_SETTINGS: dict[str, Any] = {
    # How many jobs may be in a running state at once.
    "max_concurrent": 1,
    # Ceiling over the *declared* memory of running jobs, in MB. 0 = no ceiling.
    # Declared, never measured: see ``system_memory_mb`` for what we can observe.
    "memory_ceiling_mb": 0,
    # Automatic retries after a retryable failure, on top of the first attempt.
    "max_retries": 1,
    "auto_retry": True,
    # A running job whose worker has not spoken for this long is considered
    # abandoned and is recovered.
    "stale_after_seconds": 900.0,
    # Seconds to wait before a retry becomes claimable again.
    "retry_delay_seconds": 0.0,
    # How long a job steps aside when its engine is temporarily unavailable
    # (a computer that is off, a server that is restarting).  A deferral is not a
    # failure and is not counted as an attempt: the work is kept, not lost.
    "defer_seconds": 60.0,
    # How many terminal jobs to keep in history.
    "keep_terminal": 200,
    # Bounded event log, so the file cannot grow forever.
    "event_limit": 500,
}

_MAX_PARAMS = 60
_MAX_TEXT = 4000
_LOCK = threading.RLock()


class GenerationQueueError(ValueError):
    """Invalid job, illegal transition, or a queue setting that cannot apply."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _now(now: Optional[float]) -> float:
    return float(now) if now is not None else time.time()


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def _nonneg_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, item in list(value.items())[:_MAX_PARAMS]:
        name = str(key)[:64]
        if isinstance(item, (bool, int, float)) or item is None:
            out[name] = item
        elif isinstance(item, str):
            out[name] = item[:2000]
        elif isinstance(item, (list, tuple)):
            out[name] = [_text(entry, 500) for entry in list(item)[:50]]
        elif isinstance(item, Mapping):
            out[name] = {str(k)[:64]: _text(v, 500) for k, v in list(item.items())[:50]}
    return out


def _string_list(value: Any, limit: int = 200) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value[:_MAX_TEXT]]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    out: list[str] = []
    for item in value:
        text = _text(item)
        if text:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _queue_path(root: str) -> str:
    return os.path.join(root, "queue.json")


def _atomic_write_json(path: str, payload: Mapping[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".queue.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def system_memory_mb() -> Optional[int]:
    """Total physical memory in MB when the platform exposes it, else None.

    This is the only memory figure we *measure*.  Per-job memory is declared by
    the caller: VRAM residency is a property of a model we cannot query.
    """
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return int((pages * page_size) // (1024 * 1024))
    except (AttributeError, ValueError, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def _empty_queue() -> dict[str, Any]:
    return {
        "version": QUEUE_SCHEMA_VERSION,
        "settings": dict(DEFAULT_SETTINGS),
        "jobs": {},
        "events": [],
    }


def _normalize_settings(raw: Any) -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    if not isinstance(raw, Mapping):
        return settings
    for key in DEFAULT_SETTINGS:
        if key not in raw:
            continue
        value = raw[key]
        if key in {"auto_retry"}:
            settings[key] = bool(value)
            continue
        if key in {"max_concurrent", "memory_ceiling_mb", "max_retries", "keep_terminal", "event_limit"}:
            settings[key] = _nonneg_int(value, DEFAULT_SETTINGS[key])
            continue
        try:
            settings[key] = max(0.0, float(value))
        except (TypeError, ValueError):
            settings[key] = DEFAULT_SETTINGS[key]
    # A queue with zero concurrency could never make progress.
    settings["max_concurrent"] = max(1, int(settings["max_concurrent"]))
    settings["event_limit"] = max(10, int(settings["event_limit"]))
    settings["keep_terminal"] = max(0, int(settings["keep_terminal"]))
    return settings


def _normalize_job(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return None
    job_id = _text(raw.get("job_id"), 80)
    if not job_id:
        return None
    state = _text(raw.get("state"), 20)
    if state not in STATES:
        state = "failed"
    progress_raw = raw.get("progress") if isinstance(raw.get("progress"), Mapping) else {}
    percent = progress_raw.get("percent")
    if percent is not None:
        try:
            percent = max(0, min(100, int(float(percent))))
        except (TypeError, ValueError):
            percent = None
    return {
        "job_id": job_id,
        "project_id": _text(raw.get("project_id"), 120),
        "scene_id": _text(raw.get("scene_id"), 120),
        "shot_id": _text(raw.get("shot_id"), 120),
        "task": _text(raw.get("task"), 40) or "other",
        "engine": _text(raw.get("engine"), 200),
        "model": _text(raw.get("model"), 200),
        "params": _mapping(raw.get("params")),
        "inputs": _string_list(raw.get("inputs")),
        "node_id": _text(raw.get("node_id"), 120),
        "state": state,
        "priority": int(raw.get("priority") or 0),
        "attempts": _nonneg_int(raw.get("attempts")),
        "deferrals": _nonneg_int(raw.get("deferrals")),
        "deferred_reason": _text(raw.get("deferred_reason"), 500),
        "max_retries": _nonneg_int(raw.get("max_retries"), DEFAULT_SETTINGS["max_retries"]),
        "memory_mb": _nonneg_int(raw.get("memory_mb")),
        "dedupe_key": _text(raw.get("dedupe_key"), 200),
        "progress_supported": bool(raw.get("progress_supported", True)),
        "cancel_supported": bool(raw.get("cancel_supported", True)),
        "cancel_requested": bool(raw.get("cancel_requested", False)),
        "progress": {
            # ``percent`` stays None until an engine reports one. Never inferred.
            "percent": percent,
            "step": _nonneg_int(progress_raw.get("step")) if progress_raw.get("step") is not None else None,
            "total_steps": _nonneg_int(progress_raw.get("total_steps"))
            if progress_raw.get("total_steps") is not None
            else None,
            "source": _text(progress_raw.get("source"), 20) or ("reported" if percent is not None else "none"),
            "note": _text(progress_raw.get("note"), 500),
            "reported_at": progress_raw.get("reported_at"),
        },
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "started_at": raw.get("started_at"),
        "finished_at": raw.get("finished_at"),
        "duration_seconds": raw.get("duration_seconds"),
        "available_at": raw.get("available_at"),
        "worker": _text(raw.get("worker"), 120),
        "external_job_id": _text(raw.get("external_job_id"), 120),
        "outputs": _string_list(raw.get("outputs")),
        "errors": _string_list(raw.get("errors")),
        "events": list(raw.get("events") or [])[-40:],
    }


def read_queue(root: str) -> dict[str, Any]:
    """The whole queue document, normalized.  A corrupt file starts clean."""
    path = _queue_path(root)
    if not os.path.isfile(path):
        return _empty_queue()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        # Never let a damaged queue file stop the app: the jobs it described
        # cannot be trusted anyway, and a fresh queue is safe (nothing runs).
        return _empty_queue()
    if not isinstance(raw, Mapping):
        return _empty_queue()
    state = _empty_queue()
    state["settings"] = _normalize_settings(raw.get("settings"))
    jobs = raw.get("jobs")
    if isinstance(jobs, Mapping):
        for key, value in jobs.items():
            job = _normalize_job(value)
            if job is not None:
                state["jobs"][str(key)] = job
    raw_events = raw.get("events")
    if isinstance(raw_events, list):
        state["events"] = [event for event in raw_events if isinstance(event, Mapping)]
    return state


def _save_queue(root: str, state: Mapping[str, Any]) -> None:
    _atomic_write_json(_queue_path(root), state)


def _record_event(state: dict[str, Any], kind: str, job_id: str, detail: Mapping[str, Any] | str = "") -> dict[str, Any]:
    event = {
        "at": time.time(),
        "kind": kind,
        "job_id": job_id,
        "detail": _mapping(detail) if isinstance(detail, Mapping) else _text(detail, 500),
    }
    events = list(state.get("events") or [])
    events.append(event)
    limit = int((state.get("settings") or {}).get("event_limit") or DEFAULT_SETTINGS["event_limit"])
    if len(events) > limit:
        events = events[-limit:]
    state["events"] = events
    return event


def _job_event(job: dict[str, Any], kind: str, detail: Mapping[str, Any] | str = "") -> None:
    events = list(job.get("events") or [])
    events.append({"at": time.time(), "kind": kind, "detail": _text(detail, 500)})
    job["events"] = events[-40:]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def settings(root: str) -> dict[str, Any]:
    return dict(read_queue(root)["settings"])


def configure(root: str, patch: Mapping[str, Any]) -> dict[str, Any]:
    """Update queue settings.  Unknown keys are rejected, not ignored."""
    if not isinstance(patch, Mapping):
        raise GenerationQueueError("Settings must be an object")
    unknown = [key for key in patch if key not in DEFAULT_SETTINGS]
    if unknown:
        raise GenerationQueueError(
            "Unknown setting(s): " + ", ".join(sorted(unknown))
            + ". Known: " + ", ".join(DEFAULT_SETTINGS)
        )
    with _LOCK:
        state = read_queue(root)
        merged = {**state["settings"], **dict(patch)}
        state["settings"] = _normalize_settings(merged)
        _record_event(state, "settings_changed", "*", patch)
        _save_queue(root, state)
        return dict(state["settings"])


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------


def _find_active_duplicate(state: Mapping[str, Any], dedupe_key: str) -> Optional[dict[str, Any]]:
    if not dedupe_key:
        return None
    for job in state["jobs"].values():
        if job.get("dedupe_key") == dedupe_key and job.get("state") not in TERMINAL_STATES:
            return job
    return None


def submit(
    root: str,
    job: Mapping[str, Any],
    *,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Add a job to the queue.  Returns ``{"created", "job", "reason"}``.

    A job is created in ``draft`` (not runnable) when ``start: false``, otherwise
    in ``pending`` and therefore claimable.  Submitting the same ``dedupe_key``
    while an earlier job is still active returns that job instead of creating a
    second one, so a double-tap cannot render a shot twice.
    """
    if not isinstance(job, Mapping):
        raise GenerationQueueError("A job must be an object")
    task = _text(job.get("task"), 40) or "other"
    if task not in TASK_KINDS:
        raise GenerationQueueError(
            f"Unknown task {task!r}. Known tasks: " + ", ".join(TASK_KINDS)
        )
    engine = _text(job.get("engine"), 200)
    if not engine:
        raise GenerationQueueError("A job must name the engine that will run it")
    dedupe_key = _text(job.get("dedupe_key"), 200)
    stamp = _now(now)

    with _LOCK:
        state = read_queue(root)
        queue_settings = state["settings"]
        memory_mb = _nonneg_int(job.get("memory_mb"))
        ceiling = int(queue_settings.get("memory_ceiling_mb") or 0)
        if ceiling and memory_mb > ceiling:
            raise GenerationQueueError(
                f"This job declares {memory_mb} MB but the queue memory ceiling is "
                f"{ceiling} MB; raise memory_ceiling_mb or lower the job's requirement"
            )

        existing = _find_active_duplicate(state, dedupe_key)
        if existing is not None:
            _record_event(state, "duplicate_ignored", existing["job_id"], {"dedupe_key": dedupe_key})
            _save_queue(root, state)
            return {
                "created": False,
                "reason": "An identical job is already active; the existing one was returned.",
                "job": dict(existing),
            }

        wants_draft = bool(job.get("draft")) or str(job.get("start") or "").lower() == "held"
        job_id = _text(job.get("job_id"), 80) or uuid.uuid4().hex[:16]
        if job_id in state["jobs"]:
            raise GenerationQueueError(f"Job id {job_id!r} already exists")
        record = _normalize_job({
            "job_id": job_id,
            "project_id": job.get("project_id"),
            "scene_id": job.get("scene_id"),
            "shot_id": job.get("shot_id"),
            "task": task,
            "engine": engine,
            "model": job.get("model"),
            "params": job.get("params"),
            "inputs": job.get("inputs"),
            "node_id": job.get("node_id"),
            "state": "draft" if wants_draft else "pending",
            "priority": job.get("priority"),
            "attempts": 0,
            "deferrals": 0,
            # The queue's policy is the default; a job may ask for its own bound.
            "max_retries": job.get("max_retries", queue_settings["max_retries"]),
            "memory_mb": memory_mb,
            "dedupe_key": dedupe_key,
            "progress_supported": job.get("progress_supported", True),
            "cancel_supported": job.get("cancel_supported", True),
            "created_at": stamp,
            "updated_at": stamp,
            "available_at": stamp,
        })
        if record is None:  # pragma: no cover - _normalize_job only fails on a missing id
            raise GenerationQueueError("Could not build the job record")
        state["jobs"][job_id] = record
        _job_event(record, "submitted", {"state": record["state"], "task": task, "engine": engine})
        _record_event(state, "submitted", job_id, {"task": task, "engine": engine})
        _save_queue(root, state)
        return {"created": True, "job": dict(record), "reason": ""}


def link_external_job(root: str, job_id: str, external_job_id: str, *, now: Optional[float] = None) -> dict[str, Any]:
    """Attach the engine's own job id, so its progress callback can report here."""
    with _LOCK:
        state = read_queue(root)
        record = _require_job(state, job_id)
        record["external_job_id"] = _text(external_job_id, 120)
        record["updated_at"] = _now(now)
        _job_event(record, "linked", record["external_job_id"])
        _record_event(state, "linked", job_id, {"external_job_id": record["external_job_id"]})
        _save_queue(root, state)
        return dict(record)


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def _require_job(state: Mapping[str, Any], job_id: str) -> dict[str, Any]:
    record = state["jobs"].get(str(job_id))
    if record is None:
        raise FileNotFoundError(f"Job {job_id!r} is not in the queue")
    return record


def _set_state(state: dict[str, Any], job: dict[str, Any], new_state: str, *, now: Optional[float]) -> None:
    if new_state not in STATES:
        raise GenerationQueueError(f"Unknown state {new_state!r}. Known: " + ", ".join(STATES))
    current = job["state"]
    if new_state != current and new_state not in _ALLOWED_TRANSITIONS.get(current, ()):
        raise GenerationQueueError(f"Illegal transition {current} → {new_state} for job {job['job_id']}")
    stamp = _now(now)
    job["state"] = new_state
    job["updated_at"] = stamp
    if new_state == "running" and not job.get("started_at"):
        job["started_at"] = stamp
    if new_state in TERMINAL_STATES:
        job["finished_at"] = stamp
        started = job.get("started_at") or job.get("created_at")
        if started:
            job["duration_seconds"] = round(max(0.0, stamp - float(started)), 3)
    if new_state == "pending":
        job["available_at"] = stamp + float((state.get("settings") or {}).get("retry_delay_seconds") or 0.0)
        # A job that is queued again is no longer held by a worker, and it is not
        # finished: leaving finished_at set would make a waiting job look done.
        job["worker"] = ""
        job["cancel_requested"] = False
        job["finished_at"] = None
        job["duration_seconds"] = None
    _job_event(job, "state", new_state)


def _busy_count(state: Mapping[str, Any]) -> int:
    return sum(1 for job in state["jobs"].values() if job["state"] in RUNNING_STATES)


def _busy_memory(state: Mapping[str, Any]) -> int:
    return sum(
        int(job.get("memory_mb") or 0)
        for job in state["jobs"].values()
        if job["state"] in RUNNING_STATES
    )


def claim_next(
    root: str,
    *,
    worker: str = "",
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Move the best runnable job to ``preparing`` and return it.

    Selection is: highest priority first, then oldest submission — except that a
    job whose declared memory would not fit under the ceiling is skipped rather
    than started and starved.  Concurrency is the configured maximum.
    """
    stamp = _now(now)
    with _LOCK:
        state = read_queue(root)
        queue_settings = state["settings"]
        limit = int(queue_settings["max_concurrent"])
        busy = _busy_count(state)
        if busy >= limit:
            return {"claimed": None, "reason": f"Concurrency limit reached ({busy}/{limit})", "waiting": _pending_count(state)}

        ceiling = int(queue_settings.get("memory_ceiling_mb") or 0)
        used = _busy_memory(state)
        candidates = [
            job for job in state["jobs"].values()
            if job["state"] == "pending" and float(job.get("available_at") or 0) <= stamp
        ]
        candidates.sort(key=lambda job: (-int(job.get("priority") or 0), float(job.get("created_at") or 0), job["job_id"]))

        for job in candidates:
            needed = int(job.get("memory_mb") or 0)
            if ceiling and used + needed > ceiling:
                continue
            # Log the claim before the state move, so the trail reads in the
            # order things happened: claimed, then preparing.
            job["worker"] = _text(worker, 120)
            _job_event(job, "claimed", {"worker": job["worker"]})
            _set_state(state, job, "preparing", now=stamp)
            _record_event(state, "claimed", job["job_id"], {"worker": job["worker"]})
            _save_queue(root, state)
            return {"claimed": dict(job), "reason": "", "waiting": _pending_count(state)}

        reason = "Nothing is runnable right now"
        if candidates:
            reason = (
                f"{len(candidates)} job(s) are waiting for {ceiling - used} MB of the "
                f"{ceiling} MB queue memory ceiling"
            )
        return {"claimed": None, "reason": reason, "waiting": _pending_count(state)}


def _pending_count(state: Mapping[str, Any]) -> int:
    return sum(1 for job in state["jobs"].values() if job["state"] == "pending")


def set_state(root: str, job_id: str, state_name: str, *, worker: str = "", now: Optional[float] = None) -> dict[str, Any]:
    """Move a job along (``running`` / ``finalizing`` …) without inventing progress."""
    with _LOCK:
        state = read_queue(root)
        job = _require_job(state, job_id)
        _set_state(state, job, state_name, now=now)
        if worker:
            job["worker"] = _text(worker, 120)
        _save_queue(root, state)
        return dict(job)


def report_progress(
    root: str,
    job_id: str,
    *,
    percent: Optional[float] = None,
    step: Optional[int] = None,
    total_steps: Optional[int] = None,
    note: str = "",
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Record progress **as reported by the engine**.

    If the job's engine does not report progress, ``percent`` is ignored and the
    job keeps ``percent = None``: we say what state it is in instead of showing a
    number nobody measured.
    """
    stamp = _now(now)
    with _LOCK:
        state = read_queue(root)
        job = _require_job(state, job_id)
        if job["state"] in TERMINAL_STATES:
            raise GenerationQueueError(f"Job {job_id} is {job['state']}; it cannot report progress")
        progress = job["progress"]
        if not job.get("progress_supported", True):
            progress["percent"] = None
            progress["step"] = None
            progress["total_steps"] = None
            progress["source"] = "none"
            # Keep whatever the engine said, but never let it read as a measure.
            said = _text(note, 400)
            progress["note"] = (
                f"{said} — this engine reports no detailed progress"
                if said
                else "this engine reports no detailed progress"
            )
            progress["reported_at"] = stamp
            job["updated_at"] = stamp
            _save_queue(root, state)
            return dict(job)

        if percent is not None:
            try:
                progress["percent"] = max(0, min(100, int(round(float(percent)))))
            except (TypeError, ValueError):
                raise GenerationQueueError("percent must be a number between 0 and 100") from None
        if progress.get("total_steps"):
            if step is not None:
                progress["step"] = max(0, min(int(progress["total_steps"]), _nonneg_int(step)))
            # A steps-based engine also gives us a truthful percentage.
            if progress.get("step") is not None and progress["total_steps"]:
                progress["percent"] = int(round(100.0 * progress["step"] / progress["total_steps"]))
        elif step is not None:
            progress["step"] = _nonneg_int(step)
            progress["total_steps"] = _nonneg_int(total_steps) or None
        elif total_steps:
            progress["total_steps"] = _nonneg_int(total_steps)
        # A step count the engine itself supplied *is* a measurement, so a first
        # report carrying ``step`` and ``total_steps`` must yield a percentage
        # here too — otherwise an engine that reports once per job would never
        # show the progress it did report.  An explicit ``percent`` always wins.
        if percent is None and progress.get("step") is not None and progress.get("total_steps"):
            progress["percent"] = max(
                0, min(100, int(round(100.0 * progress["step"] / progress["total_steps"])))
            )
        progress["source"] = "reported" if progress.get("percent") is not None else "none"
        if note:
            progress["note"] = _text(note, 500)
        progress["reported_at"] = stamp
        job["updated_at"] = stamp
        _save_queue(root, state)
        return dict(job)


def defer(
    root: str,
    job_id: str,
    *,
    reason: str = "",
    delay_seconds: Optional[float] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Step a job aside because its engine is temporarily unavailable.

    This is the "the computer is off" path: the work goes back to ``pending`` with
    a delay, **without** counting an attempt and without recording an error.  A
    deferral is not a failure — nothing was attempted — and the job is kept for as
    long as it takes for the engine to come back.  ``deferrals`` counts how many
    times it stepped aside, so a permanently absent node is visible without being
    turned into a permanent failure.
    """
    stamp = _now(now)
    note = _text(reason, 500)
    with _LOCK:
        state = read_queue(root)
        job = _require_job(state, job_id)
        if job["state"] not in RUNNING_STATES:
            raise GenerationQueueError(
                f"Only a job a runner is holding can be deferred; this one is {job['state']}"
            )
        delay = float(
            delay_seconds
            if delay_seconds is not None
            else (state["settings"].get("defer_seconds") or DEFAULT_SETTINGS["defer_seconds"])
        )
        job["deferrals"] = int(job.get("deferrals") or 0) + 1
        job["deferred_reason"] = note or "the engine is not available right now"
        # ``pending`` is a legal move from every running state, and it clears the
        # worker/hold fields so the job is genuinely waiting again.
        _set_state(state, job, "pending", now=stamp)
        job["available_at"] = stamp + max(0.0, delay)
        _job_event(job, "deferred", {"reason": job["deferred_reason"], "seconds": round(max(0.0, delay), 1)})
        _record_event(state, "deferred", job_id, {"reason": job["deferred_reason"], "deferrals": job["deferrals"]})
        _save_queue(root, state)
        return dict(job)


def complete(root: str, job_id: str, outputs: Any = (), *, now: Optional[float] = None) -> dict[str, Any]:
    """Finish a job with the artefacts it really produced."""
    with _LOCK:
        state = read_queue(root)
        job = _require_job(state, job_id)
        files = _string_list(outputs)
        if not files:
            raise GenerationQueueError(
                "A completed job must list the file(s) it produced; "
                "a job without outputs is a failure, not a completion"
            )
        for path in files:
            if not os.path.isabs(path) or not os.path.exists(path):
                raise GenerationQueueError(f"Output {path!r} does not exist on disk")
        job["outputs"] = files
        job["progress"]["percent"] = 100 if job["progress"].get("percent") is not None else None
        job["progress"]["source"] = "reported" if job["progress"].get("percent") is not None else "none"
        _set_state(state, job, "completed", now=now)
        _record_event(state, "completed", job_id, {"outputs": len(files)})
        _save_queue(root, state)
        _trim_history(state, root)
        return dict(job)


def fail(
    root: str,
    job_id: str,
    error: str,
    *,
    retryable: bool = True,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Record a real failure.  Retries are bounded and counted."""
    message = _text(error, 2000) or "Unknown failure"
    stamp = _now(now)
    with _LOCK:
        state = read_queue(root)
        job = _require_job(state, job_id)
        errors = list(job.get("errors") or [])
        errors.append(message)
        job["errors"] = errors[-20:]
        job["progress"]["source"] = job["progress"].get("source") or "none"
        queue_settings = state["settings"]
        attempts = int(job.get("attempts") or 0) + 1
        job["attempts"] = attempts
        can_retry = (
            retryable
            and bool(queue_settings.get("auto_retry", True))
            and attempts <= int(job.get("max_retries") or 0)
        )
        _job_event(job, "failed", {"error": message, "retry": can_retry, "attempt": attempts})
        if can_retry:
            _set_state(state, job, "failed", now=stamp)
            # ``failed -> pending`` is the one legal transition out of failed, and
            # it exists precisely so an automatic retry is explicit in the log.
            _set_state(state, job, "pending", now=stamp)
            _job_event(job, "retry_scheduled", {"attempt": attempts, "max_retries": job.get("max_retries")})
            job["available_at"] = stamp + float(queue_settings.get("retry_delay_seconds") or 0.0)
            _record_event(state, "retry_scheduled", job_id, {"attempt": attempts, "error": message})
        else:
            _set_state(state, job, "failed", now=stamp)
            _record_event(state, "failed", job_id, {"attempt": attempts, "error": message})
        _save_queue(root, state)
        if job["state"] == "failed":
            _trim_history(state, root)
        return dict(job)


def retry(root: str, job_id: str, *, now: Optional[float] = None) -> dict[str, Any]:
    """Manually requeue a failed or cancelled job, if it has retries left."""
    stamp = _now(now)
    with _LOCK:
        state = read_queue(root)
        job = _require_job(state, job_id)
        if job["state"] not in {"failed", "cancelled"}:
            raise GenerationQueueError(
                f"Only a failed or cancelled job can be retried; this one is {job['state']}"
            )
        attempts = int(job.get("attempts") or 0)
        allowed = int(job.get("max_retries") or 0)
        if attempts > allowed:
            raise GenerationQueueError(
                f"Job {job_id} already used its {allowed} retry(ies); raise max_retries to allow more"
            )
        previous = job["state"]
        _set_state(state, job, "pending", now=stamp)
        job["cancel_requested"] = False
        job["available_at"] = stamp
        _record_event(state, "retried", job_id, {"from": previous})
        _save_queue(root, state)
        return dict(job)


def cancel(root: str, job_id: str, *, reason: str = "", now: Optional[float] = None) -> dict[str, Any]:
    """Cancel when it is technically possible, and say so when it is not.

    A job that has not started is cancelled immediately.  A job a worker is
    holding is only marked ``cancelled`` when its engine declared it can be
    stopped; otherwise the request is recorded as ``cancel_requested`` and the
    caller is told the engine must acknowledge it — we never claim a running job
    stopped if it did not.
    """
    stamp = _now(now)
    note = _text(reason, 500)
    with _LOCK:
        state = read_queue(root)
        job = _require_job(state, job_id)
        if job["state"] in TERMINAL_STATES:
            return {
                "job": dict(job),
                "cancelled": job["state"] == "cancelled",
                "action": "none",
                "reason": f"Job is already {job['state']}",
            }
        if job["state"] in {"draft", "pending"}:
            _set_state(state, job, "cancelled", now=stamp)
            job["cancel_requested"] = True
            _record_event(state, "cancelled", job_id, {"reason": note or "cancelled before start"})
            _save_queue(root, state)
            return {"job": dict(job), "cancelled": True, "action": "cancelled_before_start", "reason": note}

        if not job.get("cancel_supported", True):
            job["cancel_requested"] = True
            job["updated_at"] = stamp
            _job_event(job, "cancel_requested", note or "engine cannot stop this job")
            _record_event(state, "cancel_requested", job_id, {"reason": note})
            _save_queue(root, state)
            return {
                "job": dict(job),
                "cancelled": False,
                "action": "requested",
                "reason": (
                    "This engine has not declared that it can stop a running job. The request was "
                    "recorded and the runner must acknowledge it; the job is still running."
                ),
            }

        _set_state(state, job, "cancelled", now=stamp)
        job["cancel_requested"] = True
        _record_event(state, "cancelled", job_id, {"reason": note or "cancelled while running"})
        _save_queue(root, state)
        _trim_history(state, root)
        return {
            "job": dict(job),
            "cancelled": True,
            "action": "cancelled_while_running",
            "reason": note,
        }


def acknowledge_cancel(root: str, job_id: str, *, now: Optional[float] = None) -> dict[str, Any]:
    """The runner confirms it actually stopped a job that was marked for cancel."""
    with _LOCK:
        state = read_queue(root)
        job = _require_job(state, job_id)
        if job["state"] in TERMINAL_STATES:
            return dict(job)
        _set_state(state, job, "cancelled", now=now)
        _record_event(state, "cancel_acknowledged", job_id, "")
        _save_queue(root, state)
        return dict(job)


# ---------------------------------------------------------------------------
# Recovery & housekeeping
# ---------------------------------------------------------------------------


def recover(root: str, *, now: Optional[float] = None) -> dict[str, Any]:
    """Recover after a crash/stall: the queue survives a backend restart.

    Jobs that were being worked on by a worker that is no longer speaking are
    ``interrupted``: they go back to ``pending`` with their attempt counted, or
    to ``failed`` when their retries are spent.  Nothing is deleted, and no job is
    reported as completed because it merely *looks* finished.
    """
    stamp = _now(now)
    with _LOCK:
        state = read_queue(root)
        queue_settings = state["settings"]
        stale_after = float(queue_settings.get("stale_after_seconds") or DEFAULT_SETTINGS["stale_after_seconds"])
        requeued: list[str] = []
        failed: list[str] = []
        drafts: list[str] = []
        for job in state["jobs"].values():
            if job["state"] not in RUNNING_STATES:
                continue
            last = float(job.get("updated_at") or job.get("created_at") or 0)
            if stamp - last < stale_after:
                continue
            attempts = int(job.get("attempts") or 0) + 1
            job["attempts"] = attempts
            job["worker"] = ""
            job["errors"] = (list(job.get("errors") or []) + [
                f"Interrupted: no progress for {int(stamp - last)}s (recovered on restart)"
            ])[-20:]
            if attempts <= int(job.get("max_retries") or 0):
                job["state"] = "failed"
                job["finished_at"] = None
                _set_state(state, job, "pending", now=stamp)
                requeued.append(job["job_id"])
            else:
                job["state"] = "failed"
                job["finished_at"] = stamp
                job["updated_at"] = stamp
                job["duration_seconds"] = round(max(0.0, stamp - float(job.get("started_at") or stamp)), 3)
                failed.append(job["job_id"])
        for job in state["jobs"].values():
            if job["state"] == "draft" and stamp - float(job.get("created_at") or stamp) > stale_after:
                drafts.append(job["job_id"])
        _record_event(state, "recovered", "*", {"requeued": len(requeued), "failed": len(failed)})
        _save_queue(root, state)
        return {
            "requeued": requeued,
            "failed": failed,
            "abandoned_drafts": drafts,
            "detail": (
                "Interrupted work was requeued where retries remained and failed where they did not. "
                "Nothing was deleted."
            ),
        }


def prune(root: str, *, now: Optional[float] = None) -> dict[str, Any]:
    """Trim finished history to the configured bound.  Active jobs are never touched."""
    with _LOCK:
        state = read_queue(root)
        removed = _trim_history(state, root)
        return {"removed": removed, "kept": len(state["jobs"])}


def _trim_history(state: dict[str, Any], root: str) -> list[str]:
    keep = int((state.get("settings") or {}).get("keep_terminal") or 0)
    terminal = sorted(
        (job for job in state["jobs"].values() if job["state"] in TERMINAL_STATES),
        key=lambda job: float(job.get("finished_at") or job.get("updated_at") or 0),
        reverse=True,
    )
    removed: list[str] = []
    for job in terminal[keep:]:
        state["jobs"].pop(job["job_id"], None)
        removed.append(job["job_id"])
    if removed:
        _record_event(state, "pruned", "*", {"removed": len(removed)})
        _save_queue(root, state)
    return removed


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def job(root: str, job_id: str) -> dict[str, Any]:
    return dict(_require_job(read_queue(root), job_id))


def list_jobs(
    root: str,
    *,
    state: Optional[str] = None,
    project_id: Optional[str] = None,
    shot_id: Optional[str] = None,
    include_terminal: bool = True,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if state is not None and state not in STATES:
        raise GenerationQueueError(f"Unknown state {state!r}. Known: " + ", ".join(STATES))
    queue = read_queue(root)
    jobs = []
    for record in queue["jobs"].values():
        if state is not None and record["state"] != state:
            continue
        if not include_terminal and record["state"] in TERMINAL_STATES:
            continue
        if project_id is not None and record.get("project_id") != project_id:
            continue
        if shot_id is not None and record.get("shot_id") != shot_id:
            continue
        jobs.append(dict(record))
    jobs.sort(key=lambda record: (float(record.get("created_at") or 0), record["job_id"]))
    return jobs[-max(1, int(limit)):]


def events(root: str, *, job_id: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
    """The queue-wide event log, or one job's own trail when ``job_id`` is given."""
    queue = read_queue(root)
    if job_id is not None:
        record = queue["jobs"].get(str(job_id))
        if record is None:
            raise FileNotFoundError(f"Job {job_id!r} is not in the queue")
        trail = [dict(event, job_id=record["job_id"]) for event in record.get("events") or []]
        return trail[-max(1, int(limit)):]
    return [dict(event) for event in (queue.get("events") or [])][-max(1, int(limit)):]


def summary(root: str, *, now: Optional[float] = None) -> dict[str, Any]:
    """Compact status for the mobile header and the queue panel."""
    queue = read_queue(root)
    counts = {name: 0 for name in STATES}
    oldest_pending: Optional[float] = None
    for record in queue["jobs"].values():
        counts[record["state"]] = counts.get(record["state"], 0) + 1
        if record["state"] == "pending":
            created = float(record.get("created_at") or 0)
            oldest_pending = created if oldest_pending is None else min(oldest_pending, created)
    queue_settings = queue["settings"]
    stamp = _now(now)
    deferred = [
        record for record in queue["jobs"].values()
        if record["state"] == "pending" and float(record.get("available_at") or 0) > stamp
    ]
    return {
        "counts": counts,
        "settings": dict(queue_settings),
        "deferred": len(deferred),
        "deferred_detail": [
            {
                "job_id": record["job_id"],
                "reason": record.get("deferred_reason") or "",
                "deferrals": int(record.get("deferrals") or 0),
                "seconds_until_retry": round(max(0.0, float(record.get("available_at") or 0) - stamp), 1),
            }
            for record in sorted(deferred, key=lambda entry: float(entry.get("available_at") or 0))[:20]
        ],
        "running": sum(counts[name] for name in RUNNING_STATES),
        "max_concurrent": int(queue_settings["max_concurrent"]),
        "declared_memory_running_mb": _busy_memory(queue),
        "memory_ceiling_mb": int(queue_settings.get("memory_ceiling_mb") or 0),
        "observed_system_memory_mb": system_memory_mb(),
        "waiting_seconds": None if oldest_pending is None else round(max(0.0, _now(now) - oldest_pending), 1),
        "progress_note": (
            "A percentage is shown only when the engine reported one. Jobs whose engine does not "
            "report progress show their state instead."
        ),
    }

"""Queue worker: the part that actually *runs* the queue (spec §10).

``services.generation_queue`` is the record — it knows what should run, in what
order, and what really happened.  This module is the executor: it claims the
next runnable job under the configured concurrency and memory ceiling, hands it
to a **handler registered for that job's engine**, and writes the outcome back.

Three things matter here, and they are the reason this is not just a thread
around ``for job in jobs``:

1. **No handler, no pretending.**  A job whose engine has no registered handler
   fails with a message that says exactly that.  The worker never marks a job
   completed without a handler having produced real files — ``complete()``
   refuses outputs that do not exist on disk, so that cannot be faked by
   accident either.
2. **Progress is forwarded, not generated.**  A handler that knows real progress
   calls ``context.report(...)``; a handler that does not reports nothing and
   the job keeps ``percent = None``.  The worker itself never increments a bar.
   Announcing the ``running`` state is a state change, not progress.
3. **Cancellation is acknowledged, never assumed.**  When a job was marked for
   cancellation, the worker tells the handler (``context.cancelled``) and only
   records the cancellation once the handler has really stopped — either by
   raising :class:`JobCancelled` or by returning normally after seeing the flag.

Handlers are registered per engine string (the same string jobs carry), so
wiring a real engine is an explicit act:

    from services import queue_worker
    queue_worker.register_handler("local:maestro", my_local_generate)

The default registry is empty on purpose: in this environment no engine can be
executed, and an empty registry reports "no runner" instead of inventing output.
"""

from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Callable, Iterable, Mapping, Optional

from . import generation_queue as queue

__all__ = [
    "HandlerUnavailable",
    "JobCancelled",
    "JobContext",
    "WorkerState",
    "clear_handlers",
    "drain",
    "handler_names",
    "register_handler",
    "run_once",
    "running_worker",
    "start_worker",
    "stop_worker",
    "unregister_handler",
    "worker_status",
]

# A handler takes the job record and a JobContext, and returns the artefacts it
# produced (absolute paths).  Returning nothing is a failure, not a success.
Handler = Callable[[Mapping[str, Any], "JobContext"], Any]

_HANDLERS: dict[str, Handler] = {}
_HANDLER_LOCK = threading.RLock()


class HandlerUnavailable(RuntimeError):
    """No handler is registered for the engine that a job names."""


class JobCancelled(RuntimeError):
    """Raised by a handler that observed the cancellation flag and stopped."""


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------


def register_handler(engine: str, handler: Handler, *, replace: bool = False) -> str:
    """Register the handler for ``engine``.  Re-registering needs ``replace``.

    Refusing a silent overwrite is deliberate: two features racing to own the
    same engine is a routing bug that must surface at registration time, not as
    mysterious output later.
    """
    name = str(engine or "").strip()
    if not name:
        raise ValueError("A handler must be registered for a non-empty engine name")
    if not callable(handler):
        raise TypeError("The handler must be callable")
    with _HANDLER_LOCK:
        if name in _HANDLERS and not replace:
            raise ValueError(
                f"A handler is already registered for engine {name!r}; "
                "pass replace=True to take it over"
            )
        _HANDLERS[name] = handler
    return name


def unregister_handler(engine: str) -> bool:
    with _HANDLER_LOCK:
        return _HANDLERS.pop(str(engine or "").strip(), None) is not None


def clear_handlers() -> None:
    """Forget every handler.  Used by tests and by an explicit engine reset."""
    with _HANDLER_LOCK:
        _HANDLERS.clear()


def handler_names() -> list[str]:
    with _HANDLER_LOCK:
        return sorted(_HANDLERS)


def _handler_for(engine: str) -> Optional[Handler]:
    with _HANDLER_LOCK:
        return _HANDLERS.get(str(engine or ""))


# ---------------------------------------------------------------------------
# What a handler can do
# ---------------------------------------------------------------------------


class JobContext:
    """The queue operations a running job is allowed to use.

    Deliberately narrow: a handler can move its own job forward and report what
    it measured.  It cannot claim other jobs, touch the settings, or finish a job
    without naming real files (``complete`` in the queue enforces that).
    """

    def __init__(self, root: str, job: Mapping[str, Any], *, worker: str = "") -> None:
        self.root = root
        self.job_id = str(job["job_id"])
        self.engine = str(job.get("engine") or "")
        self.task = str(job.get("task") or "other")
        self.worker = worker
        self.params = dict(job.get("params") or {})
        self.inputs = list(job.get("inputs") or [])
        self.project_id = str(job.get("project_id") or "")
        self.scene_id = str(job.get("scene_id") or "")
        self.shot_id = str(job.get("shot_id") or "")
        self.node_id = str(job.get("node_id") or "")
        self.model = str(job.get("model") or "")
        self.external_job_id = str(job.get("external_job_id") or "")

    # -- state ------------------------------------------------------------

    def set_state(self, state_name: str) -> dict[str, Any]:
        """``running`` / ``finalizing`` — where the job is, not how far along."""
        return queue.set_state(self.root, self.job_id, state_name, worker=self.worker)

    def link(self, external_job_id: str) -> dict[str, Any]:
        """Attach the engine's own job id (ComfyUI prompt id, Studio job id…)."""
        return queue.link_external_job(self.root, self.job_id, external_job_id)

    def cancelled(self) -> bool:
        """True once someone asked for this job to stop.  Handlers must honour it."""
        try:
            return bool(queue.job(self.root, self.job_id).get("cancel_requested"))
        except FileNotFoundError:
            return False

    # -- progress ---------------------------------------------------------

    def report(
        self,
        *,
        percent: Optional[float] = None,
        step: Optional[int] = None,
        total_steps: Optional[int] = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Forward a number the engine really produced, or nothing at all."""
        return queue.report_progress(
            self.root,
            self.job_id,
            percent=percent,
            step=step,
            total_steps=total_steps,
            note=note,
        )

    def fail(self, error: str, *, retryable: bool = True) -> dict[str, Any]:
        """Record a real failure now, from inside the handler."""
        return queue.fail(self.root, self.job_id, error, retryable=retryable)

    def defer(self, reason: str, *, delay_seconds: Optional[float] = None) -> dict[str, Any]:
        """Step aside because the engine is not available right now.

        Use this when the work *could* run but the machine that runs it does not
        answer — a computer that is off, a server that is restarting.  It is not a
        failure and does not consume an attempt: the job goes back to the queue and
        is kept until the engine returns.
        """
        return queue.defer(self.root, self.job_id, reason=reason, delay_seconds=delay_seconds)

    def complete(self, outputs: Iterable[str]) -> dict[str, Any]:
        """Finish with the files the handler actually produced."""
        return queue.complete(self.root, self.job_id, outputs)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"JobContext({self.job_id!r}, engine={self.engine!r}, task={self.task!r})"


# ---------------------------------------------------------------------------
# Running one job
# ---------------------------------------------------------------------------


def _normalize_outputs(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        value = value.get("outputs")
        if value is None:
            return []
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    return [str(item) for item in value if str(item or "")]


def run_once(
    root: str,
    *,
    worker: str = "local",
    handlers: Optional[Mapping[str, Handler]] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Claim and execute at most one job.  One call = one job, always.

    Returns a report of what happened, including the honest ``outcome``:
    ``idle`` (nothing runnable), ``no_handler``, ``completed``, ``failed``,
    ``cancelled``, ``cancelled_before_start`` or ``error``.
    """
    claim = queue.claim_next(root, worker=worker, now=now)
    claimed = claim.get("claimed")
    if not claimed:
        return {
            "claimed": False,
            "outcome": "idle",
            "job_id": "",
            "reason": claim.get("reason") or "Nothing is runnable right now",
            "waiting": claim.get("waiting"),
        }

    job_id = str(claimed["job_id"])
    engine = str(claimed.get("engine") or "")
    prior_deferrals = int(claimed.get("deferrals") or 0)

    # A job cancelled between submission and the claim never reaches a handler.
    try:
        current = queue.job(root, job_id)
    except FileNotFoundError:
        return {"claimed": True, "outcome": "error", "job_id": job_id,
                "reason": "The job disappeared while it was being claimed"}
    if current.get("cancel_requested"):
        queue.acknowledge_cancel(root, job_id)
        return {
            "claimed": True,
            "outcome": "cancelled_before_start",
            "job_id": job_id,
            "reason": "Cancelled before the handler started",
        }

    # An explicit mapping is used exclusively, so a test (or a caller running a
    # private engine) can never be silently served by the global registry.
    handler = handlers.get(engine) if handlers is not None else _handler_for(engine)
    if handler is None:
        message = (
            f"No runner is registered for engine {engine!r}. The job stayed out of the "
            "engine because nothing on this machine can execute it."
        )
        queue.fail(root, job_id, message, retryable=False)
        return {"claimed": True, "outcome": "no_handler", "job_id": job_id, "reason": message}

    context = JobContext(root, current, worker=worker)
    try:
        produced = handler(current, context)
    except JobCancelled:
        queue.acknowledge_cancel(root, job_id)
        return {
            "claimed": True,
            "outcome": "cancelled",
            "job_id": job_id,
            "reason": "The handler stopped: cancellation acknowledged",
        }
    except Exception as error:  # a handler's failure is the job's failure
        message = f"{type(error).__name__}: {error}".strip() or "The handler failed"
        record = queue.fail(root, job_id, message, retryable=True)
        traceback.print_exc()
        return {
            "claimed": True,
            "outcome": "failed",
            "job_id": job_id,
            "reason": message,
            "state": record.get("state"),
            "retry_scheduled": record.get("state") == "pending",
        }

    outputs = _normalize_outputs(produced)

    # The handler may have ended the job itself (a real failure from inside, a
    # deferral because the engine is away).  Honour that instead of writing a
    # second outcome on top of it.
    try:
        settled = queue.job(root, job_id)
    except FileNotFoundError:
        settled = None
    if settled is not None:
        if settled["state"] in queue.TERMINAL_STATES and not outputs:
            return {
                "claimed": True,
                "outcome": str(settled["state"]),
                "job_id": job_id,
                "reason": (settled.get("errors") or [""])[-1],
                "decided_by_handler": True,
            }
        if int(settled.get("deferrals") or 0) > prior_deferrals and not outputs:
            return {
                "claimed": True,
                "outcome": "deferred",
                "job_id": job_id,
                "reason": str(settled.get("deferred_reason") or "the engine is not available"),
                "deferrals": int(settled.get("deferrals") or 0),
            }

    # The handler stopped.  If it stopped *because* it was asked to, that is the
    # cancellation being acknowledged — not a completion with partial files.
    if context.cancelled():
        queue.acknowledge_cancel(root, job_id)
        return {
            "claimed": True,
            "outcome": "cancelled",
            "job_id": job_id,
            "reason": "Cancellation acknowledged; partial output was not recorded as a result",
        }

    if not outputs:
        message = (
            "The handler returned no output file. A job without artefacts is not a "
            "success, so it was recorded as a failure."
        )
        queue.fail(root, job_id, message, retryable=True)
        return {"claimed": True, "outcome": "failed", "job_id": job_id, "reason": message}

    try:
        record = queue.complete(root, job_id, outputs)
    except queue.GenerationQueueError as error:
        # Typically a path the handler reported that does not exist.  Record the
        # real reason instead of a completion we cannot back with a file.
        queue.fail(root, job_id, str(error), retryable=False)
        return {"claimed": True, "outcome": "failed", "job_id": job_id, "reason": str(error)}

    return {
        "claimed": True,
        "outcome": "completed",
        "job_id": job_id,
        "outputs": list(record.get("outputs") or []),
        "reason": "",
    }


def drain(
    root: str,
    *,
    limit: int = 100,
    worker: str = "local",
    handlers: Optional[Mapping[str, Handler]] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Run jobs until nothing is claimable or ``limit`` jobs have been handled.

    ``limit`` exists so a caller (a request, a tick) can never block forever on a
    queue that keeps being fed.
    """
    reports: list[dict[str, Any]] = []
    for _ in range(max(1, int(limit))):
        report = run_once(root, worker=worker, handlers=handlers, now=now)
        reports.append(report)
        if not report.get("claimed"):
            break
    counts: dict[str, int] = {}
    for report in reports:
        # ``idle`` describes the queue, not a job, so it is not counted here:
        # outcomes must add up to the number of jobs that were handled.
        if not report.get("claimed"):
            continue
        outcome = str(report.get("outcome"))
        counts[outcome] = counts.get(outcome, 0) + 1
    return {"ran": sum(1 for r in reports if r.get("claimed")), "outcomes": counts, "reports": reports}


# ---------------------------------------------------------------------------
# The background worker
# ---------------------------------------------------------------------------


class WorkerState:
    """Thread-safe status of the optional background worker."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.running = False
        self.ticks = 0
        self.last_tick = 0.0
        self.last_outcome = ""
        self.last_error = ""
        self.interval = 0.0
        self.root = ""
        self.worker = ""
        self.thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "ticks": self.ticks,
                "last_tick": self.last_tick,
                "last_outcome": self.last_outcome,
                "last_error": self.last_error,
                "interval_seconds": self.interval,
                "worker": self.worker,
                "handlers": handler_names(),
            }


_STATUS = WorkerState()
_STATUS_LOCK = threading.RLock()


def worker_status() -> dict[str, Any]:
    return _STATUS.snapshot()


def running_worker() -> bool:
    return bool(_STATUS.snapshot()["running"])


def _loop(state: WorkerState, root: str, worker: str, interval: float) -> None:
    while True:
        # One job per tick: the queue's own concurrency setting decides whether
        # a second claim is allowed, so the loop never has to guess.
        try:
            report = drain(root, limit=1, worker=worker)
        except Exception as error:  # never let the thread die silently
            with state._lock:
                state.last_error = f"{type(error).__name__}: {error}"
                state.last_outcome = "error"
        else:
            with state._lock:
                state.ticks += 1
                state.last_tick = time.time()
                state.last_error = ""
                reports = report.get("reports") or []
                state.last_outcome = str(reports[-1].get("outcome", "")) if reports else ""
        if state._stop.wait(interval):  # noqa: SLF001 - the loop owns the event
            return


def start_worker(
    root: str,
    *,
    worker: str = "local",
    interval: float = 2.0,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Start the background worker, recovering interrupted jobs first.

    Starting is idempotent: a second call reports the running worker instead of
    spawning a second thread that would double-claim work.
    """
    with _STATUS_LOCK:
        if _STATUS.running:
            return {**_STATUS.snapshot(), "started": False, "reason": "Worker already running"}
        recovered = queue.recover(root, now=now)
        _STATUS._stop = threading.Event()  # noqa: SLF001
        _STATUS.root = root
        _STATUS.worker = worker
        _STATUS.interval = max(0.2, float(interval))
        _STATUS.running = True
        thread = threading.Thread(
            target=_loop, args=(_STATUS, root, worker, max(0.2, float(interval))),
            name="maestro-queue-worker", daemon=True,
        )
        _STATUS.thread = thread
        thread.start()
        return {**_STATUS.snapshot(), "started": True, "recovered": recovered}


def stop_worker(timeout: float = 5.0) -> dict[str, Any]:
    """Ask the worker to stop and wait briefly for the thread to finish."""
    with _STATUS_LOCK:
        if not _STATUS.running:
            return {**_STATUS.snapshot(), "stopped": False, "reason": "Worker is not running"}
        _STATUS._stop.set()  # noqa: SLF001
        thread = _STATUS.thread
        _STATUS.running = False
        _STATUS.thread = None
    if thread is not None and thread.is_alive():
        thread.join(timeout=max(0.0, float(timeout)))
    return {**worker_status(), "stopped": True, "alive": bool(thread and thread.is_alive())}

"""HTTP boundary for the persistent production queue (spec §10).

A router factory rather than module-level routes, so the queue API can be
mounted on the app and exercised over HTTP in tests without importing the whole
application (the same shape as ``services.active_downloads_api``).

Two honesty rules are enforced here, and they are the reason this layer exists
instead of letting clients write queue state directly:

* **A percentage only exists if an engine reported one.**  ``/progress`` accepts
  a percent because a handler measured it; a job whose engine reports nothing
  keeps ``percent = null`` and the UI shows its state.
* **A completed job has real files.**  ``/complete`` names output paths and the
  queue refuses any path that is not absolute and present on disk, so finishing a
  job without artefacts is not expressible through this API either.

The same boundary hands the artefacts back.  ``/jobs/{id}/outputs`` lists what a
finished job produced *by index* and ``/jobs/{id}/outputs/{index}`` streams one of
them, resumable with an HTTP ``Range`` request.  A caller never names a file: the
index is resolved against this queue's own record, so delegating a job to another
Maestro (`services.remote_engine`) cannot be turned into arbitrary file access.
"""
from __future__ import annotations

import os
import traceback
from typing import Any, Callable, Iterator, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from . import generation_queue as queue
from . import queue_worker as worker

__all__ = ["create_router", "recover_on_start", "parse_range"]

# Streaming a clip instead of buffering it: server-side chunk, and the media type
# a browser needs to play or a download to be labelled correctly.
_STREAM_CHUNK = 256 * 1024

_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
}


def media_type_for(path: str) -> str:
    """The content type for a produced file, by extension."""
    return _MEDIA_TYPES.get(os.path.splitext(str(path))[1].lower(), "application/octet-stream")


def parse_range(header: str, size: int) -> Optional[tuple[int, int]]:
    """Parse one ``bytes=`` range into an inclusive ``(start, end)``.

    Returns ``None`` for a header that is absent, malformed or unsatisfiable — the
    caller decides between serving the whole file and answering ``416``.
    """
    text = str(header or "").strip()
    if not text.lower().startswith("bytes=") or size <= 0:
        return None
    spec = text[len("bytes="):].split(",")[0].strip()
    first, _, last = spec.partition("-")
    try:
        if not first:
            # ``bytes=-N``: the last N bytes.
            length = int(last)
            if length <= 0:
                return None
            return (max(0, size - length), size - 1)
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return (start, min(end, size - 1))


def _stream(path: str, start: int, end: int, chunk: int = _STREAM_CHUNK) -> Iterator[bytes]:
    """Read ``[start, end]`` from disk in bounded pieces (never all in RAM)."""
    remaining = end - start + 1
    with open(path, "rb") as handle:
        handle.seek(start)
        while remaining > 0:
            data = handle.read(min(chunk, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def create_router(*, root_provider: Callable[[], str]) -> APIRouter:
    """Build the production-queue router for the queue root ``root_provider`` returns."""
    router = APIRouter()

    def root() -> str:
        path = root_provider()
        if not path:
            raise HTTPException(status_code=500, detail="The queue has no storage directory")
        return path

    def http_error(error: Exception) -> HTTPException:
        if isinstance(error, FileNotFoundError):
            return HTTPException(status_code=404, detail=str(error))
        if isinstance(error, queue.GenerationQueueError):
            return HTTPException(status_code=400, detail=str(error))
        if isinstance(error, (TypeError, ValueError)):
            return HTTPException(status_code=400, detail=str(error))
        traceback.print_exc()
        return HTTPException(status_code=500, detail=str(error)[:500])

    async def json_body(request: Request) -> dict:
        return await request.json() if await request.body() else {}

    @router.get("/api/v1/production/queue")
    def read_queue(state: str = "", project_id: str = "", shot_id: str = "",
                   include_terminal: bool = True, limit: int = 200):
        """The queue: jobs, counts, settings, worker status and a progress note."""
        base = root()
        try:
            return {
                "summary": queue.summary(base),
                "jobs": queue.list_jobs(
                    base,
                    state=state or None,
                    project_id=project_id or None,
                    shot_id=shot_id or None,
                    include_terminal=include_terminal,
                    limit=limit,
                ),
                "worker": worker.worker_status(),
                "handlers": worker.handler_names(),
                "progress_note": (
                    "A percentage is shown only when the engine reported one; otherwise the "
                    "job is described by its state. An HTTP request finishing is not "
                    "generation progress."
                ),
            }
        except Exception as error:
            raise http_error(error) from error

    @router.get("/api/v1/production/queue/settings")
    def read_settings():
        base = root()
        try:
            return {
                "settings": queue.settings(base),
                "defaults": dict(queue.DEFAULT_SETTINGS),
                "observed_system_memory_mb": queue.system_memory_mb(),
            }
        except Exception as error:
            raise http_error(error) from error

    @router.put("/api/v1/production/queue/settings")
    async def configure(request: Request):
        """Concurrency, memory ceiling, retries, staleness and history bound."""
        body = await json_body(request)
        try:
            return queue.configure(root(), body)
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/production/queue/jobs")
    async def submit(request: Request):
        """Submit a job.  ``start: "held"`` (or ``draft: true``) creates a draft.

        An identical ``dedupe_key`` whose earlier job is still active returns that
        job instead of creating a second one, so a double tap cannot render the
        same shot twice.
        """
        body = await json_body(request)
        try:
            return queue.submit(root(), body)
        except Exception as error:
            raise http_error(error) from error

    @router.get("/api/v1/production/queue/jobs/{job_id}")
    def read_job(job_id: str, events: bool = False):
        base = root()
        try:
            record = queue.job(base, job_id)
            if events:
                return {**record, "event_log": queue.events(base, job_id=job_id)}
            return record
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/production/queue/jobs/{job_id}/state")
    async def set_state(job_id: str, request: Request):
        """Move a job along (``running`` / ``finalizing``).  This is not progress."""
        body = await json_body(request)
        try:
            return queue.set_state(
                root(), job_id, str(body.get("state") or ""),
                worker=str(body.get("worker") or ""))
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/production/queue/jobs/{job_id}/progress")
    async def report_progress(job_id: str, request: Request):
        """Record progress the engine really produced.  ``percent`` is optional."""
        body = await json_body(request)
        try:
            return queue.report_progress(
                root(), job_id,
                percent=body.get("percent"),
                step=body.get("step"),
                total_steps=body.get("total_steps"),
                note=str(body.get("note") or ""))
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/production/queue/jobs/{job_id}/complete")
    async def complete(job_id: str, request: Request):
        """Finish a job.  Every named output must exist on disk."""
        body = await json_body(request)
        try:
            return queue.complete(root(), job_id, body.get("outputs") or [])
        except Exception as error:
            raise http_error(error) from error

    def _output_files(job_id: str) -> tuple[dict, list[str]]:
        record = queue.job(root(), job_id)
        return record, [str(path) for path in (record.get("outputs") or [])]

    @router.get("/api/v1/production/queue/jobs/{job_id}/outputs")
    def read_job_outputs(job_id: str):
        """What a finished job produced — named, never as a local path.

        Only the file name and its measured size cross the boundary: a peer that
        delegated the job asks for output *N*, it does not get to name a file.
        """
        try:
            record, files = _output_files(job_id)
        except Exception as error:
            raise http_error(error) from error
        entries: list[dict[str, Any]] = []
        for index, path in enumerate(files):
            present = os.path.isfile(path)
            entries.append({
                "index": index,
                "name": os.path.basename(path),
                "media_type": media_type_for(path),
                "bytes": os.path.getsize(path) if present else 0,
                "present": present,
            })
        return {
            "job_id": record["job_id"],
            "state": record["state"],
            "outputs": entries,
            "note": (
                "Ask for an output by index; the path itself is this machine's business."
            ),
        }

    @router.get("/api/v1/production/queue/jobs/{job_id}/outputs/{index}")
    def read_job_output(job_id: str, index: int, request: Request):
        """Hand back one produced file, resumable with a ``Range`` request."""
        try:
            record, files = _output_files(job_id)
        except Exception as error:
            raise http_error(error) from error
        if index < 0 or index >= len(files):
            raise HTTPException(
                status_code=404,
                detail=f"Job {record['job_id']} has no output at index {index}",
            )
        path = files[index]
        if not os.path.isfile(path):
            raise HTTPException(
                status_code=409,
                detail="The output this job recorded is no longer on disk",
            )
        name = os.path.basename(path)
        media_type = media_type_for(path)
        size = os.path.getsize(path)
        header = request.headers.get("range") or ""
        wanted = parse_range(header, size)
        if wanted is None:
            if header.strip() and size > 0:
                # A range this file cannot satisfy is 416, not a silent full body.
                return Response(
                    status_code=416,
                    headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"},
                )
            return FileResponse(
                path, media_type=media_type, filename=name,
                headers={"Accept-Ranges": "bytes"},
            )
        start, end = wanted
        # Streaming, not buffering: a multi-gigabyte clip never sits in RAM, and
        # the range makes an interrupted transfer resumable from where it stopped.
        return StreamingResponse(
            content=_stream(path, start, end),
            status_code=206,
            media_type=media_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(end - start + 1),
                "Accept-Ranges": "bytes",
                "Content-Disposition": f'attachment; filename="{name}"',
            },
        )

    @router.post("/api/v1/production/queue/jobs/{job_id}/fail")
    async def fail(job_id: str, request: Request):
        body = await json_body(request)
        try:
            return queue.fail(
                root(), job_id, str(body.get("error") or ""),
                retryable=bool(body.get("retryable", True)))
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/production/queue/jobs/{job_id}/retry")
    def retry(job_id: str):
        try:
            return queue.retry(root(), job_id)
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/production/queue/jobs/{job_id}/cancel")
    async def cancel(job_id: str, request: Request):
        """Cancel when it is technically possible, and say so when it is not."""
        body = await json_body(request)
        try:
            return queue.cancel(root(), job_id, reason=str(body.get("reason") or ""))
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/production/queue/jobs/{job_id}/acknowledge-cancel")
    def acknowledge_cancel(job_id: str):
        """The runner confirms it really stopped a job it was asked to cancel."""
        try:
            return queue.acknowledge_cancel(root(), job_id)
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/production/queue/jobs/{job_id}/link")
    async def link(job_id: str, request: Request):
        """Attach the engine's own job id so its progress callback reports here."""
        body = await json_body(request)
        try:
            return queue.link_external_job(
                root(), job_id, str(body.get("external_job_id") or ""))
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/production/queue/claim")
    async def claim(request: Request):
        """Claim the next runnable job (a node agent or the local worker)."""
        body = await json_body(request)
        try:
            return queue.claim_next(root(), worker=str(body.get("worker") or ""))
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/production/queue/run")
    async def run(request: Request):
        """Run what is claimable now, up to ``limit`` jobs, reporting each outcome."""
        body = await json_body(request)
        try:
            return worker.drain(
                root(),
                limit=int(body.get("limit") or 1),
                worker=str(body.get("worker") or "local"),
            )
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/production/queue/recover")
    def recover():
        try:
            return queue.recover(root())
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/production/queue/prune")
    def prune():
        """Trim finished history to the configured bound.  Active jobs are kept."""
        try:
            return queue.prune(root())
        except Exception as error:
            raise http_error(error) from error

    @router.get("/api/v1/production/queue/events")
    def events(job_id: str = "", limit: int = 200):
        try:
            return {
                "events": queue.events(root(), job_id=job_id or None, limit=limit),
                "handlers": worker.handler_names(),
            }
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/production/queue/worker/start")
    async def start_worker(request: Request):
        """Start the background runner, recovering interrupted work first."""
        body = await json_body(request)
        try:
            return worker.start_worker(
                root(),
                worker=str(body.get("worker") or "local"),
                interval=float(body.get("interval") or 2.0),
            )
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/production/queue/worker/stop")
    def stop_worker():
        try:
            return worker.stop_worker()
        except Exception as error:
            raise http_error(error) from error

    return router


def recover_on_start(root: str, *, print_line: Optional[Callable[[str], None]] = None) -> dict:
    """Recover interrupted work when the backend starts again (spec §10).

    Interrupted jobs go back to ``pending`` while retries remain and to ``failed``
    when they are spent; a job that merely *looks* finished is never completed,
    and nothing is deleted.  The background worker is not started here: running
    an engine is an explicit act, so ``/worker/start`` does that.
    """
    log = print_line or print
    try:
        result = queue.recover(root)
    except Exception as error:  # a broken queue file must not stop the app
        log(f"[Queue] Could not recover the production queue: {error}")
        return {"requeued": [], "failed": [], "abandoned_drafts": [], "error": str(error)}
    if result.get("requeued") or result.get("failed"):
        log(
            "[Queue] Recovered after restart: "
            f"{len(result['requeued'])} job(s) requeued, "
            f"{len(result['failed'])} exhausted their retries."
        )
    return result

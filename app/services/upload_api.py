"""Chunked reference uploads, as an HTTP surface a test can serve for real.

``services.upload_chunks`` owns the transfer logic: sessions, chunk state,
assembly, integrity and cleanup.  This module is only its HTTP shell, and it is a
**router factory** for the same reason ``production_queue_api`` is one — the whole
surface can then be driven over HTTP in a test without booting the application.

That matters for one path in particular: a computer that has no GPU sends the
media a request names to the peer that will render it, through these endpoints
(see ``remote_engine.transfer_media``).  A test that exercised a hand-written
stand-in for this router would be testing the stand-in; the peer in
``tests/test_remote_engine.py`` therefore mounts *this* router.
"""

from __future__ import annotations

import traceback
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

from . import upload_chunks

__all__ = ["create_router", "read_bounded_chunk"]


def http_error(error: Exception) -> HTTPException:
    """One place that decides which HTTP status an upload error becomes."""
    if isinstance(error, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, (upload_chunks.UploadSessionError, TypeError, ValueError)):
        return HTTPException(status_code=400, detail=str(error))
    traceback.print_exc()
    return HTTPException(status_code=500, detail=str(error)[:500])


async def read_bounded_chunk(request: Request, limit: int) -> bytes:
    """Read a request body without ever buffering more than ``limit`` bytes."""
    parts: list[bytes] = []
    total = 0
    async for part in request.stream():
        total += len(part)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"Chunk exceeds the {limit} byte limit for this session",
            )
        parts.append(part)
    return b"".join(parts)


def create_router(
    *,
    sessions_root_provider: Callable[[], str],
    uploads_root_provider: Callable[[], str],
) -> APIRouter:
    """The upload-session API, rooted wherever the caller says."""
    router = APIRouter()

    def sessions_root() -> str:
        return sessions_root_provider()

    def uploads_root() -> str:
        return uploads_root_provider()

    @router.get("/api/v1/uploads/limits")
    def upload_limits():
        return {
            "limits": upload_chunks.kind_limits(),
            "chunk_size": upload_chunks.DEFAULT_CHUNK_SIZE,
            "min_chunk_size": 64 * 1024,
            "max_chunk_size": 256 * 1024 * 1024,
            "reserve_bytes": upload_chunks.MIN_FREE_RESERVE,
            "note": (
                "Limits are per media kind and configurable; see docs/FILM-STUDIO.md "
                "for how to raise them."
            ),
        }

    @router.post("/api/v1/uploads/sessions")
    async def upload_session_create(request: Request):
        """Open a resumable upload session for a large reference file."""
        body = await request.json()
        try:
            return await asyncio_to_thread(
                upload_chunks.init_session,
                sessions_root(),
                filename=str(body.get("filename") or ""),
                total_bytes=int(body.get("total_bytes") or 0),
                content_type=str(body.get("content_type") or ""),
                expected_sha256=str(body.get("sha256") or ""),
                chunk_size=int(body.get("chunk_size") or upload_chunks.DEFAULT_CHUNK_SIZE),
            )
        except Exception as error:
            raise http_error(error) from error

    @router.get("/api/v1/uploads/sessions/{session_id}")
    def upload_session_status(session_id: str):
        """What arrived and what is missing — the resume contract."""
        try:
            return upload_chunks.session_status(sessions_root(), session_id)
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/uploads/sessions/{session_id}/chunks")
    async def upload_session_chunk(session_id: str, request: Request, index: int = 0,
                                   sha256: str = ""):
        """Store one chunk.  The body is capped at the session's chunk size."""
        root = sessions_root()
        try:
            state = upload_chunks.session_status(root, session_id)
        except Exception as error:
            raise http_error(error) from error
        limit = int(state.get("chunk_size") or upload_chunks.DEFAULT_CHUNK_SIZE)
        data = await read_bounded_chunk(request, limit)
        try:
            return await asyncio_to_thread(
                upload_chunks.append_chunk, root, session_id, index, data,
                chunk_sha256=sha256,
            )
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/uploads/sessions/{session_id}/complete")
    async def upload_session_complete(session_id: str, request: Request):
        """Assemble the chunks into the uploads directory, streaming."""
        body = await request.json() if await request.body() else {}
        body = body if isinstance(body, dict) else {}
        try:
            return await asyncio_to_thread(
                upload_chunks.complete_session,
                sessions_root(),
                session_id,
                final_dir=uploads_root(),
                dedup=bool(body.get("dedup", True)),
            )
        except Exception as error:
            raise http_error(error) from error

    @router.delete("/api/v1/uploads/sessions/{session_id}")
    def upload_session_cancel(session_id: str, keep_record: bool = True):
        try:
            return upload_chunks.cancel_session(
                sessions_root(), session_id, keep_record=keep_record)
        except Exception as error:
            raise http_error(error) from error

    @router.post("/api/v1/uploads/sessions/cleanup")
    async def upload_sessions_cleanup(request: Request):
        """Delete finished sessions, and stale ones only when asked."""
        body = await request.json() if await request.body() else {}
        body = body if isinstance(body, dict) else {}
        try:
            return await asyncio_to_thread(
                upload_chunks.cleanup_sessions,
                sessions_root(),
                older_than=float(body.get("older_than") or upload_chunks.STALE_AFTER_SECONDS),
                include_open=bool(body.get("include_open", False)),
            )
        except Exception as error:
            raise http_error(error) from error

    return router


async def asyncio_to_thread(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run the (blocking) transfer work off the event loop.  Kept local so this
    module imports without an event loop and stays trivial to read."""
    import asyncio

    return await asyncio.to_thread(function, *args, **kwargs)

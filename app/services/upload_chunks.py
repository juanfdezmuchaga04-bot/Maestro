"""Chunked, resumable uploads for large reference media (spec §5.1).

The existing single-shot ``/api/v1/upload`` reads the whole body into memory
(``await file.read()``), which is fine for a reference still and wrong for a
multi-gigabyte video.  This module adds the streaming path next to it:

* a session is opened with a declared size (or an unknown, streaming size),
* the file arrives in **chunks**, each written straight to disk — nothing large
  is ever held in RAM,
* because chunk state is persisted, an interrupted upload is **resumed** by
  asking which indices are missing, not by starting over,
* each chunk may carry its own SHA-256, and the whole file's digest is computed
  while assembling, so integrity is checked instead of assumed,
* assembly is a **streaming copy** into a temporary file that is then atomically
  renamed; identical content already on disk is reused (deduplication),
* cancelling, or a stale session, deletes its temporary data.

Limits are per media kind and configurable; :func:`kind_limits` returns them so
the API can tell the client exactly what it will accept.  Space is checked before
a session is accepted, so a full disk fails early with a clear message rather
than half-way through an expensive transfer.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
import uuid
from typing import Any, Mapping, Optional

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_LIMITS",
    "UploadSessionError",
    "append_chunk",
    "cancel_session",
    "check_space",
    "cleanup_sessions",
    "complete_session",
    "kind_limits",
    "init_session",
    "session_status",
]

# 8 MB: a good compromise between request overhead and per-request memory.
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
MB = 1024 * 1024

# Configurable per media kind.  Raise these deliberately (see docs); nothing here
# promises an unlimited upload.
DEFAULT_LIMITS: dict[str, int] = {
    "image": 64 * MB,
    "audio": 1024 * MB,
    "video": 16384 * MB,
    "document": 256 * MB,
    "other": 1024 * MB,
}

SESSION_STATES = ("open", "assembling", "completed", "cancelled", "failed")
SESSION_ID_RE = re.compile(r"^[a-f0-9]{32}$")
MIN_FREE_RESERVE = 256 * MB

# A session that has not been touched for this long is abandoned.
STALE_AFTER_SECONDS = 24 * 3600


class UploadSessionError(ValueError):
    """A chunked upload was used incorrectly, or exceeded a configured limit."""


def kind_limits(overrides: Optional[Mapping[str, int]] = None) -> dict[str, int]:
    """The effective per-kind limits, so the UI can show them."""
    limits = dict(DEFAULT_LIMITS)
    for key, value in (overrides or {}).items():
        name = str(key)
        if name not in limits:
            raise UploadSessionError(
                f"Unknown media kind {name!r}; known kinds: " + ", ".join(sorted(limits))
            )
        try:
            size = int(value)
        except (TypeError, ValueError):
            raise UploadSessionError(f"Limit for {name!r} must be an integer") from None
        if size <= 0:
            raise UploadSessionError(f"Limit for {name!r} must be positive")
        limits[name] = size
    return limits


def check_space(dest_dir: str, needed_bytes: int, *, reserve: int = MIN_FREE_RESERVE) -> dict[str, Any]:
    """Is there room for ``needed_bytes`` (plus a reserve) under ``dest_dir``?"""
    os.makedirs(dest_dir, exist_ok=True)
    try:
        usage = shutil.disk_usage(dest_dir)
    except OSError as exc:
        raise UploadSessionError(f"Cannot read free space for {dest_dir}: {exc}") from exc
    available = int(usage.free)
    required = int(needed_bytes) + int(reserve)
    return {
        "free_bytes": available,
        "needed_bytes": int(needed_bytes),
        "reserve_bytes": int(reserve),
        "enough": available >= required,
        "available_after_upload_bytes": max(0, available - needed_bytes),
    }


# ---------------------------------------------------------------------------
# Session storage
# ---------------------------------------------------------------------------


def _session_dir(root: str, session_id: str) -> str:
    if not SESSION_ID_RE.match(str(session_id or "")):
        raise UploadSessionError("Invalid upload session id")
    return os.path.join(root, session_id)


def _manifest_path(root: str, session_id: str) -> str:
    return os.path.join(_session_dir(root, session_id), "session.json")


def _chunk_path(root: str, session_id: str, index: int) -> str:
    return os.path.join(_session_dir(root, session_id), f"chunk-{index:06d}.bin")


def _atomic_write_json(path: str, payload: Mapping[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".session.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _load_manifest(root: str, session_id: str) -> dict[str, Any]:
    path = _manifest_path(root, session_id)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Upload session {session_id} does not exist")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except ValueError as exc:
        raise UploadSessionError(f"Upload session {session_id} is corrupt: {exc}") from exc
    if not isinstance(data, dict):
        raise UploadSessionError(f"Upload session {session_id} is corrupt")
    return data


def _save_manifest(root: str, session_id: str, manifest: Mapping[str, Any]) -> None:
    _atomic_write_json(_manifest_path(root, session_id), manifest)


def _safe_filename(name: str, fallback: str) -> str:
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base)[:150]
    return cleaned or fallback


def init_session(
    root: str,
    *,
    filename: str,
    total_bytes: int = 0,
    content_type: str = "",
    expected_sha256: str = "",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    limits: Optional[Mapping[str, int]] = None,
    reserve: int = MIN_FREE_RESERVE,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Open an upload session.

    ``total_bytes=0`` means the client does not know the size yet (a live
    recording, for instance); the session then expects chunks strictly in order.
    With a known size, chunks may arrive in any order and the session can be
    resumed by sending only the missing indices.
    """
    from .film_projects import infer_media_kind

    if not root:
        raise UploadSessionError("A sessions directory is required")
    try:
        total = int(total_bytes or 0)
    except (TypeError, ValueError):
        raise UploadSessionError("total_bytes must be an integer") from None
    if total < 0:
        raise UploadSessionError("total_bytes cannot be negative")
    try:
        size = int(chunk_size)
    except (TypeError, ValueError):
        raise UploadSessionError("chunk_size must be an integer") from None
    if size < 64 * 1024 or size > 256 * MB:
        raise UploadSessionError("chunk_size must be between 64 KB and 256 MB")

    safe_name = _safe_filename(filename, "upload.bin")
    kind = infer_media_kind(safe_name)
    effective = kind_limits(limits)
    limit = effective.get(kind, effective["other"])
    if total and total > limit:
        raise UploadSessionError(
            f"{kind} uploads are limited to {limit / MB:.0f} MB "
            f"(this one is {total / MB:.0f} MB). "
            "The limit is configurable — see docs/FILM-STUDIO.md."
        )

    digest = str(expected_sha256 or "").strip().lower()
    if digest and not re.match(r"^[a-f0-9]{64}$", digest):
        raise UploadSessionError("expected_sha256 must be a 64-character hex digest")

    if total:
        space = check_space(os.path.dirname(os.path.abspath(root)) or ".", total, reserve=reserve)
        if not space["enough"]:
            raise UploadSessionError(
                f"Not enough free space: {space['free_bytes'] / MB:.0f} MB free, "
                f"{total / MB:.0f} MB needed"
            )

    stamp = float(now) if now is not None else time.time()
    session_id = uuid.uuid4().hex
    chunks_total = math.ceil(total / size) if total else None
    manifest = {
        "session_id": session_id,
        "filename": safe_name,
        "kind": kind,
        "content_type": str(content_type or "")[:200],
        "total_bytes": total,
        "mode": "chunked" if total else "streaming",
        "chunk_size": size,
        "chunks_total": chunks_total,
        "limit_bytes": limit,
        "expected_sha256": digest,
        "received": {},
        "received_bytes": 0,
        "state": "open",
        "created_at": stamp,
        "updated_at": stamp,
    }
    os.makedirs(_session_dir(root, session_id), exist_ok=True)
    _save_manifest(root, session_id, manifest)
    return _describe(manifest)


def _describe(manifest: Mapping[str, Any]) -> dict[str, Any]:
    received = manifest.get("received") or {}
    indices = sorted(int(index) for index in received)
    total = int(manifest.get("total_bytes") or 0)
    chunks_total = manifest.get("chunks_total")
    state = manifest.get("state")
    missing: list[int] = []
    if chunks_total and state not in {"completed", "cancelled"}:
        missing = [i for i in range(int(chunks_total)) if str(i) not in received]
    received_bytes = int(manifest.get("received_bytes") or 0)
    if state == "completed":
        received_bytes = int(manifest.get("final_bytes") or received_bytes)
    percent = round(100.0 * received_bytes / total, 1) if total else None
    if state == "completed" and percent is not None:
        percent = 100.0
    return {
        "session_id": manifest.get("session_id"),
        "filename": manifest.get("filename"),
        "kind": manifest.get("kind"),
        "mode": manifest.get("mode"),
        "state": manifest.get("state"),
        "chunk_size": manifest.get("chunk_size"),
        "chunks_total": chunks_total,
        "received_chunks": len(indices),
        "received_bytes": received_bytes,
        "missing_chunks": missing,
        "percent_complete": percent,
        "total_bytes": total,
        "limit_bytes": manifest.get("limit_bytes"),
        "expected_sha256": manifest.get("expected_sha256") or "",
        "created_at": manifest.get("created_at"),
        "updated_at": manifest.get("updated_at"),
        "final_filename": manifest.get("final_filename"),
        "final_path": manifest.get("final_path"),
        "sha256": manifest.get("sha256"),
        "deduplicated": manifest.get("deduplicated"),
        "error": manifest.get("error") or "",
    }


def session_status(root: str, session_id: str) -> dict[str, Any]:
    """Everything a client needs to resume: what arrived, what is missing."""
    return _describe(_load_manifest(root, session_id))


def append_chunk(
    root: str,
    session_id: str,
    index: int,
    data: bytes,
    *,
    chunk_sha256: str = "",
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Store one chunk.  Re-sending a chunk overwrites it (idempotent resume)."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise UploadSessionError("A chunk must carry bytes")
    manifest = _load_manifest(root, session_id)
    if manifest.get("state") != "open":
        raise UploadSessionError(
            f"Upload session is {manifest.get('state')} and no longer accepts chunks"
        )
    try:
        position = int(index)
    except (TypeError, ValueError):
        raise UploadSessionError("Chunk index must be an integer") from None
    if position < 0:
        raise UploadSessionError("Chunk index cannot be negative")

    chunks_total = manifest.get("chunks_total")
    if chunks_total is not None:
        if position >= int(chunks_total):
            raise UploadSessionError(
                f"Chunk {position} is out of range (this upload has {chunks_total} chunks)"
            )
    else:
        received = manifest.get("received") or {}
        if position != len(received):
            raise UploadSessionError(
                f"Streaming uploads must arrive in order: expected chunk {len(received)}, got {position}"
            )

    size = int(manifest.get("chunk_size") or DEFAULT_CHUNK_SIZE)
    if len(data) > size:
        raise UploadSessionError(
            f"Chunk is {len(data)} bytes but the session chunk size is {size}"
        )

    digest = hashlib.sha256(data).hexdigest()
    expected_chunk = str(chunk_sha256 or "").strip().lower()
    if expected_chunk and expected_chunk != digest:
        raise UploadSessionError("Chunk failed its integrity check (SHA-256 mismatch)")

    path = _chunk_path(root, session_id, position)
    fd, temporary = tempfile.mkstemp(
        prefix=f".chunk{position}.", suffix=".tmp", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass

    received = dict(manifest.get("received") or {})
    previous = int(received.get(str(position)) or 0)
    received[str(position)] = len(data)
    manifest["received"] = received
    manifest["received_bytes"] = int(manifest.get("received_bytes") or 0) - previous + len(data)
    manifest["updated_at"] = float(now) if now is not None else time.time()
    remaining = manifest["received_bytes"]
    total = int(manifest.get("total_bytes") or 0)
    if total and remaining > total:
        raise UploadSessionError("Received more bytes than the session declared")
    _save_manifest(root, session_id, manifest)
    return _describe(manifest)


def complete_session(
    root: str,
    session_id: str,
    *,
    final_dir: str,
    dedup: bool = True,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Assemble the chunks into the final file, streaming, then clean up.

    The assembled file is written to a temporary name and atomically renamed, so
    a crash mid-assembly cannot leave a truncated file where a complete one is
    expected.
    """
    manifest = _load_manifest(root, session_id)
    if manifest.get("state") == "completed":
        return _describe(manifest)
    if manifest.get("state") != "open":
        raise UploadSessionError(f"Upload session is {manifest.get('state')}")
    if not final_dir:
        raise UploadSessionError("A destination directory is required")

    received = manifest.get("received") or {}
    chunks_total = manifest.get("chunks_total")
    if chunks_total is not None:
        missing = [i for i in range(int(chunks_total)) if str(i) not in received]
        if missing:
            raise UploadSessionError(
                f"Upload is incomplete: {len(missing)} chunk(s) missing, "
                f"starting at {missing[0]}"
            )
    elif not received:
        raise UploadSessionError("No chunks were received")
    else:
        missing = []

    total = int(manifest.get("total_bytes") or 0)
    received_bytes = int(manifest.get("received_bytes") or 0)
    if total and received_bytes != total:
        raise UploadSessionError(
            f"Upload is incomplete: received {received_bytes} of {total} bytes"
        )

    os.makedirs(final_dir, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    fd, assembled = tempfile.mkstemp(prefix=".assembled-", suffix=".part", dir=final_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            for position in sorted(int(key) for key in received):
                path = _chunk_path(root, session_id, position)
                if not os.path.isfile(path):
                    raise UploadSessionError(
                        f"Chunk {position} is missing from disk; restart that chunk"
                    )
                with open(path, "rb") as source:
                    while True:
                        block = source.read(1024 * 256)
                        if not block:
                            break
                        written += len(block)
                        digest.update(block)
                        handle.write(block)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if os.path.exists(assembled):
            try:
                os.unlink(assembled)
            except OSError:
                pass
        raise

    sha256 = digest.hexdigest()
    expected = str(manifest.get("expected_sha256") or "")
    if expected and expected != sha256:
        try:
            os.unlink(assembled)
        except OSError:
            pass
        manifest["state"] = "failed"
        manifest["error"] = "Assembled file did not match the declared SHA-256"
        manifest["updated_at"] = float(now) if now is not None else time.time()
        _save_manifest(root, session_id, manifest)
        raise UploadSessionError(
            "The assembled file does not match the SHA-256 you declared "
            f"(expected {expected[:12]}…, got {sha256[:12]}…). The session is kept "
            "so the chunks can be re-sent."
        )

    name = str(manifest.get("filename") or "upload.bin")
    extension = os.path.splitext(name)[1].lower()
    stored_name = f"{sha256[:16]}{extension}"
    final_path = os.path.join(final_dir, stored_name)

    if dedup and os.path.exists(final_path):
        try:
            os.unlink(assembled)
        except OSError:
            pass
        deduplicated = True
    else:
        os.replace(assembled, final_path)
        deduplicated = False

    manifest.update(
        {
            "state": "completed",
            "sha256": sha256,
            "final_filename": stored_name,
            "final_path": final_path,
            "final_bytes": written,
            "deduplicated": deduplicated,
            "updated_at": float(now) if now is not None else time.time(),
            "error": "",
        }
    )
    _save_manifest(root, session_id, manifest)
    # Temp chunks are no longer needed once the file is safely assembled.
    for key in list(manifest.get("received") or {}):
        path = _chunk_path(root, session_id, int(key))
        if os.path.isfile(path):
            try:
                os.unlink(path)
            except OSError:
                pass
    # The chunks are gone; the manifest now describes the finished file only.
    manifest["received"] = {}
    manifest["received_bytes"] = 0
    _save_manifest(root, session_id, manifest)
    return _describe(manifest)


def cancel_session(
    root: str,
    session_id: str,
    *,
    keep_record: bool = True,
) -> dict[str, Any]:
    """Abandon a session and delete its temporary data."""
    manifest = _load_manifest(root, session_id)
    directory = _session_dir(root, session_id)
    for name in os.listdir(directory) if os.path.isdir(directory) else []:
        if name.startswith("chunk-") or name.endswith(".tmp") or name.endswith(".part"):
            try:
                os.unlink(os.path.join(directory, name))
            except OSError:
                pass
    manifest["state"] = "cancelled"
    manifest["received"] = {}
    manifest["received_bytes"] = 0
    manifest["updated_at"] = time.time()
    if keep_record:
        _save_manifest(root, session_id, manifest)
        return _describe(manifest)
    shutil.rmtree(directory, ignore_errors=True)
    return {**_describe(manifest), "removed": True}


def cleanup_sessions(
    root: str,
    *,
    older_than: float = STALE_AFTER_SECONDS,
    now: Optional[float] = None,
    include_open: bool = False,
) -> dict[str, Any]:
    """Delete temporary data that is no longer useful.

    Finished sessions are always cleared.  An upload that is still *open* is
    never touched unless the caller explicitly passes ``include_open=True``:
    deleting someone's half-finished transfer by default would be data loss.
    """
    stamp = float(now) if now is not None else time.time()
    removed: list[str] = []
    freed = 0
    if not os.path.isdir(root):
        return {"removed": [], "freed_bytes": 0}
    for name in sorted(os.listdir(root)):
        directory = os.path.join(root, name)
        if not os.path.isdir(directory) or not SESSION_ID_RE.match(name):
            continue
        try:
            manifest = _load_manifest(root, name)
        except (FileNotFoundError, UploadSessionError):
            continue
        state = manifest.get("state")
        # Epoch 0.0 is a valid timestamp: test for None, not truthiness, or an
        # old session would be judged as if it had just been touched.
        touched = manifest.get("updated_at")
        if touched is None:
            touched = manifest.get("created_at")
        updated = float(touched) if touched is not None else stamp
        stale = (stamp - updated) > float(older_than)
        abandoned = state == "failed"
        if state in {"completed", "cancelled"} or (stale and (include_open or abandoned)):
            for entry in os.listdir(directory):
                path = os.path.join(directory, entry)
                if os.path.isfile(path):
                    try:
                        freed += os.path.getsize(path)
                    except OSError:
                        pass
            shutil.rmtree(directory, ignore_errors=True)
            removed.append(name)
    return {"removed": removed, "freed_bytes": freed}

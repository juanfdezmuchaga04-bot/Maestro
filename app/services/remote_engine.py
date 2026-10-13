"""Remote engine — the modest computer directs, a computer with a GPU renders.

Maestro is local-first: the app, the projects and the engine normally live on the
same machine.  That is exactly what a low-powered laptop cannot do — the engine
needs CUDA and RAM the laptop does not have.  This module is the seam that keeps
everything else intact: the interface, the projects, the persistent queue and the
films/editor flows stay on the directing machine, while **the generation happens
somewhere else**, on another Maestro that does have the hardware.

How the work moves:

    local queue job (engine ``node:<peer_id>``)
        → HTTP request to that peer's own production queue (with a token)
        → the peer renders it with its own engine (ComfyUI on its GPU)
        → this machine follows the peer's job and forwards its *real* progress
        → the finished files are downloaded into this workspace → ``completed``

Rules this module keeps, because they are what separates an integration from a
promise:

* **A peer is asked, never assumed.**  A peer is only advertised as a provider
  when a probe actually reached it; its capabilities come from its own
  ``/api/v1/engines`` report or from what the user declared, never from its name.
* **An unreachable computer is not a failure.**  If the peer does not answer, the
  job is *deferred*: it goes back to ``pending``, consumes no attempt and records
  no error.  It runs when the peer answers again — the same rule the local engine
  already follows.
* **Progress is forwarded, not invented.**  The percentage a job shows is the one
  the peer's own engine measured; a peer that reports nothing keeps
  ``percent = null``.
* **A file or nothing.**  The job completes only with a downloaded file that
  arrived with the size the peer declared.  A finished remote job whose transfer
  failed is deferred, not recorded as a result.
* **Delegation is not file access.**  The peer is asked for output *N* of the job
  it ran; no caller ever names a path (see ``production_queue_api``), and the peer
  authenticates the caller with its own ``MAESTRO_ACCESS_TOKEN`` (a bearer token),
  so no password is stored here.
* **Sending the same job twice does not render it twice.**  Every delegated job
  carries a ``dedupe_key`` derived from the local job id, so a retry adopts the
  job that already exists on the peer instead of starting a second render.

Nothing here opens a port, publishes a tunnel or talks to the internet: the peer
is reached on the private network the user already has (LAN or Tailscale), and the
transport is stdlib ``urllib`` so this module imports on a machine with no GPU, no
torch and no ComfyUI.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote

from . import comfyui_adapter
from . import generation_queue as queue
from .engine_providers import CAPABILITIES, CAPABILITY_SPECS
from .queue_worker import JobCancelled

__all__ = [
    "DEFAULT_PEER_ENGINE",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_POLL_TIMEOUT",
    "DEFAULT_TIMEOUT",
    "ENGINE_PREFIX",
    "PEERS_FILENAME",
    "PEER_ONLINE_TTL",
    "RemoteAuthError",
    "RemoteEngineError",
    "RemoteMaestroClient",
    "add_peer",
    "describe_peer",
    "engines_for_peers",
    "generation_sources",
    "get_peer",
    "list_peers",
    "peer_credentials",
    "peer_id_from_engine",
    "peer_id_from_job",
    "probe_peer",
    "provider_entries",
    "remember_probe",
    "remote_payload",
    "register_remote_handlers",
    "remove_peer",
    "run_job",
    "unregister_remote_handlers",
    "update_peer",
    "validate_base_url",
]

# A delegated job names its peer as the engine: ``node:<peer_id>``, the same id
# convention ``engine_providers`` uses for a compute node provider.
ENGINE_PREFIX = "node"
PEERS_FILENAME = "remote_peers.json"

# The engine a peer is asked to run with when it does not say otherwise: the
# peer's own default generation path.
DEFAULT_PEER_ENGINE = "comfyui"

DEFAULT_TIMEOUT = 10.0
DEFAULT_POLL_TIMEOUT = 1800.0
DEFAULT_POLL_INTERVAL = 3.0

# How long a successful probe keeps a peer "online" for the engine registry.  The
# registry only advertises what was measured recently; a stale probe is not
# evidence, and an explicit probe (or a finished job) refreshes it.
PEER_ONLINE_TTL = 3600.0

MAX_JSON_BYTES = 4 * 1024 * 1024
DOWNLOAD_CHUNK = 1024 * 1024

_PEER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ACTIVE_STATES = frozenset({"draft", "pending", "preparing", "running", "finalizing"})
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


class RemoteEngineError(RuntimeError):
    """A peer could not be used: unreachable, refusing, or answering nonsense."""


class RemoteAuthError(RemoteEngineError):
    """The peer refused this machine's credential."""


# ---------------------------------------------------------------------------
# Peer store  (app/settings/remote_peers/remote_peers.json, gitignored)
# ---------------------------------------------------------------------------


def _peers_path(root: str) -> str:
    return os.path.join(root, PEERS_FILENAME)


def _atomic_write_json(path: str, payload: Mapping[str, Any], *, private: bool = True) -> None:
    """Write atomically, and keep the file readable only by its owner.

    The file holds the peer's token, so it is created 0600 (best effort: the
    platform may not support modes, which is never fatal).
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".remote_peers.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            try:
                os.chmod(temporary, 0o600)
            except OSError:  # pragma: no cover - platform without POSIX modes
                pass
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _empty_store() -> dict[str, Any]:
    return {"version": 1, "peers": {}}


def _load(root: str) -> dict[str, Any]:
    path = _peers_path(root)
    if not os.path.isfile(path):
        return _empty_store()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        # A corrupt store must not take the app down: peers can be re-added, and
        # their jobs simply have no route until they are.
        return _empty_store()
    state = _empty_store()
    peers = raw.get("peers") if isinstance(raw, Mapping) else None
    if isinstance(peers, Mapping):
        state["peers"] = {str(k): v for k, v in peers.items() if isinstance(v, Mapping)}
    return state


def _require_peer_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _PEER_ID_RE.match(text):
        raise RemoteEngineError(
            "A peer id must be 1-64 characters of letters, digits, dot, dash or underscore"
        )
    return text


def validate_base_url(base_url: str) -> str:
    """Normalise a peer's base URL, reusing the studio's URL rules."""
    try:
        return comfyui_adapter.validate_base_url(base_url)
    except comfyui_adapter.ComfyUIError as error:
        raise RemoteEngineError(str(error)) from None


def _capabilities(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw: Sequence[Any] = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = list(value)
    else:
        raise RemoteEngineError("capabilities must be a list of capability names")
    out: list[str] = []
    for item in raw:
        name = str(item).strip()
        if not name:
            continue
        if name not in CAPABILITIES:
            raise RemoteEngineError(
                f"Unknown capability {name!r}. Known capabilities: " + ", ".join(CAPABILITIES)
            )
        if name not in out:
            out.append(name)
    return out


def _string_list(value: Any, limit: int = 200) -> list[str]:
    if isinstance(value, str):
        return [value.strip()[:200]] if value.strip() else []
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()[:200]
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _now(now: Optional[float]) -> float:
    return float(now) if now is not None else time.time()


def _is_online(probe: Mapping[str, Any], stamp: float) -> bool:
    at = probe.get("at")
    try:
        seen = float(at) if at is not None else 0.0
    except (TypeError, ValueError):
        return False
    return bool(probe.get("reachable")) and seen > 0 and (stamp - seen) <= PEER_ONLINE_TTL


def describe_peer(record: Mapping[str, Any], *, now: Optional[float] = None) -> dict[str, Any]:
    """A peer as the API and the UI may see it — **without** the token."""
    stamp = _now(now)
    probe = record.get("last_probe")
    probe = dict(probe) if isinstance(probe, Mapping) else {}
    return {
        "peer_id": record.get("peer_id"),
        "label": record.get("label") or record.get("peer_id"),
        "base_url": record.get("base_url"),
        "engine": record.get("engine") or DEFAULT_PEER_ENGINE,
        "capabilities": _string_list(record.get("capabilities")),
        "capabilities_source": record.get("capabilities_source") or "declared",
        "models": _string_list(record.get("models")),
        "notes": record.get("notes") or "",
        "token_set": bool(record.get("token")),
        "online": _is_online(probe, stamp),
        "online_ttl_seconds": PEER_ONLINE_TTL,
        "last_probe": probe or None,
        "added_at": record.get("added_at"),
        "updated_at": record.get("updated_at"),
        "engine_id": f"{ENGINE_PREFIX}:{record.get('peer_id')}",
    }


def list_peers(root: str, *, now: Optional[float] = None) -> list[dict[str, Any]]:
    state = _load(root)
    peers = [
        describe_peer(record, now=now)
        for record in state["peers"].values()
        if isinstance(record, Mapping)
    ]
    peers.sort(key=lambda peer: (not peer["online"], str(peer["peer_id"])))
    return peers


def get_peer(root: str, peer_id: str) -> dict[str, Any]:
    """The stored record **including** the token — for outbound calls only."""
    wanted = _require_peer_id(peer_id)
    state = _load(root)
    record = state["peers"].get(wanted)
    if not isinstance(record, Mapping):
        raise FileNotFoundError(f"Remote peer {wanted!r} is not configured")
    return dict(record)


def peer_credentials(root: str, peer_id: str) -> tuple[str, str]:
    """``(base_url, token)`` for a peer, or :class:`FileNotFoundError`."""
    record = get_peer(root, peer_id)
    return str(record.get("base_url") or ""), str(record.get("token") or "")


def engines_for_peers(root: str) -> list[str]:
    """The queue engine strings a delegated job can carry: ``node:<peer_id>``."""
    return [
        f"{ENGINE_PREFIX}:{peer['peer_id']}"
        for peer in list_peers(root)
        if peer.get("peer_id")
    ]


def add_peer(
    root: str,
    peer: Mapping[str, Any],
    *,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Configure a peer.  Returns the peer as described *without* its token."""
    if not isinstance(peer, Mapping):
        raise RemoteEngineError("A peer must be an object")
    peer_id = _require_peer_id(peer.get("peer_id"))
    base_url = validate_base_url(str(peer.get("base_url") or ""))
    declared = _capabilities(peer.get("capabilities"))
    stamp = _now(now)
    state = _load(root)
    existing = state["peers"].get(peer_id)
    added_at = stamp
    if isinstance(existing, Mapping) and existing.get("added_at") is not None:
        added_at = existing["added_at"]
    record = {
        "peer_id": peer_id,
        "label": str(peer.get("label") or peer_id).strip()[:200] or peer_id,
        "base_url": base_url,
        "engine": str(peer.get("engine") or DEFAULT_PEER_ENGINE).strip() or DEFAULT_PEER_ENGINE,
        "capabilities": declared,
        # Capabilities are either declared by the user or measured from the
        # peer's own report; the source is recorded so a later probe knows
        # whether it is allowed to refresh them.
        "capabilities_source": "declared" if declared else "probe",
        "models": _string_list(peer.get("models")),
        "token": str(peer.get("token") or ""),
        "notes": str(peer.get("notes") or "")[:400],
        "added_at": added_at,
        "updated_at": stamp,
        "last_probe": dict(existing.get("last_probe") or {}) if isinstance(existing, Mapping) else {},
    }
    state["peers"][peer_id] = record
    _atomic_write_json(_peers_path(root), state)
    return describe_peer(record, now=stamp)


def update_peer(
    root: str,
    peer_id: str,
    patch: Mapping[str, Any],
    *,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Change a peer's address, token, engine or declared capabilities."""
    wanted = _require_peer_id(peer_id)
    if not isinstance(patch, Mapping):
        raise RemoteEngineError("A peer update must be an object")
    state = _load(root)
    record = state["peers"].get(wanted)
    if not isinstance(record, Mapping):
        raise FileNotFoundError(f"Remote peer {wanted!r} is not configured")
    updated = dict(record)
    if "base_url" in patch:
        updated["base_url"] = validate_base_url(str(patch.get("base_url") or ""))
    if "label" in patch:
        updated["label"] = str(patch.get("label") or wanted).strip()[:200] or wanted
    if "engine" in patch:
        updated["engine"] = str(patch.get("engine") or DEFAULT_PEER_ENGINE).strip() or DEFAULT_PEER_ENGINE
    if "token" in patch:
        updated["token"] = str(patch.get("token") or "")
    if "notes" in patch:
        updated["notes"] = str(patch.get("notes") or "")[:400]
    if "capabilities" in patch:
        declared = _capabilities(patch.get("capabilities"))
        updated["capabilities"] = declared
        updated["capabilities_source"] = "declared" if declared else "probe"
    if "models" in patch:
        updated["models"] = _string_list(patch.get("models"))
    updated["updated_at"] = _now(now)
    state["peers"][wanted] = updated
    _atomic_write_json(_peers_path(root), state)
    return describe_peer(updated, now=updated["updated_at"])


def remove_peer(root: str, peer_id: str) -> dict[str, Any]:
    """Forget a peer.  Jobs already sent to it are not touched."""
    wanted = _require_peer_id(peer_id)
    state = _load(root)
    if state["peers"].pop(wanted, None) is None:
        raise FileNotFoundError(f"Remote peer {wanted!r} is not configured")
    _atomic_write_json(_peers_path(root), state)
    return {
        "removed": wanted,
        "note": (
            "Nothing was changed on the other computer: work already delegated there "
            "keeps running and its results stay in that computer's own queue."
        ),
    }


def remember_probe(
    root: str,
    peer_id: str,
    probe: Mapping[str, Any],
    *,
    now: Optional[float] = None,
) -> None:
    """Store the outcome of a probe, refreshing measured capabilities only."""
    stamp = _now(now)
    try:
        state = _load(root)
    except Exception:  # pragma: no cover - defensive
        return
    record = state["peers"].get(peer_id)
    if not isinstance(record, Mapping):
        return
    updated = dict(record)
    updated["last_probe"] = dict(probe)
    updated["updated_at"] = stamp
    if record.get("capabilities_source") == "probe" and probe.get("capabilities"):
        updated["capabilities"] = _string_list(probe.get("capabilities"))
    if probe.get("models"):
        updated["models"] = _string_list(probe.get("models"))
    state["peers"][peer_id] = updated
    try:
        _atomic_write_json(_peers_path(root), state)
    except OSError:  # pragma: no cover - a full disk must not fail a health check
        pass


def generation_sources(root: str, *, now: Optional[float] = None) -> list[dict[str, Any]]:
    """Online peers that reported a capability which actually renders something.

    This is what ``services.film_api`` consults to decide whether a production can
    run: a peer that merely exists is not a source, and neither is one that only
    offers processing (ffmpeg work happens here, on this computer).
    """
    sources: list[dict[str, Any]] = []
    for peer in list_peers(root, now=now):
        rendering = [
            name for name in peer["capabilities"] if CAPABILITY_SPECS.get(name, {}).get("needs_gpu")
        ]
        if not peer["online"] or not rendering:
            continue
        sources.append({
            "kind": "remote_maestro",
            "peer_id": peer["peer_id"],
            "label": peer["label"],
            "base_url": peer["base_url"],
            "engine_id": peer["engine_id"],
            "capabilities": list(peer["capabilities"]),
            "models": list(peer["models"]),
            "online": True,
            "last_probe_at": (peer.get("last_probe") or {}).get("at"),
        })
    return sources


def provider_entries(root: str, *, now: Optional[float] = None) -> list[dict[str, Any]]:
    """Peers shaped for :func:`engine_providers.build_registry`.

    Only a peer whose last probe is fresh is reported online, so a capability is
    never advertised on the strength of a peer that was switched off an hour ago.
    """
    stamp = _now(now)
    entries: list[dict[str, Any]] = []
    for peer in list_peers(root, now=stamp):
        probe = peer.get("last_probe") or {}
        entries.append({
            "node_id": peer["peer_id"],
            "label": f"{peer['label']} ({peer['base_url']})",
            "capabilities": list(peer["capabilities"]),
            "models": list(peer["models"]),
            "local": False,
            "online": bool(peer["online"]),
            "last_seen": probe.get("at"),
            "address": peer["base_url"],
            "notes": (
                "Maestro on another computer; work is delegated to that machine's own "
                "queue and the results are transferred back here."
            ),
        })
    return entries


# ---------------------------------------------------------------------------
# HTTP client for a peer's own Maestro API
# ---------------------------------------------------------------------------


def _detail(payload: Any) -> str:
    if isinstance(payload, Mapping):
        text = payload.get("detail") or payload.get("error") or ""
        if text:
            return str(text)[:400]
    return ""


def _json_of(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return {"detail": raw.decode("utf-8", errors="replace")[:400]}


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _total_from_range(header: str) -> Optional[int]:
    """``"bytes 0-99/2048"`` → ``2048``."""
    text = str(header or "")
    if "/" not in text:
        return None
    return _int_or_none(text.rsplit("/", 1)[-1])


class RemoteMaestroClient:
    """The peer's production queue, spoken over HTTP with a bearer token.

    Only endpoints that already exist are used.  Every transport failure is
    surfaced as :class:`RemoteEngineError` (or :class:`RemoteAuthError` for a
    refused credential) so a caller can tell "the computer is off" apart from
    "the file is missing".
    """

    def __init__(self, base_url: str, *, token: str = "", timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = validate_base_url(base_url)
        self.token = str(token or "")
        self.timeout = float(timeout)

    # -- transport --------------------------------------------------------

    def _headers(self, payload: Optional[Mapping[str, Any]]) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
        return headers

    def _raise_for_transport(self, error: Exception) -> None:
        reason = getattr(error, "reason", error)
        raise RemoteEngineError(f"Cannot reach Maestro at {self.base_url}: {reason}") from error

    def call(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Optional[Mapping[str, Any]] = None,
    ) -> tuple[int, Any]:
        """One JSON request.  Returns ``(status, body)``; transport errors raise."""
        data = json.dumps(dict(payload)).encode("utf-8") if payload is not None else None
        request = urlrequest.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=self._headers(payload),
            method=method,
        )
        try:
            with urlrequest.urlopen(request, timeout=self.timeout) as response:
                status = int(getattr(response, "status", 200) or 200)
                raw = response.read(MAX_JSON_BYTES + 1)
        except urlerror.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read(MAX_JSON_BYTES)
            except Exception:  # pragma: no cover - defensive
                raw = b""
            body = _json_of(raw)
            if exc.code in {401, 403}:
                raise RemoteAuthError(
                    f"{self.base_url} refused this machine's credential "
                    f"(HTTP {exc.code}): {_detail(body) or 'no detail'}"
                ) from exc
            return int(exc.code), body
        except (urlerror.URLError, OSError) as error:
            self._raise_for_transport(error)
            raise  # pragma: no cover - _raise_for_transport always raises

        if len(raw) > MAX_JSON_BYTES:
            raise RemoteEngineError(
                f"{self.base_url}{path} answered with more JSON than this client will read"
            )
        return status, _json_of(raw)

    # -- the peer's own report -------------------------------------------

    def describe(self) -> dict[str, Any]:
        """Reach the peer and ask what it can really do (its measured state)."""
        status, engines = self.call("/api/v1/engines")
        if status != 200:
            raise RemoteEngineError(
                f"{self.base_url} answered HTTP {status} for /api/v1/engines: "
                f"{_detail(engines) or 'no detail'}"
            )
        if not isinstance(engines, Mapping):
            raise RemoteEngineError(f"{self.base_url} did not answer with an engine report")
        available = [
            str(entry.get("capability"))
            for entry in (engines.get("availability") or [])
            if isinstance(entry, Mapping) and entry.get("available") and entry.get("capability")
        ]
        local = engines.get("local_engine")
        local = local if isinstance(local, Mapping) else {}
        queue_status, queue_state = self.call("/api/v1/production/queue?include_terminal=false&limit=1")
        queue_state = queue_state if isinstance(queue_state, Mapping) else {}
        worker_state = queue_state.get("worker")
        worker_state = worker_state if isinstance(worker_state, Mapping) else {}
        return {
            "reachable": True,
            "base_url": self.base_url,
            "capabilities": available,
            "models": _string_list(local.get("models")),
            "generation_possible": bool(local.get("generation_possible")),
            "queue_worker_running": bool(worker_state.get("running")) if queue_status == 200 else False,
            "queue_handlers": _string_list(queue_state.get("handlers")) if queue_status == 200 else [],
            "authenticated": bool(self.token),
        }

    # -- the peer's queue -------------------------------------------------

    def queue_view(self, *, include_terminal: bool = True, limit: int = 500) -> dict[str, Any]:
        status, body = self.call(
            "/api/v1/production/queue?"
            f"include_terminal={'true' if include_terminal else 'false'}&limit={int(limit)}"
        )
        if status != 200 or not isinstance(body, Mapping):
            raise RemoteEngineError(
                f"{self.base_url} answered HTTP {status} for its queue: {_detail(body) or 'no detail'}"
            )
        return dict(body)

    def submit_job(self, job: Mapping[str, Any]) -> dict[str, Any]:
        status, body = self.call(
            "/api/v1/production/queue/jobs", method="POST", payload=dict(job)
        )
        if status not in {200, 201} or not isinstance(body, Mapping):
            raise RemoteEngineError(
                f"{self.base_url} refused the job (HTTP {status}): {_detail(body) or 'no detail'}"
            )
        return dict(body)

    def job(self, job_id: str) -> dict[str, Any]:
        status, body = self.call(
            f"/api/v1/production/queue/jobs/{quote(str(job_id), safe='')}"
        )
        if status == 404:
            raise RemoteEngineError(f"{self.base_url} does not know job {job_id!r}")
        if status != 200 or not isinstance(body, Mapping):
            raise RemoteEngineError(
                f"{self.base_url} answered HTTP {status} for job {job_id!r}"
            )
        return dict(body)

    def outputs(self, job_id: str) -> list[dict[str, Any]]:
        status, body = self.call(
            f"/api/v1/production/queue/jobs/{quote(str(job_id), safe='')}/outputs"
        )
        if status != 200 or not isinstance(body, Mapping):
            raise RemoteEngineError(
                f"{self.base_url} answered HTTP {status} for the outputs of {job_id!r}"
            )
        return [dict(entry) for entry in (body.get("outputs") or []) if isinstance(entry, Mapping)]

    def cancel(self, job_id: str, *, reason: str = "") -> dict[str, Any]:
        status, body = self.call(
            f"/api/v1/production/queue/jobs/{quote(str(job_id), safe='')}/cancel",
            method="POST",
            payload={"reason": reason},
        )
        if status != 200 or not isinstance(body, Mapping):
            raise RemoteEngineError(
                f"{self.base_url} answered HTTP {status} when asked to cancel {job_id!r}"
            )
        return dict(body)

    def acknowledge_cancel(self, job_id: str) -> dict[str, Any]:
        status, body = self.call(
            f"/api/v1/production/queue/jobs/{quote(str(job_id), safe='')}/acknowledge-cancel",
            method="POST",
        )
        return dict(body) if isinstance(body, Mapping) else {"status": status}

    # -- transferring the result -----------------------------------------

    def download(
        self,
        job_id: str,
        index: int,
        destination: str,
        *,
        expected_bytes: int = 0,
        timeout: Optional[float] = None,
        chunk: int = DOWNLOAD_CHUNK,
    ) -> dict[str, Any]:
        """Fetch output ``index`` to ``destination``, resuming a partial file.

        The partial download is kept as ``<destination>.part`` and a retry
        continues from its size with a ``Range`` request, so a dropped connection
        costs the remainder of the clip, not the whole clip.
        """
        path = f"/api/v1/production/queue/jobs/{quote(str(job_id), safe='')}/outputs/{int(index)}"
        partial = f"{destination}.part"
        resume_from = os.path.getsize(partial) if os.path.isfile(partial) else 0
        os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)

        def _open(offset: int) -> Any:
            headers = self._headers(None)
            if offset:
                headers["Range"] = f"bytes={offset}-"
            request = urlrequest.Request(f"{self.base_url}{path}", headers=headers, method="GET")
            try:
                return urlrequest.urlopen(request, timeout=timeout or self.timeout)
            except urlerror.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read(400).decode("utf-8", errors="replace")
                except Exception:  # pragma: no cover - defensive
                    detail = ""
                if exc.code in {401, 403}:
                    raise RemoteAuthError(
                        f"{self.base_url} refused this machine's credential (HTTP {exc.code})"
                    ) from exc
                if exc.code == 416:
                    # Our partial file is longer than what the peer has now: start over.
                    raise RemoteEngineError(
                        "the peer no longer offers that file at that offset; the partial "
                        "download was discarded"
                    ) from exc
                raise RemoteEngineError(
                    f"{self.base_url} answered HTTP {exc.code} for output {index} of "
                    f"{job_id!r}{f': {detail[:300]}' if detail else ''}"
                ) from exc
            except (urlerror.URLError, OSError) as error:
                self._raise_for_transport(error)
                raise  # pragma: no cover

        try:
            response = _open(resume_from)
        except RemoteEngineError:
            if resume_from and os.path.isfile(partial):
                os.unlink(partial)
            raise

        with response:
            status = int(getattr(response, "status", 200) or 200)
            total = _total_from_range(response.headers.get("Content-Range") or "")
            declared = total or _int_or_none(response.headers.get("Content-Length")) or expected_bytes
            if status == 200 and resume_from:
                # The peer ignored the range, so start the file again rather than
                # appending a second copy to the partial one.
                resume_from = 0
            written = resume_from
            mode = "ab" if resume_from else "wb"
            with open(partial, mode) as handle:
                while True:
                    data = response.read(chunk)
                    if not data:
                        break
                    handle.write(data)
                    written += len(data)

        if written <= 0:
            raise RemoteEngineError(f"output {index} of job {job_id!r} arrived empty")
        if declared and written != declared:
            raise RemoteEngineError(
                f"output {index} of job {job_id!r} is incomplete: {written} of {declared} bytes"
            )
        os.replace(partial, destination)
        return {
            "path": destination,
            "bytes": written,
            "declared_bytes": declared or 0,
            "resumed_from": resume_from,
        }


# ---------------------------------------------------------------------------
# Peer routing helpers
# ---------------------------------------------------------------------------


def peer_id_from_engine(engine: str) -> str:
    """``"node:desk-1"`` → ``"desk-1"``; anything else → ``""``."""
    text = str(engine or "").strip()
    prefix, _, suffix = text.partition(":")
    if prefix != ENGINE_PREFIX or not suffix.strip():
        return ""
    candidate = suffix.strip()
    return candidate if _PEER_ID_RE.match(candidate) else ""


def peer_id_from_job(job: Mapping[str, Any]) -> str:
    """Which peer a job names: its engine id first, then ``params.peer_id``."""
    from_engine = peer_id_from_engine(str(job.get("engine") or ""))
    if from_engine:
        return from_engine
    params = job.get("params")
    declared = str((params or {}).get("peer_id") or "").strip() if isinstance(params, Mapping) else ""
    return declared if _PEER_ID_RE.match(declared) else ""


def probe_peer(
    root: str,
    peer_id: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Ask a peer what it can do and remember the answer — reachability is measured."""
    record = get_peer(root, peer_id)
    client = RemoteMaestroClient(
        str(record.get("base_url") or ""), token=str(record.get("token") or ""), timeout=timeout
    )
    stamp = _now(now)
    try:
        described = client.describe()
    except RemoteEngineError as error:
        probe = {
            "at": stamp,
            "reachable": False,
            "error": f"{type(error).__name__}: {error}"[:400],
            "capabilities": [],
            "models": [],
        }
        remember_probe(root, peer_id, probe, now=stamp)
        online = False
    else:
        probe = {"at": stamp, **described}
        remember_probe(root, peer_id, probe, now=stamp)
        online = True
    return {
        "peer_id": peer_id,
        "online": online,
        "probe": probe,
        "peer": describe_peer(get_peer(root, peer_id), now=stamp),
    }


# ---------------------------------------------------------------------------
# Running a delegated job
# ---------------------------------------------------------------------------


def _destination_name(job_id: str, name: Any) -> str:
    """A local file name for a remote artefact: no separators, no collisions."""
    base = os.path.basename(str(name or "output.bin").replace("\\", "/")).strip() or "output.bin"
    return f"{str(job_id)[:12]}_{base}"


def remote_payload(job: Mapping[str, Any], peer: Mapping[str, Any], dedupe_key: str) -> dict[str, Any]:
    """The job as the peer should receive it.

    Local-only keys are stripped: a ``base_url`` or a peer id from this machine
    means nothing on the other one.  ``cancel_supported`` is declared because the
    remote engine has to be able to stop the render when this side asks.
    """
    params = dict(job.get("params") or {})
    for local_key in ("base_url", "peer_id"):
        params.pop(local_key, None)
    remote_engine = str(params.pop("remote_engine", "") or peer.get("engine") or DEFAULT_PEER_ENGINE)
    return {
        "project_id": str(job.get("project_id") or ""),
        "scene_id": str(job.get("scene_id") or ""),
        "shot_id": str(job.get("shot_id") or ""),
        "task": str(job.get("task") or "other"),
        "engine": remote_engine,
        "model": str(job.get("model") or ""),
        "params": params,
        "inputs": list(job.get("inputs") or []),
        "priority": int(job.get("priority") or 0),
        "memory_mb": int(job.get("memory_mb") or 0),
        "dedupe_key": dedupe_key,
        "cancel_supported": True,
    }


def _adoptable(jobs: Sequence[Mapping[str, Any]], dedupe_key: str) -> str:
    """The id of the remote job this delegation already created, if any.

    An active one always wins; otherwise the most recently finished job with that
    key is adopted, so a retry after a dropped connection downloads the result
    instead of rendering the shot again.
    """
    active: list[Mapping[str, Any]] = []
    finished: list[Mapping[str, Any]] = []
    for entry in jobs:
        if str(entry.get("dedupe_key") or "") != dedupe_key:
            continue
        state = str(entry.get("state") or "")
        if state in _ACTIVE_STATES:
            active.append(entry)
        elif state == "completed":
            finished.append(entry)
    chosen = active[0] if active else (finished[-1] if finished else None)
    return str((chosen or {}).get("job_id") or "")


def _find_remote_job(client: RemoteMaestroClient, dedupe_key: str) -> str:
    try:
        view = client.queue_view(include_terminal=True)
    except RemoteEngineError:
        return ""
    jobs = view.get("jobs")
    if not isinstance(jobs, list):
        return ""
    return _adoptable([job for job in jobs if isinstance(job, Mapping)], dedupe_key)


def _safe_report(context: Any, **kwargs: Any) -> None:
    """Forward progress, unless the job is already finished.

    A job cancelled from the interface becomes terminal immediately, and the
    queue refuses progress for a finished job.  Letting that raise would turn an
    acknowledged cancellation into a failure and bury the real reason.
    """
    try:
        context.report(**kwargs)
    except (queue.GenerationQueueError, FileNotFoundError):
        return


def _finished(context: Any) -> bool:
    """Has this job already ended (for example, cancelled while we waited)?"""
    try:
        state = str(queue.job(context.root, context.job_id).get("state") or "")
    except (FileNotFoundError, AttributeError):
        return True
    return state in _TERMINAL_STATES


def _forward_progress(context: Any, record: Mapping[str, Any]) -> None:
    """Forward the peer's own measurement; invent nothing.

    The queue derives a percentage from a step count when it is given one, so a
    source whose steps and percentage disagree would make this machine show a
    number it never measured.  The peer's own percentage wins in that case and the
    step numbers are left out, instead of letting two honest-looking figures fight
    it out on screen.
    """
    progress = record.get("progress")
    if not isinstance(progress, Mapping):
        return
    percent = progress.get("percent")
    step = progress.get("step")
    total_steps = progress.get("total_steps")
    note = str(progress.get("note") or "")
    if percent is None and step is None and not note:
        return
    if isinstance(percent, (int, float)) and isinstance(step, int) and isinstance(total_steps, int):
        if total_steps > 0 and round(100.0 * step / total_steps) != int(round(float(percent))):
            step = None
            total_steps = None
    _safe_report(
        context,
        percent=percent if isinstance(percent, (int, float)) else None,
        step=step if isinstance(step, int) else None,
        total_steps=total_steps if isinstance(total_steps, int) else None,
        note=note[:200],
    )


def _last_error(record: Mapping[str, Any]) -> str:
    errors = record.get("errors")
    if isinstance(errors, list) and errors:
        return str(errors[-1])[:400]
    return ""


def _stop_remote(
    client: RemoteMaestroClient,
    remote_id: str,
    *,
    poll_interval: float,
    sleep: Callable[[float], None],
    wait_seconds: float = 30.0,
) -> str:
    """Ask the peer to stop, then wait for it to really stop.  Returns its state."""
    try:
        client.cancel(remote_id, reason="the directing computer asked to stop this job")
    except RemoteEngineError as error:
        return f"could not be asked to stop ({error})"[:300]
    end = time.time() + float(wait_seconds)
    state = "unknown"
    while time.time() < end:
        try:
            state = str(client.job(remote_id).get("state") or "unknown")
        except RemoteEngineError:
            return state
        if state in _TERMINAL_STATES:
            return state
        sleep(max(0.05, float(poll_interval)))
    return state


def run_job(
    job: Mapping[str, Any],
    context: Any,
    *,
    peers_root: str,
    out_dir: str,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Run one job on another Maestro.  Returns the local files it produced."""
    peer_id = peer_id_from_job(job)
    if not peer_id:
        context.fail(
            "This job does not name the remote peer that should run it. A delegated job "
            f'carries engine "{ENGINE_PREFIX}:<peer_id>".',
            retryable=False,
        )
        return []

    try:
        peer = get_peer(peers_root, peer_id)
    except FileNotFoundError:
        context.fail(
            f"Remote peer {peer_id!r} is not configured on this computer, so the job was "
            "not sent anywhere.",
            retryable=False,
        )
        return []

    client = RemoteMaestroClient(
        str(peer.get("base_url") or ""),
        token=str(peer.get("token") or ""),
        timeout=timeout,
    )
    try:
        described = client.describe()
    except RemoteAuthError as error:
        context.fail(
            f"{error}. Set MAESTRO_ACCESS_TOKEN on that computer and store the same value "
            "as this peer's token.",
            retryable=False,
        )
        return []
    except RemoteEngineError as error:
        # A computer that is off is not a failure: keep the work.
        context.defer(f"{error}. The job stays queued until that computer answers.")
        return []

    params = dict(job.get("params") or {})
    capability = str(params.get("capability") or "").strip()
    if capability and capability not in described["capabilities"]:
        context.fail(
            f"{peer_id} does not report the capability {capability!r} this job needs. "
            "It reports: " + (", ".join(described["capabilities"]) or "nothing") + ".",
            retryable=False,
        )
        return []

    remember_probe(peers_root, peer_id, described)

    job_id = str(job.get("job_id") or "")
    dedupe_key = f"maestro:{job_id}"
    remote_id = _find_remote_job(client, dedupe_key)
    if remote_id:
        _safe_report(
            context,
            note=f"Reusing the job {remote_id} this delegation already created on {peer_id}.",
        )
    else:
        try:
            submitted = client.submit_job(remote_payload(job, peer, dedupe_key))
        except RemoteAuthError as error:
            context.fail(str(error), retryable=False)
            return []
        except RemoteEngineError as error:
            context.defer(f"{error}. The job was not sent; it stays queued.")
            return []
        remote = submitted.get("job")
        remote_id = str((remote if isinstance(remote, Mapping) else {}).get("job_id") or "")
        if not remote_id:
            raise RuntimeError(
                f"{peer_id} accepted the job but returned no job id: {submitted.get('reason') or ''}"
            )
        try:
            context.set_state("running")
        except (queue.GenerationQueueError, FileNotFoundError):
            pass
        _safe_report(context, note=f"Sent to {peer_id} as job {remote_id}.")

    context.link(remote_id)

    deadline = time.time() + float(poll_timeout)
    last_state = ""
    record: dict[str, Any] = {}
    while time.time() < deadline:
        if context.cancelled():
            state = _stop_remote(
                client, remote_id, poll_interval=poll_interval, sleep=sleep
            )
            note = f"Stop requested; {peer_id} reports job {remote_id} is {state}."
            _safe_report(context, note=note)
            if state in {"cancelled", "failed"}:
                # Only now is the cancellation a fact rather than a request.
                raise JobCancelled(note)
            if not _finished(context):
                context.fail(
                    f"{note} The render may be finishing on that computer; its result was not "
                    "recorded here.",
                    retryable=False,
                )
            return []
        try:
            record = client.job(remote_id)
        except RemoteAuthError as error:
            context.fail(str(error), retryable=False)
            return []
        except RemoteEngineError as error:
            context.defer(
                f"{error}. The work is kept on that computer and this job stays queued."
            )
            return []
        state = str(record.get("state") or "")
        if state != last_state:
            last_state = state
            _safe_report(context, note=f"{peer_id} reports its job is {state}")
        _forward_progress(context, record)
        if state == "completed":
            break
        if state == "failed":
            raise RuntimeError(
                f"{peer_id} failed job {remote_id}: {_last_error(record) or 'no reason reported'}"
            )
        if state == "cancelled":
            context.fail(
                f"{peer_id} cancelled job {remote_id}: "
                f"{_last_error(record) or 'no reason reported'}",
                retryable=False,
            )
            return []
        sleep(max(0.05, float(poll_interval)))
    else:
        context.defer(
            f"{peer_id} is still working on job {remote_id} after {int(poll_timeout)}s. "
            "The job stays queued and picks the result up when it is ready."
        )
        return []

    try:
        outputs = client.outputs(remote_id)
    except RemoteEngineError as error:
        context.defer(f"{error}. The finished result stays on that computer and is retried here.")
        return []
    present = [entry for entry in outputs if entry.get("present")]
    if not present:
        raise RuntimeError(
            f"{peer_id} completed job {remote_id} but reported no output file on disk"
        )

    files: list[str] = []
    for entry in present:
        destination = os.path.join(out_dir, _destination_name(job_id, entry.get("name")))
        try:
            result = client.download(
                remote_id,
                int(entry.get("index") or 0),
                destination,
                expected_bytes=int(entry.get("bytes") or 0),
            )
        except RemoteAuthError as error:
            context.fail(str(error), retryable=False)
            return []
        except RemoteEngineError as error:
            context.defer(
                f"The result finished on {peer_id} but the transfer was interrupted "
                f"({error}). The job stays queued and the download resumes where it stopped."
            )
            return []
        size = os.path.getsize(result["path"])
        if size <= 0:
            raise RuntimeError(f"{result['path']} arrived empty")
        files.append(result["path"])
        _safe_report(
            context,
            note=f"retrieved {os.path.basename(result['path'])} ({size} bytes) from {peer_id}",
        )

    return files


def register_remote_handlers(
    *,
    peers_root: str,
    out_dir: str,
    engines: Optional[Sequence[str]] = None,
    poll_timeout: float = DEFAULT_POLL_TIMEOUT,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    timeout: float = DEFAULT_TIMEOUT,
    replace: bool = True,
) -> list[str]:
    """Route ``node:<peer_id>`` jobs to the peer's own queue.

    ``replace`` defaults to True because Maestro re-registers at startup for
    whichever peers are configured then.
    """
    from . import queue_worker

    names = list(engines) if engines is not None else engines_for_peers(peers_root)

    def handler(job: Mapping[str, Any], context: Any) -> list[str]:
        return run_job(
            job,
            context,
            peers_root=peers_root,
            out_dir=out_dir,
            poll_timeout=poll_timeout,
            poll_interval=poll_interval,
            timeout=timeout,
        )

    registered: list[str] = []
    for engine in names:
        name = str(engine or "").strip()
        if not name:
            continue
        registered.append(queue_worker.register_handler(name, handler, replace=replace))
    return registered


def unregister_remote_handlers(engines: Sequence[str]) -> list[str]:
    """Forget the delegating runner for ``engines``; returns the ones removed."""
    from . import queue_worker

    return [name for name in engines if queue_worker.unregister_handler(name)]

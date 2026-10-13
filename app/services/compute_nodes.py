"""Compute node registry — the phone directs, another computer computes.

The studio separates *where a project is directed* from *where it is rendered*.
This module owns that separation: nodes announce themselves with real, measured
capabilities, keep themselves alive with a heartbeat, and jobs are assigned to a
node that can actually run them.  When a node goes away its jobs are not lost —
they are parked and handed back the moment it returns (spec §9.3).

Security posture (spec §9.3, §12):

* A node authenticates with a token that is generated once at registration, given
  to the caller exactly once, and stored **only as a SHA-256 hash**.  Heartbeats
  without the right token are rejected.
* Node ids are validated against a strict pattern and are never used as paths.
* State is written atomically, so a crash cannot truncate the registry.
* Nothing here opens a port, publishes a tunnel or talks to the internet.  It is
  the bookkeeping the application layer needs; exposing the API safely is the
  job of the existing ``services.remote_access`` (Tailscale Serve) plus
  authentication.

Honesty rules: a node is "online" only because its last heartbeat is recent
enough, and it can only be selected for a capability it declared.  Nothing is
inferred from a node's name or hardware summary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from typing import Any, Iterable, Mapping, Optional, Sequence

from .engine_providers import CAPABILITIES, EngineProviderError

__all__ = [
    "DEFAULT_OFFLINE_AFTER",
    "NODE_STATES",
    "ComputeNodeError",
    "assign_job",
    "get_node",
    "heartbeat",
    "jobs_for_node",
    "list_nodes",
    "nodes_with_capability",
    "orphaned_jobs",
    "recover_node",
    "register_node",
    "release_job",
    "remove_node",
    "select_node",
    "summary",
    "update_node",
]

# A node is considered online when it has been heard from within this many
# seconds.  Overridable per call; stored nowhere, so no persisted value can go
# stale.
DEFAULT_OFFLINE_AFTER = 90.0

NODE_STATES = ("online", "offline")

# Assignment states.  ``waiting_node`` is the one that matters: the job has been
# accepted and is intentionally parked because its node is unreachable.
ASSIGNMENT_STATES = ("pending", "waiting_node", "running", "completed", "failed", "cancelled")

_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VISIBLE_TEXT_LIMIT = 400
_MAX_CAPABILITIES = 64


class ComputeNodeError(ValueError):
    """Invalid node registration, bad token, or an unusable node id."""


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _registry_path(root: str) -> str:
    return os.path.join(root, "compute_nodes.json")


def _atomic_write_json(path: str, payload: Mapping[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".compute_nodes.", suffix=".tmp", dir=directory)
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


def _empty_registry() -> dict[str, Any]:
    return {"version": 1, "nodes": {}, "assignments": {}}


def _load(root: str) -> dict[str, Any]:
    path = _registry_path(root)
    if not os.path.isfile(path):
        return _empty_registry()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        # A corrupt registry must not take the studio down.  Start clean; the
        # nodes re-register on their next heartbeat.
        return _empty_registry()
    if not isinstance(raw, dict):
        return _empty_registry()
    state = _empty_registry()
    nodes = raw.get("nodes")
    if isinstance(nodes, dict):
        state["nodes"] = {str(k): v for k, v in nodes.items() if isinstance(v, dict)}
    assignments = raw.get("assignments")
    if isinstance(assignments, dict):
        state["assignments"] = {
            str(k): v for k, v in assignments.items() if isinstance(v, dict)
        }
    return state


def _save(root: str, state: Mapping[str, Any]) -> None:
    _atomic_write_json(_registry_path(root), state)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_node_id(value: Any) -> str:
    text = str(value or "").strip()
    if not _NODE_ID_RE.match(text):
        raise ComputeNodeError(
            "Node id must be 1-64 characters of letters, digits, dot, dash or underscore"
        )
    return text


def _text(value: Any, limit: int = _VISIBLE_TEXT_LIMIT) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def _capabilities(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw: Iterable[Any] = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        raw = value
    else:
        raise ComputeNodeError("capabilities must be a list of capability names")
    seen: list[str] = []
    for item in raw:
        name = str(item).strip()
        if not name:
            continue
        if name not in CAPABILITIES:
            raise ComputeNodeError(
                f"Unknown capability {name!r}. Known capabilities: " + ", ".join(CAPABILITIES)
            )
        if name not in seen:
            seen.append(name)
    if len(seen) > _MAX_CAPABILITIES:
        raise ComputeNodeError("Too many capabilities declared")
    return seen


def _string_list(value: Any, limit: int = 200) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value[:_VISIBLE_TEXT_LIMIT]]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()[:_VISIBLE_TEXT_LIMIT]
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _mapping(value: Any, limit: int = 40) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, Any] = {}
    for key, item in list(value.items())[:limit]:
        name = str(key)[:64]
        if isinstance(item, (bool, int, float)) or item is None:
            out[name] = item
        elif isinstance(item, str):
            out[name] = item[:_VISIBLE_TEXT_LIMIT]
        elif isinstance(item, Mapping):
            out[name] = {
                str(k)[:64]: (v if isinstance(v, (bool, int, float)) or v is None else str(v)[:200])
                for k, v in list(item.items())[:40]
            }
    return out


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now(now: Optional[float]) -> float:
    return float(now) if now is not None else time.time()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _decorate(node: Mapping[str, Any], now: float, offline_after: float) -> dict[str, Any]:
    last_seen = node.get("last_seen")
    try:
        last_seen_value = float(last_seen) if last_seen is not None else None
    except (TypeError, ValueError):
        last_seen_value = None
    age = None if last_seen_value is None else max(0.0, now - last_seen_value)
    online = age is not None and age <= offline_after
    declared = _string_list(node.get("capabilities"))
    local = bool(node.get("local", False))
    return {
        "node_id": node.get("node_id"),
        "label": node.get("label") or node.get("node_id"),
        "kind": node.get("kind") or ("local" if local else "remote"),
        "local": local,
        "state": "online" if online else "offline",
        "online": online,
        "last_seen": last_seen_value,
        "seconds_since_seen": None if age is None else round(age, 1),
        "offline_after_seconds": offline_after,
        "capabilities": declared,
        # Remotely declared capabilities are of unknown quality; we report the
        # hardware the node itself measured rather than inventing a verdict.
        "hardware": dict(node.get("hardware") or {}),
        "storage": dict(node.get("storage") or {}),
        "models": _string_list(node.get("models")),
        "max_concurrent_jobs": int(node.get("max_concurrent_jobs") or 1),
        "notes": node.get("notes") or "",
        "address": node.get("address") or "",
        "created_at": node.get("created_at"),
    }


def list_nodes(
    root: str,
    *,
    now: Optional[float] = None,
    offline_after: float = DEFAULT_OFFLINE_AFTER,
) -> list[dict[str, Any]]:
    state = _load(root)
    stamp = _now(now)
    nodes = [
        _decorate(node, stamp, offline_after)
        for node in state["nodes"].values()
        if isinstance(node, Mapping)
    ]
    # Local node first, then online before offline, then by id.
    nodes.sort(key=lambda n: (not n["local"], not n["online"], str(n["node_id"])))
    return nodes


def get_node(
    root: str,
    node_id: str,
    *,
    now: Optional[float] = None,
    offline_after: float = DEFAULT_OFFLINE_AFTER,
) -> dict[str, Any]:
    wanted = _require_node_id(node_id)
    state = _load(root)
    node = state["nodes"].get(wanted)
    if not isinstance(node, Mapping):
        raise FileNotFoundError(f"Compute node {wanted!r} is not registered")
    return _decorate(node, _now(now), offline_after)


def nodes_with_capability(
    root: str,
    capability: str,
    *,
    now: Optional[float] = None,
    offline_after: float = DEFAULT_OFFLINE_AFTER,
    online_only: bool = True,
) -> list[dict[str, Any]]:
    if capability not in CAPABILITIES:
        raise ComputeNodeError(
            f"Unknown capability {capability!r}. Known: " + ", ".join(CAPABILITIES)
        )
    return [
        node
        for node in list_nodes(root, now=now, offline_after=offline_after)
        if capability in node["capabilities"] and (node["online"] or not online_only)
    ]


def select_node(
    root: str,
    capability: str,
    *,
    now: Optional[float] = None,
    offline_after: float = DEFAULT_OFFLINE_AFTER,
    exclude: Sequence[str] = (),
) -> dict[str, Any]:
    """Pick the node that should run ``capability``.

    Local node wins (it has no network hop and no extra latency), then online
    remotes ordered by id.  A node already at its concurrency limit is skipped so
    we do not pile work onto a busy device.
    """
    candidates = nodes_with_capability(
        root, capability, now=now, offline_after=offline_after, online_only=True
    )
    counts = _assignment_counts(root)
    skipped: list[str] = []
    for node in candidates:
        node_id = str(node["node_id"])
        if node_id in set(exclude):
            continue
        limit = max(1, int(node.get("max_concurrent_jobs") or 1))
        active = counts.get(node_id, 0)
        if active >= limit:
            skipped.append(node_id)
            continue
        result = dict(node)
        result["active_assignments"] = active
        result["capacity_remaining"] = max(0, limit - active)
        result["busy_nodes_skipped"] = skipped
        return result
    raise EngineProviderError(
        f"No online compute node can run {capability!r}"
        + (f" (skipped at capacity: {', '.join(skipped)})" if skipped else "")
    )


def _assignment_counts(root: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for assignment in _load(root)["assignments"].values():
        if assignment.get("state") in {"pending", "running"}:
            node_id = str(assignment.get("node_id") or "")
            counts[node_id] = counts.get(node_id, 0) + 1
    return counts


def jobs_for_node(
    root: str,
    node_id: str,
    *,
    include_terminal: bool = False,
) -> list[dict[str, Any]]:
    wanted = _require_node_id(node_id)
    out = []
    for job_id, assignment in _load(root)["assignments"].items():
        if str(assignment.get("node_id")) != wanted:
            continue
        state = str(assignment.get("state") or "pending")
        if not include_terminal and state in {"completed", "failed", "cancelled"}:
            continue
        out.append({"job_id": job_id, **dict(assignment)})
    out.sort(key=lambda item: (str(item.get("created_at") or ""), str(item["job_id"])))
    return out


def orphaned_jobs(root: str, *, now: Optional[float] = None) -> list[dict[str, Any]]:
    """Assignments whose node is offline — the queue waiting to come back."""
    state = _load(root)
    stamp = _now(now)
    offline: set[str] = set()
    for node in state["nodes"].values():
        if not isinstance(node, Mapping):
            continue
        last_seen = node.get("last_seen")
        try:
            age = stamp - float(last_seen) if last_seen is not None else None
        except (TypeError, ValueError):
            age = None
        if age is None or age > DEFAULT_OFFLINE_AFTER:
            offline.add(str(node.get("node_id")))
    out = []
    for job_id, assignment in state["assignments"].items():
        if str(assignment.get("node_id")) in offline and assignment.get("state") in {
            "pending",
            "waiting_node",
            "running",
        }:
            out.append({"job_id": job_id, **dict(assignment)})
    out.sort(key=lambda item: str(item["job_id"]))
    return out


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def register_node(
    root: str,
    node: Mapping[str, Any],
    *,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Register (or re-register) a node and return it with a fresh token.

    The token is returned **exactly once**; only its hash is persisted.  Calling
    this again for an existing node rotates the token, which is the recovery path
    if a node loses its credentials.
    """
    if not isinstance(node, Mapping):
        raise ComputeNodeError("A node registration must be an object")
    node_id = _require_node_id(node.get("node_id"))
    state = _load(root)
    stamp = _now(now)
    token = secrets.token_urlsafe(32)
    existing = state["nodes"].get(node_id)
    # ``created_at`` is a float, and 0.0 is a legitimate epoch — test for None,
    # not truthiness, or a node registered at t=0 would look brand new.
    created_at = stamp
    if isinstance(existing, Mapping) and existing.get("created_at") is not None:
        created_at = existing["created_at"]
    record = {
        "node_id": node_id,
        "label": _text(node.get("label")) or node_id,
        "kind": _text(node.get("kind"), 32) or ("local" if node.get("local") else "remote"),
        "local": bool(node.get("local", False)),
        "capabilities": _capabilities(node.get("capabilities")),
        "models": _string_list(node.get("models")),
        "hardware": _mapping(node.get("hardware")),
        "storage": _mapping(node.get("storage")),
        "address": _text(node.get("address"), 300),
        "notes": _text(node.get("notes")),
        "max_concurrent_jobs": max(1, min(64, int(node.get("max_concurrent_jobs") or 1))),
        "token_hash": _hash_token(token),
        "created_at": created_at,
        "last_seen": stamp,
        "last_stats": _mapping(node.get("stats")),
    }
    state["nodes"][node_id] = record
    _save(root, state)
    described = _decorate(record, stamp, DEFAULT_OFFLINE_AFTER)
    described["token"] = token
    described["token_notice"] = (
        "Store this token now; only its hash is kept and it is never shown again."
    )
    return described


def heartbeat(
    root: str,
    node_id: str,
    token: str,
    *,
    stats: Optional[Mapping[str, Any]] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Record that a node is alive.  Requires the registration token."""
    wanted = _require_node_id(node_id)
    state = _load(root)
    node = state["nodes"].get(wanted)
    if not isinstance(node, Mapping):
        raise FileNotFoundError(f"Compute node {wanted!r} is not registered")
    expected = str(node.get("token_hash") or "")
    if not expected or not secrets.compare_digest(expected, _hash_token(str(token or ""))):
        raise PermissionError(f"Invalid token for compute node {wanted!r}")
    stamp = _now(now)
    updated = dict(node)
    updated["last_seen"] = stamp
    if stats is not None:
        updated["last_stats"] = _mapping(stats)
        # A node may correct its own concurrency limit and the models it now has.
        if "max_concurrent_jobs" in (stats or {}):
            try:
                updated["max_concurrent_jobs"] = max(
                    1, min(64, int(stats["max_concurrent_jobs"]))
                )
            except (TypeError, ValueError):
                pass
        if "models" in (stats or {}):
            updated["models"] = _string_list(stats.get("models"))
        if "hardware" in (stats or {}):
            updated["hardware"] = _mapping(stats.get("hardware"))
        if "storage" in (stats or {}):
            updated["storage"] = _mapping(stats.get("storage"))
    state["nodes"][wanted] = updated
    _save(root, state)
    return _decorate(updated, stamp, DEFAULT_OFFLINE_AFTER)


def update_node(root: str, node_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
    """Update a node's declared capabilities/labels from the control surface."""
    wanted = _require_node_id(node_id)
    if not isinstance(patch, Mapping):
        raise ComputeNodeError("A node update must be an object")
    state = _load(root)
    node = state["nodes"].get(wanted)
    if not isinstance(node, Mapping):
        raise FileNotFoundError(f"Compute node {wanted!r} is not registered")
    updated = dict(node)
    if "capabilities" in patch:
        updated["capabilities"] = _capabilities(patch.get("capabilities"))
    if "models" in patch:
        updated["models"] = _string_list(patch.get("models"))
    if "label" in patch:
        updated["label"] = _text(patch.get("label")) or wanted
    if "notes" in patch:
        updated["notes"] = _text(patch.get("notes"))
    if "address" in patch:
        updated["address"] = _text(patch.get("address"), 300)
    if "max_concurrent_jobs" in patch:
        updated["max_concurrent_jobs"] = max(
            1, min(64, int(patch.get("max_concurrent_jobs") or 1))
        )
    if "local" in patch:
        updated["local"] = bool(patch.get("local"))
    state["nodes"][wanted] = updated
    _save(root, state)
    return _decorate(updated, time.time(), DEFAULT_OFFLINE_AFTER)


def remove_node(root: str, node_id: str) -> dict[str, Any]:
    """Unregister a node.  Its pending jobs become waiting assignments."""
    wanted = _require_node_id(node_id)
    state = _load(root)
    node = state["nodes"].pop(wanted, None)
    if node is None:
        raise FileNotFoundError(f"Compute node {wanted!r} is not registered")
    parked = 0
    for job_id, assignment in list(state["assignments"].items()):
        if str(assignment.get("node_id")) == wanted and assignment.get("state") in {
            "pending",
            "running",
        }:
            state["assignments"][job_id] = {**dict(assignment), "state": "waiting_node"}
            parked += 1
    _save(root, state)
    return {"removed": wanted, "parked_jobs": parked}


def assign_job(
    root: str,
    job_id: str,
    node_id: str,
    *,
    capability: str = "",
    task: str = "",
    params: Optional[Mapping[str, Any]] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Bind a queued job to a node, or park it if that node is offline."""
    identifier = str(job_id or "").strip()
    if not identifier:
        raise ComputeNodeError("A job id is required")
    if capability and capability not in CAPABILITIES:
        raise ComputeNodeError(f"Unknown capability {capability!r}")
    state = _load(root)
    wanted = _require_node_id(node_id)
    node = state["nodes"].get(wanted)
    if not isinstance(node, Mapping):
        raise FileNotFoundError(f"Compute node {wanted!r} is not registered")
    stamp = _now(now)
    last_seen = node.get("last_seen")
    try:
        online = last_seen is not None and (stamp - float(last_seen)) <= DEFAULT_OFFLINE_AFTER
    except (TypeError, ValueError):
        online = False
    if capability and capability not in _string_list(node.get("capabilities")):
        raise ComputeNodeError(
            f"Node {wanted!r} did not declare {capability!r}; it declared: "
            + (", ".join(_string_list(node.get("capabilities"))) or "nothing")
        )
    assignment = {
        "job_id": identifier,
        "node_id": wanted,
        "capability": capability,
        "task": _text(task, 64),
        "params": _mapping(params),
        "state": "pending" if online else "waiting_node",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
        "updated_at": stamp,
        "attempts": int((state["assignments"].get(identifier) or {}).get("attempts") or 0),
    }
    state["assignments"][identifier] = assignment
    _save(root, state)
    return dict(assignment)


def release_job(root: str, job_id: str, *, outcome: str = "completed", error: str = "") -> dict[str, Any]:
    """Mark an assignment terminal (or return it to the parked queue)."""
    identifier = str(job_id or "").strip()
    if not identifier:
        raise ComputeNodeError("A job id is required")
    if outcome not in ASSIGNMENT_STATES:
        raise ComputeNodeError(
            f"Unknown outcome {outcome!r}. Known: " + ", ".join(ASSIGNMENT_STATES)
        )
    state = _load(root)
    assignment = state["assignments"].get(identifier)
    if not isinstance(assignment, Mapping):
        raise FileNotFoundError(f"Job {identifier!r} is not assigned to any node")
    updated = {
        **dict(assignment),
        "state": outcome,
        "error": _text(error, 2000),
        "updated_at": time.time(),
    }
    state["assignments"][identifier] = updated
    _save(root, state)
    return dict(updated)


def recover_node(
    root: str,
    node_id: str,
    *,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Hand back the work that was parked while this node was away.

    Returns the jobs the caller should resubmit to its own queue.  Terminal
    assignments are never resurrected, and a job that already failed is not
    silently retried without incrementing its attempt counter.
    """
    wanted = _require_node_id(node_id)
    state = _load(root)
    node = state["nodes"].get(wanted)
    if not isinstance(node, Mapping):
        raise FileNotFoundError(f"Compute node {wanted!r} is not registered")
    stamp = _now(now)
    resumed: list[dict[str, Any]] = []
    for job_id, assignment in list(state["assignments"].items()):
        if str(assignment.get("node_id")) != wanted:
            continue
        if assignment.get("state") not in {"waiting_node", "running"}:
            continue
        updated = {
            **dict(assignment),
            "state": "pending",
            "attempts": int(assignment.get("attempts") or 0) + 1,
            "updated_at": stamp,
        }
        state["assignments"][job_id] = updated
        resumed.append(dict(updated))
    _save(root, state)
    return {
        "node_id": wanted,
        "resumed": resumed,
        "count": len(resumed),
        "detail": "These jobs were parked while the node was unreachable; resubmit them.",
    }


def summary(root: str, *, now: Optional[float] = None) -> dict[str, Any]:
    """Compact status for the control surface / mobile header."""
    nodes = list_nodes(root, now=now)
    assignments = _load(root)["assignments"]
    active = sum(
        1 for a in assignments.values() if a.get("state") in {"pending", "running"}
    )
    waiting = sum(1 for a in assignments.values() if a.get("state") == "waiting_node")
    online = [node for node in nodes if node["online"]]
    return {
        "nodes": nodes,
        "node_count": len(nodes),
        "online_count": len(online),
        "local_online": any(node["local"] and node["online"] for node in nodes),
        "active_assignments": active,
        "waiting_for_node": waiting,
        "capabilities_online": sorted(
            {capability for node in online for capability in node["capabilities"]}
        ),
    }

"""ComfyUI adapter — a real client for ComfyUI's local HTTP API.

ComfyUI exposes a small, stable HTTP surface.  This module speaks it properly
instead of assuming anything about a workflow:

============================  ===============================================
``GET  /system_stats``        connection + device check
``GET  /object_info``         which node classes this server actually has
``POST /prompt``              queue a workflow, returns ``prompt_id``
``GET  /history/{id}``        finished outputs for a prompt
``GET  /queue``               what is running / pending right now
``POST /interrupt``           stop the currently running prompt
``POST /queue {"delete":[]}`` remove a queued but not-yet-started prompt
============================  ===============================================

Two rules from the spec shape the design:

1. **"No supongas que todos los workflows usan las mismas entradas o salidas."**
   A workflow is a *template* with a manifest that declares, explicitly, which
   semantic inputs map to which graph node field and which outputs it produces.
   :func:`bind_template` substitutes by that declaration and refuses parameters
   the manifest does not declare.  Nothing is guessed from node titles.

2. **"No declares una integración completada hasta ejecutar una prueba real."**
   :func:`template_supported` does not trust the manifest: it checks every node
   class in the graph against the server's own ``/object_info``.  A template
   whose node classes are missing from the connected server is reported as
   unsupported, and :meth:`ComfyUIAdapter.describe` only advertises capabilities
   whose templates are confirmed present.

The module is dependency-free (stdlib ``urllib``), so it is importable and
testable on a machine with no GPU, no torch and no ComfyUI — the tests drive it
against a local stub server implementing the same protocol.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Mapping, Optional, Sequence
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlparse

__all__ = [
    "ComfyUIError",
    "ComfyUIClient",
    "ComfyUIAdapter",
    "WorkflowTemplate",
    "load_template",
    "list_templates",
    "bind_template",
    "template_supported",
    "validate_base_url",
    "probe_server",
]

DEFAULT_TIMEOUT = 10.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
TEMPLATE_MANIFEST = "manifest.json"
TEMPLATE_GRAPH = "workflow.json"


class ComfyUIError(RuntimeError):
    """A ComfyUI request failed, or a workflow could not be used as declared."""


# ---------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------


def validate_base_url(base_url: str, *, allow_private: bool = True) -> str:
    """Normalise a ComfyUI base URL.

    ComfyUI is normally reached on the local network, so private addresses are
    allowed by default — but the scheme is not negotiable, and a URL carrying
    credentials is rejected rather than silently used.
    """
    text = str(base_url or "").strip().rstrip("/")
    if not text:
        raise ComfyUIError("A ComfyUI base URL is required")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        raise ComfyUIError("ComfyUI base URL must use http:// or https://")
    if not parsed.hostname:
        raise ComfyUIError("ComfyUI base URL has no host")
    if parsed.username or parsed.password:
        raise ComfyUIError("ComfyUI base URL must not embed credentials")
    return text


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


class ComfyUIClient:
    """Minimal, honest HTTP client.  Every failure is surfaced, never swallowed."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client_id: Optional[str] = None,
    ) -> None:
        self.base_url = validate_base_url(base_url)
        self.timeout = float(timeout)
        self.client_id = client_id or uuid.uuid4().hex

    # -- low level --------------------------------------------------------

    def _call(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Optional[Mapping[str, Any]] = None,
        accept_statuses: Sequence[int] = (),
    ) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urlrequest.Request(url, data=data, headers=headers, method=method)
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except urlerror.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read(MAX_RESPONSE_BYTES)
            except Exception:  # pragma: no cover - defensive
                raw = b""
            detail = raw.decode("utf-8", errors="replace")[:600] if raw else ""
            if exc.code in accept_statuses:
                # ComfyUI answers a rejected graph with 400 plus node_errors;
                # the caller needs the body, not a bare status code.
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError:
                    raise ComfyUIError(
                        f"ComfyUI {method} {path} returned HTTP {exc.code}: {detail}"
                    ) from exc
            raise ComfyUIError(
                f"ComfyUI {method} {path} returned HTTP {exc.code}"
                + (f": {detail}" if detail else "")
            ) from exc
        except urlerror.URLError as exc:
            raise ComfyUIError(f"Cannot reach ComfyUI at {self.base_url}: {exc.reason}") from exc
        except OSError as exc:
            raise ComfyUIError(f"Cannot reach ComfyUI at {self.base_url}: {exc}") from exc

        if len(body) > MAX_RESPONSE_BYTES:
            raise ComfyUIError("ComfyUI response was larger than the adapter will read")
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError as exc:
            raise ComfyUIError(f"ComfyUI returned a non-JSON response for {path}") from exc

    # -- documented endpoints --------------------------------------------

    def check_connection(self) -> dict[str, Any]:
        """``GET /system_stats`` — proves the server is a reachable ComfyUI."""
        stats = self._call("/system_stats")
        if not isinstance(stats, Mapping):
            raise ComfyUIError("ComfyUI /system_stats did not return an object")
        devices = stats.get("devices")
        device_list = list(devices) if isinstance(devices, list) else []
        return {
            "reachable": True,
            "base_url": self.base_url,
            "comfyui_version": (stats.get("system") or {}).get("comfyui_version")
            if isinstance(stats.get("system"), Mapping)
            else None,
            "python_version": (stats.get("system") or {}).get("python_version")
            if isinstance(stats.get("system"), Mapping)
            else None,
            "devices": [
                {
                    "name": str(d.get("name") or ""),
                    "type": str(d.get("type") or ""),
                    "vram_total": d.get("vram_total"),
                    "vram_free": d.get("vram_free"),
                }
                for d in device_list
                if isinstance(d, Mapping)
            ],
        }

    def object_info(self) -> dict[str, Any]:
        """``GET /object_info`` — the node classes this server really has."""
        info = self._call("/object_info")
        if not isinstance(info, Mapping):
            raise ComfyUIError("ComfyUI /object_info did not return an object")
        return dict(info)

    def submit(
        self,
        workflow: Mapping[str, Any],
        *,
        extra_data: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """``POST /prompt`` — queue a workflow and return its ``prompt_id``."""
        if not isinstance(workflow, Mapping) or not workflow:
            raise ComfyUIError("A non-empty workflow graph is required")
        payload: dict[str, Any] = {"prompt": dict(workflow), "client_id": self.client_id}
        if extra_data:
            payload["extra_data"] = dict(extra_data)
        response = self._call(
            "/prompt", method="POST", payload=payload, accept_statuses=(400,)
        )
        if not isinstance(response, Mapping):
            raise ComfyUIError("ComfyUI /prompt did not return an object")
        prompt_id = response.get("prompt_id")
        if not prompt_id:
            # ComfyUI reports a rejected graph as node_errors; keep them verbatim
            # so the caller can see exactly which node the server disliked.
            node_errors = response.get("node_errors")
            raise ComfyUIError(
                "ComfyUI rejected the workflow"
                + (f": {json.dumps(node_errors)[:600]}" if node_errors else "")
            )
        return {
            "prompt_id": str(prompt_id),
            "number": response.get("number"),
            "node_errors": response.get("node_errors") or {},
        }

    def history(self, prompt_id: str) -> dict[str, Any]:
        """``GET /history/{id}`` — outputs for a finished prompt (or empty)."""
        data = self._call(f"/history/{str(prompt_id)}")
        if not isinstance(data, Mapping):
            raise ComfyUIError("ComfyUI /history did not return an object")
        entry = data.get(str(prompt_id))
        return dict(entry) if isinstance(entry, Mapping) else {}

    def queue(self) -> dict[str, Any]:
        """``GET /queue`` — running and pending prompts."""
        data = self._call("/queue")
        if not isinstance(data, Mapping):
            raise ComfyUIError("ComfyUI /queue did not return an object")
        running = data.get("queue_running")
        pending = data.get("queue_pending")
        return {
            "running": list(running) if isinstance(running, list) else [],
            "pending": list(pending) if isinstance(pending, list) else [],
        }

    def interrupt(self) -> dict[str, Any]:
        """``POST /interrupt`` — stop whatever is executing now."""
        self._call("/interrupt", method="POST", payload={})
        return {"interrupted": True}

    def delete_queued(self, prompt_id: str) -> dict[str, Any]:
        """``POST /queue {"delete": [id]}`` — drop a prompt that has not started."""
        self._call("/queue", method="POST", payload={"delete": [str(prompt_id)]})
        return {"deleted": str(prompt_id)}


# ---------------------------------------------------------------------------
# Workflow templates
# ---------------------------------------------------------------------------


class WorkflowTemplate:
    """A versioned workflow plus the manifest that declares its interface."""

    def __init__(self, directory: str, manifest: Mapping[str, Any], graph: Mapping[str, Any]) -> None:
        self.directory = directory
        self.manifest = dict(manifest)
        self.graph = dict(graph)

    @property
    def template_id(self) -> str:
        return str(self.manifest.get("template_id") or "")

    @property
    def version(self) -> str:
        return str(self.manifest.get("version") or "0")

    @property
    def label(self) -> str:
        return str(self.manifest.get("label") or self.template_id)

    @property
    def capabilities(self) -> tuple[str, ...]:
        raw = self.manifest.get("capabilities")
        return tuple(str(item) for item in raw) if isinstance(raw, (list, tuple)) else ()

    @property
    def inputs(self) -> dict[str, Any]:
        raw = self.manifest.get("inputs")
        return {str(k): dict(v) for k, v in raw.items()} if isinstance(raw, Mapping) else {}

    @property
    def outputs(self) -> dict[str, Any]:
        raw = self.manifest.get("outputs")
        return {str(k): dict(v) for k, v in raw.items()} if isinstance(raw, Mapping) else {}

    def node_classes(self) -> list[str]:
        return sorted(
            {
                str(node.get("class_type"))
                for node in self.graph.values()
                if isinstance(node, Mapping) and node.get("class_type")
            }
        )

    def describe(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "version": self.version,
            "label": self.label,
            "capabilities": list(self.capabilities),
            "inputs": self.inputs,
            "outputs": self.outputs,
            "node_classes": self.node_classes(),
            "notes": str(self.manifest.get("notes") or ""),
        }


def _read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise ComfyUIError(f"Missing ComfyUI template file: {path}") from None
    except ValueError as exc:
        raise ComfyUIError(f"Invalid JSON in {path}: {exc}") from exc


def load_template(templates_dir: str, template_id: str) -> WorkflowTemplate:
    identifier = str(template_id or "").strip()
    if not identifier or "/" in identifier or "\\" in identifier or identifier.startswith("."):
        raise ComfyUIError(f"Invalid template id {template_id!r}")
    directory = os.path.join(templates_dir, identifier)
    if not os.path.isdir(directory):
        raise ComfyUIError(f"Unknown ComfyUI template {identifier!r}")
    manifest = _read_json(os.path.join(directory, TEMPLATE_MANIFEST))
    graph = _read_json(os.path.join(directory, TEMPLATE_GRAPH))
    if not isinstance(manifest, Mapping):
        raise ComfyUIError(f"Template {identifier!r}: manifest must be an object")
    if not isinstance(graph, Mapping) or not graph:
        raise ComfyUIError(f"Template {identifier!r}: workflow must be a non-empty object")
    template = WorkflowTemplate(directory, manifest, graph)
    if template.template_id and template.template_id != identifier:
        raise ComfyUIError(
            f"Template {identifier!r} declares a different id: {template.template_id!r}"
        )
    return template


def list_templates(templates_dir: str) -> list[WorkflowTemplate]:
    if not os.path.isdir(templates_dir):
        return []
    templates = []
    for name in sorted(os.listdir(templates_dir)):
        if name.startswith("."):
            continue
        if not os.path.isdir(os.path.join(templates_dir, name)):
            continue
        try:
            templates.append(load_template(templates_dir, name))
        except ComfyUIError:
            # A broken template is skipped here and reported by the health check;
            # it must not stop the server from being usable.
            continue
    return templates


def template_supported(template: WorkflowTemplate, object_info: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Does the connected server actually have this template's node classes?"""
    known = set(str(k) for k in object_info)
    missing = [cls for cls in template.node_classes() if cls not in known]
    return (not missing, missing)


def bind_template(
    template: WorkflowTemplate,
    params: Mapping[str, Any],
) -> dict[str, Any]:
    """Substitute declared inputs into the graph.

    Parameters the manifest does not declare are rejected: silently ignoring them
    would make a request look successful while the workflow ran with defaults.
    """
    if not isinstance(params, Mapping):
        raise ComfyUIError("Workflow parameters must be an object")
    declarations = template.inputs
    unknown = [key for key in params if key not in declarations]
    if unknown:
        raise ComfyUIError(
            f"Template {template.template_id!r} does not declare input(s): "
            + ", ".join(sorted(unknown))
            + (". Declared: " + ", ".join(sorted(declarations)) if declarations else "")
        )
    graph: dict[str, Any] = json.loads(json.dumps(template.graph))
    for name, spec in declarations.items():
        required = bool(spec.get("required", False))
        if name in params:
            value = params[name]
        elif "default" in spec:
            value = spec["default"]
        elif required:
            raise ComfyUIError(
                f"Template {template.template_id!r} requires input {name!r}"
            )
        else:
            continue
        node_id = str(spec.get("node") or "")
        field = spec.get("field")
        if not node_id or field is None:
            raise ComfyUIError(
                f"Template {template.template_id!r} input {name!r} has no node/field binding"
            )
        node = graph.get(node_id)
        if not isinstance(node, Mapping):
            raise ComfyUIError(
                f"Template {template.template_id!r} input {name!r} references node {node_id!r}, "
                "which is not in the graph"
            )
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            raise ComfyUIError(
                f"Template {template.template_id!r} node {node_id!r} has no inputs object"
            )
        inputs[str(field)] = value
    return graph


def _submit_order(graph: Mapping[str, Any]) -> list[str]:
    """Topologically sort node ids so a graph is sent in a dependency order."""
    incoming: dict[str, set[str]] = {str(k): set() for k in graph}
    for node_id, node in graph.items():
        if not isinstance(node, Mapping):
            continue
        for value in (node.get("inputs") or {}).values():
            if (
                isinstance(value, list)
                and len(value) == 2
                and isinstance(value[0], str)
                and value[0] in incoming
            ):
                incoming[str(node_id)].add(value[0])
    order: list[str] = []
    ready = [nid for nid, deps in incoming.items() if not deps]
    ready.sort()
    while ready:
        current = ready.pop(0)
        order.append(current)
        for nid, deps in incoming.items():
            if current in deps:
                deps.discard(current)
                if not deps and nid not in order and nid not in ready:
                    ready.append(nid)
        ready.sort()
    if len(order) != len(incoming):
        raise ComfyUIError("Workflow graph has a cycle; refusing to submit it")
    return order


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ComfyUIAdapter:
    """Ties a client to a template library and exposes capability-level calls."""

    def __init__(
        self,
        base_url: str,
        *,
        templates_dir: str,
        timeout: float = DEFAULT_TIMEOUT,
        client_id: Optional[str] = None,
        label: str = "",
    ) -> None:
        self.client = ComfyUIClient(base_url, timeout=timeout, client_id=client_id)
        self.templates_dir = templates_dir
        self.label = label or f"ComfyUI at {self.client.base_url}"

    # -- capability reporting --------------------------------------------

    def describe(self) -> dict[str, Any]:
        """Probe the server and report only what it can really do."""
        health = self.client.check_connection()
        object_info = self.client.object_info()
        supported: list[dict[str, Any]] = []
        unsupported: list[dict[str, Any]] = []
        capabilities: list[str] = []
        for template in list_templates(self.templates_dir):
            ok, missing = template_supported(template, object_info)
            entry = {
                "template_id": template.template_id,
                "version": template.version,
                "label": template.label,
                "capabilities": list(template.capabilities),
                "missing_node_classes": missing,
            }
            if ok:
                supported.append(entry)
                for capability in template.capabilities:
                    if capability not in capabilities:
                        capabilities.append(capability)
            else:
                unsupported.append(entry)
        return {
            "reachable": True,
            "base_url": self.client.base_url,
            "label": self.label,
            "comfyui_version": health.get("comfyui_version"),
            "devices": health.get("devices") or [],
            "object_info_nodes": len(object_info),
            "models": self.model_names(object_info),
            "capabilities": capabilities,
            "templates": supported,
            "unsupported_templates": unsupported,
            "notes": (
                "Capabilities are listed only for templates whose node classes are "
                "present on this server."
            ),
        }

    def capabilities(self) -> list[str]:
        return list(self.describe()["capabilities"])

    def model_names(self, object_info: Optional[Mapping[str, Any]] = None) -> list[str]:
        """Checkpoint names the server exposes, when it reports them."""
        info = dict(object_info) if object_info is not None else self.client.object_info()
        loader = info.get("CheckpointLoaderSimple")
        if not isinstance(loader, Mapping):
            return []
        required = loader.get("input", {}).get("required", {}) if isinstance(loader.get("input"), Mapping) else {}
        names = required.get("ckpt_name") if isinstance(required, Mapping) else None
        if isinstance(names, list) and names and isinstance(names[0], list):
            return [str(item) for item in names[0]]
        return []

    # -- running work -----------------------------------------------------

    def submit(
        self,
        template_id: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        template = load_template(self.templates_dir, template_id)
        graph = bind_template(template, params)
        order = _submit_order(graph)
        ordered_graph = {node_id: graph[node_id] for node_id in order}
        result = self.client.submit(
            ordered_graph,
            extra_data={"maestro_template": template.template_id, "maestro_version": template.version},
        )
        return {
            **result,
            "template_id": template.template_id,
            "template_version": template.version,
            "capabilities": list(template.capabilities),
        }

    def progress(self, prompt_id: str) -> dict[str, Any]:
        """Real state, from the server's own queue and history."""
        identifier = str(prompt_id or "")
        if not identifier:
            raise ComfyUIError("A prompt id is required")
        history = self.client.history(identifier)
        if history:
            status = history.get("status") if isinstance(history.get("status"), Mapping) else {}
            state = str(status.get("status_str") or "completed")
            completed = bool(status.get("completed", state == "success"))
            return {
                "prompt_id": identifier,
                "state": "completed" if completed and state != "error" else state,
                "completed": completed,
                "messages": status.get("messages") or [],
            }
        queue = self.client.queue()
        running_ids = [entry[1] for entry in queue["running"] if isinstance(entry, list) and len(entry) > 1]
        pending_ids = [entry[1] for entry in queue["pending"] if isinstance(entry, list) and len(entry) > 1]
        if identifier in running_ids:
            return {"prompt_id": identifier, "state": "running", "completed": False}
        if identifier in pending_ids:
            return {"prompt_id": identifier, "state": "pending", "completed": False}
        return {"prompt_id": identifier, "state": "unknown", "completed": False}

    def results(self, prompt_id: str) -> dict[str, Any]:
        """Outputs the server reports for a finished prompt."""
        identifier = str(prompt_id or "")
        history = self.client.history(identifier)
        if not history:
            raise ComfyUIError(f"ComfyUI has no history for prompt {identifier}")
        outputs = history.get("outputs") if isinstance(history.get("outputs"), Mapping) else {}
        files: list[dict[str, Any]] = []
        for node_id, node_output in outputs.items():
            if not isinstance(node_output, Mapping):
                continue
            for kind, items in node_output.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    files.append(
                        {
                            "node_id": str(node_id),
                            "kind": str(kind),
                            "filename": str(item.get("filename") or ""),
                            "subfolder": str(item.get("subfolder") or ""),
                            "type": str(item.get("type") or "output"),
                        }
                    )
        status = history.get("status") if isinstance(history.get("status"), Mapping) else {}
        return {
            "prompt_id": identifier,
            "files": files,
            "status": status.get("status_str") or "unknown",
            "completed": bool(status.get("completed", False)),
            "view_url": f"{self.client.base_url}/view",
        }

    def cancel(self, prompt_id: str) -> dict[str, Any]:
        """Cancel honestly: delete it if queued, interrupt if it is running.

        ComfyUI's ``/interrupt`` is global — it stops whatever is executing — so
        the adapter only calls it when the prompt is actually the running one,
        and reports exactly what it did.
        """
        identifier = str(prompt_id or "")
        state = self.progress(identifier)["state"]
        if state == "pending":
            self.client.delete_queued(identifier)
            after = self.progress(identifier)["state"]
            return {
                "prompt_id": identifier,
                "action": "deleted_from_queue",
                "state_after": after,
                "cancelled": after not in {"pending", "running"},
            }
        if state == "running":
            self.client.interrupt()
            after = self.progress(identifier)["state"]
            return {
                "prompt_id": identifier,
                "action": "interrupted",
                "state_after": after,
                "cancelled": after not in {"pending", "running"},
                "note": "ComfyUI interrupt is global; it stops the currently executing prompt.",
            }
        return {
            "prompt_id": identifier,
            "action": "none",
            "state_after": state,
            "cancelled": state not in {"pending", "running"},
        }


def probe_server(
    base_url: str,
    *,
    templates_dir: str,
    timeout: float = DEFAULT_TIMEOUT,
    label: str = "",
) -> dict[str, Any]:
    """One-shot probe whose result feeds :func:`engine_providers.build_registry`."""
    adapter = ComfyUIAdapter(base_url, templates_dir=templates_dir, timeout=timeout, label=label)
    return adapter.describe()


def default_templates_dir() -> str:
    """The template library that ships with the repository."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "comfy_workflows")

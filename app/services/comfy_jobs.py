"""Run queued work on the local ComfyUI engine (spec §10, §18, §21 phase 6).

This is the piece that turns the persistent queue into generated footage:

    queue job (engine ``comfyui``) → ComfyUI /prompt → poll its own history →
    copy the produced clip into the Maestro workspace → ``completed`` with a real
    file path

Rules it keeps, because they are the difference between an integration and a
promise:

* **Only what the engine reports.**  ComfyUI's HTTP API gives queue state, not a
  percentage, so the job is told "waiting" or "running" and keeps
  ``percent = null``.  No bar is invented.
* **The computer being off is not a failure.**  If ComfyUI does not answer, the
  job is *deferred*: it goes back to ``pending`` with a delay, no attempt is
  consumed, and nothing is lost.  The engine is registered and used exactly the
  way the setup screen proved it.
* **A clip or nothing.**  The job completes only with a downloaded, verified
  video file in the workspace.  A finished prompt whose file cannot be fetched is
  a failure with the real reason, not a success.
* **Cancellation is acknowledged.**  When a stop is requested, the handler asks
  ComfyUI to cancel the prompt and then reports that it really stopped.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Mapping, Optional, Sequence

from . import comfy_models, comfy_bootstrap, comfyui_adapter
from .queue_worker import JobCancelled

__all__ = [
    "DEFAULT_LOCAL_URL",
    "ENGINE",
    "register_comfyui_handler",
    "run_job",
    "unregister_comfyui_handlers",
]

# The engine string a job carries to be routed here.  It matches the provider id
# convention in engine_providers (``comfyui:<base_url>``) by prefix, so a job can
# name either ``comfyui`` or a specific server.
ENGINE = "comfyui"
DEFAULT_LOCAL_URL = "http://127.0.0.1:8188"
_VIDEO_SUFFIXES = (".mp4", ".webm", ".mkv", ".mov")


def _resolve_base_url(
    params: Mapping[str, Any],
    node_id: str,
    node_resolver: Optional[Callable[[str], str]],
    engine: str = "",
) -> str:
    """Which server this job runs on, in the order the job itself declares it.

    1. an explicit ``base_url`` in the job's own parameters,
    2. the address inside the engine string (``comfyui:http://desk:8188``), which
       is the same id convention the provider registry uses,
    3. the registered node's address,
    4. this computer's own ComfyUI.
    """
    explicit = str(params.get("base_url") or "").strip()
    if explicit:
        return comfyui_adapter.validate_base_url(explicit)
    suffix = str(engine or "").split(":", 1)
    if len(suffix) == 2 and suffix[1].strip():
        return comfyui_adapter.validate_base_url(suffix[1])
    if node_id and node_resolver is not None:
        resolved = str(node_resolver(node_id) or "").strip()
        if resolved:
            return comfyui_adapter.validate_base_url(resolved)
    return DEFAULT_LOCAL_URL


def _pick_video(files: Any) -> Optional[Mapping[str, Any]]:
    entries = [entry for entry in (files or []) if isinstance(entry, Mapping)]
    for entry in entries:
        if str(entry.get("filename") or "").lower().endswith(_VIDEO_SUFFIXES):
            return entry
    for entry in entries:
        if "video" in str(entry.get("kind") or "").lower():
            return entry
    return None


def run_job(
    job: Mapping[str, Any],
    context: Any,
    *,
    templates_dir: str = "",
    out_dir: str,
    node_resolver: Optional[Callable[[str], str]] = None,
    poll_timeout: float = 1800.0,
    poll_interval: float = 3.0,
    timeout: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Execute one job on ComfyUI.  Returns the real file(s) it produced."""
    params = dict(job.get("params") or {})
    node_id = str(job.get("node_id") or "")
    templates = templates_dir or comfyui_adapter.default_templates_dir()
    base_url = _resolve_base_url(params, node_id, node_resolver, str(job.get("engine") or ""))

    model_id = str(params.get("model_id") or "wan22_ti2v_5b")
    try:
        model = comfy_models.get_model(model_id)
    except comfy_models.ComfyModelError as error:
        context.fail(str(error), retryable=False)
        return []

    try:
        adapter = comfyui_adapter.ComfyUIAdapter(base_url, templates_dir=templates, timeout=timeout)
        described = adapter.describe()
    except Exception as error:
        # The machine that renders is not answering.  Keep the work.
        context.defer(
            f"ComfyUI at {base_url} did not answer ({error}); the job stays queued until it returns."
        )
        return []

    template_id = str(params.get("template_id") or (model.templates[0] if model.templates else ""))
    supported = {entry["template_id"] for entry in described.get("templates") or []}
    if template_id not in supported:
        unsupported = next(
            (entry for entry in described.get("unsupported_templates") or []
             if entry["template_id"] == template_id),
            None,
        )
        missing = ", ".join((unsupported or {}).get("missing_node_classes") or []) or "unknown"
        context.fail(
            f"ComfyUI at {base_url} cannot run {template_id}: node class(es) missing ({missing}). "
            "Update ComfyUI, then retry.",
            retryable=False,
        )
        return []

    missing_files = comfy_bootstrap.missing_model_files(model, adapter.client.object_info())
    if missing_files:
        context.fail(
            "ComfyUI does not have the model files this job needs ("
            + ", ".join(entry["filename"] for entry in missing_files)
            + f"). Download them into {missing_files[0]['directory']}.",
            retryable=False,
        )
        return []

    template = comfyui_adapter.load_template(templates, template_id)
    declared = template.inputs
    forwarded = {
        key: value for key, value in params.items() if key in declared and key not in {"base_url", "template_id", "model_id"}
    }
    if not str(forwarded.get("prompt") or "").strip():
        context.fail("This job has no prompt, so there is nothing to generate.", retryable=False)
        return []

    submit_params = {**model.template_params, **forwarded}
    submit_params.setdefault("filename_prefix", f"maestro_{str(job.get('job_id') or 'job')[:10]}")
    try:
        submitted = adapter.submit(template_id, submit_params)
    except Exception as error:
        raise RuntimeError(f"ComfyUI rejected the workflow: {error}") from error

    prompt_id = str(submitted.get("prompt_id") or "")
    context.link(prompt_id)
    context.set_state("running")
    # A note, never a percentage: ComfyUI's HTTP API does not report steps.
    context.report(note=f"ComfyUI prompt {prompt_id} accepted; no step percentage is reported")

    deadline = time.time() + float(poll_timeout)
    state = "unknown"
    while time.time() < deadline:
        if context.cancelled():
            outcome = adapter.cancel(prompt_id)
            note = f"ComfyUI cancellation: {outcome.get('action')}"
            context.report(note=note)
            raise JobCancelled(note)
        progress = adapter.progress(prompt_id)
        new_state = str(progress.get("state") or "unknown")
        if new_state != state:
            state = new_state
            context.report(note=f"ComfyUI reports the prompt is {state}")
        if progress.get("completed") and state == "completed":
            break
        if state in {"error", "failed"}:
            raise RuntimeError(f"ComfyUI reported {state} for prompt {prompt_id}")
        sleep(max(0.05, float(poll_interval)))
    else:
        context.defer(
            f"ComfyUI is still working on prompt {prompt_id} after {int(poll_timeout)}s; "
            "the job stays queued and will pick the result up when it is ready."
        )
        return []

    results = adapter.results(prompt_id)
    chosen = _pick_video(results.get("files"))
    if chosen is None:
        raise RuntimeError(
            "ComfyUI finished but reported no video file"
            + (f" (reported: {results.get('files')})" if results.get("files") else ".")
        )

    suffix = os.path.splitext(str(chosen.get("filename") or ""))[1] or ".mp4"
    os.makedirs(out_dir, exist_ok=True)
    destination = os.path.join(out_dir, f"{submit_params['filename_prefix']}_{prompt_id[:8]}{suffix}")
    comfy_bootstrap.download_from_comfy(base_url, chosen, destination, timeout=timeout)
    check = comfy_bootstrap.verify_video_file(destination)
    if not check["valid"]:
        raise RuntimeError(f"The clip came back but is not usable: {check['reason']}")
    context.report(percent=None, note=f"clip retrieved ({check['bytes']} bytes, {check['container']})")
    return [destination]


def register_comfyui_handler(
    *,
    engines: Sequence[str] = (ENGINE,),
    templates_dir: str = "",
    out_dir: str,
    node_resolver: Optional[Callable[[str], str]] = None,
    poll_timeout: float = 1800.0,
    poll_interval: float = 3.0,
    timeout: float = 10.0,
    replace: bool = True,
) -> list[str]:
    """Register the local ComfyUI runner with the queue worker.

    ``engines`` is explicit because a job may name either the bare engine
    (``comfyui``) or one specific server (``comfyui:http://desk:8188``, the same
    id convention the provider registry uses).  ``replace`` defaults to True since
    Maestro re-registers at startup for whichever workspace is active then.
    """
    from . import queue_worker

    def handler(job: Mapping[str, Any], context: Any) -> list[str]:
        return run_job(
            job,
            context,
            templates_dir=templates_dir,
            out_dir=out_dir,
            node_resolver=node_resolver,
            poll_timeout=poll_timeout,
            poll_interval=poll_interval,
            timeout=timeout,
        )

    registered: list[str] = []
    for engine in engines:
        name = str(engine or "").strip()
        if not name:
            continue
        registered.append(queue_worker.register_handler(name, handler, replace=replace))
    return registered


def unregister_comfyui_handlers(engines: Sequence[str] = (ENGINE,)) -> list[str]:
    """Forget the ComfyUI runner for ``engines``; returns the ones removed."""
    from . import queue_worker

    return [name for name in engines if queue_worker.unregister_handler(name)]

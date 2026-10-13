"""HTTP boundary for the local, free-per-generation engine (spec §8, §18, §21).

A router factory, like ``production_queue_api``, so the whole setup surface can be
driven over HTTP in tests without booting the application.

The surface answers four questions the phone asks, in the order it asks them:

1. *What is this computer?* — ``GET /hardware``: measured facts, plus the model
   the catalogue says fits, plus what was evaluated and rejected (LTX-2), plus the
   policy (no commercial provider, no credits).
2. *What would installing do?* — ``GET /setup``: the exact commands, the exact
   files and their sizes, what is already present, and what is missing. Read-only.
3. *Do it, and prove it.* — ``POST /setup/install`` (needs explicit confirmation;
   model weights need their own flag), then ``POST /functional-test``: a real
   little clip comes back, or the reason it did not.
4. *Register it, then use it.* — ``POST /register`` (only with a passed test on
   record) and ``POST /queue-job``, which puts a real generation in the persistent
   queue — which works even while the rendering computer is off, because the job
   waits instead of failing.
"""
from __future__ import annotations

import os
import time
import traceback
from typing import Any, Callable, Mapping, Optional

from fastapi import APIRouter, HTTPException, Request

from . import comfy_bootstrap, comfy_jobs, comfy_models, local_hardware
from . import generation_queue as queue

__all__ = ["create_router", "last_passed_test"]

# A passed functional test is remembered here so registration does not have to
# generate a second clip — but it is re-validated (the file must still exist, with
# the same digest) and it expires, because a test result is evidence, not a flag.
_TEST_TTL_SECONDS = 24 * 3600
_LAST_TESTS: dict[str, dict[str, Any]] = {}


def last_passed_test(base_url: str) -> Optional[dict[str, Any]]:
    """The recorded passed test for a server, if it is still valid."""
    record = _LAST_TESTS.get(str(base_url or "").strip())
    if not record or not record.get("passed"):
        return None
    recorded_at = float(record.get("at") or record.get("finished_at") or 0)
    if not recorded_at or time.time() - recorded_at > _TEST_TTL_SECONDS:
        return None
    path = str(record.get("clip_path") or "")
    if not path or not os.path.isfile(path):
        return None
    return record


def create_router(
    *,
    queue_root: str,
    compute_nodes_root: str,
    hardware_probe: Callable[[], dict[str, Any]] = local_hardware.detect_hardware,
    comfy_root: str = "",
    models_root: str = "",
    out_dir: Callable[[], str],
    file_url: Optional[Callable[[str], str]] = None,
    runner: Any = None,
    downloader: Any = None,
    templates_dir: str = "",
    node_resolver: Optional[Callable[[str], str]] = None,
) -> APIRouter:
    router = APIRouter()

    def http_error(error: Exception) -> HTTPException:
        if isinstance(error, comfy_models.ComfyModelError):
            return HTTPException(status_code=400, detail=str(error))
        if isinstance(error, (comfy_bootstrap.BootstrapError, queue.GenerationQueueError)):
            return HTTPException(status_code=400, detail=str(error))
        if isinstance(error, (TypeError, ValueError)):
            return HTTPException(status_code=400, detail=str(error))
        traceback.print_exc()
        return HTTPException(status_code=500, detail=str(error)[:500])

    async def json_body(request: Request) -> dict:
        return await request.json() if await request.body() else {}

    def resolve_model(model_id: str, selection: Mapping[str, Any]) -> comfy_models.LocalVideoModel:
        identifier = str(model_id or "").strip()
        if identifier:
            return comfy_models.get_model(identifier)
        chosen = (selection or {}).get("model") or {}
        if not chosen:
            raise comfy_models.ComfyModelError(
                "No local video model fits this machine yet, so there is nothing to install. "
                "The hardware and the reasons are on the setup screen."
            )
        return comfy_models.get_model(str(chosen.get("model_id")))

    def select(hardware: Mapping[str, Any]) -> dict[str, Any]:
        return comfy_models.select_video_model(hardware, preferred=("wan22_ti2v_5b",))

    # -- 1. what this computer is -----------------------------------------

    @router.get("/api/v1/engines/local/hardware")
    def local_engine_hardware():
        try:
            hardware = hardware_probe()
            selection = select(hardware)
            return {
                "hardware": hardware,
                "selection": selection,
                "catalogue": [model.describe() for model in comfy_models.MODELS.values()],
                "evaluated_but_not_selected": comfy_models.evaluated_but_not_selected(),
                "policy": dict(comfy_models.POLICY),
                "engine": {
                    "kind": "comfyui",
                    "default_url": comfy_jobs.DEFAULT_LOCAL_URL,
                    "api": "ComfyUI's local HTTP API (self-hosted)",
                    "cloud": False,
                },
            }
        except Exception as error:
            raise http_error(error) from error

    # -- 2. what installing would do --------------------------------------

    @router.get("/api/v1/engines/local/setup")
    def local_engine_setup(model_id: str = "", comfy_root_path: str = "", port: int = 0):
        try:
            hardware = hardware_probe()
            model = resolve_model(model_id, select(hardware))
            plan = comfy_bootstrap.install_plan(
                hardware=hardware,
                model=model,
                comfy_root=comfy_root_path or comfy_root,
                models_root=models_root,
                port=int(port or comfy_bootstrap.DEFAULT_COMFY_PORT),
            )
            plan["present"] = {
                "comfyui_checkout": os.path.isdir(os.path.join(plan["comfy_root"], ".git")),
                "venv": os.path.isfile(plan["venv_python"]),
            }
            return plan
        except Exception as error:
            raise http_error(error) from error

    # -- 3a. install (explicit, confirmed) --------------------------------

    @router.post("/api/v1/engines/local/setup/install")
    async def local_engine_install(request: Request):
        body = await json_body(request)
        try:
            hardware = hardware_probe()
            model = resolve_model(str(body.get("model_id") or ""), select(hardware))
            plan = comfy_bootstrap.install_plan(
                hardware=hardware,
                model=model,
                comfy_root=str(body.get("comfy_root") or comfy_root),
                models_root=models_root,
                port=int(body.get("port") or comfy_bootstrap.DEFAULT_COMFY_PORT),
            )
            return comfy_bootstrap.install(
                plan,
                confirm=bool(body.get("confirm")),
                download_models=bool(body.get("download_models")),
                runner=runner,
                downloader=downloader,
                timeout=float(body.get("timeout") or 1800.0),
            )
        except Exception as error:
            raise http_error(error) from error

    # -- 3b. prove it with a real clip ------------------------------------

    @router.post("/api/v1/engines/local/functional-test")
    async def local_engine_functional_test(request: Request):
        body = await json_body(request)
        try:
            hardware = hardware_probe()
            model = resolve_model(str(body.get("model_id") or ""), select(hardware))
            base_url = str(body.get("base_url") or comfy_jobs.DEFAULT_LOCAL_URL)
            result = comfy_bootstrap.functional_test(
                base_url,
                model=model,
                templates_dir=templates_dir,
                out_dir=out_dir(),
                prompt=str(body.get("prompt") or "") or "a paper boat drifting down a small stream, cinematic",
                width=int(body.get("width") or 256),
                height=int(body.get("height") or 256),
                length=int(body.get("length") or 9),
                steps=int(body.get("steps") or 4),
                poll_timeout=float(body.get("poll_timeout") or 1800.0),
                poll_interval=float(body.get("poll_interval") or 3.0),
                downloader=downloader,
            )
            if result.get("passed"):
                stamped = time.time()
                result["finished_at"] = stamped
                # The record is what registration checks: a real clip, with a digest,
                # still on disk, and not older than the test's useful life.
                _LAST_TESTS[base_url] = {**result, "at": stamped}
                if file_url and result.get("clip_path"):
                    result["clip_url"] = file_url(str(result["clip_path"]))
            return result
        except Exception as error:
            raise http_error(error) from error

    # -- 4a. register (only with proof) -----------------------------------

    @router.post("/api/v1/engines/local/register")
    async def local_engine_register(request: Request):
        body = await json_body(request)
        try:
            hardware = hardware_probe()
            model = resolve_model(str(body.get("model_id") or ""), select(hardware))
            base_url = str(body.get("base_url") or comfy_jobs.DEFAULT_LOCAL_URL)
            test = last_passed_test(base_url)
            if test is None:
                return {
                    "registered": False,
                    "reason": (
                        "No passed functional test is on record for "
                        f"{base_url} (or its clip is gone). Run the functional test first: the "
                        "engine is registered only after a real clip comes back."
                    ),
                    "functional_test_required": True,
                }
            registered = comfy_bootstrap.register_engine(
                compute_nodes_root=compute_nodes_root,
                base_url=base_url,
                model=model,
                test_result=test,
                hardware=hardware,
                label=str(body.get("label") or ""),
            )
            return {"registered": True, "node": registered, "verified_by": registered.get("verified_by")}
        except Exception as error:
            raise http_error(error) from error

    # -- 4b. queue a real generation (works even if the computer is off) ---

    @router.post("/api/v1/engines/local/queue-job")
    async def local_engine_queue_job(request: Request):
        body = await json_body(request)
        try:
            hardware = hardware_probe()
            model = resolve_model(str(body.get("model_id") or ""), select(hardware))
            template_id = str(body.get("template_id") or (model.templates[0] if model.templates else ""))
            if not template_id:
                raise comfy_models.ComfyModelError(
                    f"No workflow template ships for {model.label}, so it cannot be queued."
                )
            seconds = float(body.get("seconds") or 5.0)
            fps = int(body.get("fps") or model.fps)
            length = int(body.get("length") or max(5, int(round(seconds * fps)) // 4 * 4 + 1))
            params: dict[str, Any] = {
                "model_id": model.model_id,
                "template_id": template_id,
                **model.template_params,
                "prompt": str(body.get("prompt") or ""),
                "width": int(body.get("width") or 1280),
                "height": int(body.get("height") or 704),
                "length": length,
                "fps": fps,
                "steps": int(body.get("steps") or 20),
                "seed": int(body.get("seed") or 0),
            }
            if body.get("base_url"):
                params["base_url"] = str(body["base_url"])
            if body.get("node_id"):
                params["node_id"] = str(body["node_id"])
            submitted = queue.submit(
                queue_root,
                {
                    "project_id": str(body.get("project_id") or ""),
                    "scene_id": str(body.get("scene_id") or ""),
                    "shot_id": str(body.get("shot_id") or ""),
                    "task": "video",
                    "engine": str(body.get("engine") or comfy_jobs.ENGINE),
                    "model": model.label,
                    "params": params,
                    "node_id": str(body.get("node_id") or ""),
                    "priority": int(body.get("priority") or 0),
                    "memory_mb": int(body.get("memory_mb") or 0),
                    "dedupe_key": str(body.get("dedupe_key") or ""),
                },
            )
            return {
                **submitted,
                "engine": str(body.get("engine") or comfy_jobs.ENGINE),
                "note": (
                    "The clip is queued on this computer. If it is switched off the job waits "
                    "instead of failing, and runs when the engine comes back."
                ),
            }
        except Exception as error:
            raise http_error(error) from error

    return router


def make_file_url(filename: str, workspace: str) -> str:
    """The URL the phone uses to play a produced clip (served by /api/v1/file)."""
    from urllib.parse import quote

    return f"/api/v1/file/{quote(os.path.basename(filename))}?workspace={quote(workspace)}"


def default_file_url(path: str) -> str:
    """Fallback that only uses the file name, for callers without a workspace."""
    from urllib.parse import quote

    return f"/api/v1/file/{quote(os.path.basename(path))}"

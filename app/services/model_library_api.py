"""HTTP surface for the model library: every model, every way to reach it, and CPU-friendly downloads.

Model ids contain ``:`` and ``/`` (``llm:owner/repo``), so they travel as a query or
body field rather than a path segment.  Errors answer ``{"detail": {"code", "message"}}``.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

from . import model_access, model_downloads
from .model_downloads import DownloadError, DownloadManager

_STATUS = {"unknown_model": 404, "not_found": 404, "license_ack_required": 409, "network_error": 502,
           "hub_error": 502}


def _error(error: DownloadError) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(error.code, 400), detail=error.to_dict())


def _compact(item: dict[str, Any]) -> dict[str, Any]:
    access = item["access"]
    return {
        **{k: item[k] for k in ("id", "kind", "name", "modality", "family", "description", "visible", "run_here",
                                "portable_here", "license_ack_required", "feasibility")},
        "license": access.get("license", ""), "license_url": access.get("license_url", ""),
        "territory": access.get("territory", ""), "max_output": access.get("max_output", ""),
        "family_label": access.get("label", ""), "free_api": access.get("free_api", False),
        "routes": [{k: r[k] for k in ("id", "kind", "note", "confidence", "same_weights")}
                   for r in access.get("routes", [])],
        "local_backends": [b["id"] for b in access.get("local_backends", [])],
    }


def create_router(manager: Callable[[], DownloadManager]) -> APIRouter:
    router = APIRouter()

    def _accelerator(value: str) -> dict[str, Any]:
        return model_downloads.hardware_profile(value if value in ("cuda", "rocm", "mps", "directml", "vulkan",
                                                                   "cpu") else "")

    @router.get("/api/v1/model-library")
    def library(accelerator: str = ""):
        hardware = _accelerator(accelerator)
        items = [_compact(i) for i in model_downloads.catalog(accelerator=hardware["accelerator"])]
        return {"hardware": hardware, "items": items, "routes": model_access.ROUTES,
                "route_kinds": model_access.ROUTE_KINDS, "local_backends": model_access.LOCAL_BACKENDS,
                "researched_on": model_access.RESEARCHED_ON}

    @router.get("/api/v1/model-library/plan")
    def get_plan(id: str, profile: str = "quality", accelerator: str = "", variant: str = ""):
        hardware = _accelerator(accelerator)
        try:
            item_plan = model_downloads.plan(id, profile=profile, accelerator=hardware["accelerator"], variant=variant)
        except DownloadError as error:
            raise _error(error) from error
        m = manager()
        for spec in item_plan["files"]:
            spec["present"] = m.existing(spec["target"]) is not None if not spec.get("hf_cache") else False
        return item_plan

    @router.post("/api/v1/model-library/inspect")
    async def inspect(request: Request):
        body = await request.json()
        hardware = _accelerator(str(body.get("accelerator") or ""))
        try:
            item_plan = model_downloads.plan(str(body.get("id") or ""), profile=str(body.get("profile") or "quality"),
                                             accelerator=hardware["accelerator"], variant=str(body.get("variant") or ""))
            return manager().inspect(item_plan)
        except DownloadError as error:
            raise _error(error) from error

    @router.get("/api/v1/model-library/downloads")
    def downloads():
        return {"jobs": manager().list()}

    @router.post("/api/v1/model-library/downloads")
    async def start(request: Request):
        body = await request.json()
        hardware = _accelerator(str(body.get("accelerator") or ""))
        try:
            return manager().start(str(body.get("id") or ""), profile=str(body.get("profile") or "quality"),
                                   accelerator=hardware["accelerator"], variant=str(body.get("variant") or ""),
                                   accept_license=bool(body.get("accept_license")),
                                   include_loras=body.get("include_loras", True) is not False)
        except DownloadError as error:
            raise _error(error) from error

    @router.post("/api/v1/model-library/downloads/{job_id}/cancel")
    def cancel(job_id: str):
        try:
            return manager().cancel(job_id)
        except DownloadError as error:
            raise _error(error) from error

    return router

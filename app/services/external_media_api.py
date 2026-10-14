"""HTTP surface for opt-in external image/video providers.

Every route answers errors as ``{"detail": {"code": ..., "message": ...}}`` so the
UI can show the provider's real reason (missing key, no credit, rate limit…)
instead of a generic failure.  No route ever returns a stored secret.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

from .external_media import ExternalMediaError, ExternalMediaManager

_STATUS = {
    "unknown_provider": 404,
    "not_found": 404,
    "invalid_input": 400,
    "missing_credentials": 412,
    "provider_disabled": 409,
    "confirmation_required": 409,
    "auth_failed": 401,
    "insufficient_credit": 402,
    "quota_exhausted": 429,
    "rate_limited": 429,
    "timeout": 504,
    "network_error": 502,
    "provider_unavailable": 503,
    "account_requirement": 403,
}


def http_error(error: ExternalMediaError) -> HTTPException:
    return HTTPException(status_code=_STATUS.get(error.code, 502), detail=error.to_dict())


async def _json(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail={"code": "invalid_input", "message": "Expected a JSON object."})
    return body


def create_router(manager: Callable[[], ExternalMediaManager]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/external/providers")
    def list_providers():
        m = manager()
        return {"providers": m.providers(), "storage": m.storage(),
                "policy": ("External providers are off until you enable one and add your own key. "
                           "Each job shows its cost and the data it sends, and runs only after you confirm it.")}

    @router.put("/api/v1/external/providers/{provider_id}")
    async def update_provider(provider_id: str, request: Request):
        body = await _json(request)
        m = manager()
        try:
            if "credentials" in body:
                if not isinstance(body["credentials"], dict):
                    raise ExternalMediaError("invalid_input", "credentials must be an object.")
                m.set_credentials(provider_id, body["credentials"])
            if "enabled" in body:
                m.set_enabled(provider_id, bool(body["enabled"]))
        except ExternalMediaError as error:
            raise http_error(error) from error
        except ValueError as error:
            raise http_error(ExternalMediaError("invalid_input", str(error))) from error
        return list_providers()

    @router.delete("/api/v1/external/providers/{provider_id}/credentials")
    def clear_credentials(provider_id: str):
        m = manager()
        try:
            m.clear_credentials(provider_id)
            m.set_enabled(provider_id, False)
        except ExternalMediaError as error:
            raise http_error(error) from error
        return list_providers()

    @router.post("/api/v1/external/providers/{provider_id}/test")
    def test_provider(provider_id: str):
        try:
            return manager().test_credentials(provider_id)
        except ExternalMediaError as error:
            raise http_error(error) from error

    @router.post("/api/v1/external/quote")
    async def quote(request: Request):
        body = await _json(request)
        try:
            return manager().quote(body)
        except ExternalMediaError as error:
            raise http_error(error) from error

    @router.get("/api/v1/external/jobs")
    def list_jobs():
        return {"jobs": manager().list()}

    @router.post("/api/v1/external/jobs")
    async def submit(request: Request):
        body = await _json(request)
        params = body.get("params")
        if not isinstance(params, dict):
            raise HTTPException(status_code=400, detail={"code": "invalid_input", "message": "params is required."})
        try:
            return manager().submit(params, str(body.get("confirm_token") or ""))
        except ExternalMediaError as error:
            raise http_error(error) from error

    @router.get("/api/v1/external/jobs/{job_id}")
    def get_job(job_id: str):
        try:
            return manager().get(job_id)
        except ExternalMediaError as error:
            raise http_error(error) from error

    @router.post("/api/v1/external/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        try:
            return manager().cancel(job_id)
        except ExternalMediaError as error:
            raise http_error(error) from error

    return router

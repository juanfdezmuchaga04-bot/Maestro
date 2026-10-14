"""Opt-in external providers for image and video generation.

Everything else in Maestro renders on hardware the user owns or on free public
compute.  This module is the one place where a render can be sent to a
**commercial API**, and it is built so that never happens by accident:

* **Off by default.**  A provider is used only after the user enables it *and*
  stores their own key.  Nothing here is registered in the automatic engine
  routing (``engine_providers.resolve`` keeps ``allow_commercial=False``).
* **Every job is confirmed.**  :meth:`ExternalMediaManager.quote` returns a
  disclosure — provider, model, the exact data that will leave the computer, the
  estimated cost with its basis, account requirements and limits — plus a
  ``confirm_token`` that is a digest of that disclosure *and* the normalised
  request.  :meth:`ExternalMediaManager.submit` refuses a job whose token does not
  match, so a request cannot be changed (or sent at all) without the user having
  seen what it costs and what it sends.
* **No invented results.**  A job reaches ``succeeded`` only after a file was
  downloaded/decoded, its bytes were recognised as the requested media type, and
  it was written into the active workspace.  JSON, HTML, an empty body or an image
  where a video was asked for is a failure with that reason.
* **Real errors, by name.**  Missing keys, rejected keys, no credit, a spent free
  allowance, rate limits, invalid input, HTTP failures, timeouts and unreachable
  providers each have their own ``error.code`` and the provider's own message.
* **Cancellation is honest.**  Replicate predictions are cancelled remotely
  (``POST /v1/predictions/{id}/cancel``).  Cloudflare Workers AI answers
  synchronously and has no cancel endpoint, so cancelling there only stops Maestro
  waiting — and the job says the request may still consume quota.

The facts in :data:`PROVIDERS` (endpoints, limits, prices) were read from each
provider's documentation on :data:`VERIFIED_ON`; prices change, so every estimate
carries its basis and the provider page to check.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Mapping, Optional
from urllib import error as urlerror
from urllib.parse import quote

from .external_secrets import SecretStore
from .public_cloud import HttpResponse, _default_transport

__all__ = [
    "PROVIDERS",
    "VERIFIED_ON",
    "ExternalMediaError",
    "ExternalMediaManager",
    "detect_media",
]

VERIFIED_ON = "2026-10-14"
REPLICATE_API = "https://api.replicate.com/v1"
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
JOBS_FILENAME = "jobs.json"
CONFIG_FILENAME = "config.json"
MAX_PROMPT_CHARS = 2000
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
MAX_KEPT_JOBS = 200

TERMINAL_STATES = {"succeeded", "failed", "cancelled"}

Transport = Callable[..., HttpResponse]


class ExternalMediaError(RuntimeError):
    """A failure with a stable ``code`` the UI and the tests can rely on."""

    def __init__(self, code: str, message: str, *, http_status: Optional[int] = None,
                 retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retry_after = retry_after

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.http_status is not None:
            out["http_status"] = self.http_status
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after
        return out


# ---------------------------------------------------------------------------
# Provider catalogue — verified facts only, each with where it was read.
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, dict[str, Any]] = {
    "cloudflare": {
        "id": "cloudflare",
        "label": "Cloudflare Workers AI",
        "modalities": ["image"],
        "account": (
            "A Cloudflare account (the Free plan needs no payment card), its Account ID, "
            "and an API token with the 'Workers AI' permission."
        ),
        "credentials": [
            {"field": "account_id", "label": "Account ID", "secret": False},
            {"field": "api_token", "label": "API token (Workers AI)", "secret": True},
        ],
        "billing": (
            "Free daily allocation of 10,000 Neurons, reset at 00:00 UTC. Beyond it, requests fail "
            "on the Free plan; on Workers Paid they cost $0.011 per 1,000 Neurons."
        ),
        "free_quota": "10,000 Neurons per day (about 170 FLUX schnell images at 1024×1024, 4 steps)",
        "retention": "Cloudflare states Workers AI does not use customer prompts or outputs to train models.",
        "cancel": "local_only",
        "cancel_note": (
            "Workers AI answers synchronously and has no cancel endpoint: cancelling stops Maestro "
            "waiting, but the request may still run and consume quota."
        ),
        "docs": [
            "https://developers.cloudflare.com/workers-ai/models/flux-1-schnell/",
            "https://developers.cloudflare.com/workers-ai/platform/pricing/",
            "https://developers.cloudflare.com/workers-ai/get-started/rest-api/",
        ],
        "terms": "https://www.cloudflare.com/service-specific-terms-developer-platform/",
        "signup": "https://dash.cloudflare.com/sign-up",
        "models": {
            "image": {
                "model": "@cf/black-forest-labs/flux-1-schnell",
                "label": "FLUX.1 [schnell]",
                "license": "Apache-2.0 (Black Forest Labs)",
                "limits": {
                    "prompt_chars": 2048,
                    "steps": [1, 8],
                    "output": "1024×1024 JPEG (fixed by the model on Workers AI)",
                },
                "timeout_s": 120,
            },
        },
    },
    "replicate": {
        "id": "replicate",
        "label": "Replicate",
        "modalities": ["image", "video"],
        "account": (
            "A Replicate account with billing set up (prepaid credit or a card) and an API token. "
            "Without credit, generation requests are refused with HTTP 402."
        ),
        "credentials": [
            {"field": "api_token", "label": "API token", "secret": True},
        ],
        "billing": (
            "Pay per use, billed by Replicate. Official models are priced per output; the amount "
            "charged is the one on the model page at run time."
        ),
        "free_quota": "",
        "retention": (
            "Inputs and outputs of API predictions are deleted by Replicate after one hour; Maestro "
            "downloads the result immediately."
        ),
        "cancel": "remote",
        "cancel_note": "Cancelling sends POST /v1/predictions/{id}/cancel; time already used may be billed.",
        "docs": [
            "https://replicate.com/docs/reference/http",
            "https://replicate.com/docs/topics/predictions/create-a-prediction",
            "https://replicate.com/docs/topics/billing",
        ],
        "terms": "https://replicate.com/terms",
        "signup": "https://replicate.com/signin",
        "models": {
            "image": {
                "model": "black-forest-labs/flux-schnell",
                "label": "FLUX.1 [schnell]",
                "license": "Apache-2.0 (Black Forest Labs)",
                "price_usd": 0.003,
                "price_basis": "≈ $0.003 per image (Replicate model page, read 2026-09/10)",
                "price_url": "https://replicate.com/black-forest-labs/flux-schnell",
                "limits": {
                    "aspect_ratios": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"],
                    "output": "PNG, about 1 megapixel",
                },
                "timeout_s": 300,
                "poll_s": 2.0,
            },
            "video": {
                "model": "wan-video/wan-2.2-t2v-fast",
                "label": "Wan 2.2 T2V Fast",
                "license": "Apache-2.0 (Wan-AI)",
                "price_usd": {"480p": 0.05, "720p": 0.10},
                "price_basis": (
                    "≈ $0.05 per video at 480p, $0.10 at 720p for the default 81 frames (Replicate "
                    "launch pricing); longer clips are estimated proportionally"
                ),
                "price_url": "https://replicate.com/wan-video/wan-2.2-t2v-fast",
                "limits": {
                    "resolutions": ["480p", "720p"],
                    "aspect_ratios": ["16:9", "9:16"],
                    "num_frames": [81, 100],
                    "frames_per_second": [5, 24],
                    "output": "MP4 (no audio). 480p is 832×480 at 16:9",
                },
                "timeout_s": 1200,
                "poll_s": 4.0,
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Media recognition
# ---------------------------------------------------------------------------


def detect_media(body: bytes) -> tuple[str, str]:
    """``(kind, extension)`` recognised from the bytes themselves, or ``("", "")``."""
    head = bytes(body[:16])
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image", ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image", ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", ".webp"
    if head[4:8] == b"ftyp":
        return "video", ".mp4"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "video", ".webm"
    return "", ""


# ---------------------------------------------------------------------------
# HTTP error classification
# ---------------------------------------------------------------------------

_QUOTA_MARKERS = ("daily free allocation", "free allocation", "neurons", "quota")
_CREDIT_MARKERS = ("insufficient credit", "billing", "payment required", "out of credit")


def _provider_message(response: HttpResponse) -> str:
    payload = response.json()
    if isinstance(payload, Mapping):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            parts = []
            for item in errors[:3]:
                if isinstance(item, Mapping):
                    code = item.get("code")
                    text = str(item.get("message") or "")
                    parts.append(f"{code}: {text}" if code else text)
            if parts:
                return "; ".join(parts)
        for key in ("detail", "error", "message", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:500]
    return f"HTTP {response.status}"


def classify_http(response: HttpResponse, provider_label: str) -> ExternalMediaError:
    status = response.status
    detail = _provider_message(response)
    lowered = detail.lower()
    retry_after: Optional[float] = None
    try:
        retry_after = float(response.headers.get("retry-after", "")) if response.headers.get("retry-after") else None
    except ValueError:
        retry_after = None
    client_error = 400 <= status < 500
    if status == 402 or (client_error and any(marker in lowered for marker in _CREDIT_MARKERS)):
        return ExternalMediaError(
            "insufficient_credit", f"{provider_label} reports no available credit ({status}): {detail}",
            http_status=status)
    if client_error and any(marker in lowered for marker in _QUOTA_MARKERS):
        return ExternalMediaError(
            "quota_exhausted", f"{provider_label} free allowance is used up ({status}): {detail}",
            http_status=status, retry_after=retry_after)
    if status in (401, 403):
        return ExternalMediaError(
            "auth_failed", f"{provider_label} rejected the API key ({status}): {detail}", http_status=status)
    if status == 429:
        return ExternalMediaError(
            "rate_limited", f"{provider_label} rate limit reached (429): {detail}",
            http_status=status, retry_after=retry_after)
    if status == 404:
        return ExternalMediaError(
            "not_found", f"{provider_label} could not find the model or job (404): {detail}", http_status=status)
    if status in (400, 413, 422):
        return ExternalMediaError(
            "invalid_input", f"{provider_label} rejected the request ({status}): {detail}", http_status=status)
    if status >= 500:
        return ExternalMediaError(
            "provider_unavailable", f"{provider_label} is unavailable ({status}): {detail}", http_status=status)
    return ExternalMediaError("http_error", f"{provider_label} answered HTTP {status}: {detail}", http_status=status)


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


class ExternalMediaManager:
    """Owns provider settings, disclosures, jobs and their worker threads."""

    def __init__(
        self,
        root: str,
        *,
        output_dir: Callable[[], tuple[str, str]],
        secrets: Optional[SecretStore] = None,
        transport: Transport = _default_transport,
        clock: Callable[[], float] = _now,
        max_workers: int = 2,
        start_threads: bool = True,
    ) -> None:
        self._root = root
        os.makedirs(root, exist_ok=True)
        self._output_dir = output_dir
        self._secrets = secrets or SecretStore(root)
        self._transport = transport
        self._clock = clock
        self._lock = threading.RLock()
        self._slots = threading.BoundedSemaphore(max(1, int(max_workers)))
        self._cancel_events: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._start_threads = start_threads
        self._config = self._load_config()
        self._jobs: dict[str, dict[str, Any]] = self._load_jobs()
        self._recover_interrupted()

    # -- persistence -------------------------------------------------------
    def _config_path(self) -> str:
        return os.path.join(self._root, CONFIG_FILENAME)

    def _jobs_path(self) -> str:
        return os.path.join(self._root, JOBS_FILENAME)

    def _load_config(self) -> dict[str, Any]:
        try:
            with open(self._config_path(), "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            raw = {}
        enabled = raw.get("enabled") if isinstance(raw, dict) else None
        return {"enabled": {k: bool(v) for k, v in (enabled or {}).items() if k in PROVIDERS}}

    def _save_config(self) -> None:
        self._atomic_write(self._config_path(), self._config)

    def _load_jobs(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self._jobs_path(), "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return {}
        jobs = raw.get("jobs") if isinstance(raw, dict) else None
        if not isinstance(jobs, list):
            return {}
        return {str(job["id"]): dict(job) for job in jobs if isinstance(job, dict) and job.get("id")}

    def _save_jobs(self) -> None:
        with self._lock:
            ordered = sorted(self._jobs.values(), key=lambda j: j.get("created_at", 0), reverse=True)
            kept = ordered[:MAX_KEPT_JOBS]
            self._jobs = {job["id"]: job for job in kept}
            self._atomic_write(self._jobs_path(), {"version": 1, "jobs": kept})

    @staticmethod
    def _atomic_write(path: str, payload: Any) -> None:
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1)
        os.replace(tmp, path)

    def _recover_interrupted(self) -> None:
        """Jobs left mid-flight by a restart are resumed or closed — never left 'running'."""
        resumable = []
        for job in self._jobs.values():
            if job.get("state") in TERMINAL_STATES:
                continue
            if job.get("provider") == "replicate" and job.get("remote_id"):
                job["cancel_requested"] = False
                job["progress"] = "Maestro restarted; checking the prediction again"
                resumable.append(job["id"])
            else:
                self._finish(job, "failed", error=ExternalMediaError(
                    "interrupted",
                    "Maestro stopped while waiting for the provider, so no result was received. "
                    "The request may still have consumed quota.",
                ), save=False)
        self._save_jobs()
        for job_id in resumable:
            self._launch(job_id)

    # -- provider settings -------------------------------------------------
    def providers(self) -> list[dict[str, Any]]:
        out = []
        for provider_id, spec in PROVIDERS.items():
            credentials = []
            for cred in spec["credentials"]:
                info = self._secrets.describe(f"{provider_id}.{cred['field']}")
                credentials.append({**cred, **info})
            ready = all(c["set"] for c in credentials)
            out.append({
                **{k: v for k, v in spec.items() if k != "credentials"},
                "credentials": credentials,
                "enabled": bool(self._config["enabled"].get(provider_id)),
                "configured": ready,
                "verified_on": VERIFIED_ON,
            })
        return out

    def storage(self) -> dict[str, str]:
        return {"kind": self._secrets.storage, "label": self._secrets.storage_label()}

    def _spec(self, provider_id: str) -> dict[str, Any]:
        spec = PROVIDERS.get(str(provider_id or ""))
        if not spec:
            raise ExternalMediaError("unknown_provider", f"Unknown provider '{provider_id}'.")
        return spec

    def set_enabled(self, provider_id: str, enabled: bool) -> None:
        self._spec(provider_id)
        with self._lock:
            self._config["enabled"][provider_id] = bool(enabled)
            self._save_config()

    def set_credentials(self, provider_id: str, values: Mapping[str, Any]) -> None:
        spec = self._spec(provider_id)
        fields = {c["field"] for c in spec["credentials"]}
        for field, value in (values or {}).items():
            if field not in fields:
                raise ExternalMediaError("invalid_input", f"{spec['label']} has no credential '{field}'.")
            text = str(value or "").strip()
            if field == "account_id" and text and not re.fullmatch(r"[0-9a-fA-F]{32}", text):
                raise ExternalMediaError("invalid_input", "A Cloudflare Account ID is 32 hexadecimal characters.")
            self._secrets.set(f"{provider_id}.{field}", text)

    def clear_credentials(self, provider_id: str) -> None:
        spec = self._spec(provider_id)
        for cred in spec["credentials"]:
            self._secrets.delete(f"{provider_id}.{cred['field']}")

    def _credential(self, provider_id: str, field: str) -> str:
        value = self._secrets.get(f"{provider_id}.{field}")
        if not value:
            label = PROVIDERS[provider_id]["label"]
            raise ExternalMediaError(
                "missing_credentials", f"{label} has no {field.replace('_', ' ')} configured. Add it in Settings.")
        return value

    def _request(self, method: str, url: str, *, provider_label: str, timeout: float,
                 data: Optional[bytes] = None, headers: Optional[Mapping[str, str]] = None) -> HttpResponse:
        try:
            return self._transport(method, url, data=data, headers=headers, timeout=timeout)
        except (socket.timeout, TimeoutError) as exc:
            raise ExternalMediaError(
                "timeout", f"{provider_label} did not answer within {int(timeout)} s ({exc}).") from exc
        except urlerror.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise ExternalMediaError(
                    "timeout", f"{provider_label} did not answer within {int(timeout)} s.") from exc
            raise ExternalMediaError(
                "network_error", f"Could not reach {provider_label}: {reason}") from exc
        except OSError as exc:
            raise ExternalMediaError("network_error", f"Could not reach {provider_label}: {exc}") from exc

    def test_credentials(self, provider_id: str) -> dict[str, Any]:
        """A free, non-generating call that proves the key is accepted."""
        spec = self._spec(provider_id)
        if provider_id == "replicate":
            token = self._credential("replicate", "api_token")
            response = self._request("GET", f"{REPLICATE_API}/account", provider_label=spec["label"], timeout=20,
                                     headers={"Authorization": f"Bearer {token}"})
            if response.status != 200:
                raise classify_http(response, spec["label"])
            account = response.json()
            username = str(account.get("username") or "") if isinstance(account, Mapping) else ""
            return {"ok": True, "message": f"Token accepted for Replicate account '{username or 'unknown'}'.",
                    "note": "This check does not prove there is credit; a job without credit fails with HTTP 402."}
        token = self._credential("cloudflare", "api_token")
        self._credential("cloudflare", "account_id")
        response = self._request("GET", f"{CLOUDFLARE_API}/user/tokens/verify", provider_label=spec["label"],
                                 timeout=20, headers={"Authorization": f"Bearer {token}"})
        payload = response.json()
        if response.status != 200 or not (isinstance(payload, Mapping) and payload.get("success")):
            raise classify_http(response, spec["label"])
        status = str((payload.get("result") or {}).get("status") or "")
        if status and status != "active":
            raise ExternalMediaError("auth_failed", f"Cloudflare reports the token as '{status}'.")
        return {"ok": True, "message": "Cloudflare token is active.",
                "note": "The token's Workers AI permission and Account ID are only proven by a generation."}

    # -- request normalisation and disclosure -----------------------------
    def normalise(self, params: Mapping[str, Any]) -> dict[str, Any]:
        provider_id = str(params.get("provider") or "")
        spec = self._spec(provider_id)
        modality = str(params.get("modality") or "")
        if modality not in spec["modalities"]:
            raise ExternalMediaError(
                "invalid_input", f"{spec['label']} does not offer {modality or 'that modality'} generation here.")
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            raise ExternalMediaError("invalid_input", "Write a description first.")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ExternalMediaError("invalid_input", f"The description is longer than {MAX_PROMPT_CHARS} characters.")
        model = spec["models"][modality]
        limits = model["limits"]
        out: dict[str, Any] = {"provider": provider_id, "modality": modality, "prompt": prompt,
                               "model": model["model"]}
        seed = params.get("seed")
        if seed not in (None, ""):
            try:
                seed = int(seed)
            except (TypeError, ValueError):
                raise ExternalMediaError("invalid_input", "Seed must be a whole number.") from None
            if not 0 <= seed <= 2**31 - 1:
                raise ExternalMediaError("invalid_input", "Seed must be between 0 and 2147483647.")
        else:
            seed = None
        if provider_id == "cloudflare":
            low, high = limits["steps"]
            steps = self._int(params.get("steps"), 4, "Steps")
            if not low <= steps <= high:
                raise ExternalMediaError("invalid_input", f"Steps must be between {low} and {high}.")
            out.update(steps=steps)
            if seed is not None:
                out["seed"] = seed
            return out
        aspect = str(params.get("aspect_ratio") or ("1:1" if modality == "image" else "16:9"))
        if aspect not in limits["aspect_ratios"]:
            raise ExternalMediaError(
                "invalid_input", f"Aspect ratio must be one of {', '.join(limits['aspect_ratios'])}.")
        out["aspect_ratio"] = aspect
        if seed is not None:
            out["seed"] = seed
        if modality == "video":
            resolution = str(params.get("resolution") or "480p")
            if resolution not in limits["resolutions"]:
                raise ExternalMediaError("invalid_input", "Resolution must be 480p or 720p.")
            frames = self._int(params.get("num_frames"), 81, "Frames")
            fps = self._int(params.get("frames_per_second"), 16, "Frames per second")
            f_low, f_high = limits["num_frames"]
            r_low, r_high = limits["frames_per_second"]
            if not f_low <= frames <= f_high:
                raise ExternalMediaError("invalid_input", f"Frames must be between {f_low} and {f_high}.")
            if not r_low <= fps <= r_high:
                raise ExternalMediaError("invalid_input", f"Frames per second must be between {r_low} and {r_high}.")
            out.update(resolution=resolution, num_frames=frames, frames_per_second=fps)
        return out

    @staticmethod
    def _int(value: Any, default: int, label: str) -> int:
        if value in (None, ""):
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ExternalMediaError("invalid_input", f"{label} must be a whole number.") from None

    def _estimate(self, request: Mapping[str, Any]) -> dict[str, Any]:
        provider_id, modality = request["provider"], request["modality"]
        model = PROVIDERS[provider_id]["models"][modality]
        if provider_id == "cloudflare":
            # Pricing page: 4.80 Neurons per 512×512 tile and 9.60 Neurons per step.
            neurons = round(4 * 4.80 + request["steps"] * 9.60, 1)
            return {
                "amount_usd": round(neurons * 0.011 / 1000, 5),
                "currency": "USD",
                "neurons": neurons,
                "free": True,
                "summary": (f"≈ {neurons} Neurons of the 10,000 free daily Neurons. No charge on the Free plan; "
                            f"≈ ${neurons * 0.011 / 1000:.4f} only if the account is on Workers Paid and past the "
                            "free allocation."),
                "basis": "4.80 Neurons per 512×512 tile (4 tiles at 1024×1024) + 9.60 Neurons per step",
                "source": "https://developers.cloudflare.com/workers-ai/platform/pricing/",
            }
        if modality == "image":
            amount = float(model["price_usd"])
        else:
            amount = float(model["price_usd"][request["resolution"]]) * max(1.0, request["num_frames"] / 81)
        return {
            "amount_usd": round(amount, 4),
            "currency": "USD",
            "free": False,
            "summary": f"≈ ${amount:.3f} charged to your Replicate account (estimate).",
            "basis": model["price_basis"],
            "source": model["price_url"],
        }

    def quote(self, params: Mapping[str, Any]) -> dict[str, Any]:
        request = self.normalise(params)
        spec = PROVIDERS[request["provider"]]
        model = spec["models"][request["modality"]]
        sent = [f"Description text ({len(request['prompt'])} characters)"]
        for key, label in (("aspect_ratio", "aspect ratio"), ("resolution", "resolution"),
                           ("num_frames", "frame count"), ("frames_per_second", "frame rate"),
                           ("steps", "steps"), ("seed", "seed")):
            if key in request:
                sent.append(f"{label}: {request[key]}")
        sent.append("Your API key (as the request's authorization header)")
        disclosure = {
            "provider": spec["label"],
            "provider_id": request["provider"],
            "model": model["model"],
            "model_label": model["label"],
            "model_license": model.get("license", ""),
            "modality": request["modality"],
            "endpoint_host": "api.replicate.com" if request["provider"] == "replicate" else "api.cloudflare.com",
            "data_sent": sent,
            "not_sent": "No files, images, audio, other prompts or gallery content are sent.",
            "cost": self._estimate(request),
            "account": spec["account"],
            "billing": spec["billing"],
            "retention": spec["retention"],
            "limits": model["limits"],
            "timeout_s": model["timeout_s"],
            "cancel": spec["cancel"],
            "cancel_note": spec["cancel_note"],
            "terms": spec["terms"],
            "docs": spec["docs"],
            "verified_on": VERIFIED_ON,
        }
        if request["modality"] == "video":
            disclosure["duration_s"] = round(request["num_frames"] / request["frames_per_second"], 2)
        token = hashlib.sha256(json.dumps([request, disclosure], sort_keys=True).encode("utf-8")).hexdigest()[:32]
        return {"request": request, "disclosure": disclosure, "confirm_token": token}

    # -- jobs --------------------------------------------------------------
    def submit(self, params: Mapping[str, Any], confirm_token: str) -> dict[str, Any]:
        quoted = self.quote(params)
        request = quoted["request"]
        provider_id = request["provider"]
        spec = PROVIDERS[provider_id]
        if not self._config["enabled"].get(provider_id):
            raise ExternalMediaError(
                "provider_disabled", f"{spec['label']} is not enabled. Enable it in Settings → External providers.")
        for cred in spec["credentials"]:
            self._credential(provider_id, cred["field"])
        if not confirm_token or confirm_token != quoted["confirm_token"]:
            raise ExternalMediaError(
                "confirmation_required",
                "This request was not confirmed with its current cost and data disclosure. Review it and confirm.")
        job_id = uuid.uuid4().hex[:16]
        job = {
            "id": job_id,
            "provider": provider_id,
            "provider_label": spec["label"],
            "modality": request["modality"],
            "request": request,
            "disclosure": quoted["disclosure"],
            "state": "queued",
            "progress": "Waiting for a free slot",
            "created_at": self._clock(),
            "updated_at": self._clock(),
            "remote_id": "",
            "cancel_requested": False,
            "error": None,
            "result": None,
            "notes": [],
        }
        with self._lock:
            self._jobs[job_id] = job
            self._save_jobs()
        self._launch(job_id)
        return self.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.get("created_at", 0), reverse=True)
            return [self._public(job) for job in jobs]

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if not job:
                raise ExternalMediaError("not_found", "No such external job.")
            return self._public(job)

    @staticmethod
    def _public(job: Mapping[str, Any]) -> dict[str, Any]:
        out = json.loads(json.dumps(job))
        out["can_cancel"] = job.get("state") not in TERMINAL_STATES and not job.get("cancel_requested")
        return out

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if not job:
                raise ExternalMediaError("not_found", "No such external job.")
            if job["state"] in TERMINAL_STATES:
                return self._public(job)
            job["cancel_requested"] = True
            if job["state"] == "queued":
                self._finish(job, "cancelled", note="Cancelled before anything was sent to the provider.")
            else:
                job["state"] = "cancelling"
                job["updated_at"] = self._clock()
                self._save_jobs()
            event = self._cancel_events.get(job["id"])
        if event:
            event.set()
        return self.get(job_id)

    def wait(self, job_id: str, timeout: float = 30.0) -> dict[str, Any]:
        """Test helper: wait for a job's worker thread to finish."""
        thread = self._threads.get(job_id)
        if thread:
            thread.join(timeout)
        return self.get(job_id)

    # -- worker ------------------------------------------------------------
    def _launch(self, job_id: str) -> None:
        event = threading.Event()
        self._cancel_events[job_id] = event
        if not self._start_threads:
            return
        thread = threading.Thread(target=self.run_job, args=(job_id,), name=f"external-{job_id}", daemon=True)
        self._threads[job_id] = thread
        thread.start()

    def _update(self, job: dict[str, Any], **fields: Any) -> None:
        with self._lock:
            job.update(fields)
            job["updated_at"] = self._clock()
            self._save_jobs()

    def _finish(self, job: dict[str, Any], state: str, *, error: Optional[ExternalMediaError] = None,
                note: str = "", save: bool = True) -> None:
        with self._lock:
            job["state"] = state
            job["updated_at"] = self._clock()
            job["finished_at"] = self._clock()
            if error is not None:
                job["error"] = error.to_dict()
                job["progress"] = error.message
            if note:
                job.setdefault("notes", []).append(note)
                if state != "failed":
                    job["progress"] = note
            if save:
                self._save_jobs()

    def run_job(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job or job.get("state") in TERMINAL_STATES:
            return
        event = self._cancel_events.setdefault(job_id, threading.Event())
        self._slots.acquire()
        try:
            if job.get("state") in TERMINAL_STATES:
                return
            if job.get("cancel_requested"):
                self._finish(job, "cancelled", note="Cancelled before anything was sent to the provider.")
                return
            if not self._config["enabled"].get(job["provider"]):
                raise ExternalMediaError("provider_disabled", f"{job['provider_label']} was disabled.")
            if job["provider"] == "cloudflare":
                body, remote_note = self._run_cloudflare(job, event)
            else:
                body, remote_note = self._run_replicate(job, event)
            if body is None:
                self._finish(job, "cancelled", note=remote_note)
                return
            self._store_result(job, body)
        except ExternalMediaError as error:
            self._finish(job, "failed", error=error)
        except Exception as error:  # noqa: BLE001 - a job must never stay 'running'
            self._finish(job, "failed", error=ExternalMediaError("internal_error", f"Unexpected error: {error}"))
        finally:
            self._slots.release()

    # Cloudflare Workers AI: one synchronous call that returns the image.
    def _run_cloudflare(self, job: dict[str, Any], event: threading.Event) -> tuple[Optional[bytes], str]:
        request = job["request"]
        spec = PROVIDERS["cloudflare"]
        model = spec["models"]["image"]
        token = self._credential("cloudflare", "api_token")
        account = self._credential("cloudflare", "account_id")
        payload: dict[str, Any] = {"prompt": request["prompt"], "steps": request["steps"]}
        if "seed" in request:
            payload["seed"] = request["seed"]
        self._update(job, state="running", progress="Sent to Cloudflare Workers AI; waiting for the image")
        response = self._request(
            "POST", f"{CLOUDFLARE_API}/accounts/{account}/ai/run/{model['model']}",
            provider_label=spec["label"], timeout=model["timeout_s"],
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        if event.is_set() or job.get("cancel_requested"):
            return None, ("Cancelled in Maestro. Cloudflare had already received the request, so it may have "
                          "consumed quota; its answer was discarded.")
        if response.status != 200:
            raise classify_http(response, spec["label"])
        content_type = response.headers.get("content-type", "")
        if content_type.startswith("image/"):
            return response.body, ""
        data = response.json()
        if not isinstance(data, Mapping) or not data.get("success", True):
            raise classify_http(HttpResponse(502, response.headers, response.body), spec["label"])
        result = data.get("result") if isinstance(data.get("result"), Mapping) else data
        image = result.get("image") if isinstance(result, Mapping) else None
        if not isinstance(image, str) or not image:
            raise ExternalMediaError("invalid_output", "Cloudflare answered without an image.")
        try:
            return base64.b64decode(image, validate=True), ""
        except (ValueError, TypeError) as exc:
            raise ExternalMediaError("invalid_output", "Cloudflare's image was not valid base64.") from exc

    # Replicate: create a prediction, poll it, cancel it remotely when asked.
    def _run_replicate(self, job: dict[str, Any], event: threading.Event) -> tuple[Optional[bytes], str]:
        request = job["request"]
        spec = PROVIDERS["replicate"]
        model = spec["models"][request["modality"]]
        label = spec["label"]
        token = self._credential("replicate", "api_token")
        auth = {"Authorization": f"Bearer {token}"}
        if not job.get("remote_id"):
            inputs: dict[str, Any] = {"prompt": request["prompt"], "aspect_ratio": request["aspect_ratio"]}
            if request["modality"] == "image":
                inputs.update(num_outputs=1, output_format="png")
            else:
                inputs.update(resolution=request["resolution"], num_frames=request["num_frames"],
                              frames_per_second=request["frames_per_second"])
            if "seed" in request:
                inputs["seed"] = request["seed"]
            self._update(job, state="submitting", progress="Creating the prediction on Replicate")
            response = self._request(
                "POST", f"{REPLICATE_API}/models/{model['model']}/predictions", provider_label=label, timeout=60,
                data=json.dumps({"input": inputs}).encode("utf-8"),
                headers={**auth, "Content-Type": "application/json"},
            )
            if response.status not in (200, 201, 202):
                raise classify_http(response, label)
            prediction = response.json()
            remote_id = str(prediction.get("id") or "") if isinstance(prediction, Mapping) else ""
            if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", remote_id):
                raise ExternalMediaError("invalid_output", "Replicate did not return a prediction id.")
            self._update(job, remote_id=remote_id, state="running",
                         progress=f"Prediction {remote_id} created; waiting for Replicate")
        remote_id = job["remote_id"]
        deadline = self._clock() + float(model["timeout_s"])
        poll = float(model.get("poll_s", 3.0))
        failures = 0
        while True:
            if event.is_set() or job.get("cancel_requested"):
                return None, self._cancel_replicate(remote_id, auth)
            if self._clock() > deadline:
                note = self._cancel_replicate(remote_id, auth)
                raise ExternalMediaError(
                    "timeout", f"Replicate did not finish within {int(model['timeout_s'])} s. {note}")
            try:
                response = self._request("GET", f"{REPLICATE_API}/predictions/{remote_id}", provider_label=label,
                                         timeout=30, headers=auth)
            except ExternalMediaError as error:
                failures += 1
                if failures >= 5:
                    raise ExternalMediaError(
                        error.code, f"{error.message} The prediction {remote_id} may still run on Replicate.") from error
                event.wait(poll)
                continue
            if response.status != 200:
                error = classify_http(response, label)
                if error.code in ("provider_unavailable", "rate_limited") and failures < 5:
                    failures += 1
                    event.wait(max(poll, error.retry_after or 0))
                    continue
                raise error
            failures = 0
            prediction = response.json()
            status = str(prediction.get("status") or "")
            if status == "succeeded":
                url = self._first_output_url(prediction.get("output"))
                self._update(job, state="downloading", progress="Downloading the result from Replicate")
                metrics = prediction.get("metrics") if isinstance(prediction.get("metrics"), Mapping) else {}
                if metrics.get("predict_time") is not None:
                    job["predict_time_s"] = metrics.get("predict_time")
                return self._download(url, label), ""
            if status == "failed":
                raise ExternalMediaError("provider_failed",
                                         f"Replicate reports the prediction failed: {prediction.get('error') or 'no reason given'}")
            if status in ("canceled", "cancelled"):
                return None, "The prediction was cancelled on Replicate."
            logs = str(prediction.get("logs") or "").strip().splitlines()
            progress = f"Replicate: {status or 'waiting'}"
            if logs:
                progress += f" — {logs[-1][:160]}"
            if progress != job.get("progress"):
                self._update(job, progress=progress, state="running")
            event.wait(poll)

    def _cancel_replicate(self, remote_id: str, auth: Mapping[str, str]) -> str:
        try:
            response = self._request("POST", f"{REPLICATE_API}/predictions/{remote_id}/cancel",
                                     provider_label="Replicate", timeout=30, headers=dict(auth))
        except ExternalMediaError as error:
            return f"Cancelling on Replicate failed ({error.message}); the prediction may continue and be billed."
        if response.status in (200, 201, 202):
            return "Cancelled on Replicate. Time already used may be billed."
        return (f"Replicate refused the cancel ({response.status}: {_provider_message(response)}); "
                "the prediction may continue and be billed.")

    @staticmethod
    def _first_output_url(output: Any) -> str:
        candidates = output if isinstance(output, list) else [output]
        for item in candidates:
            if isinstance(item, str) and item.startswith("https://"):
                return item
        raise ExternalMediaError("invalid_output", "Replicate reported success but returned no downloadable file.")

    def _download(self, url: str, label: str) -> bytes:
        response = self._request("GET", url, provider_label=label, timeout=300)
        if response.status != 200:
            raise ExternalMediaError("download_failed", f"Downloading the result failed with HTTP {response.status}.",
                                     http_status=response.status)
        if len(response.body) > MAX_DOWNLOAD_BYTES:
            raise ExternalMediaError("invalid_output", "The result is larger than Maestro accepts (300 MB).")
        return response.body

    def _store_result(self, job: dict[str, Any], body: bytes) -> None:
        kind, ext = detect_media(body)
        if not kind:
            raise ExternalMediaError("invalid_output", "The provider's answer is not a recognised image or video file.")
        if kind != job["modality"]:
            raise ExternalMediaError("invalid_output", f"A {job['modality']} was requested but a {kind} was returned.")
        workspace, directory = self._output_dir()
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.fromtimestamp(self._clock()).strftime("%Y-%m-%d-%Hh%Mm%Ss")
        name = f"{stamp}_external_{job['provider']}_{job['id']}{ext}"
        path = os.path.join(directory, name)
        tmp = path + ".part"
        with open(tmp, "wb") as handle:
            handle.write(body)
        os.replace(tmp, path)
        request = job["request"]
        meta = {
            "generation_mode": f"external_{job['modality']}",
            "prompt": request["prompt"],
            "params": {
                "prompt": request["prompt"],
                "external_provider": job["provider_label"],
                "external_model": request["model"],
                "external_job_id": job["id"],
                "external_remote_id": job.get("remote_id", ""),
                **{k: v for k, v in request.items() if k not in ("prompt", "provider")},
            },
            "external": {
                "provider": job["provider"],
                "model": request["model"],
                "remote_id": job.get("remote_id", ""),
                "cost_estimate": job["disclosure"]["cost"],
                "generated_at": datetime.fromtimestamp(self._clock()).isoformat(timespec="seconds"),
            },
        }
        with open(os.path.splitext(path)[0] + ".meta.json", "w", encoding="utf-8") as handle:
            json.dump(meta, handle, indent=1)
        result = {
            "filename": name,
            "workspace": workspace,
            "bytes": len(body),
            "kind": kind,
            "url": f"/api/v1/file/{quote(name, safe='')}?workspace={quote(workspace, safe='')}",
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        with self._lock:
            job["result"] = result
        self._finish(job, "succeeded", note=f"Saved {name} ({len(body):,} bytes) to workspace '{workspace}'.")

"""Download Maestro's models on any computer — no NVIDIA GPU, no PyTorch, standard library only.

The model files a Maestro install needs are listed in ``model_manifest.json``
(generated from ``app/defaults`` and every family handler by
``scripts/build_model_manifest.py``).  This module turns that list into a download
plan for one model and executes it, placing every file exactly where Maestro's own
loader looks (``ckpts/…``, ``loras/<family>/…``, ``ckpts/llm/…``), so a model fetched
here is used as-is by Maestro — on this machine or after copying the folder to
another one.

What it guarantees:

* **Hardware-aware formats.**  A definition often lists several precisions of the
  same weights.  NVFP4, Nunchaku and FP8-scaled files only run (or only run well)
  on NVIDIA hardware, so on a CPU, AMD, Intel or Apple machine they are never the
  default; full precision (BF16/FP16) or a portable GGUF is chosen instead, and the
  reason is reported.
* **Only what is really free to download.**  Each repository is asked
  (``/api/models/{repo}?blobs=true``) whether it is public, gated or gone, and what
  each file weighs and hashes to.  A gated repo is not fetched without the user's
  own token (and accepted licence on Hugging Face); nothing is guessed.
* **Licences acknowledged.**  A model whose licence is non-commercial, research
  only, or excludes a territory (HunyuanVideo and MiniMax H3 exclude the EU) is only
  downloaded with ``accept_license=True``.
* **Verified, resumable transfers.**  Downloads go to ``.part`` files, resume with
  HTTP ranges, are checked against the size and SHA-256 published by the Hub, and are
  renamed into place only when they match.  Cancel stops between chunks and keeps
  the partial file for a later resume.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import threading
import time
import uuid
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import quote, urlsplit

from . import model_access

__all__ = [
    "MANIFEST_PATH", "DownloadError", "HubClient", "DownloadManager", "load_manifest", "catalog", "plan",
    "hardware_profile", "variant_info",
]

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_manifest.json")
HF = "https://huggingface.co"
CHUNK = 1024 * 1024
DISK_MARGIN = 1024 ** 3


class DownloadError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


# ---------------------------------------------------------------------------
# Manifest and catalogue
# ---------------------------------------------------------------------------

_MANIFEST_CACHE: dict[str, Any] = {}


def load_manifest(path: str = MANIFEST_PATH) -> dict[str, Any]:
    if path not in _MANIFEST_CACHE:
        with open(path, encoding="utf-8") as handle:
            _MANIFEST_CACHE[path] = json.load(handle)
    return _MANIFEST_CACHE[path]


_FORMATS = [  # (regex on the lower-case filename, format id, label, nvidia_only, portable)
    (r"nvfp4", "nvfp4", "NVFP4 (NVIDIA Blackwell only)", True, False),
    (r"nunchaku|svdq", "nunchaku", "Nunchaku INT4/FP4 (NVIDIA CUDA only)", True, False),
    (r"\.gguf$|gguf_q|q[2-8]_k|q8_0", "gguf", "GGUF quantised (portable: CPU, Vulkan, Metal)", False, True),
    (r"fp8|e4m3|e5m2", "fp8", "FP8 (fast on NVIDIA Ada/Hopper; other hardware must upcast)", True, False),
    (r"int8|quanto", "int8", "INT8 (quanto; half the size of BF16)", False, False),
    (r"bf16|fp16|f16|fp32", "full", "BF16/FP16 full precision (best quality, any hardware)", False, True),
]


def variant_info(url: str) -> dict[str, Any]:
    name = url.rsplit("/", 1)[-1].split("?")[0]
    lowered = name.lower()
    for pattern, fmt, label, nvidia_only, portable in _FORMATS:
        if re.search(pattern, lowered):
            return {"url": url, "filename": name, "format": fmt, "label": label,
                    "nvidia_only": nvidia_only, "portable": portable}
    return {"url": url, "filename": name, "format": "full", "label": "Single precision as published",
            "nvidia_only": False, "portable": True}


_STEM_TOKENS = re.compile(
    r"(_|-|\.)(quanto|mbf16|mfp16|bf16|fp16|f16|fp32|int8|fp8|e4m3fn|e5m2|scaled|nvfp4|mixed|"
    r"q[2-8]_k(_[sml])?|q8_0|gguf|nunchaku|svdq|r\d+)", re.I)


def _preference(profile: str) -> list[str]:
    if profile == "compact":
        return ["gguf", "int8", "full", "fp8", "nunchaku", "nvfp4"]
    return ["full", "int8", "gguf", "fp8", "nunchaku", "nvfp4"]


def choose_one(alternatives: list[str], *, profile: str = "quality", accelerator: str = "cpu",
               explicit: str = "") -> tuple[Optional[str], list[str]]:
    """Pick one file among a definition's alternative precisions of the *same* weights.

    Maestro's loader treats each ``URLs`` list (and each ``modules`` group) as
    alternatives and loads exactly one, so exactly one is downloaded.
    """
    if not alternatives:
        return None, []
    if explicit:
        named = [u for u in alternatives if u.rsplit("/", 1)[-1] == explicit]
        if named:
            return named[0], [f"Using the requested file {explicit}."]
    candidates = [variant_info(u) for u in alternatives]
    usable = [c for c in candidates if accelerator == "cuda" or not c["nvidia_only"]]
    notes = []
    if not usable:
        usable = candidates
        notes.append(f"Only NVIDIA-specific files exist for {candidates[0]['filename']}: they will not run "
                     "without an NVIDIA GPU.")
    order = _preference(profile)
    usable.sort(key=lambda c: order.index(c["format"]))
    best = usable[0]
    others = [c["filename"] for c in candidates if c["url"] != best["url"]]
    if others:
        notes.append(f"Chose {best['filename']} ({best['label']}); alternatives: {', '.join(others)}.")
    return best["url"], notes


def _license_needs_ack(access: Mapping[str, Any]) -> list[str]:
    reasons = []
    text = str(access.get("license", "")).lower()
    if access.get("territory"):
        reasons.append(access["territory"])
    if "non-commercial" in text or "noncommercial" in text or "research" in text or "nc)" in text:
        reasons.append(f"Licence: {access.get('license')}")
    return reasons


def _items(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = []
    for model_id, entry in manifest["models"].items():
        out.append({"id": model_id, "kind": "model", "name": entry["name"], "modality": entry["modality"],
                    "description": entry.get("description", ""), "visible": entry.get("visible", True),
                    "family": model_access.family_for(model_id)})
    for repo, entry in manifest.get("llm", {}).items():
        out.append({"id": f"llm:{repo}", "kind": "llm", "name": entry["name"], "modality": "text",
                    "description": entry.get("size_hint", ""), "visible": True,
                    "family": model_access.family_for(repo, "llm")})
    for key, entry in manifest.get("extras", {}).items():
        out.append({"id": f"extra:{key}", "kind": "extra", "name": entry["name"], "modality": entry["modality"],
                    "description": "", "visible": True, "family": model_access.family_for(key, "extra")})
    return out


def hardware_profile(override: str = "") -> dict[str, Any]:
    """Which accelerator this computer offers, measured as cheaply as possible.

    ``override`` (``cuda``/``rocm``/``mps``/``directml``/``vulkan``/``cpu``) lets a user
    plan downloads for a different machine.
    """
    if override:
        return {"accelerator": override, "basis": "chosen by the user"}
    system, machine = platform.system(), platform.machine().lower()
    if shutil.which("nvidia-smi"):
        return {"accelerator": "cuda", "basis": "nvidia-smi is present"}
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return {"accelerator": "mps", "basis": "Apple Silicon"}
    if system == "Linux" and (shutil.which("rocminfo") or os.path.exists("/dev/kfd")):
        return {"accelerator": "rocm", "basis": "ROCm runtime present"}
    if shutil.which("vulkaninfo"):
        return {"accelerator": "vulkan", "basis": "a Vulkan runtime is present"}
    return {"accelerator": "cpu", "basis": "no GPU runtime detected"}


_HOW_TO_RUN = {
    "cuda": "Maestro's built-in engine runs it on this NVIDIA GPU (VRAM permitting).",
    "rocm": "Use ComfyUI with ROCm (supported Radeon only) or stable-diffusion.cpp with Vulkan; video needs ≥ 12 GB VRAM.",
    "mps": "Use ComfyUI on Apple Silicon (MPS) or stable-diffusion.cpp with Metal; unified memory must hold the weights.",
    "directml": "Use ComfyUI with --directml or stable-diffusion.cpp with Vulkan.",
    "vulkan": "Use stable-diffusion.cpp with Vulkan (GGUF) for image/video, llama.cpp for text.",
    "cpu": "Text and transcription run well; images are slow; video is not practical — render on a computer of yours "
           "with a GPU (Studio nodes), on free public compute, or through a free API listed here.",
}


def catalog(*, accelerator: str = "cpu", manifest: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
    manifest = manifest or load_manifest()
    out = []
    for item in _items(manifest):
        access = model_access.access_for(item["family"], item["id"])
        feasibility = model_access.local_feasibility(item["name"], item["modality"])
        how = _HOW_TO_RUN.get(accelerator, _HOW_TO_RUN["cpu"])
        if item["modality"] == "text" and accelerator != "cuda":
            how = "Runs on this computer's CPU (llama.cpp / faster-whisper are built in)."
        try:
            notes = plan(item["id"], accelerator=accelerator, manifest=manifest)["notes"]
        except DownloadError:
            notes = []
        nvidia_only = [n for n in notes if n.startswith("Only NVIDIA-specific")]
        if nvidia_only and accelerator != "cuda":
            how = ("This variant ships NVIDIA-only weights; pick a portable sibling of the same family "
                   "(BF16, INT8 or GGUF) on this computer. " + how)
        out.append({**item, "access": access, "feasibility": feasibility, "run_here": how,
                    "portable_here": not nvidia_only or accelerator == "cuda",
                    "license_ack_required": _license_needs_ack(access)})
    return out


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------

_HF_URL = re.compile(r"^https://huggingface\.co/([^/]+/[^/]+)/resolve/([^/]+)/(.+)$")


def _file(url: str, target: str, role: str) -> dict[str, Any]:
    match = _HF_URL.match(url)
    spec = {"url": url, "target": target.replace("\\", "/"), "role": role, "repo": "", "path": "", "revision": ""}
    if match:
        spec.update(repo=match.group(1), revision=match.group(2), path=match.group(3).split("?")[0])
    return spec


def _hf_file(repo: str, path: str, revision: str, target: str, role: str) -> dict[str, Any]:
    rev = revision or "main"
    url = f"{HF}/{repo}/resolve/{quote(rev, safe='')}/{quote(path)}"
    return {"url": url, "target": target.replace("\\", "/"), "role": role, "repo": repo, "path": path,
            "revision": rev}


def plan(item_id: str, *, profile: str = "quality", accelerator: str = "cpu", variant: str = "",
         include_loras: bool = True, include_shared: bool = False,
         manifest: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Files to fetch for one catalogue item, with targets relative to the app folder.

    ``ckpts/`` is Maestro's primary model folder; the manager maps it onto the
    configured one.
    """
    manifest = manifest or load_manifest()
    files: list[dict[str, Any]] = []
    notes: list[str] = []
    if item_id.startswith("llm:"):
        repo = item_id[4:]
        entry = manifest.get("llm", {}).get(repo)
        if not entry:
            raise DownloadError("unknown_model", f"Unknown model '{item_id}'.")
        files.append(_hf_file(repo, entry["gguf_file"], "", f"ckpts/{entry['folder']}/{entry['gguf_file']}", "weights"))
        if entry.get("mmproj_file"):
            files.append(_hf_file(entry.get("mmproj_repo") or repo, entry["mmproj_file"], "",
                                  f"ckpts/{entry['folder']}/{entry['mmproj_file']}", "vision projector"))
        notes.append("GGUF runs on any CPU through Maestro's built-in llama.cpp.")
    elif item_id.startswith("extra:"):
        entry = manifest.get("extras", {}).get(item_id[6:])
        if not entry:
            raise DownloadError("unknown_model", f"Unknown model '{item_id}'.")
        for comp in entry.get("companions", []):
            for name in comp["files"]:
                rel = "/".join(p for p in (comp["folder"], name) if p)
                files.append(_hf_file(comp["repo"], rel, comp.get("revision", ""),
                                      f"ckpts/{comp.get('target') or comp['folder']}/{name}".replace("//", "/"),
                                      "component"))
        cache = entry.get("hf_cache")
        if cache:
            base = f"ckpts/{cache['folder']}/models--{cache['repo'].replace('/', '--')}"
            for name in cache["files"]:
                files.append({**_hf_file(cache["repo"], name, "", f"{base}/snapshots/<revision>/{name}", "weights"),
                              "hf_cache": {"folder": f"ckpts/{cache['folder']}", "repo": cache["repo"]}})
    else:
        entry = manifest["models"].get(item_id)
        if not entry:
            raise DownloadError("unknown_model", f"Unknown model '{item_id}'.")
        groups = [("transformer", entry.get("URLs", [])), ("transformer (second stage)", entry.get("URLs2", []))]
        groups += [("module", group) for group in entry.get("modules", [])]
        for role, alternatives in groups:
            picked, why = choose_one(alternatives, profile=profile, accelerator=accelerator, explicit=variant)
            notes += why
            if picked:
                files.append(_file(picked, f"ckpts/{variant_info(picked)['filename']}", role))
        for key, role in (("preload_URLs", "preloaded component"), ("VAE_URLs", "VAE")):
            files += [_file(u, f"ckpts/{variant_info(u)['filename']}", role) for u in entry.get(key, [])]
        picked, why = choose_one(entry.get("text_encoder_URLs", []), profile=profile, accelerator=accelerator)
        notes += why
        folder = entry.get("text_encoder_folder", "")
        if picked:
            files.append(_file(picked, "/".join(p for p in ("ckpts", folder, variant_info(picked)["filename"]) if p),
                               "text encoder"))
        if include_loras and entry.get("lora_dir"):
            files += [_file(u, f"{entry['lora_dir']}/{variant_info(u)['filename']}", "built-in LoRA")
                      for u in entry.get("loras", [])]
        for comp in entry.get("companions", []):
            for name in comp["files"]:
                rel = "/".join(p for p in (comp["folder"], name) if p)
                target = "/".join(p for p in ("ckpts", comp.get("target") or comp["folder"], name) if p)
                files.append(_hf_file(comp["repo"], rel, comp.get("revision", ""), target, "component"))
        if entry.get("companion_error"):
            notes.append(f"Some components could not be listed: {entry['companion_error']}")
        if include_shared:
            for comp in manifest.get("shared", []):
                for name in comp["files"]:
                    files.append(_hf_file(comp["repo"], f"{comp['folder']}/{name}", "",
                                          f"ckpts/{comp['folder']}/{name}", f"shared: {comp['purpose']}"))
    unique: dict[str, dict[str, Any]] = {}
    for spec in files:
        unique.setdefault(spec["target"] or spec["url"], spec)
    return {"id": item_id, "profile": profile, "accelerator": accelerator, "files": list(unique.values()),
            "notes": notes}


# ---------------------------------------------------------------------------
# Hugging Face metadata and transport
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status: int, headers: Mapping[str, str], stream: Any) -> None:
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.stream = stream

    def read(self, size: int = -1) -> bytes:
        return self.stream.read(size) if self.stream is not None else b""

    def close(self) -> None:
        if self.stream is not None:
            self.stream.close()


class _NoAuthAcrossHosts(urlrequest.HTTPRedirectHandler):
    """Hub downloads redirect to a CDN; the token must not follow them there."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlsplit(newurl).hostname != urlsplit(req.full_url).hostname:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_OPENER = urlrequest.build_opener(_NoAuthAcrossHosts())


def default_open(url: str, headers: Mapping[str, str], timeout: float) -> _Response:
    request = urlrequest.Request(url, headers={"User-Agent": "maestro-model-downloads", **dict(headers)})
    try:
        response = _OPENER.open(request, timeout=timeout)
        return _Response(response.status, dict(response.headers), response)
    except urlerror.HTTPError as error:
        return _Response(error.code, dict(error.headers or {}), error)


def hf_token() -> str:
    token = str(os.environ.get("HF_TOKEN") or "").strip()
    if token:
        return token
    for path in (os.environ.get("HF_TOKEN_PATH"), os.path.expanduser("~/.cache/huggingface/token")):
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    return handle.read().strip()
            except OSError:
                pass
    return ""


class HubClient:
    def __init__(self, opener: Callable[..., _Response] = default_open, token: Optional[str] = None) -> None:
        self._open = opener
        self._token = hf_token() if token is None else token
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def auth(self, url: str) -> dict[str, str]:
        if self._token and urlsplit(url).hostname == "huggingface.co":
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    def repo(self, repo: str, revision: str = "main") -> dict[str, Any]:
        """Public/gated status, licence and per-file size + SHA-256 for one repository."""
        key = (repo, revision or "main")
        if key in self._cache:
            return self._cache[key]
        path = f"/api/models/{repo}" + (f"/revision/{quote(revision, safe='')}" if revision not in ("", "main") else "")
        url = f"{HF}{path}?blobs=true"
        try:
            response = self._open(url, self.auth(url), 30)
        except (urlerror.URLError, OSError) as exc:
            raise DownloadError("network_error", f"Could not reach Hugging Face: {exc}") from exc
        try:
            body = response.read()
        finally:
            response.close()
        if response.status in (401, 403):
            info = {"repo": repo, "status": "gated_or_private", "files": {},
                    "message": "Hugging Face requires a signed-in account that accepted this model's licence."}
        elif response.status == 404:
            info = {"repo": repo, "status": "not_found", "files": {}, "message": "Repository not found."}
        elif response.status != 200:
            raise DownloadError("hub_error", f"Hugging Face answered HTTP {response.status} for {repo}.")
        else:
            data = json.loads(body.decode("utf-8"))
            gated = data.get("gated") or False
            card = data.get("cardData") or {}
            files = {}
            for sibling in data.get("siblings") or []:
                lfs = sibling.get("lfs") or {}
                files[sibling.get("rfilename")] = {"size": lfs.get("size") or sibling.get("size"),
                                                   "sha256": lfs.get("sha256") or ""}
            status = "public" if not gated and not data.get("private") and not data.get("disabled") else (
                "gated" if gated else "unavailable")
            info = {"repo": repo, "status": status, "gated": gated, "sha": data.get("sha", ""),
                    "license": card.get("license") or "", "files": files,
                    "message": "" if status == "public" else
                    f"Gated ({gated}): accept the licence on {HF}/{repo} with your free account and connect "
                    "Hugging Face (launcher action or HF_TOKEN)."}
        self._cache[key] = info
        return info


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

TERMINAL = {"succeeded", "failed", "cancelled"}


class DownloadManager:
    def __init__(self, app_dir: str, *, checkpoints_root: str = "ckpts", extra_roots: Iterable[str] = (),
                 state_dir: Optional[str] = None, hub: Optional[HubClient] = None,
                 opener: Callable[..., _Response] = default_open, start_threads: bool = True,
                 disk_usage: Callable[[str], Any] = shutil.disk_usage,
                 manifest: Optional[Mapping[str, Any]] = None) -> None:
        self.manifest = manifest
        self.app_dir = os.path.abspath(app_dir)
        root = checkpoints_root if os.path.isabs(checkpoints_root) else os.path.join(self.app_dir, checkpoints_root)
        self.ckpts = os.path.abspath(root)
        self.extra_roots = [os.path.abspath(r if os.path.isabs(r) else os.path.join(self.app_dir, r))
                            for r in extra_roots]
        self.state_dir = state_dir or os.path.join(self.app_dir, "settings", "model_downloads")
        os.makedirs(self.state_dir, exist_ok=True)
        self.hub = hub or HubClient(opener)
        self._open = opener
        self._disk_usage = disk_usage
        self._start_threads = start_threads
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._events: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._queue = threading.Semaphore(1)
        self._load()

    # -- paths ----------------------------------------------------------------
    def resolve(self, target: str) -> str:
        if target.startswith("ckpts/"):
            return os.path.join(self.ckpts, *target[6:].split("/"))
        if target == "ckpts":
            return self.ckpts
        return os.path.join(self.app_dir, *target.split("/"))

    def existing(self, target: str) -> Optional[str]:
        primary = self.resolve(target)
        if os.path.isfile(primary):
            return primary
        if target.startswith("ckpts/"):
            for root in self.extra_roots:
                candidate = os.path.join(root, *target[6:].split("/"))
                if os.path.isfile(candidate):
                    return candidate
        return None

    # -- persistence ------------------------------------------------------------
    def _state_file(self) -> str:
        return os.path.join(self.state_dir, "downloads.json")

    def _load(self) -> None:
        try:
            with open(self._state_file(), encoding="utf-8") as handle:
                jobs = json.load(handle).get("jobs", [])
        except (OSError, ValueError, AttributeError):
            jobs = []
        for job in jobs:
            if job.get("state") not in TERMINAL:
                job["state"] = "failed"
                job["error"] = {"code": "interrupted",
                                "message": "Maestro stopped during this download. Start it again to resume; "
                                           "partial files are kept."}
            self._jobs[job["id"]] = job

    def _save(self) -> None:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)[:100]
            tmp = self._state_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"jobs": jobs}, handle, indent=1)
            os.replace(tmp, self._state_file())

    # -- inspection -------------------------------------------------------------
    def inspect(self, item_plan: Mapping[str, Any]) -> dict[str, Any]:
        """Sizes, hashes and availability of a plan's files, from the Hub's own metadata."""
        files, repos, total, missing = [], {}, 0, 0
        for spec in item_plan["files"]:
            entry = dict(spec)
            if spec.get("hf_cache"):
                snapshots = self.resolve(spec["target"].split("/snapshots/")[0] + "/snapshots")
                entry["present"] = os.path.isdir(snapshots) and any(
                    os.path.isfile(os.path.join(snapshots, rev, spec["path"])) for rev in os.listdir(snapshots))
            else:
                entry["present"] = self.existing(spec["target"]) is not None
            if spec["repo"]:
                info = repos.get((spec["repo"], spec["revision"]))
                if info is None:
                    info = self.hub.repo(spec["repo"], spec["revision"])
                    repos[(spec["repo"], spec["revision"])] = info
                meta = info["files"].get(spec["path"])
                entry["availability"] = info["status"]
                entry["size"] = meta.get("size") if meta else None
                entry["sha256"] = meta.get("sha256") if meta else ""
                if info["status"] == "public" and meta is None:
                    entry["availability"] = "missing_file"
            else:
                entry.update(availability="external", size=None, sha256="")
            if not entry["present"]:
                missing += 1
                total += int(entry.get("size") or 0)
            files.append(entry)
        repo_list = [{k: v for k, v in info.items() if k != "files"} for info in repos.values()]
        # A gated repo's metadata is public, but its files need a token whose account accepted the licence.
        allowed = {"public", "external"} | ({"gated"} if self.hub.has_token else set())
        blocked = [f for f in files if not f["present"] and f["availability"] not in allowed]
        return {**{k: v for k, v in item_plan.items() if k != "files"}, "files": files, "repos": repo_list,
                "bytes_to_download": total, "files_to_download": missing,
                "free_to_download": not blocked, "blocked": [f["target"] or f["url"] for f in blocked]}

    # -- jobs ------------------------------------------------------------------
    def start(self, item_id: str, *, profile: str = "quality", accelerator: str = "cpu", variant: str = "",
              accept_license: bool = False, include_loras: bool = True) -> dict[str, Any]:
        info = next((i for i in catalog(accelerator=accelerator, manifest=self.manifest) if i["id"] == item_id), None)
        if info is None:
            raise DownloadError("unknown_model", f"Unknown model '{item_id}'.")
        if info["license_ack_required"] and not accept_license:
            raise DownloadError("license_ack_required",
                                "Accept the licence terms first: " + " · ".join(info["license_ack_required"]))
        item_plan = plan(item_id, profile=profile, accelerator=accelerator, variant=variant,
                         include_loras=include_loras, manifest=self.manifest)
        job = {"id": uuid.uuid4().hex[:12], "item": item_id, "name": info["name"], "plan": item_plan,
               "state": "queued", "created_at": time.time(), "updated_at": time.time(), "bytes_done": 0,
               "bytes_total": None, "current": "", "files_done": 0, "files_total": len(item_plan["files"]),
               "error": None, "log": [], "cancel_requested": False}
        with self._lock:
            self._jobs[job["id"]] = job
            self._events[job["id"]] = threading.Event()
            self._save()
        if self._start_threads:
            thread = threading.Thread(target=self.run, args=(job["id"],), daemon=True, name=f"download-{job['id']}")
            self._threads[job["id"]] = thread
            thread.start()
        return self.get(job["id"])

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise DownloadError("not_found", "No such download.")
            return json.loads(json.dumps(job))

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(json.dumps(j)) for j in
                    sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)]

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise DownloadError("not_found", "No such download.")
            if job["state"] not in TERMINAL:
                job["cancel_requested"] = True
                if job["state"] == "queued":
                    job["state"] = "cancelled"
                self._save()
            event = self._events.get(job_id)
        if event:
            event.set()
        return self.get(job_id)

    def wait(self, job_id: str, timeout: float = 30) -> dict[str, Any]:
        thread = self._threads.get(job_id)
        if thread:
            thread.join(timeout)
        return self.get(job_id)

    def _update(self, job: dict[str, Any], **fields: Any) -> None:
        with self._lock:
            job.update(fields)
            job["updated_at"] = time.time()

    def run(self, job_id: str) -> None:
        job = self._jobs[job_id]
        event = self._events.setdefault(job_id, threading.Event())
        with self._queue:
            if job["state"] in TERMINAL:
                return
            try:
                self._update(job, state="checking", current="Asking Hugging Face what each file weighs")
                report = self.inspect(job["plan"])
                if report["blocked"]:
                    raise DownloadError("not_free_to_download",
                                        "These files are not publicly downloadable: " + ", ".join(report["blocked"][:5])
                                        + ". Accept the licence on Hugging Face and connect your account, then retry.")
                todo = [f for f in report["files"] if not f["present"]]
                need = sum(int(f.get("size") or 0) for f in todo)
                os.makedirs(self.ckpts, exist_ok=True)
                free = self._disk_usage(self.ckpts).free
                if need and free < need + DISK_MARGIN:
                    raise DownloadError("disk_full", f"Needs {need / 1024**3:.1f} GB plus 1 GB margin; "
                                                     f"{free / 1024**3:.1f} GB free in {self.ckpts}.")
                self._update(job, state="downloading", bytes_total=need, bytes_done=0,
                             files_done=len(report["files"]) - len(todo))
                for spec in todo:
                    if event.is_set():
                        break
                    self._fetch(job, spec, event)
                    with self._lock:
                        job["files_done"] += 1
                if event.is_set() or job.get("cancel_requested"):
                    self._update(job, state="cancelled", current="Cancelled; partial files are kept for a resume")
                else:
                    self._update(job, state="succeeded", current=f"{len(todo)} file(s) downloaded and verified")
            except DownloadError as error:
                self._update(job, state="failed", error=error.to_dict(), current=error.message)
            except Exception as error:  # noqa: BLE001 - never leave a job running
                self._update(job, state="failed", error={"code": "internal_error", "message": str(error)},
                             current=str(error))
            finally:
                self._save()

    def _destination(self, spec: Mapping[str, Any]) -> str:
        cache = spec.get("hf_cache")
        if not cache:
            return self.resolve(spec["target"])
        info = self.hub.repo(cache["repo"])
        folder = self.resolve(cache["folder"])
        base = os.path.join(folder, "models--" + cache["repo"].replace("/", "--"))
        os.makedirs(os.path.join(base, "refs"), exist_ok=True)
        with open(os.path.join(base, "refs", "main"), "w", encoding="utf-8") as handle:
            handle.write(info.get("sha") or "main")
        return os.path.join(base, "snapshots", info.get("sha") or "main", spec["path"])

    def _fetch(self, job: dict[str, Any], spec: Mapping[str, Any], event: threading.Event) -> None:
        dest = self._destination(spec)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".part"
        expected_size = int(spec.get("size") or 0) or None
        expected_sha = str(spec.get("sha256") or "")
        digest = hashlib.sha256()
        have = 0
        if os.path.isfile(part):
            with open(part, "rb") as handle:
                for block in iter(lambda: handle.read(CHUNK), b""):
                    digest.update(block)
                    have += len(block)
            if expected_size is not None and have > expected_size:
                os.remove(part)
                digest, have = hashlib.sha256(), 0
        self._update(job, current=f"{os.path.basename(dest)} ({spec['role']})")
        with self._lock:
            job["bytes_done"] += have
        if expected_size is None or have < expected_size:
            headers = {**self.hub.auth(spec["url"])}
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                response = self._open(spec["url"], headers, 60)
            except (urlerror.URLError, OSError) as exc:
                raise DownloadError("network_error", f"{spec['url']}: {exc}") from exc
            try:
                if response.status == 200 and have:
                    with self._lock:  # the server ignored the range: start over
                        job["bytes_done"] -= have
                    digest, have = hashlib.sha256(), 0
                    mode = "wb"
                elif response.status == 206:
                    mode = "ab"
                elif response.status == 200:
                    mode = "wb"
                elif response.status in (401, 403):
                    raise DownloadError("not_free_to_download", f"{spec['url']} requires authorisation "
                                                                f"(HTTP {response.status}).")
                else:
                    raise DownloadError("http_error", f"{spec['url']} answered HTTP {response.status}.")
                with open(part, mode) as handle:
                    while True:
                        if event.is_set():
                            return
                        block = response.read(CHUNK)
                        if not block:
                            break
                        handle.write(block)
                        digest.update(block)
                        have += len(block)
                        with self._lock:
                            job["bytes_done"] += len(block)
                            job["updated_at"] = time.time()
            finally:
                response.close()
        if expected_size is not None and have != expected_size:
            raise DownloadError("size_mismatch", f"{os.path.basename(dest)}: got {have} bytes, expected {expected_size}.")
        if expected_sha and digest.hexdigest() != expected_sha:
            os.remove(part)
            raise DownloadError("checksum_mismatch", f"{os.path.basename(dest)}: SHA-256 does not match the Hub's; "
                                                     "the partial file was removed.")
        os.replace(part, dest)
        with self._lock:
            job["log"].append({"file": dest, "bytes": have, "sha256": digest.hexdigest(),
                               "verified": bool(expected_sha)})

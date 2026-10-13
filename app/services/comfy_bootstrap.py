"""Set up the local ComfyUI engine on this computer — measured, confirmed, proven.

This module is the bridge between "your machine" and "Maestro can generate a
clip here".  It does four things, and it refuses to do them dishonestly:

1. **Plan** (:func:`install_plan`) — exactly which commands would run, which
   files would be downloaded, how many GB that is, and what is already present.
   No side effects: it is what the setup screen shows before asking anything.
2. **Install** (:func:`install`) — runs the plan only with explicit confirmation,
   and **never downloads the model weights unless the caller asked for that
   specifically** (spec §22.4).  Steps are reported as ``done``, ``skipped`` or
   ``failed``; a step that did not run is never reported as done, and the run
   stops at the first failure with its exit code and output.
3. **Prove it** (:func:`functional_test`) — asks the running ComfyUI for a real
   clip through the shipped Wan 2.2 template: it checks the server's own
   ``/object_info`` for the node classes *and* for the model files, submits a
   deliberately tiny clip, waits for its own history to report it finished, and
   downloads the produced file into the Maestro workspace, verifying that it is a
   non-empty video.  Anything less is reported as a failure with its reason.
4. **Register** (:func:`register_engine`) — only with a passed test in hand.
   The engine becomes usable exactly when it has been proven, and the proof is
   recorded on the node.

Nothing here assumes a GPU, a driver or a network: those are checked, and the
missing ones are named.  No commercial provider and no credit balance is touched
at any point.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
import uuid
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib import request as urlrequest
from urllib.parse import urlencode

from . import comfy_models, comfyui_adapter

__all__ = [
    "COMFY_REPO_URL",
    "DEFAULT_COMFY_PORT",
    "BootstrapError",
    "available_model_files",
    "default_comfy_root",
    "default_models_root",
    "download_from_comfy",
    "download_model_files",
    "functional_test",
    "install",
    "install_plan",
    "missing_model_files",
    "register_engine",
    "verify_video_file",
]

COMFY_REPO_URL = "https://github.com/comfyanonymous/ComfyUI.git"
DEFAULT_COMFY_PORT = 8188
_MODEL_DIRS = {"diffusion_models": "diffusion_models", "text_encoders": "text_encoders", "vae": "vae"}
_HF_RESOLVE = "https://huggingface.co/{repo}/resolve/main/{path}"
_VIDEO_SUFFIXES = (".mp4", ".webm", ".mkv", ".mov")

Runner = Callable[[Sequence[str], Optional[str], float], "tuple[int, str, str]"]
Downloader = Callable[[str, str], int]


class BootstrapError(RuntimeError):
    """A setup step cannot be planned or executed as asked."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def default_comfy_root(home: str = "") -> str:
    base = home or os.path.expanduser("~")
    return os.path.join(base, "ComfyUI")


def default_models_root(comfy_root: str) -> str:
    return os.path.join(comfy_root, "models")


def _venv_python(comfy_root: str) -> str:
    if os.name == "nt":
        return os.path.join(comfy_root, ".venv", "Scripts", "python.exe")
    return os.path.join(comfy_root, ".venv", "bin", "python")


def node_id_for_url(base_url: str) -> str:
    """A stable node id derived from the address, never from a filesystem path."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(base_url or "")).strip("-").lower()
    return f"comfyui-{slug[-48:] or 'local'}"


# ---------------------------------------------------------------------------
# What is already on the server
# ---------------------------------------------------------------------------


def _loader_choices(object_info: Mapping[str, Any], class_type: str, field: str) -> list[str]:
    node = object_info.get(class_type)
    if not isinstance(node, Mapping):
        return []
    inputs = node.get("input")
    required = inputs.get("required") if isinstance(inputs, Mapping) else None
    choices = required.get(field) if isinstance(required, Mapping) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], list):
        return [str(item) for item in choices[0]]
    return []


def available_model_files(object_info: Mapping[str, Any]) -> dict[str, list[str]]:
    """Which model files this ComfyUI can actually load, per kind.

    Read from the loaders' own dropdowns in ``/object_info``, so it is the server
    telling us its inventory — not a directory listing we guessed.
    """
    return {
        "diffusion_models": _loader_choices(object_info, "UNETLoader", "unet_name"),
        "text_encoders": _loader_choices(object_info, "CLIPLoader", "clip_name"),
        "vae": _loader_choices(object_info, "VAELoader", "vae_name"),
    }


def missing_model_files(
    model: comfy_models.LocalVideoModel,
    object_info: Mapping[str, Any],
) -> list[dict[str, str]]:
    """The model's files that this server does not have, with where to put them."""
    available = available_model_files(object_info)
    missing: list[dict[str, str]] = []
    for entry in model.files:
        if entry.filename not in available.get(entry.directory, []):
            missing.append(
                {
                    "filename": entry.filename,
                    "directory": f"ComfyUI/models/{_MODEL_DIRS.get(entry.directory, entry.directory)}",
                    "size_gb": str(entry.size_gb),
                    "url": entry.url,
                }
            )
    return missing


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def hardware_blockers(hardware: Mapping[str, Any], model: comfy_models.LocalVideoModel) -> list[str]:
    """What would stop the install on this machine, in the machine's own words."""
    blockers: list[str] = []
    tools = hardware.get("tools") or {}
    if "git" not in tools:
        blockers.append("git is not on PATH, and ComfyUI is fetched with git.")
    if not (hardware.get("cuda") or {}).get("available"):
        blockers.append(
            "No CUDA GPU was detected ("
            + str((hardware.get("cuda") or {}).get("reason") or "no reason reported")
            + "). "
            + f"{model.label} is a GPU model; running it on CPU is not something this setup claims to do."
        )
    fit = comfy_models.check_fit(model, hardware)
    blockers.extend(fit["blockers"])
    return blockers


def install_plan(
    *,
    hardware: Mapping[str, Any],
    model: comfy_models.LocalVideoModel,
    comfy_root: str = "",
    models_root: str = "",
    python: str = "",
    base_url: str = "",
    port: int = DEFAULT_COMFY_PORT,
) -> dict[str, Any]:
    """What would be run and downloaded, and what is already there.  No side effects."""
    root = comfy_root or default_comfy_root()
    models = models_root or default_models_root(root)
    interpreter = python or sys.executable
    venv_python = _venv_python(root)
    url = base_url or f"http://127.0.0.1:{int(port)}"

    downloads = [
        {
            "filename": entry.filename,
            "directory": os.path.join(models, _MODEL_DIRS.get(entry.directory, entry.directory)),
            "size_gb": entry.size_gb,
            "url": _HF_RESOLVE.format(repo=entry.repo, path=entry.path_in_repo),
            "path_in_repo": entry.path_in_repo,
            "repo": entry.repo,
        }
        for entry in model.files
    ]

    steps: list[dict[str, Any]] = [
        {
            "id": "checkout",
            "label": "Get ComfyUI",
            "command": ["git", "clone", "--depth", "1", COMFY_REPO_URL, root],
            "cwd": None,
            "why": "ComfyUI is the local server that runs the model; it is cloned into your own folder.",
            "already_present": os.path.isdir(os.path.join(root, ".git")),
        },
        {
            "id": "venv",
            "label": "Create its own Python environment",
            "command": [interpreter, "-m", "venv", os.path.join(root, ".venv")],
            "cwd": None,
            "why": "Its dependencies stay out of Maestro's environment.",
            "already_present": os.path.isfile(venv_python),
        },
        {
            "id": "torch",
            "label": "Install PyTorch",
            "command": [venv_python, "-m", "pip", "install", "torch", "torchvision", "torchaudio"],
            "cwd": root,
            "why": (
                "PyTorch is what runs the model on your GPU. For a specific CUDA build, add "
                "--index-url https://download.pytorch.org/whl/cu<NN> to this command."
            ),
            "already_present": False,
            "needs_network": True,
        },
        {
            "id": "requirements",
            "label": "Install ComfyUI's requirements",
            "command": [venv_python, "-m", "pip", "install", "-r", os.path.join(root, "requirements.txt")],
            "cwd": root,
            "why": "The rest of ComfyUI's runtime dependencies.",
            "already_present": False,
            "needs_network": True,
        },
        {
            "id": "models",
            "label": f"Download {model.label} weights ({model.model_disk_gb} GB)",
            "command": None,
            "cwd": None,
            "why": "The model files themselves. Maestro never downloads these without being asked to.",
            "requires_flag": "download_models",
            "downloads": downloads,
        },
        {
            "id": "server",
            "label": f"Start ComfyUI on 127.0.0.1:{int(port)}",
            "command": [venv_python, "main.py", "--listen", "127.0.0.1", "--port", str(int(port))],
            "cwd": root,
            "why": "Bound to loopback by default: the API is not exposed to your network by this step.",
            "long_running": True,
            "manual_step": True,
        },
        {
            "id": "functional_test",
            "label": "Prove the engine with a real clip",
            "command": None,
            "action": "functional_test",
            "why": "Nothing is registered until a real clip comes back.",
        },
        {
            "id": "register",
            "label": "Register the engine in Maestro",
            "command": None,
            "action": "register",
            "why": "Only after the functional test passes.",
        },
    ]

    return {
        "comfy_root": root,
        "models_root": models,
        "base_url": url,
        "python": interpreter,
        "venv_python": venv_python,
        "model": model.describe(),
        "policy": dict(comfy_models.POLICY),
        "steps": steps,
        "downloads": downloads,
        "download_gb": round(sum(entry["size_gb"] for entry in downloads), 2),
        "disk_required_gb": comfy_models.required_disk_gb(model),
        "disk_free_gb": (hardware.get("disk") or {}).get("free_gb"),
        "blockers": hardware_blockers(hardware, model),
        "workflow": model.templates[0] if model.templates else "",
        "notes": (
            "Everything runs on this computer. No account, no API key and no credits are "
            "involved. Model weights are only downloaded when explicitly requested."
        ),
    }


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------


def _default_runner(command: Sequence[str], cwd: Optional[str], timeout: float) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(  # noqa: S603 - list form, no shell
            list(command),
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        return (127, "", f"{command[0]} not found: {error}")
    except subprocess.TimeoutExpired:
        return (124, "", f"{command[0]} did not finish within {timeout}s")
    except OSError as error:
        return (126, "", f"{command[0]} could not be executed: {error}")
    return (int(completed.returncode), (completed.stdout or "")[-4000:], (completed.stderr or "")[-4000:])


def _default_downloader(url: str, destination: str) -> int:
    """Stream one file to ``destination`` (atomic), returning the bytes written."""
    directory = os.path.dirname(destination)
    os.makedirs(directory, exist_ok=True)
    temporary = f"{destination}.part-{uuid.uuid4().hex[:8]}"
    written = 0
    try:
        with urlrequest.urlopen(url, timeout=60) as response, open(temporary, "wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return written


def download_model_files(
    model: comfy_models.LocalVideoModel,
    models_root: str,
    *,
    downloader: Optional[Downloader] = None,
    report: Optional[Callable[[dict[str, Any]], None]] = None,
) -> list[dict[str, Any]]:
    """Download the model's files, skipping the ones already present at full size."""
    fetch = downloader or _default_downloader
    results: list[dict[str, Any]] = []
    for entry in model.files:
        destination = os.path.join(models_root, _MODEL_DIRS.get(entry.directory, entry.directory), entry.filename)
        url = _HF_RESOLVE.format(repo=entry.repo, path=entry.path_in_repo)
        if os.path.isfile(destination):
            existing_gb = os.path.getsize(destination) / (1024 ** 3)
            # The catalogue's sizes are the published ones, so a file at 98% of it
            # is the real file; anything smaller is a partial download.
            if existing_gb >= entry.size_gb * 0.98:
                result = {
                    "filename": entry.filename,
                    "status": "already_present",
                    "bytes": os.path.getsize(destination),
                    "path": destination,
                }
                results.append(result)
                if report:
                    report(result)
                continue
        try:
            written = fetch(url, destination)
        except Exception as error:  # network, disk, 404 — all reported, none hidden
            result = {
                "filename": entry.filename,
                "status": "failed",
                "reason": f"{type(error).__name__}: {error}",
                "url": url,
                "path": destination,
            }
            results.append(result)
            if report:
                report(result)
            return results
        result = {"filename": entry.filename, "status": "downloaded", "bytes": written, "path": destination}
        results.append(result)
        if report:
            report(result)
    return results


def install(
    plan: Mapping[str, Any],
    *,
    confirm: bool = False,
    download_models: bool = False,
    runner: Optional[Runner] = None,
    downloader: Optional[Downloader] = None,
    timeout: float = 1800.0,
    report: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Run the plan.  Confirmation is mandatory; model downloads need their own flag."""
    if not confirm:
        return {
            "started": False,
            "completed": False,
            "reason": (
                "Installing ComfyUI and its Python environment must be explicitly confirmed; "
                "pass confirm=true (the setup screen asks first)."
            ),
            "steps": [],
        }
    run = runner or _default_runner
    steps: list[dict[str, Any]] = []
    stopped = False

    for step in plan.get("steps") or []:
        identifier = str(step.get("id"))
        if stopped:
            steps.append({"id": identifier, "status": "not_reached", "detail": "A previous step failed."})
            continue
        if step.get("requires_flag") and not download_models:
            steps.append(
                {
                    "id": identifier,
                    "status": "skipped",
                    "detail": (
                        "Model weights were not requested. Nothing was downloaded; pass "
                        "download_models=true to fetch them."
                    ),
                }
            )
            continue
        if step.get("already_present"):
            steps.append({"id": identifier, "status": "skipped", "detail": "Already present."})
            continue
        if step.get("manual_step"):
            steps.append(
                {
                    "id": identifier,
                    "status": "manual",
                    "detail": "Run this on the computer that renders: " + " ".join(map(str, step.get("command") or [])),
                    "command": list(step.get("command") or []),
                }
            )
            continue
        if step.get("action"):
            steps.append(
                {
                    "id": identifier,
                    "status": "pending_action",
                    "detail": "Run it from the setup screen: " + str(step.get("action")),
                }
            )
            continue

        if identifier == "models":
            results = download_model_files(
                _model_from_plan(plan), str(plan.get("models_root") or ""), downloader=downloader, report=report
            )
            failed = [entry for entry in results if entry["status"] == "failed"]
            steps.append(
                {
                    "id": identifier,
                    "status": "failed" if failed else "done",
                    "detail": f"{len(results)} file(s) handled"
                    + (f"; first failure: {failed[0]['reason']}" if failed else ""),
                    "files": results,
                }
            )
            stopped = bool(failed)
            continue

        command = list(step.get("command") or [])
        if not command:
            steps.append({"id": identifier, "status": "skipped", "detail": "No command."})
            continue
        code, stdout, stderr = run(command, step.get("cwd"), timeout)
        if code != 0:
            stopped = True
            steps.append(
                {
                    "id": identifier,
                    "status": "failed",
                    "detail": f"{command[0]} exited with {code}: {(stderr or stdout).strip()[:800]}",
                    "command": command,
                    "exit_code": code,
                }
            )
        else:
            steps.append(
                {"id": identifier, "status": "done", "detail": (stdout or "").strip()[-400:], "command": command}
            )
        if report:
            report({"filename": "", "status": steps[-1]["status"], "step": identifier, "detail": steps[-1]["detail"]})

    failed_any = any(step["status"] == "failed" for step in steps)
    return {
        "started": True,
        "completed": not failed_any,
        "steps": steps,
        "next": (
            "Start ComfyUI on the rendering computer, then run the functional test."
            if not failed_any
            else "Fix the failed step and run the install again; nothing else was touched."
        ),
    }


def _model_from_plan(plan: Mapping[str, Any]) -> comfy_models.LocalVideoModel:
    model_id = str((plan.get("model") or {}).get("model_id") or "")
    return comfy_models.get_model(model_id)


# ---------------------------------------------------------------------------
# The functional test
# ---------------------------------------------------------------------------


def verify_video_file(path: str, *, min_bytes: int = 1024) -> dict[str, Any]:
    """Is this really a video file of a usable size?  Magic bytes, not the name."""
    if not os.path.isfile(path):
        return {"valid": False, "reason": f"{path} does not exist"}
    size = os.path.getsize(path)
    if size < min_bytes:
        return {"valid": False, "reason": f"{path} is only {size} bytes"}
    with open(path, "rb") as handle:
        head = handle.read(16)
    container = ""
    if len(head) >= 12 and head[4:8] == b"ftyp":
        container = "mp4/mov"
    elif head[:4] == b"\x1a\x45\xdf\xa3":
        container = "matroska/webm"
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        container = "webp"
    if not container:
        return {"valid": False, "reason": f"{path} is not a recognisable video container (first bytes: {head[:8]!r})"}
    return {"valid": True, "container": container, "bytes": size}


def download_from_comfy(
    base_url: str,
    file_entry: Mapping[str, Any],
    destination: str,
    *,
    downloader: Optional[Downloader] = None,
    timeout: float = 60.0,
) -> int:
    """Fetch one produced file from ComfyUI's ``/view`` endpoint to ``destination``."""
    query = urlencode(
        {
            "filename": str(file_entry.get("filename") or ""),
            "subfolder": str(file_entry.get("subfolder") or ""),
            "type": str(file_entry.get("type") or "output"),
        }
    )
    url = f"{comfyui_adapter.validate_base_url(base_url)}/view?{query}"
    if downloader is not None:
        return downloader(url, destination)
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
    written = 0
    with urlrequest.urlopen(url, timeout=timeout) as response, open(destination, "wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
            written += len(chunk)
    return written


def _pick_video_file(files: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    for entry in files:
        filename = str(entry.get("filename") or "").lower()
        if filename.endswith(_VIDEO_SUFFIXES):
            return entry
    for entry in files:
        if "video" in str(entry.get("kind") or "").lower():
            return entry
    return None


def functional_test(
    base_url: str,
    *,
    model: comfy_models.LocalVideoModel,
    templates_dir: str = "",
    out_dir: str,
    prompt: str = "a paper boat drifting down a small stream, cinematic",
    width: int = 256,
    height: int = 256,
    length: int = 9,
    steps: int = 4,
    poll_timeout: float = 1800.0,
    poll_interval: float = 3.0,
    timeout: float = 10.0,
    downloader: Optional[Downloader] = None,
    now: Optional[Callable[[], float]] = None,
) -> dict[str, Any]:
    """Generate one real (deliberately tiny) clip and bring the file back.

    The clip is small on purpose — 256x256, 9 frames, 4 steps — so the proof costs
    seconds rather than the ~5 s 720p the model can do.  The point is to prove the
    path, not to make something pretty.
    """
    clock = now or time.time
    started = clock()
    templates = templates_dir or comfyui_adapter.default_templates_dir()
    template_id = model.templates[0] if model.templates else ""
    result: dict[str, Any] = {
        "passed": False,
        "base_url": base_url,
        "model_id": model.model_id,
        "template_id": template_id,
        "clip_path": "",
        "reason": "",
        "checked": {},
    }
    if not template_id:
        result["reason"] = f"No workflow template is shipped for {model.label}."
        return result

    try:
        adapter = comfyui_adapter.ComfyUIAdapter(base_url, templates_dir=templates, timeout=timeout)
        described = adapter.describe()
    except Exception as error:
        result["reason"] = f"ComfyUI is not reachable at {base_url}: {error}"
        return result
    result["checked"]["comfyui_version"] = described.get("comfyui_version")
    result["checked"]["devices"] = described.get("devices") or []

    supported = {entry["template_id"] for entry in described.get("templates") or []}
    if template_id not in supported:
        unsupported = next(
            (entry for entry in described.get("unsupported_templates") or [] if entry["template_id"] == template_id),
            None,
        )
        missing = (unsupported or {}).get("missing_node_classes") or []
        result["reason"] = (
            f"This ComfyUI does not have the node class(es) {template_id} needs"
            + (f": {', '.join(missing)}." if missing else ".")
            + " Update ComfyUI (and, for the video nodes, use a current build) and try again."
        )
        result["checked"]["missing_node_classes"] = missing
        return result

    missing_files = missing_model_files(model, adapter.client.object_info())
    if missing_files:
        result["reason"] = (
            "These model files are not on this ComfyUI yet: "
            + ", ".join(entry["filename"] for entry in missing_files)
            + f". Download them into {missing_files[0]['directory']} "
            + f"(about {model.model_disk_gb} GB in total)."
        )
        result["checked"]["missing_files"] = missing_files
        return result

    params = dict(model.template_params)
    params.update(
        {
            "prompt": prompt,
            "width": int(width),
            "height": int(height),
            "length": int(length),
            "steps": int(steps),
            "seed": 0,
            "filename_prefix": f"maestro_test_{int(clock())}",
        }
    )
    try:
        submitted = adapter.submit(template_id, params)
    except Exception as error:
        result["reason"] = f"ComfyUI rejected the test workflow: {error}"
        return result
    prompt_id = str(submitted.get("prompt_id") or "")
    result["prompt_id"] = prompt_id

    deadline = clock() + float(poll_timeout)
    state = "unknown"
    while clock() < deadline:
        progress = adapter.progress(prompt_id)
        state = str(progress.get("state") or "unknown")
        result["state"] = state
        if progress.get("completed") and state == "completed":
            break
        if state in {"error", "failed"}:
            result["reason"] = f"ComfyUI reported {state} for the test prompt."
            return result
        time.sleep(max(0.05, float(poll_interval)))
    else:
        result["reason"] = (
            f"The test clip was still {state} after {int(poll_timeout)}s. It was not cancelled: "
            "ComfyUI is still working on it, and the engine is not registered until a clip "
            "comes back."
        )
        return result

    try:
        outputs = adapter.results(prompt_id)
    except Exception as error:
        result["reason"] = f"ComfyUI produced no readable result: {error}"
        return result
    chosen = _pick_video_file(outputs.get("files") or [])
    if not chosen:
        result["reason"] = (
            "ComfyUI finished but reported no video file"
            + (f" (it reported: {outputs.get('files')})" if outputs.get("files") else ".")
        )
        return result

    suffix = os.path.splitext(str(chosen.get("filename") or ""))[1] or ".mp4"
    os.makedirs(out_dir, exist_ok=True)
    destination = os.path.join(out_dir, f"comfy_test_{prompt_id[:12]}{suffix}")
    try:
        download_from_comfy(base_url, chosen, destination, downloader=downloader, timeout=timeout)
    except Exception as error:
        result["reason"] = f"The clip exists on ComfyUI but could not be copied back: {error}"
        return result

    check = verify_video_file(destination)
    if not check["valid"]:
        result["reason"] = f"The file came back but is not a usable video: {check['reason']}"
        return result

    digest = hashlib.sha256()
    with open(destination, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)

    result.update(
        {
            "passed": True,
            "clip_path": destination,
            "clip_filename": os.path.basename(destination),
            "bytes": check["bytes"],
            "container": check["container"],
            "sha256": digest.hexdigest(),
            "seconds": round(clock() - started, 1),
            "reason": "",
            "note": (
                "A real clip was generated by this ComfyUI and copied into the Maestro workspace. "
                f"It was deliberately small ({width}x{height}, {length} frames, {steps} steps)."
            ),
        }
    )
    return result


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_engine(
    *,
    compute_nodes_root: str,
    base_url: str,
    model: comfy_models.LocalVideoModel,
    test_result: Mapping[str, Any],
    hardware: Optional[Mapping[str, Any]] = None,
    label: str = "",
    compute_nodes: Any = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Register the local ComfyUI node — only with a passed functional test.

    The node carries the proof: which clip was produced, when, and from which
    template, so a registered engine can always be traced back to a real run.
    """
    if not (test_result or {}).get("passed"):
        raise BootstrapError(
            "The engine is only registered after a real clip comes back; the functional test "
            "has not passed" + (f" ({test_result.get('reason')})" if (test_result or {}).get("reason") else ".")
        )
    module = compute_nodes
    if module is None:
        from . import compute_nodes as module  # local import keeps this module importable alone

    adapter = comfyui_adapter.ComfyUIAdapter(base_url, templates_dir=comfyui_adapter.default_templates_dir())
    described = adapter.describe()
    capabilities = list(described.get("capabilities") or [])
    node_id = node_id_for_url(base_url)
    memory_mb = (hardware or {}).get("memory", {}).get("total_mb") if hardware else None
    gpu = (hardware or {}).get("primary_gpu") if hardware else None

    node = {
        "node_id": node_id,
        "label": label or f"Local ComfyUI ({described.get('comfyui_version') or 'version unknown'})",
        "kind": "comfyui",
        "address": base_url,
        "capabilities": capabilities,
        "models": list(described.get("models") or []),
        "hardware": {
            "gpu": (gpu or {}).get("name") if isinstance(gpu, Mapping) else "",
            "vram_total_mb": (gpu or {}).get("vram_total_mb") if isinstance(gpu, Mapping) else None,
            "system_memory_mb": memory_mb,
            "source": "measured by the local hardware probe",
        },
        "storage": {"models_gb": model.model_disk_gb},
        "notes": (
            f"Local, free-per-generation engine (no credits). Verified by a functional test at "
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime((test_result or {}).get('finished_at') or time.time()))}: "
            f"{test_result.get('prompt_id')} produced {test_result.get('clip_filename')} "
            f"({test_result.get('bytes')} bytes, sha256 {str(test_result.get('sha256'))[:16]}…) with "
            f"template {test_result.get('template_id')}."
        ),
    }
    registered = module.register_node(compute_nodes_root, node, now=now)
    return {
        **registered,
        "model": model.describe(),
        "capabilities": capabilities,
        "verified_by": {
            "prompt_id": test_result.get("prompt_id"),
            "clip_path": test_result.get("clip_path"),
            "sha256": test_result.get("sha256"),
            "template_id": test_result.get("template_id"),
        },
    }

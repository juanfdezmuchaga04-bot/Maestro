"""What this computer really is, measured — never assumed (spec §9, §16, §22.3).

The install path only works if it is honest about the machine it is running on:
which GPU is present, how much VRAM it really has, how much RAM and free disk
there is.  Everything here is either measured or reported as ``unknown`` with the
reason why; nothing is inferred from a model name, a hostname or a wish.

Design notes:

* Every external tool is invoked as an argument list (``subprocess`` with a list,
  never ``shell=True``), so nothing here can be talked into running a command.
* The probe is injectable (``runner=``), which is what makes it testable on a
  machine with no NVIDIA GPU at all — the tests feed it recorded ``nvidia-smi``
  output, including malformed and missing output.
* VRAM is read in MiB from ``nvidia-smi``.  ``os.sysconf`` gives total system
  memory on POSIX; where the platform cannot tell us, the field stays ``None``
  and ``unmeasured`` explains it.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Mapping, Optional, Sequence

__all__ = [
    "NVIDIA_SMI_QUERY",
    "TOOLS",
    "detect_hardware",
    "has_cuda",
    "classify_tier",
    "disk_usage_gb",
    "total_memory_mb",
    "tool_paths",
]

# One query, comma separated, no units — the shape nvidia-smi has supported for
# years.  ``nounits`` keeps parsing simple and locale-independent.
NVIDIA_SMI_QUERY = [
    "nvidia-smi",
    "--query-gpu=index,name,memory.total,driver_version,compute_cap",
    "--format=csv,noheader,nounits",
]

_TOOLS = ("git", "nvidia-smi", "ffmpeg", "ffprobe")
TOOLS = _TOOLS

# A runner returns (returncode, stdout, stderr).
Runner = Callable[[Sequence[str], float], "tuple[int, str, str]"]


def _default_runner(command: Sequence[str], timeout: float) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(  # noqa: S603 - list form, no shell
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        return (127, "", f"{command[0]} not found: {error}")
    except subprocess.TimeoutExpired:
        return (124, "", f"{command[0]} timed out after {timeout}s")
    except OSError as error:  # permission denied, exec format error…
        return (126, "", f"{command[0]} could not be executed: {error}")
    return (
        int(completed.returncode),
        completed.stdout or "",
        completed.stderr or "",
    )


def _int_or_none(value: Any) -> Optional[int]:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def tool_paths() -> dict[str, str]:
    """Where each tool is, when it is on PATH.  Absent tools are simply absent."""
    found: dict[str, str] = {}
    for tool in _TOOLS:
        path = shutil.which(tool)
        if path:
            found[tool] = path
    return found


def total_memory_mb() -> Optional[int]:
    """Total physical memory in MB where the platform exposes it, else None."""
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, ValueError, OSError):
        return None
    if not pages or not page_size or pages < 0 or page_size <= 0:
        return None
    return int((pages * page_size) // (1024 * 1024))


def disk_usage_gb(path: str) -> dict[str, Optional[float]]:
    """Free/total space of the filesystem holding ``path``."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {"path": path, "free_gb": None, "total_gb": None}
    return {
        "path": path,
        "free_gb": round(usage.free / (1024 ** 3), 2),
        "total_gb": round(usage.total / (1024 ** 3), 2),
    }


def parse_nvidia_smi(output: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn ``nvidia-smi`` CSV into GPU records; anything odd is reported.

    A malformed line is not dropped silently: it is returned as a problem so the
    UI can say "the driver answered, but not in a shape this adapter understands"
    instead of showing a machine with no GPU.
    """
    gpus: list[dict[str, Any]] = []
    problems: list[str] = []
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            problems.append(f"Unparsable nvidia-smi line: {line!r}")
            continue
        vram_mb = _int_or_none(parts[2])
        if vram_mb is None:
            problems.append(f"nvidia-smi reported no usable VRAM for {line!r}")
            continue
        gpus.append(
            {
                "index": _int_or_none(parts[0]),
                "name": parts[1],
                "vram_total_mb": vram_mb,
                "driver_version": parts[3] if len(parts) > 3 and parts[3] else "",
                "compute_capability": parts[4] if len(parts) > 4 and parts[4] else "",
            }
        )
    return gpus, problems


def classify_tier(vram_mb: Optional[int]) -> dict[str, Any]:
    """A coarse tier, used only to pick a model that can actually run here.

    The boundaries are deliberately conservative and are *not* a claim about what
    a given workflow needs — each model's own requirement is checked against the
    measured VRAM separately (see :mod:`services.comfy_models`).
    """
    if not vram_mb:
        return {
            "tier": "no_gpu",
            "vram_total_mb": None,
            "note": "No NVIDIA GPU was detected, so a local video model cannot be run here.",
        }
    if vram_mb < 6000:
        return {"tier": "below_6gb", "vram_total_mb": vram_mb, "note": "Below what any current local video model needs."}
    if vram_mb < 10000:
        return {"tier": "consumer_8gb", "vram_total_mb": vram_mb, "note": "Consumer GPU range."}
    if vram_mb < 16000:
        return {"tier": "consumer_12gb", "vram_total_mb": vram_mb, "note": "Consumer GPU range."}
    if vram_mb < 24000:
        return {"tier": "midrange_16gb", "vram_total_mb": vram_mb, "note": "Mid-range GPU."}
    if vram_mb < 48000:
        return {"tier": "high_24gb", "vram_total_mb": vram_mb, "note": "High-end consumer GPU."}
    return {"tier": "workstation_48gb_plus", "vram_total_mb": vram_mb, "note": "Workstation-class GPU."}


def detect_hardware(
    *,
    runner: Optional[Runner] = None,
    disk_path: str = "",
    timeout: float = 10.0,
    tools: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Measure this machine.  Unknown values are ``None`` and explained in ``unmeasured``.

    ``tools`` may be given to override the PATH lookup (used by the tests, so a
    machine without an NVIDIA driver can still exercise the parsing path).
    """
    run = runner or _default_runner
    unmeasured: list[str] = []

    gpus: list[dict[str, Any]] = []
    problems: list[str] = []
    known_tools = dict(tools) if tools is not None else tool_paths()
    nvidia_smi = known_tools.get("nvidia-smi")
    if not nvidia_smi:
        unmeasured.append(
            "nvidia-smi is not on PATH, so no GPU could be queried. Install the NVIDIA "
            "driver (or use a machine that has one) if you expect a GPU here."
        )
    else:
        code, stdout, stderr = run(NVIDIA_SMI_QUERY, timeout)
        if code != 0:
            problems.append(f"nvidia-smi exited with {code}: {(stderr or '').strip()[:300]}")
        gpus, parse_problems = parse_nvidia_smi(stdout)
        problems.extend(parse_problems)
        if not gpus and not problems:
            unmeasured.append("nvidia-smi reported no GPUs.")

    memory_mb = total_memory_mb()
    if memory_mb is None:
        unmeasured.append(
            "Total system memory could not be read on this platform (os.sysconf is POSIX-only)."
        )

    path = disk_path or os.path.expanduser("~")
    disk = disk_usage_gb(path)
    if disk["free_gb"] is None:
        unmeasured.append(f"Free disk space at {path!r} could not be read.")

    cpu_model = platform.processor() or platform.machine()
    primary = max(gpus, key=lambda gpu: int(gpu.get("vram_total_mb") or 0), default=None)

    hardware: dict[str, Any] = {
        "detected_at": time.time(),
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "executable": sys.executable,
        },
        "cpu": {
            "logical_cores": os.cpu_count(),
            "model": cpu_model,
        },
        "memory": {"total_mb": memory_mb, "total_gb": round(memory_mb / 1024, 1) if memory_mb else None},
        "disk": disk,
        "gpus": gpus,
        "primary_gpu": primary,
        "vram_total_mb": int(primary["vram_total_mb"]) if primary else None,
        "tools": known_tools,
        "problems": problems,
        "unmeasured": unmeasured,
    }
    hardware["tier"] = classify_tier(hardware["vram_total_mb"])
    hardware["cuda"] = {
        "available": bool(gpus),
        "reason": (
            f"nvidia-smi reported {len(gpus)} GPU(s)"
            if gpus
            else (problems[0] if problems else unmeasured[0] if unmeasured else "no GPU detected")
        ),
    }
    return hardware


def has_cuda(hardware: dict[str, Any]) -> bool:
    return bool((hardware.get("cuda") or {}).get("available"))

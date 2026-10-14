"""A free NVIDIA GPU in a notebook, running *this same* Maestro as your render node.

A computer without an NVIDIA GPU cannot run Maestro's engine at NVIDIA speed — but
it does not have to.  Maestro already delegates a render to another Maestro (a
"peer", see ``services.remote_engine``) and gets back exactly what that machine's
own engine produced.  Free notebook platforms lend an NVIDIA GPU for hours a week:

* **Kaggle Notebooks** — T4 ×2 (16 GB each) or P100, about 30 GPU-hours per week,
  internet access once the account is phone-verified;
* **Lightning AI Studios** — a free monthly credit (reported ≈ 80 GPU-hours on T4).

``scripts/maestro_cloud_node.py`` turns such a session into a peer: it installs
Maestro with a PyTorch build that matches the driver, starts it in token mode,
opens a TryCloudflare tunnel and prints a **connection code**.  Pasting that code
into Maestro on the laptop (Models → Free NVIDIA GPU) registers the peer, so Studio
and Films renders run there with the real pipeline and come back as files.

This module holds the parts both sides share and that can be tested anywhere:
reading ``nvidia-smi``, choosing the PyTorch wheel, choosing a disk, recognising
the tunnel URL and encoding/decoding the connection code.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Mapping, Optional, Sequence

__all__ = ["CloudNodeError", "PLATFORMS", "parse_gpus", "driver_cuda", "choose_torch", "pick_storage",
           "tunnel_url", "make_code", "parse_code", "PEER_ID"]

PEER_ID = "free-gpu"
CODE_PREFIX = "maestro-node:"
TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

PLATFORMS: list[dict[str, Any]] = [
    {
        "id": "kaggle", "label": "Kaggle Notebooks", "free": True,
        "gpu": "NVIDIA T4 ×2 (16 GB each) or P100 16 GB", "quota": "≈ 30 GPU-hours per week; sessions up to 9–12 h",
        "account": "Free Kaggle account, phone-verified (needed for GPU and internet access)",
        "terms": "Personal, non-commercial use; no cryptomining. Exposing a service through a tunnel is not "
                 "named in the published policy — it is a grey area, so keep sessions personal and attended.",
        "source": "https://www.kaggle.com/docs/notebooks", "confidence": "secondary",
        "recommended": True,
    },
    {
        "id": "lightning", "label": "Lightning AI Studios", "free": True,
        "gpu": "T4 (others on credits)", "quota": "Monthly free credits, reported ≈ 80 GPU-hours on T4",
        "account": "Free account, phone-verified (no card reported)",
        "terms": "Studios are general-purpose cloud machines; serving a port is a supported feature.",
        "source": "https://lightning.ai/pricing/", "confidence": "secondary", "recommended": True,
    },
    {
        "id": "colab", "label": "Google Colab (free tier)", "free": False,
        "gpu": "T4 when available", "quota": "Not guaranteed",
        "account": "Google account",
        "terms": "Not usable for this: the free tier disallows 'bypassing the notebook UI to interact primarily "
                 "via a web UI' and remote control. Allowed only with paid compute units.",
        "source": "https://research.google.com/colaboratory/faq.html", "confidence": "official",
        "recommended": False,
    },
]


class CloudNodeError(ValueError):
    pass


def parse_gpus(csv_text: str) -> list[dict[str, Any]]:
    """``nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv,noheader,nounits``."""
    gpus = []
    for line in (csv_text or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3 or not parts[0]:
            continue
        try:
            memory = int(float(parts[1]))
            cc = float(parts[2])
        except ValueError:
            continue
        gpus.append({"name": parts[0], "memory_mb": memory, "compute_capability": cc})
    return gpus


def driver_cuda(smi_text: str) -> Optional[tuple[int, int]]:
    """The highest CUDA version the installed driver supports, from plain ``nvidia-smi``."""
    match = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", smi_text or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def choose_torch(cuda: Optional[tuple[int, int]], compute_capability: float) -> dict[str, Any]:
    """The PyTorch build Maestro's launcher uses that this driver and GPU can run.

    Maestro's preferred runtime is torch 2.10 on CUDA 13 (driver ≥ 580); the legacy
    runtime is torch 2.7.x on CUDA 12.8.  CUDA 12.8+ builds no longer target GPUs
    older than Turing (compute 7.5), so a P100 gets the CUDA 12.6 build.
    """
    if cuda is None:
        raise CloudNodeError("nvidia-smi did not report a CUDA version: this session has no NVIDIA GPU "
                             "(enable the GPU accelerator in the notebook settings).")
    if cuda >= (13, 0) and compute_capability >= 7.5:
        return {"torch": "torch==2.10.0 torchvision==0.25.0 torchaudio==2.10.0",
                "index": "https://download.pytorch.org/whl/cu130", "label": "torch 2.10 / CUDA 13.0"}
    if cuda >= (12, 8) and compute_capability >= 7.5:
        return {"torch": "torch==2.7.1 torchvision==0.22.1 torchaudio==2.7.1",
                "index": "https://download.pytorch.org/whl/cu128", "label": "torch 2.7.1 / CUDA 12.8"}
    if cuda >= (12, 6):
        return {"torch": "torch==2.7.1 torchvision==0.22.1 torchaudio==2.7.1",
                "index": "https://download.pytorch.org/whl/cu126", "label": "torch 2.7.1 / CUDA 12.6"}
    raise CloudNodeError(f"The driver supports CUDA {cuda[0]}.{cuda[1]}; Maestro needs 12.6 or newer.")


def pick_storage(candidates: Sequence[str], free_bytes: Mapping[str, int]) -> str:
    """The writable candidate folder with the most free space (model weights are large)."""
    usable = [c for c in candidates if free_bytes.get(c, 0) > 0]
    if not usable:
        raise CloudNodeError("No writable disk was found for the model files.")
    return max(usable, key=lambda c: free_bytes[c])


def tunnel_url(line: str) -> str:
    match = TUNNEL_RE.search(line or "")
    return match.group(0) if match else ""


def make_code(url: str, token: str, *, label: str, gpu: str = "", expires_hint: str = "") -> str:
    payload = {"v": 1, "url": url, "token": token, "label": label, "gpu": gpu, "expires": expires_hint}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return CODE_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_code(code: str) -> dict[str, Any]:
    text = str(code or "").strip()
    if not text.startswith(CODE_PREFIX):
        raise CloudNodeError("This is not a Maestro node code (it starts with 'maestro-node:').")
    body = text[len(CODE_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CloudNodeError("The node code is damaged; copy it again from the notebook output.") from exc
    if not isinstance(data, dict) or data.get("v") != 1:
        raise CloudNodeError("Unsupported node code version.")
    url, token = str(data.get("url") or ""), str(data.get("token") or "")
    if not url.startswith("https://") or len(token) < 24:
        raise CloudNodeError("The node code has no usable address or token.")
    return {"url": url.rstrip("/"), "token": token, "label": str(data.get("label") or "Free GPU node")[:120],
            "gpu": str(data.get("gpu") or "")[:200], "expires": str(data.get("expires") or "")[:200]}

"""SeedVR2 video upscaling (to 1440p / 4K) on an NVIDIA GPU — usually a free cloud node.

SeedVR2 (ByteDance-Seed, Apache-2.0) is a one-step diffusion video *restorer*: it
re-synthesises detail instead of only interpolating pixels, which is what makes a
720p/1080p render hold up at 4K.  Maestro runs the standalone command-line tool of
the ComfyUI-SeedVR2_VideoUpscaler project (Apache-2.0), pinned to one commit, in
the same Python environment as the engine.  Nothing here is sent anywhere: the
tool runs on this machine's GPU and downloads its weights (verified by sha256)
from Hugging Face on first use.

What the tool does not do, Maestro does around it:

* it writes video without sound, so the source audio is muxed back with ffmpeg;
* its settings are chosen from the GPU's memory (GGUF quantised weights, BlockSwap
  of transformer blocks to system RAM, tiled VAE) so a 16 GB T4 fits;
* its "Upscaling batch i/N" log lines become real progress, and a cancel request
  stops the process.

Measured on this project's hardware: nothing yet — the settings follow the
upstream project's documented low-VRAM recipes and are marked as such.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable

REPO_URL = "https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler"
PINNED_COMMIT = "4490bd1f482e026674543386bb2a4d176da245b9"
LICENSE = "Apache-2.0"
# Packages the CLI needs on top of Maestro's own requirements (torch, diffusers,
# safetensors, opencv, einops… are already there).
EXTRA_PACKAGES = ("omegaconf>=2.3.0", "peft>=0.17.0", "rotary_embedding_torch>=0.5.3", "gguf")
EXTRA_IMPORTS = ("omegaconf", "peft", "rotary_embedding_torch", "gguf")

# Target short side in pixels for each finishing resolution Maestro offers.
TARGETS = {"1080p": 1080, "1440p": 1440, "4k": 2160}

_PROGRESS = re.compile(r"Upscaling batch\s+(\d+)\s*/\s*(\d+)")


class SeedVR2Error(RuntimeError):
    """A real reason the upscale could not run — shown to the user as is."""


def target_short_side(resolution: str) -> int:
    key = str(resolution or "").strip().lower()
    if key not in TARGETS:
        raise SeedVR2Error(f"Unknown finishing resolution {resolution!r}; use one of {', '.join(TARGETS)}.")
    return TARGETS[key]


def choose_settings(vram_gb: float) -> dict:
    """Model and memory settings for a GPU with ``vram_gb`` of memory.

    The tiers follow the upstream README's low-VRAM guidance: the 3B model in GGUF
    Q8 with 16 of its 32 blocks swapped to RAM for a 16 GB card (Kaggle/Colab T4),
    Q4 with every block swapped below 12 GB, and the 7B model only from 24 GB.
    ``batch_size`` must be 4n+1; larger batches are steadier over time but cost
    memory.  Below 8 GB SeedVR2 is not offered at all.
    """
    vram = float(vram_gb or 0)
    if vram < 8:
        raise SeedVR2Error(
            f"SeedVR2 needs an NVIDIA GPU with at least 8 GB of memory; this one reports {vram:.0f} GB. "
            "Use FlashVSR or Lanczos upscaling instead, or connect a free NVIDIA node."
        )
    if vram >= 24:
        return {"tier": "24GB+", "dit_model": "seedvr2_ema_7b-Q4_K_M.gguf", "blocks_to_swap": 20,
                "batch_size": 21, "tile": 1024, "overlap": 128, "chunk_size": 105}
    if vram >= 15:
        return {"tier": "16GB", "dit_model": "seedvr2_ema_3b-Q8_0.gguf", "blocks_to_swap": 16,
                "batch_size": 9, "tile": 768, "overlap": 128, "chunk_size": 45}
    return {"tier": "8-12GB", "dit_model": "seedvr2_ema_3b-Q4_K_M.gguf", "blocks_to_swap": 32,
            "batch_size": 5, "tile": 512, "overlap": 64, "chunk_size": 25}


def build_command(python: str, cli: str, source: str, output: str, *, short_side: int,
                  settings: dict, model_dir: str, seed: int = 42) -> list[str]:
    command = [
        python, cli, source,
        "--output", output,
        "--output_format", "mp4",
        "--video_backend", "ffmpeg",
        "--model_dir", model_dir,
        "--dit_model", str(settings["dit_model"]),
        "--resolution", str(int(short_side)),
        "--batch_size", str(int(settings["batch_size"])),
        "--uniform_batch_size",
        "--chunk_size", str(int(settings["chunk_size"])),
        "--temporal_overlap", "2",
        "--color_correction", "lab",
        "--seed", str(int(seed)),
        "--dit_offload_device", "cpu",
        "--vae_offload_device", "cpu",
        "--tensor_offload_device", "cpu",
        "--vae_encode_tiled", "--vae_encode_tile_size", str(settings["tile"]),
        "--vae_encode_tile_overlap", str(settings["overlap"]),
        "--vae_decode_tiled", "--vae_decode_tile_size", str(settings["tile"]),
        "--vae_decode_tile_overlap", str(settings["overlap"]),
    ]
    if int(settings.get("blocks_to_swap") or 0) > 0:
        command += ["--blocks_to_swap", str(int(settings["blocks_to_swap"])), "--swap_io_components"]
    return command


def parse_progress(line: str) -> tuple[int, int] | None:
    match = _PROGRESS.search(str(line or ""))
    if not match:
        return None
    done, total = int(match.group(1)), int(match.group(2))
    return (done, total) if total > 0 else None


def tool_dir(app_dir: str) -> str:
    return os.path.join(app_dir, "tools", "seedvr2")


def model_dir(app_dir: str) -> str:
    return os.path.join(app_dir, "ckpts", "seedvr2")


def installed(app_dir: str) -> bool:
    return os.path.isfile(os.path.join(tool_dir(app_dir), "inference_cli.py"))


def _missing_imports(python: str) -> list[str]:
    probe = "import importlib.util,sys;print(','.join(m for m in sys.argv[1:] if importlib.util.find_spec(m) is None))"
    result = subprocess.run([python, "-c", probe, *EXTRA_IMPORTS], capture_output=True, text=True, timeout=120, check=False)
    return [name for name in result.stdout.strip().split(",") if name]


def ensure_installed(app_dir: str, *, python: str = sys.executable, log: Callable[[str], None] = print) -> str:
    """Clone the pinned tool and add its missing Python packages; return the CLI path.

    Idempotent: an existing checkout at the pinned commit and importable packages
    mean nothing is done.
    """
    target = tool_dir(app_dir)
    cli = os.path.join(target, "inference_cli.py")
    git = shutil.which("git")
    if not os.path.isfile(cli):
        if not git:
            raise SeedVR2Error("git is needed to fetch the SeedVR2 tool and was not found on this machine.")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        log(f"Fetching SeedVR2 ({LICENSE}) at {PINNED_COMMIT[:10]}…")
        subprocess.run([git, "clone", "--filter=blob:none", REPO_URL, target], check=True,
                       capture_output=True, text=True, timeout=600)
        subprocess.run([git, "-C", target, "checkout", "--quiet", PINNED_COMMIT], check=True,
                       capture_output=True, text=True, timeout=120)
    missing = _missing_imports(python)
    if missing:
        wanted = [spec for spec, name in zip(EXTRA_PACKAGES, EXTRA_IMPORTS) if name in missing]
        log("Installing SeedVR2 packages: " + ", ".join(wanted))
        uv = shutil.which("uv")
        command = ([uv, "pip", "install", "--python", python, *wanted] if uv
                   else [python, "-m", "pip", "install", "--quiet", *wanted])
        result = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=False)
        if result.returncode != 0:
            raise SeedVR2Error("Could not install SeedVR2's packages: " + (result.stderr or result.stdout)[-400:])
    return cli


def _has_audio(ffprobe: str | None, path: str) -> bool:
    if not ffprobe:
        return False
    result = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                             "-of", "csv=p=0", path], capture_output=True, text=True, timeout=60, check=False)
    return bool(result.stdout.strip())


def remux_audio_command(ffmpeg: str, video: str, audio_source: str, output: str) -> list[str]:
    return [ffmpeg, "-y", "-v", "error", "-i", video, "-i", audio_source, "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", output]


def upscale(source: str, output: str, *, resolution: str, vram_gb: float, app_dir: str,
            python: str = sys.executable,
            on_progress: Callable[[int, int, str], None] | None = None,
            cancelled: Callable[[], bool] | None = None,
            log: Callable[[str], None] = print) -> str:
    """Upscale ``source`` to ``resolution`` and write ``output`` (with the source's audio)."""
    if not os.path.isfile(source):
        raise SeedVR2Error(f"The video to upscale does not exist: {source}")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SeedVR2Error("ffmpeg is needed for SeedVR2 output and was not found on this machine.")
    settings = choose_settings(vram_gb)
    cli = ensure_installed(app_dir, python=python, log=log)
    os.makedirs(model_dir(app_dir), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    silent = os.path.splitext(output)[0] + ".video-only.mp4"
    command = build_command(python, cli, source, silent, short_side=target_short_side(resolution),
                            settings=settings, model_dir=model_dir(app_dir))
    log(f"SeedVR2 {settings['tier']} profile: {settings['dit_model']}, BlockSwap {settings['blocks_to_swap']}")
    process = subprocess.Popen(command, cwd=tool_dir(app_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1)
    tail: list[str] = []
    stop = threading.Event()

    def watch_cancel() -> None:
        while not stop.wait(2):
            if cancelled and cancelled():
                process.terminate()
                return

    watcher = threading.Thread(target=watch_cancel, daemon=True)
    watcher.start()
    try:
        assert process.stdout is not None
        for line in process.stdout:
            tail = (tail + [line.rstrip()])[-25:]
            step = parse_progress(line)
            if step and on_progress:
                on_progress(step[0], step[1], f"SeedVR2 batch {step[0]}/{step[1]}")
        process.wait()
    finally:
        stop.set()
        if process.poll() is None:
            process.kill()
    if cancelled and cancelled():
        raise SeedVR2Error("Cancelled")
    if process.returncode != 0 or not os.path.isfile(silent):
        raise SeedVR2Error(f"SeedVR2 exited with code {process.returncode}: " + " | ".join(tail[-6:])[-600:])
    if _has_audio(shutil.which("ffprobe"), source):
        result = subprocess.run(remux_audio_command(ffmpeg, silent, source, output), capture_output=True,
                                text=True, timeout=1800, check=False)
        if result.returncode != 0:
            raise SeedVR2Error("Could not put the audio back: " + result.stderr[-300:])
        os.remove(silent)
    else:
        os.replace(silent, output)
    return output

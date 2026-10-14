"""Automatic production: a description and a few images in, a finished video out.

This is a thin conductor over pieces Maestro already has; it adds no new renderer.

1. **Compute check** — a local CUDA GPU or at least one connected NVIDIA node
   (for example the free Kaggle node).  Without either nothing is started: the
   Director's multi-clip render cannot run on public demo Spaces.
2. **Recipe** — from the requested resolution and the render GPU's memory: the
   native render size, the in-engine upscaler (FlashVSR, a diffusion
   super-resolution model) and RIFE frame interpolation for smooth motion.  4K is
   reached with an extra SeedVR2 pass on the finished video.
3. **Director** — a ``short_film_story`` pipeline with ``auto_mode`` on: the LLM
   plans scenes and shots, images are generated (with the user's images as
   references), the clips are rendered as one continuous multi-clip job and joined.
   On a computer without a GPU each render goes to the node(s).
4. **Finishing** — for 4K, SeedVR2 runs on a GPU (here or on a node) and the
   original soundtrack is kept.
5. **Result** — the final file path, published like every other output.

Every stage reports what really happened; a failure keeps the real reason.
State is saved to disk after each change so the UI can follow a production
after a page reload.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

RESOLUTIONS = ("720p", "1080p", "1440p", "4k")
ASPECTS = {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (960, 960)}
IMAGE_ROLES = ("reference", "character", "location")
MIN_SECONDS, MAX_SECONDS = 5, 600
DEFAULT_RENDER_VRAM_GB = 16.0
STATE_DIR = "_auto_productions"
TERMINAL = ("completed", "failed", "cancelled")

# FlashVSR factor applied inside the render job to reach each target from 720p.
_IN_ENGINE_UPSCALE = {"720p": "", "1080p": "flashvsr1.5", "1440p": "flashvsr2", "4k": "flashvsr1.5"}


class AutoProductionError(ValueError):
    """A request that cannot be produced, with the reason shown to the user."""


def validate_request(body: Mapping[str, Any]) -> dict:
    description = str(body.get("description") or "").strip()
    if len(description) < 10:
        raise AutoProductionError("Describe the piece in at least a sentence (10 characters or more).")
    try:
        seconds = int(float(body.get("duration_seconds") or 30))
    except (TypeError, ValueError) as exc:
        raise AutoProductionError("duration_seconds must be a number.") from exc
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise AutoProductionError(f"Duration must be between {MIN_SECONDS} and {MAX_SECONDS} seconds.")
    resolution = str(body.get("resolution") or "1080p").lower()
    if resolution not in RESOLUTIONS:
        raise AutoProductionError(f"Resolution must be one of {', '.join(RESOLUTIONS)}.")
    aspect = str(body.get("aspect_ratio") or "16:9")
    if aspect not in ASPECTS:
        raise AutoProductionError(f"Aspect ratio must be one of {', '.join(ASPECTS)}.")
    images = []
    for index, raw in enumerate(body.get("images") or []):
        entry = raw if isinstance(raw, Mapping) else {"path": raw}
        path = str(entry.get("path") or "").strip()
        if not path:
            continue
        role = str(entry.get("role") or ("reference" if index == 0 else "character"))
        if role not in IMAGE_ROLES:
            raise AutoProductionError(f"Image role must be one of {', '.join(IMAGE_ROLES)}.")
        images.append({"path": path, "role": role, "label": str(entry.get("label") or "")[:80]})
    if sum(1 for image in images if image["role"] == "reference") > 1:
        raise AutoProductionError("Only one image can be the main reference.")
    return {
        "description": description[:8000],
        "duration_seconds": seconds,
        "resolution": resolution,
        "aspect_ratio": aspect,
        "smooth_motion": bool(body.get("smooth_motion", False)),
        "style": str(body.get("style") or "").strip()[:400],
        "video_model": str(body.get("video_model") or "").strip(),
        "image_model": str(body.get("image_model") or "").strip(),
        "narrative": bool(body.get("narrative", False)),
        "images": images,
        "workspace": str(body.get("workspace") or "").strip(),
    }


def plan_recipe(request: Mapping[str, Any], *, render_vram_gb: float, finishing_vram_gb: float) -> dict:
    """How the requested resolution is reached on the GPU(s) available.

    The render itself stays at 720p-class size on a 16 GB card (what the Director's
    execution profile can fit); the extra pixels come from FlashVSR inside the job
    and, for 4K, a SeedVR2 pass on the joined video.
    """
    resolution = str(request["resolution"])
    width, height = ASPECTS[str(request["aspect_ratio"])]
    notes = []
    upscale = _IN_ENGINE_UPSCALE[resolution]
    if render_vram_gb and render_vram_gb < 12 and upscale:
        notes.append(f"The render GPU has {render_vram_gb:g} GB; FlashVSR is skipped and Lanczos is used instead.")
        upscale = "lanczos1.5" if upscale == "flashvsr1.5" else "lanczos2"
    finish = ""
    if resolution == "4k":
        if finishing_vram_gb >= 8:
            finish = "4k"
        else:
            raise AutoProductionError(
                "4K needs SeedVR2 on an NVIDIA GPU with at least 8 GB (here or on a connected node); "
                "none is available. Choose 1440p or connect a free NVIDIA node."
            )
    return {
        "render_size": f"{width}x{height}",
        "render_vram_gb": float(render_vram_gb or DEFAULT_RENDER_VRAM_GB),
        "in_engine_upscale": upscale,
        "temporal_upsampling": "rife2" if request.get("smooth_motion") else "",
        "finish_resolution": finish,
        "notes": notes,
    }


def director_params(request: Mapping[str, Any], recipe: Mapping[str, Any]) -> dict:
    """The Director request (the same keys the Director screen sends)."""
    description = str(request["description"])
    if request.get("style"):
        description += f"\n\nVisual style: {request['style']}"
    params: dict[str, Any] = {
        "pipeline_type": "short_film_story",
        "scene_description": description,
        "target_duration": int(request["duration_seconds"]),
        "narrative_mode": bool(request.get("narrative")),
        "auto_mode": True,
        "seamless": False,
        "characters": [],
        "director_resolution_preset": "720p",
        "director_aspect_ratio": str(request["aspect_ratio"]),
        "video_params": {"resolution": recipe["render_size"]},
        "image_params": {"resolution": recipe["render_size"]},
        "video_spatial_upsampling": recipe["in_engine_upscale"],
        "video_temporal_upsampling": recipe["temporal_upsampling"],
        "render_hardware": {"gpu_vram_gb": float(recipe["render_vram_gb"])},
    }
    if request.get("video_model"):
        params["video_model"] = request["video_model"]
    if request.get("image_model"):
        params["image_model"] = request["image_model"]
    if request.get("workspace"):
        params["workspace"] = request["workspace"]
    images = list(request.get("images") or [])
    reference = next((image for image in images if image["role"] == "reference"), None)
    if reference:
        params["reference_image_path"] = reference["path"]
    for role, key in (("character", "character"), ("location", "location")):
        chosen = [image for image in images if image["role"] == role]
        if chosen:
            params[f"{key}_ref_paths"] = [image["path"] for image in chosen]
            params[f"{key}_ref_labels"] = [image["label"] or f"{role} {i + 1}" for i, image in enumerate(chosen)]
    return params


def final_video(status: Mapping[str, Any]) -> str:
    """The joined video of a completed Director pipeline (the last output)."""
    outputs = [str(name) for name in (status.get("output_files") or []) if name]
    if not outputs:
        return ""
    joined = [name for name in outputs if "_multiclip" in os.path.basename(name)]
    name = joined[-1] if joined else outputs[-1]
    if os.path.isabs(name):
        return name
    return os.path.join(str(status.get("out_dir") or ""), os.path.basename(name))


class Conductor:
    """Runs productions in background threads; dependencies are injected.

    ``compute()`` → ``{"local_vram_gb": float, "nodes": [{"peer_id", "label", "online", "vram_gb"}]}``
    ``start_pipeline(params)`` → pipeline id
    ``pipeline_status(pid)`` → status dict (with ``out_dir``) or None
    ``stop_pipeline(pid)``
    ``finish(source, resolution, report, cancelled)`` → path of the upscaled video
    """

    def __init__(self, root: str, *, compute: Callable[[], dict], start_pipeline: Callable[[dict], str],
                 pipeline_status: Callable[[str], dict | None], stop_pipeline: Callable[[str], Any],
                 finish: Callable[..., str], poll_seconds: float = 3.0) -> None:
        self.root = os.path.join(root, STATE_DIR)
        self.compute = compute
        self.start_pipeline = start_pipeline
        self.pipeline_status = pipeline_status
        self.stop_pipeline = stop_pipeline
        self.finish = finish
        self.poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._cancel: dict[str, threading.Event] = {}
        os.makedirs(self.root, exist_ok=True)

    # -- persistence -------------------------------------------------------
    def _path(self, production_id: str) -> str:
        safe = "".join(ch for ch in production_id if ch.isalnum())
        return os.path.join(self.root, f"{safe}.json")

    def _save(self, state: dict) -> dict:
        state["updated_at"] = time.time()
        path = self._path(state["id"])
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=1)
        os.replace(temporary, path)
        return state

    def get(self, production_id: str) -> dict | None:
        try:
            with open(self._path(production_id), encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            return None
        if state.get("status") not in TERMINAL and production_id not in self._cancel:
            # Started by a backend that has since stopped: say so instead of
            # showing a stage that is no longer running.
            state.update(status="failed", error="Maestro restarted while this production was running.")
            self._save(state)
        return state

    def list_all(self) -> list[dict]:
        items = []
        for name in sorted(os.listdir(self.root)):
            if name.endswith(".json"):
                state = self.get(name[:-5])
                if state:
                    items.append(state)
        return sorted(items, key=lambda item: item.get("created_at") or 0, reverse=True)

    def _update(self, state: dict, **changes: Any) -> dict:
        with self._lock:
            state.update(changes)
            if "log" in changes:
                state["log"] = state["log"][-60:]
            return self._save(state)

    def _note(self, state: dict, text: str) -> None:
        self._update(state, log=[*state.get("log", []), {"at": time.time(), "text": str(text)[:300]}])

    # -- lifecycle -----------------------------------------------------------
    def check_compute(self) -> dict:
        facts = self.compute() or {}
        local = float(facts.get("local_vram_gb") or 0)
        nodes = [node for node in facts.get("nodes") or [] if node.get("online")]
        if local <= 0 and not nodes:
            raise AutoProductionError(
                "There is no NVIDIA GPU to render on: this computer has none and no node is connected. "
                "Connect a free NVIDIA node first (Auto → Free NVIDIA GPU)."
            )
        node_vram = max((float(node.get("vram_gb") or 0) or DEFAULT_RENDER_VRAM_GB for node in nodes), default=0.0)
        render = local if local > 0 else node_vram
        return {"local_vram_gb": local, "nodes": nodes, "render_vram_gb": render,
                "finishing_vram_gb": max(local, node_vram)}

    def start(self, body: Mapping[str, Any]) -> dict:
        request = validate_request(body)
        compute = self.check_compute()
        recipe = plan_recipe(request, render_vram_gb=compute["render_vram_gb"],
                             finishing_vram_gb=compute["finishing_vram_gb"])
        production_id = uuid.uuid4().hex[:10]
        state = {
            "id": production_id, "status": "running", "stage": "directing", "created_at": time.time(),
            "request": request, "recipe": recipe,
            "compute": {"local_vram_gb": compute["local_vram_gb"],
                        "nodes": [{"peer_id": n.get("peer_id"), "label": n.get("label"),
                                   "vram_gb": n.get("vram_gb")} for n in compute["nodes"]]},
            "pipeline_id": "", "progress": {"percent": None, "message": "Starting"},
            "director_video": "", "result": "", "error": "", "log": [],
        }
        self._save(state)
        self._cancel[production_id] = threading.Event()
        threading.Thread(target=self._run, args=(state,), daemon=True,
                         name=f"auto-production-{production_id}").start()
        return state

    def cancel(self, production_id: str) -> dict | None:
        event = self._cancel.get(production_id)
        state = self.get(production_id)
        if event is None or state is None:
            return state
        event.set()
        if state.get("pipeline_id") and state.get("stage") == "directing":
            try:
                self.stop_pipeline(state["pipeline_id"])
            except Exception as error:  # noqa: BLE001 - reported, the loop still stops
                self._note(state, f"Could not stop the Director pipeline: {error}")
        return state

    def _run(self, state: dict) -> None:
        cancelled = self._cancel[state["id"]]
        try:
            where = (f"this computer's GPU ({state['compute']['local_vram_gb']:g} GB)"
                     if state["compute"]["local_vram_gb"] > 0
                     else ", ".join(str(n.get("label")) for n in state["compute"]["nodes"]))
            self._note(state, f"Rendering on {where}. Recipe: {state['recipe']['render_size']}"
                       + (f" + {state['recipe']['in_engine_upscale']}" if state["recipe"]["in_engine_upscale"] else "")
                       + (f" + {state['recipe']['temporal_upsampling']}" if state["recipe"]["temporal_upsampling"] else "")
                       + (" + SeedVR2 4K" if state["recipe"]["finish_resolution"] else ""))
            for note in state["recipe"]["notes"]:
                self._note(state, note)
            params = director_params(state["request"], state["recipe"])
            pipeline_id = self.start_pipeline(params)
            self._update(state, pipeline_id=pipeline_id)
            self._note(state, f"Director pipeline {pipeline_id} started: planning scenes and shots.")
            status = self._follow_pipeline(state, pipeline_id, cancelled)
            if status is None:
                return
            video = final_video(status)
            if not video or not os.path.isfile(video):
                raise RuntimeError("The Director finished but its final video is not on disk"
                                   + (f" ({video})" if video else "") + ".")
            self._update(state, director_video=video)
            result = video
            if state["recipe"]["finish_resolution"]:
                self._update(state, stage="finishing", progress={"percent": 0, "message": "SeedVR2 upscale"})
                self._note(state, "Upscaling the finished video with SeedVR2.")

                def report(percent: Any = None, message: str = "") -> None:
                    self._update(state, progress={"percent": percent, "message": str(message)[:200]})

                result = self.finish(video, state["recipe"]["finish_resolution"], report, cancelled.is_set)
                if cancelled.is_set():
                    self._update(state, status="cancelled", stage="", error="Cancelled")
                    return
            self._update(state, status="completed", stage="done", result=result,
                         progress={"percent": 100, "message": "Done"})
            self._note(state, f"Finished: {os.path.basename(result)}")
        except Exception as error:  # noqa: BLE001 - the real reason is the production's outcome
            if cancelled.is_set():
                self._update(state, status="cancelled", stage="", error="Cancelled")
            else:
                self._update(state, status="failed", error=f"{error}"[:600] or type(error).__name__)
        finally:
            self._cancel.pop(state["id"], None)

    def _follow_pipeline(self, state: dict, pipeline_id: str, cancelled: threading.Event) -> dict | None:
        last = None
        while True:
            if cancelled.is_set():
                self._update(state, status="cancelled", stage="", error="Cancelled")
                return None
            status = self.pipeline_status(pipeline_id) or {}
            current = str(status.get("status") or "")
            progress = status.get("progress") if isinstance(status.get("progress"), Mapping) else {}
            total = progress.get("total") or 0
            percent = int(100 * (progress.get("current") or 0) / total) if total else None
            snapshot = (status.get("phase"), percent, progress.get("message"))
            if snapshot != last:
                last = snapshot
                self._update(state, progress={"percent": percent, "phase": status.get("phase") or "",
                                              "message": str(progress.get("message") or "")[:200]})
            if current == "completed":
                return status
            if current in {"failed", "cancelled"}:
                raise RuntimeError(f"Director pipeline {current}: {status.get('error') or 'no reason given'}")
            if current == "paused":
                raise RuntimeError(f"The Director paused for review ({status.get('pause_reason')}); "
                                   "open it in the Director to continue.")
            time.sleep(self.poll_seconds)

"""Film production project persistence for Maestro's AI Studio layer.

An *FilmProject* is the production-scoped sibling of
:mod:`services.editor_projects`.  The Editor project owns a timeline; a film
project owns the *production*: the creative brief, the imported source media
with the creative role the user assigned to it, the continuity bible, the
editable storyboard, the soundtrack plan, the per-shot generation history and
the creative decisions taken along the way.

Design constraints (identical in spirit to ``editor_projects``):

* no FastAPI / WanGP / torch imports, so the whole module stays cheap to unit
  test and importable without a GPU;
* every path that touches the filesystem is validated against the workspace
  root;
* writes are atomic, so a crash mid-save can never truncate a project;
* unknown/extra keys survive a round-trip, so a newer UI can add fields
  without the backend dropping them.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from services.editor_projects import workspace_directory as _workspace_directory


FILM_SCHEMA_VERSION = 1
FILM_PROJECT_DIR = ".maestro_film"

_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# ── Asset taxonomy ───────────────────────────────────────────────────────────
# The *kind* is what the file physically is; the *role* is what the user wants
# it to do in the production.  A photograph can be a character reference, a
# location reference or a lighting reference depending on intent, so the two are
# deliberately separate axes.
MEDIA_KINDS = ("image", "video", "audio", "document", "other")

IMAGE_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif",
    ".tiff", ".webp",
}
VIDEO_EXTENSIONS = {
    ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg",
    ".webm", ".wmv",
}
AUDIO_EXTENSIONS = {
    ".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".ogg", ".opus",
    ".wav", ".wma",
}
DOCUMENT_EXTENSIONS = {
    ".csv", ".doc", ".docx", ".fountain", ".json", ".md", ".pdf", ".rtf",
    ".srt", ".txt", ".vtt",
}
#: Formats we can catalogue but that no local tool in the default install reads
#: without a conversion step.  ``film_assets.conversion_plan`` uses this.
UNSUPPORTED_NATIVE_EXTENSIONS = {
    ".doc", ".docx", ".heic", ".rtf", ".wma", ".wmv", ".flv",
}

#: Creative roles an imported file can be assigned.  ``groups`` below decide
#: which role reaches which part of the Director's planner inputs.
CREATIVE_ROLES = (
    "character_reference",
    "location_reference",
    "set_design",
    "wardrobe",
    "lighting",
    "palette",
    "camera_movement",
    "style_reference",
    "prop",
    "soundtrack",
    "source_music_keep",
    "character_voice",
    "narration_voice",
    "dialogue_audio",
    "sfx",
    "ambience",
    "source_video_keep",
    "source_video_edit",
    "script_document",
    "other",
)

#: Image roles the Director planners understand as visual references.
IMAGE_REFERENCE_ROLES = (
    "character_reference",
    "location_reference",
    "set_design",
    "wardrobe",
    "lighting",
    "palette",
    "camera_movement",
    "style_reference",
    "prop",
)
#: Audio roles that make up the soundtrack plan.
AUDIO_ROLES = (
    "soundtrack",
    "source_music_keep",
    "character_voice",
    "narration_voice",
    "dialogue_audio",
    "sfx",
    "ambience",
)
#: Video roles for footage the user brought in themselves.
VIDEO_ROLES = ("source_video_keep", "source_video_edit")
#: Text roles.
DOCUMENT_ROLES = ("script_document",)

APPROACH_MODES = ("faithful", "creative", "hybrid")
PROJECT_STATUSES = (
    "draft", "brief", "planned", "production", "assembly", "exported",
)
SHOT_STATUSES = (
    "planned", "queued", "generating", "generated", "approved", "failed",
    "skipped",
)
CAMERA_MOVEMENTS = (
    "", "static", "pan_left", "pan_right", "tilt_up", "tilt_down",
    "dolly_in", "dolly_out", "tracking", "crane", "handheld", "steadicam",
    "orbit", "zoom_in", "zoom_out", "drone", "whip_pan",
)
SHOT_SIZES = (
    "", "extreme_wide", "wide", "full", "medium_wide", "medium",
    "medium_close", "close", "extreme_close", "insert", "over_the_shoulder",
    "two_shot", "pov", "aerial",
)
TRANSITIONS = ("cut", "dissolve", "fade_black", "fade_white", "whip", "match_cut")

_lock = threading.RLock()


class FilmProjectError(ValueError):
    """A film project, asset or shot was invalid or unsafe."""


# ── scalar helpers ──────────────────────────────────────────────────────────


def _finite_number(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _bounded_number(value: Any, default: float, low: float, high: float) -> float:
    return min(high, max(low, _finite_number(value, default)))


def _text(value: Any, limit: int = 20000) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _choice(value: Any, allowed: Sequence[str], default: str) -> str:
    candidate = _text(value, 64)
    return candidate if candidate in allowed else default


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off", ""):
            return False
    return default


def _safe_identifier(value: Any, *, fallback: Optional[str] = None) -> str:
    text = _text(value, 80)
    if _PROJECT_ID_RE.fullmatch(text):
        return text
    if fallback is not None:
        return fallback
    raise FilmProjectError("Invalid film project identifier")


def _safe_workspace_name(value: Any) -> str:
    text = _text(value, 120) or "default"
    if text == "default":
        return text
    if (
        text in {".", ".."}
        or os.path.basename(text) != text
        or any(character in text for character in ("/", "\\", "\0"))
    ):
        raise FilmProjectError("Invalid workspace name")
    return text


def infer_media_kind(filename: str) -> str:
    """Best-effort physical kind of a file, by extension only."""
    extension = Path(str(filename or "")).suffix.lower()
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in DOCUMENT_EXTENSIONS:
        return "document"
    return "other"


def roles_for_kind(kind: str) -> tuple[str, ...]:
    """Roles that make sense for a media kind — used by the UI and the API."""
    if kind == "image":
        return IMAGE_REFERENCE_ROLES + ("palette", "other")
    if kind == "audio":
        return AUDIO_ROLES
    if kind == "video":
        return VIDEO_ROLES + ("style_reference", "camera_movement")
    if kind == "document":
        return DOCUMENT_ROLES + ("other",)
    return ("other",)


def _role_for_kind(value: Any, kind: str, default: str) -> str:
    candidate = _text(value, 64)
    if candidate in CREATIVE_ROLES and candidate in roles_for_kind(kind):
        return candidate
    if candidate in CREATIVE_ROLES:
        # Explicit but unusual (e.g. an image kept as "soundtrack"): honour the
        # user's correction rather than silently rewriting it.
        return candidate
    return default


# ── paths & atomic IO ───────────────────────────────────────────────────────


def film_project_directory(save_root: str, workspace: str) -> str:
    return os.path.join(_workspace_directory(save_root, workspace), FILM_PROJECT_DIR)


def _project_path(save_root: str, workspace: str, project_id: str) -> str:
    safe_id = _safe_identifier(project_id)
    return os.path.join(film_project_directory(save_root, workspace), f"{safe_id}.json")


def _atomic_write_json(path: str, payload: Mapping[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.isfile(temporary):
            try:
                os.remove(temporary)
            except OSError:
                pass


# ── normalisation ───────────────────────────────────────────────────────────


def _normalize_brief(raw: Any) -> dict[str, Any]:
    brief = raw if isinstance(raw, Mapping) else {}
    extra = {
        key: copy.deepcopy(value)
        for key, value in brief.items()
        if key not in _BRIEF_KEYS
    }
    normalized: dict[str, Any] = {
        "logline": _text(brief.get("logline"), 2000),
        "synopsis": _text(brief.get("synopsis"), 20000),
        "goal": _text(brief.get("goal"), 4000),
        "genre": _text(brief.get("genre"), 200),
        "tone": _text(brief.get("tone"), 200),
        "visual_style": _text(brief.get("visual_style"), 4000),
        "language": _text(brief.get("language"), 64),
        "audience": _text(brief.get("audience"), 400),
        "aspect_ratio": _choice(
            brief.get("aspect_ratio"),
            ("16:9", "9:16", "2.39:1", "4:3", "1:1", "21:9"),
            "16:9",
        ),
        "target_duration_sec": _bounded_number(
            brief.get("target_duration_sec"), 0.0, 0.0, 36000.0
        ),
        "reference_notes": _text(brief.get("reference_notes"), 20000),
        "must_keep": _string_list(brief.get("must_keep"), 200),
        "avoid": _string_list(brief.get("avoid"), 200),
    }
    normalized.update(extra)
    return normalized


_BRIEF_KEYS = {
    "logline", "synopsis", "goal", "genre", "tone", "visual_style", "language",
    "audience", "aspect_ratio", "target_duration_sec", "reference_notes",
    "must_keep", "avoid",
}


def _string_list(value: Any, limit: int) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        items: Iterable[Any] = [value]
    elif isinstance(value, Iterable):
        items = list(value)
    else:
        items = [value]
    result: list[str] = []
    for item in items:
        text = _text(item, 2000)
        if text:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _normalize_analysis(raw: Any) -> dict[str, Any]:
    analysis = raw if isinstance(raw, Mapping) else {}
    extra = {
        key: copy.deepcopy(value)
        for key, value in analysis.items()
        if key not in _ANALYSIS_KEYS
    }
    normalized: dict[str, Any] = {
        "available": _bool(analysis.get("available"), False),
        "status": _choice(analysis.get("status"), ("unknown", "ok", "partial", "unavailable"), "unknown"),
        "limitations": _string_list(analysis.get("limitations"), 40),
        "duration": _bounded_number(analysis.get("duration"), 0.0, 0.0, 10 ** 6),
        "width": int(_bounded_number(analysis.get("width"), 0.0, 0.0, 100000.0)),
        "height": int(_bounded_number(analysis.get("height"), 0.0, 0.0, 100000.0)),
        "fps": _bounded_number(analysis.get("fps"), 0.0, 0.0, 1000.0),
        "has_audio": _bool(analysis.get("has_audio"), False),
        "size": int(_bounded_number(analysis.get("size"), 0.0, 0.0, 10 ** 15)),
        "analyzed_at": _finite_number(analysis.get("analyzed_at"), 0.0),
        "palette": [
            _text(color, 32)
            for color in _string_list(analysis.get("palette"), 12)
            if _HEX_COLOR_RE.fullmatch(_text(color, 32))
        ],
        "text_excerpt": _text(analysis.get("text_excerpt"), 20000),
        "bpm": _bounded_number(analysis.get("bpm"), 0.0, 0.0, 400.0),
        "sections": _normalize_sections(analysis.get("sections")),
        "beats": _normalize_beats(analysis.get("beats")),
    }
    normalized.update(extra)
    return normalized


_ANALYSIS_KEYS = {
    "available", "status", "limitations", "duration", "width", "height",
    "fps", "has_audio", "size", "analyzed_at", "palette", "text_excerpt",
    "bpm", "sections", "beats",
}


def _normalize_beats(raw: Any) -> list[float]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    beats: list[float] = []
    for entry in list(raw)[:4000]:
        beats.append(round(_bounded_number(entry, 0.0, 0.0, 10 ** 6), 4))
    return beats


def _normalize_sections(raw: Any) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return sections
    for entry in list(raw)[:500]:
        if not isinstance(entry, Mapping):
            continue
        start = _bounded_number(entry.get("start"), 0.0, 0.0, 10 ** 6)
        end = _bounded_number(entry.get("end"), start, 0.0, 10 ** 6)
        sections.append({
            "start": round(start, 4),
            "end": round(max(start, end), 4),
            "label": _text(entry.get("label"), 200),
            "energy": _bounded_number(entry.get("energy"), 0.0, 0.0, 1.0),
        })
    return sections


def _normalize_asset(raw: Any, *, index: int = 0) -> dict[str, Any]:
    asset = raw if isinstance(raw, Mapping) else {}
    kind = _choice(asset.get("kind"), MEDIA_KINDS, infer_media_kind(_text(asset.get("filename"), 500)))
    default_role = roles_for_kind(kind)[0]
    extra = {
        key: copy.deepcopy(value)
        for key, value in asset.items()
        if key not in _ASSET_KEYS
    }
    normalized: dict[str, Any] = {
        "id": _safe_identifier(asset.get("id"), fallback=uuid.uuid4().hex[:16]),
        "name": _text(asset.get("name"), 500) or f"Asset {index + 1}",
        "filename": _text(asset.get("filename"), 500),
        "display_name": _text(asset.get("display_name"), 500),
        "kind": kind,
        "role": _role_for_kind(asset.get("role"), kind, default_role),
        "role_locked": _bool(asset.get("role_locked"), False),
        "role_confidence": _bounded_number(asset.get("role_confidence"), 0.0, 0.0, 1.0),
        "role_reason": _text(asset.get("role_reason"), 1000),
        "origin": _choice(asset.get("origin"), ("upload", "output", "project", "url", "generated"), "upload"),
        "workspace": _text(asset.get("workspace"), 120),
        "notes": _text(asset.get("notes"), 4000),
        "tags": _string_list(asset.get("tags"), 40),
        # ``reproduce_literally`` is the explicit answer to "should this
        # reference be copied frame-for-frame, or is it inspiration only?".
        # Nothing infers this silently; the UI defaults to inspiration.
        "reproduce_literally": _bool(asset.get("reproduce_literally"), False),
        "keep_original": _bool(asset.get("keep_original"), kind in ("audio", "video")),
        "added_at": _finite_number(asset.get("added_at"), time.time()),
        "analysis": _normalize_analysis(asset.get("analysis")),
        "conversions": _normalize_conversions(asset.get("conversions")),
        "assignments": _normalize_assignments(asset.get("assignments")),
    }
    normalized.update(extra)
    return normalized


_ASSET_KEYS = {
    "id", "name", "filename", "display_name", "kind", "role", "role_locked",
    "role_confidence", "role_reason", "origin", "workspace", "notes", "tags",
    "reproduce_literally", "keep_original", "added_at", "analysis",
    "conversions", "assignments",
}


def _normalize_conversions(raw: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return result
    for entry in list(raw)[:50]:
        if not isinstance(entry, Mapping):
            continue
        result.append({
            "target_format": _text(entry.get("target_format"), 32),
            "status": _choice(
                entry.get("status"),
                ("suggested", "running", "completed", "failed"),
                "suggested",
            ),
            "command": _text(entry.get("command"), 2000),
            "reason": _text(entry.get("reason"), 1000),
            "output_filename": _text(entry.get("output_filename"), 500),
        })
    return result


def _normalize_assignments(raw: Any) -> list[dict[str, Any]]:
    """Which shots/scenes/scene-elements reference this asset."""
    result: list[dict[str, Any]] = []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return result
    for entry in list(raw)[:2000]:
        if not isinstance(entry, Mapping):
            continue
        result.append({
            "target": _choice(entry.get("target"), ("shot", "scene", "bible", "soundtrack"), "shot"),
            "target_id": _text(entry.get("target_id"), 80),
            "field": _text(entry.get("field"), 64),
        })
    return result


def _normalize_character(raw: Any, index: int = 0) -> dict[str, Any]:
    entry = raw if isinstance(raw, Mapping) else {}
    return {
        "id": _safe_identifier(entry.get("id"), fallback=f"char-{index + 1}-{uuid.uuid4().hex[:6]}"),
        "name": _text(entry.get("name"), 200) or f"Character {index + 1}",
        "display_name": _text(entry.get("display_name"), 200),
        "physical_description": _text(entry.get("physical_description"), 8000),
        "wardrobe": _text(entry.get("wardrobe"), 4000),
        "voice_description": _text(entry.get("voice_description"), 2000),
        "age_range": _text(entry.get("age_range"), 100),
        "reference_asset_ids": _string_list(entry.get("reference_asset_ids"), 40),
        "voice_asset_ids": _string_list(entry.get("voice_asset_ids"), 20),
        "continuity_notes": _text(entry.get("continuity_notes"), 4000),
    }


def _normalize_location(raw: Any, index: int = 0) -> dict[str, Any]:
    entry = raw if isinstance(raw, Mapping) else {}
    return {
        "id": _safe_identifier(entry.get("id"), fallback=f"loc-{index + 1}-{uuid.uuid4().hex[:6]}"),
        "name": _text(entry.get("name"), 200) or f"Location {index + 1}",
        "description": _text(entry.get("description"), 8000),
        "time_of_day": _text(entry.get("time_of_day"), 200),
        "lighting": _text(entry.get("lighting"), 4000),
        "weather": _text(entry.get("weather"), 200),
        "palette": [
            _text(color, 32)
            for color in _string_list(entry.get("palette"), 12)
            if _HEX_COLOR_RE.fullmatch(_text(color, 32))
        ],
        "reference_asset_ids": _string_list(entry.get("reference_asset_ids"), 40),
        "continuity_notes": _text(entry.get("continuity_notes"), 4000),
    }


def _normalize_bible(raw: Any) -> dict[str, Any]:
    bible = raw if isinstance(raw, Mapping) else {}
    characters = [
        _normalize_character(entry, index)
        for index, entry in enumerate(_mapping_sequence(bible.get("characters"))[:200])
    ]
    locations = [
        _normalize_location(entry, index)
        for index, entry in enumerate(_mapping_sequence(bible.get("locations"))[:200])
    ]
    return {
        "characters": characters,
        "locations": locations,
        "wardrobe": _string_list(bible.get("wardrobe"), 200),
        "props": _string_list(bible.get("props"), 200),
        "lighting": _text(bible.get("lighting"), 8000),
        "palette": [
            _text(color, 32)
            for color in _string_list(bible.get("palette"), 24)
            if _HEX_COLOR_RE.fullmatch(_text(color, 32))
        ],
        "style_notes": _text(bible.get("style_notes"), 8000),
        "continuity_rules": _string_list(bible.get("continuity_rules"), 100),
        "reference_asset_ids": _string_list(bible.get("reference_asset_ids"), 200),
    }


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [entry for entry in value if isinstance(entry, Mapping)]


def _normalize_take(raw: Any, index: int = 0) -> dict[str, Any]:
    entry = raw if isinstance(raw, Mapping) else {}
    return {
        "index": int(_bounded_number(entry.get("index"), index, 0, 10000)),
        "job_id": _text(entry.get("job_id"), 120),
        "filename": _text(entry.get("filename"), 500),
        "status": _choice(entry.get("status"), SHOT_STATUSES + ("cancelled",), "generated"),
        "created_at": _finite_number(entry.get("created_at"), time.time()),
        "model": _text(entry.get("model"), 200),
        "prompt": _text(entry.get("prompt"), 20000),
        "seed": entry.get("seed") if isinstance(entry.get("seed"), int) else None,
        "error": _text(entry.get("error"), 2000),
        "params": copy.deepcopy(entry.get("params")) if isinstance(entry.get("params"), Mapping) else {},
    }


def _normalize_shot(raw: Any, index: int = 0, *, default_scene_id: str = "") -> dict[str, Any]:
    shot = raw if isinstance(raw, Mapping) else {}
    extra = {
        key: copy.deepcopy(value)
        for key, value in shot.items()
        if key not in _SHOT_KEYS
    }
    takes = [
        _normalize_take(entry, take_index)
        for take_index, entry in enumerate(_mapping_sequence(shot.get("takes"))[:200])
    ]
    normalized: dict[str, Any] = {
        "id": _safe_identifier(shot.get("id"), fallback=uuid.uuid4().hex[:16]),
        "index": int(_bounded_number(shot.get("index"), index, 0, 100000)),
        "scene_id": _text(shot.get("scene_id"), 80) or default_scene_id,
        "objective": _text(shot.get("objective"), 4000),
        "description": _text(shot.get("description"), 8000),
        "shot_size": _choice(shot.get("shot_size"), SHOT_SIZES, ""),
        "camera_movement": _choice(shot.get("camera_movement"), CAMERA_MOVEMENTS, ""),
        "duration_sec": _bounded_number(shot.get("duration_sec"), 4.0, 0.5, 600.0),
        "transition": _choice(shot.get("transition"), TRANSITIONS, "cut"),
        "start_image_asset_id": _text(shot.get("start_image_asset_id"), 80),
        "reference_asset_ids": _string_list(shot.get("reference_asset_ids"), 60),
        "character_ids": _string_list(shot.get("character_ids"), 40),
        "location_id": _text(shot.get("location_id"), 80),
        "elements": _string_list(shot.get("elements"), 100),
        "dialogue": _text(shot.get("dialogue"), 8000),
        "music_notes": _text(shot.get("music_notes"), 4000),
        "sfx": _text(shot.get("sfx"), 4000),
        "lighting": _text(shot.get("lighting"), 4000),
        "continuity_refs": _string_list(shot.get("continuity_refs"), 60),
        "image_prompt": _text(shot.get("image_prompt"), 20000),
        "video_prompt": _text(shot.get("video_prompt"), 40000),
        "negative_prompt": _text(shot.get("negative_prompt"), 8000),
        "model": _text(shot.get("model"), 200),
        "model_params": copy.deepcopy(shot.get("model_params")) if isinstance(shot.get("model_params"), Mapping) else {},
        "source": _choice(shot.get("source"), ("manual", "director", "imported"), "manual"),
        "status": _choice(shot.get("status"), SHOT_STATUSES, "planned"),
        "approved": _bool(shot.get("approved"), False),
        "approved_take_index": (
            int(_bounded_number(shot.get("approved_take_index"), -1, -1, 10000))
            if shot.get("approved_take_index") is not None else None
        ),
        "output_filename": _text(shot.get("output_filename"), 500),
        "generation_notes": _text(shot.get("generation_notes"), 4000),
        "takes": takes,
        "updated_at": _finite_number(shot.get("updated_at"), time.time()),
    }
    normalized.update(extra)
    return normalized


_SHOT_KEYS = {
    "id", "index", "scene_id", "objective", "description", "shot_size",
    "camera_movement", "duration_sec", "transition", "start_image_asset_id",
    "reference_asset_ids", "character_ids", "location_id", "elements",
    "dialogue", "music_notes", "sfx", "lighting", "continuity_refs",
    "image_prompt", "video_prompt", "negative_prompt", "model", "model_params",
    "source", "status", "approved", "approved_take_index", "output_filename",
    "generation_notes", "takes", "updated_at",
}


def _normalize_scene(raw: Any, index: int = 0) -> dict[str, Any]:
    scene = raw if isinstance(raw, Mapping) else {}
    return {
        "id": _safe_identifier(scene.get("id"), fallback=f"scene-{index + 1}-{uuid.uuid4().hex[:6]}"),
        "index": int(_bounded_number(scene.get("index"), index, 0, 100000)),
        "title": _text(scene.get("title"), 300) or f"Scene {index + 1}",
        "objective": _text(scene.get("objective"), 4000),
        "description": _text(scene.get("description"), 20000),
        "location_id": _text(scene.get("location_id"), 80),
        "time_of_day": _text(scene.get("time_of_day"), 200),
        "mood": _text(scene.get("mood"), 400),
        "music_cue": _text(scene.get("music_cue"), 1000),
        "shot_ids": _string_list(scene.get("shot_ids"), 2000),
    }


def _normalize_soundtrack_track(raw: Any, index: int = 0) -> dict[str, Any]:
    entry = raw if isinstance(raw, Mapping) else {}
    return {
        "id": _safe_identifier(entry.get("id"), fallback=f"trk-{index + 1}-{uuid.uuid4().hex[:6]}"),
        "asset_id": _text(entry.get("asset_id"), 80),
        "target": _choice(
            entry.get("target"),
            ("music", "dialogue", "narration", "sfx", "ambience"),
            "music",
        ),
        "label": _text(entry.get("label"), 300),
        "gain_db": _bounded_number(entry.get("gain_db"), 0.0, -60.0, 24.0),
        "fade_in_sec": _bounded_number(entry.get("fade_in_sec"), 0.0, 0.0, 600.0),
        "fade_out_sec": _bounded_number(entry.get("fade_out_sec"), 0.0, 0.0, 600.0),
        # A user-supplied track is never dropped silently: ``preserve`` is set
        # for every track whose asset came from the user's own files.
        "preserve": _bool(entry.get("preserve"), True),
        "muted": _bool(entry.get("muted"), False),
        "start_sec": _bounded_number(entry.get("start_sec"), 0.0, 0.0, 36000.0),
    }


def _normalize_soundtrack(raw: Any) -> dict[str, Any]:
    soundtrack = raw if isinstance(raw, Mapping) else {}
    return {
        "tracks": [
            _normalize_soundtrack_track(entry, index)
            for index, entry in enumerate(_mapping_sequence(soundtrack.get("tracks"))[:200])
        ],
        "sync": copy.deepcopy(soundtrack.get("sync")) if isinstance(soundtrack.get("sync"), Mapping) else {
            "available": False,
            "reason": "",
            "bpm": 0.0,
            "sections": [],
            "cuts": [],
        },
        "master_gain_db": _bounded_number(soundtrack.get("master_gain_db"), 0.0, -60.0, 24.0),
        "mute_source_music": _bool(soundtrack.get("mute_source_music"), False),
    }


def _normalize_job(raw: Any) -> dict[str, Any]:
    job = raw if isinstance(raw, Mapping) else {}
    return {
        "id": _text(job.get("id"), 120),
        "shot_id": _text(job.get("shot_id"), 80),
        "kind": _choice(job.get("kind"), ("image", "video", "audio", "assembly", "export"), "video"),
        "status": _choice(
            job.get("status"),
            ("queued", "running", "completed", "failed", "cancelled"),
            "queued",
        ),
        "created_at": _finite_number(job.get("created_at"), time.time()),
        "updated_at": _finite_number(job.get("updated_at"), time.time()),
        "output_filename": _text(job.get("output_filename"), 500),
        "error": _text(job.get("error"), 4000),
        "params": copy.deepcopy(job.get("params")) if isinstance(job.get("params"), Mapping) else {},
    }


def _normalize_decision(raw: Any) -> dict[str, Any]:
    decision = raw if isinstance(raw, Mapping) else {}
    return {
        "at": _finite_number(decision.get("at"), time.time()),
        "kind": _text(decision.get("kind"), 64) or "note",
        "message": _text(decision.get("message"), 4000),
        "data": copy.deepcopy(decision.get("data")) if isinstance(decision.get("data"), Mapping) else {},
    }


def _normalize_export(raw: Any) -> dict[str, Any]:
    entry = raw if isinstance(raw, Mapping) else {}
    return {
        "filename": _text(entry.get("filename"), 500),
        "created_at": _finite_number(entry.get("created_at"), 0.0),
        "editor_project_id": _text(entry.get("editor_project_id"), 80),
        # The render runs in the shared job queue, so an export is tracked by its
        # job id until the worker writes a file (or fails/cancels).
        "job_id": _text(entry.get("job_id"), 120),
        "status": _choice(
            entry.get("status"),
            ("queued", "running", "completed", "failed", "cancelled"),
            "queued",
        ),
        "error": _text(entry.get("error"), 2000),
        "settings": copy.deepcopy(entry.get("settings")) if isinstance(entry.get("settings"), Mapping) else {},
        "report": copy.deepcopy(entry.get("report")) if isinstance(entry.get("report"), Mapping) else {},
    }


def _normalize_timeline(raw: Any) -> dict[str, Any]:
    timeline = raw if isinstance(raw, Mapping) else {}
    return {
        "editor_project_id": _text(timeline.get("editor_project_id"), 80),
        "last_assembled_at": _finite_number(timeline.get("last_assembled_at"), 0.0),
        "assembly_report": copy.deepcopy(timeline.get("assembly_report")) if isinstance(timeline.get("assembly_report"), Mapping) else {},
        "assembly_settings": copy.deepcopy(timeline.get("assembly_settings")) if isinstance(timeline.get("assembly_settings"), Mapping) else {},
    }


def _normalize_plan(raw: Any) -> dict[str, Any]:
    plan = raw if isinstance(raw, Mapping) else {}
    return {
        "director_skill": _text(plan.get("director_skill"), 64),
        "title": _text(plan.get("title"), 300),
        "global_style": _text(plan.get("global_style"), 8000),
        "continuity_notes": _string_list(plan.get("continuity_notes"), 200),
        "planned_at": _finite_number(plan.get("planned_at"), 0.0),
        "imported_from": _text(plan.get("imported_from"), 200),
        "production_plan": copy.deepcopy(plan.get("production_plan")) if isinstance(plan.get("production_plan"), Mapping) else {},
        "notes": _text(plan.get("notes"), 8000),
    }


def normalize_brief(raw: Any) -> dict[str, Any]:
    """Public wrapper around the brief validator, for callers that patch a brief."""
    return _normalize_brief(raw)


def create_film_project(
    *,
    name: str = "Untitled production",
    workspace: str = "default",
    brief: Optional[Mapping[str, Any]] = None,
    approach: str = "hybrid",
) -> dict[str, Any]:
    now = time.time()
    project_id = uuid.uuid4().hex[:16]
    project = {
        "schema_version": FILM_SCHEMA_VERSION,
        "id": project_id,
        "name": _text(name, 300) or "Untitled production",
        "workspace": _safe_workspace_name(workspace),
        "created_at": now,
        "updated_at": now,
        "status": "draft",
        "approach": _choice(approach, APPROACH_MODES, "hybrid"),
        "brief": _normalize_brief(brief),
        "assets": {},
        "bible": _normalize_bible(None),
        "scenes": [],
        "shots": [],
        "soundtrack": _normalize_soundtrack(None),
        "timeline": _normalize_timeline(None),
        "plan": _normalize_plan(None),
        "jobs": [],
        "decisions": [],
        "exports": [],
    }
    return project


def normalize_film_project(project: Mapping[str, Any], *, workspace: Optional[str] = None) -> dict[str, Any]:
    if not isinstance(project, Mapping):
        raise FilmProjectError("Film project must be an object")
    normalized = copy.deepcopy(dict(project))
    normalized["schema_version"] = FILM_SCHEMA_VERSION
    normalized["id"] = _safe_identifier(normalized.get("id"), fallback=uuid.uuid4().hex[:16])
    normalized["name"] = _text(normalized.get("name"), 300) or "Untitled production"
    target_workspace = workspace if workspace is not None else normalized.get("workspace")
    normalized["workspace"] = _safe_workspace_name(target_workspace)
    now = time.time()
    normalized["created_at"] = _finite_number(normalized.get("created_at"), now)
    normalized["updated_at"] = _finite_number(normalized.get("updated_at"), now)
    normalized["status"] = _choice(normalized.get("status"), PROJECT_STATUSES, "draft")
    normalized["approach"] = _choice(normalized.get("approach"), APPROACH_MODES, "hybrid")
    normalized["brief"] = _normalize_brief(normalized.get("brief"))

    assets: dict[str, Any] = {}
    raw_assets = normalized.get("assets")
    if isinstance(raw_assets, Mapping):
        for index, (asset_id, value) in enumerate(raw_assets.items()):
            candidate = value if isinstance(value, Mapping) else {}
            candidate = dict(candidate)
            candidate.setdefault("id", asset_id)
            asset = _normalize_asset(candidate, index=index)
            assets[asset["id"]] = asset
    elif isinstance(raw_assets, Sequence) and not isinstance(raw_assets, (str, bytes)):
        for index, value in enumerate(raw_assets):
            asset = _normalize_asset(value, index=index)
            assets[asset["id"]] = asset
    normalized["assets"] = assets

    normalized["bible"] = _normalize_bible(normalized.get("bible"))
    scenes = [
        _normalize_scene(entry, index)
        for index, entry in enumerate(_mapping_sequence(normalized.get("scenes"))[:1000])
    ]
    seen_scene_ids = {scene["id"] for scene in scenes}
    shots = [
        _normalize_shot(entry, index, default_scene_id=(scenes[0]["id"] if scenes else ""))
        for index, entry in enumerate(_mapping_sequence(normalized.get("shots"))[:10000])
    ]
    for shot in shots:
        if shot["scene_id"] and shot["scene_id"] not in seen_scene_ids:
            shot["scene_id"] = scenes[0]["id"] if scenes else ""
    normalized["scenes"] = scenes
    normalized["shots"] = _reindex_shots(shots)
    normalized["soundtrack"] = _normalize_soundtrack(normalized.get("soundtrack"))
    normalized["timeline"] = _normalize_timeline(normalized.get("timeline"))
    normalized["plan"] = _normalize_plan(normalized.get("plan"))
    normalized["jobs"] = [
        _normalize_job(entry) for entry in _mapping_sequence(normalized.get("jobs"))[:2000]
    ]
    normalized["decisions"] = [
        _normalize_decision(entry) for entry in _mapping_sequence(normalized.get("decisions"))[:2000]
    ]
    normalized["exports"] = [
        _normalize_export(entry) for entry in _mapping_sequence(normalized.get("exports"))[:500]
    ]
    return normalized


def _reindex_shots(shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(shots, key=lambda shot: shot.get("index", 0))
    for index, shot in enumerate(ordered):
        shot["index"] = index
    return ordered


# ── persistence ─────────────────────────────────────────────────────────────


def save_film_project(save_root: str, workspace: str, project: Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize_film_project(project, workspace=workspace)
    normalized["updated_at"] = time.time()
    path = _project_path(save_root, normalized["workspace"], normalized["id"])
    with _lock:
        _atomic_write_json(path, normalized)
    return normalized


def load_film_project(save_root: str, workspace: str, project_id: str) -> dict[str, Any]:
    path = _project_path(save_root, workspace, project_id)
    if not os.path.isfile(path):
        raise FileNotFoundError(project_id)
    with _lock, open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return normalize_film_project(payload, workspace=workspace)


def list_film_projects(save_root: str, workspace: str) -> list[dict[str, Any]]:
    directory = film_project_directory(save_root, workspace)
    if not os.path.isdir(directory):
        return []
    summaries: list[dict[str, Any]] = []
    with _lock:
        for entry in os.scandir(directory):
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            try:
                with open(entry.path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                project = normalize_film_project(payload, workspace=workspace)
                summaries.append(film_project_summary(project))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
    summaries.sort(key=lambda item: item["updated_at"], reverse=True)
    return summaries


def delete_film_project(save_root: str, workspace: str, project_id: str) -> bool:
    path = _project_path(save_root, workspace, project_id)
    with _lock:
        if not os.path.isfile(path):
            return False
        os.remove(path)
    return True


def film_project_summary(project: Mapping[str, Any]) -> dict[str, Any]:
    shots = project.get("shots") or []
    approved = sum(1 for shot in shots if isinstance(shot, Mapping) and shot.get("approved"))
    generated = sum(
        1 for shot in shots
        if isinstance(shot, Mapping) and _shot_has_render(shot)
    )
    return {
        "id": project.get("id"),
        "name": project.get("name"),
        "workspace": project.get("workspace"),
        "status": project.get("status"),
        "approach": project.get("approach"),
        "genre": (project.get("brief") or {}).get("genre", ""),
        "created_at": project.get("created_at"),
        "updated_at": project.get("updated_at"),
        "asset_count": len(project.get("assets") or {}),
        "scene_count": len(project.get("scenes") or []),
        "shot_count": len(shots),
        "generated_shot_count": generated,
        "approved_shot_count": approved,
        "planned_duration_sec": round(film_project_duration(project), 3),
        "editor_project_id": (project.get("timeline") or {}).get("editor_project_id", ""),
        "export_count": len(project.get("exports") or []),
    }


def shot_has_render(shot: Mapping[str, Any]) -> bool:
    """True once at least one retained take produced a file for this shot.

    Only takes that actually wrote a file count. A failed or cancelled take must
    not make the shot look finished, or retrying it would be skipped as "already
    rendered".
    """
    for take in shot.get("takes") or []:
        if isinstance(take, Mapping) and str(take.get("filename") or "").strip():
            return True
    return bool(str(shot.get("output_filename") or "").strip())


# Backwards-compatible private alias (this module used to expose only the
# underscore-prefixed name).
_shot_has_render = shot_has_render


def film_project_duration(project: Mapping[str, Any]) -> float:
    return sum(
        max(0.0, _finite_number(shot.get("duration_sec"), 0.0))
        for shot in (project.get("shots") or [])
        if isinstance(shot, Mapping)
    )


# ── mutation helpers (pure: they take and return the project doc) ───────────


def record_decision(
    project: dict[str, Any],
    kind: str,
    message: str,
    data: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    decisions = list(project.get("decisions") or [])
    decisions.append({
        "at": time.time(),
        "kind": _text(kind, 64) or "note",
        "message": _text(message, 4000),
        "data": copy.deepcopy(dict(data)) if isinstance(data, Mapping) else {},
    })
    project["decisions"] = decisions[-2000:]
    return project


def find_asset(project: Mapping[str, Any], asset_id: str) -> Optional[dict[str, Any]]:
    assets = project.get("assets") or {}
    if not isinstance(assets, Mapping):
        return None
    asset = assets.get(_text(asset_id, 80))
    return asset if isinstance(asset, dict) else None


def find_shot(project: Mapping[str, Any], shot_id: str) -> Optional[dict[str, Any]]:
    for shot in project.get("shots") or []:
        if isinstance(shot, dict) and shot.get("id") == _text(shot_id, 80):
            return shot
    return None


def upsert_asset(project: dict[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """Add or merge one asset. Existing fields survive a partial update."""
    if not isinstance(payload, Mapping):
        raise FilmProjectError("Asset payload must be an object")
    assets = dict(project.get("assets") or {})
    existing = assets.get(_text(payload.get("id"), 80)) if payload.get("id") else None
    merged: dict[str, Any] = {}
    if isinstance(existing, Mapping):
        merged.update(existing)
    merged.update(dict(payload))
    asset = _normalize_asset(merged, index=len(assets))
    assets[asset["id"]] = asset
    project["assets"] = assets
    return project


def update_asset(project: dict[str, Any], asset_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
    asset = find_asset(project, asset_id)
    if asset is None:
        raise FilmProjectError("Asset not found")
    allowed = {key: value for key, value in dict(patch or {}).items() if key in _ASSET_KEYS}
    allowed.pop("id", None)
    if "analysis" in allowed and isinstance(allowed["analysis"], Mapping):
        merged_analysis = dict(asset.get("analysis") or {})
        merged_analysis.update(dict(allowed["analysis"]))
        allowed["analysis"] = merged_analysis
    candidate = dict(asset)
    candidate.update(allowed)
    if "role" in allowed:
        # A user correction is authoritative: lock it so later automatic
        # classification cannot overwrite the decision.
        candidate["role_locked"] = True
        candidate["role_confidence"] = 1.0
    assets = dict(project.get("assets") or {})
    assets[asset_id] = _normalize_asset(candidate)
    project["assets"] = assets
    return project


def remove_asset(project: dict[str, Any], asset_id: str) -> dict[str, Any]:
    assets = dict(project.get("assets") or {})
    if asset_id not in assets:
        raise FilmProjectError("Asset not found")
    assets.pop(asset_id)
    project["assets"] = assets
    for shot in project.get("shots") or []:
        if not isinstance(shot, dict):
            continue
        shot["reference_asset_ids"] = [
            value for value in shot.get("reference_asset_ids") or [] if value != asset_id
        ]
        if shot.get("start_image_asset_id") == asset_id:
            shot["start_image_asset_id"] = ""
    tracks = []
    for track in (project.get("soundtrack") or {}).get("tracks") or []:
        if isinstance(track, Mapping) and track.get("asset_id") == asset_id:
            continue
        tracks.append(track)
    project.setdefault("soundtrack", {})["tracks"] = tracks
    # The bible must not keep pointing at a file that no longer exists, or
    # continuity would hand a deleted photo back to the model.
    bible = project.setdefault("bible", {})
    for key, fields in (("characters", ("reference_asset_ids", "voice_asset_ids")), ("locations", ("reference_asset_ids",))):
        for entry in bible.get(key) or []:
            if not isinstance(entry, dict):
                continue
            for field in fields:
                if field in entry:
                    entry[field] = [
                        value for value in entry.get(field) or [] if value != asset_id
                    ]
    return project


def assets_with_roles(project: Mapping[str, Any], roles: Sequence[str]) -> list[dict[str, Any]]:
    wanted = set(roles)
    return [
        asset
        for asset in (project.get("assets") or {}).values()
        if isinstance(asset, Mapping) and asset.get("role") in wanted
    ]


def upsert_shot(project: dict[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise FilmProjectError("Shot payload must be an object")
    shots = [shot for shot in (project.get("shots") or []) if isinstance(shot, dict)]
    incoming = dict(payload)
    shot_id = _text(incoming.get("id"), 80)
    if shot_id:
        for index, shot in enumerate(shots):
            if shot.get("id") == shot_id:
                merged = dict(shot)
                merged.update(incoming)
                merged["updated_at"] = time.time()
                shots[index] = _normalize_shot(merged, shot.get("index", index), default_scene_id=shot.get("scene_id", ""))
                project["shots"] = _reindex_shots(shots)
                return project
    incoming.setdefault("index", len(shots))
    scenes = project.get("scenes") or []
    incoming.setdefault("scene_id", scenes[0]["id"] if scenes else "")
    shot = _normalize_shot(incoming, len(shots), default_scene_id=incoming.get("scene_id", ""))
    shots.append(shot)
    project["shots"] = _reindex_shots(shots)
    return project


def remove_shot(project: dict[str, Any], shot_id: str) -> dict[str, Any]:
    shots = [
        shot for shot in (project.get("shots") or [])
        if isinstance(shot, dict) and shot.get("id") != shot_id
    ]
    project["shots"] = _reindex_shots(shots)
    return project


def move_shot(project: dict[str, Any], shot_id: str, target_index: int) -> dict[str, Any]:
    shots = _reindex_shots([
        shot for shot in (project.get("shots") or []) if isinstance(shot, dict)
    ])
    position = next((i for i, shot in enumerate(shots) if shot.get("id") == shot_id), None)
    if position is None:
        raise FilmProjectError("Shot not found")
    shot = shots.pop(position)
    destination = int(_bounded_number(target_index, position, 0, len(shots)))
    shots.insert(destination, shot)
    # The list is already in the intended order, so stamp indexes directly:
    # re-sorting here would undo the move using the stale index values.
    for index, entry in enumerate(shots):
        entry["index"] = index
    project["shots"] = shots
    return project


def upsert_scene(project: dict[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    scenes = [scene for scene in (project.get("scenes") or []) if isinstance(scene, dict)]
    incoming = dict(payload)
    scene_id = _text(incoming.get("id"), 80)
    if scene_id:
        for index, scene in enumerate(scenes):
            if scene.get("id") == scene_id:
                merged = dict(scene)
                merged.update(incoming)
                scenes[index] = _normalize_scene(merged, scene.get("index", index))
                project["scenes"] = scenes
                return project
    incoming.setdefault("index", len(scenes))
    scenes.append(_normalize_scene(incoming, len(scenes)))
    project["scenes"] = scenes
    return project


def add_take(project: dict[str, Any], shot_id: str, take: Mapping[str, Any]) -> dict[str, Any]:
    shot = find_shot(project, shot_id)
    if shot is None:
        raise FilmProjectError("Shot not found")
    takes = list(shot.get("takes") or [])
    entry = dict(take or {})
    entry["index"] = len(takes)
    entry.setdefault("created_at", time.time())
    takes.append(_normalize_take(entry, entry["index"]))
    shot["takes"] = takes
    if entry.get("status") != "failed" and entry.get("filename"):
        shot["output_filename"] = _text(entry.get("filename"), 500)
        shot["status"] = "generated"
    elif entry.get("status") == "failed":
        shot["status"] = "failed"
    shot["updated_at"] = time.time()
    return project


def approve_take(project: dict[str, Any], shot_id: str, take_index: int) -> dict[str, Any]:
    shot = find_shot(project, shot_id)
    if shot is None:
        raise FilmProjectError("Shot not found")
    takes = list(shot.get("takes") or [])
    if not takes:
        raise FilmProjectError("Shot has no takes to approve")
    index = int(_bounded_number(take_index, len(takes) - 1, 0, len(takes) - 1))
    take = takes[index]
    if not take.get("filename"):
        raise FilmProjectError("That take has no rendered file")
    shot["approved"] = True
    shot["approved_take_index"] = index
    shot["output_filename"] = _text(take.get("filename"), 500)
    shot["status"] = "approved"
    shot["updated_at"] = time.time()
    return project


def add_job(project: dict[str, Any], job: Mapping[str, Any]) -> dict[str, Any]:
    jobs = [entry for entry in (project.get("jobs") or []) if isinstance(entry, dict)]
    entry = _normalize_job(job)
    if not entry["id"]:
        entry["id"] = uuid.uuid4().hex[:16]
    jobs = [existing for existing in jobs if existing.get("id") != entry["id"]]
    jobs.append(entry)
    project["jobs"] = jobs[-2000:]
    return project


def update_job(project: dict[str, Any], job_id: str, patch: Mapping[str, Any]) -> dict[str, Any]:
    jobs = [entry for entry in (project.get("jobs") or []) if isinstance(entry, dict)]
    for index, entry in enumerate(jobs):
        if entry.get("id") != job_id:
            continue
        merged = dict(entry)
        merged.update(dict(patch or {}))
        merged["updated_at"] = time.time()
        jobs[index] = _normalize_job(merged)
        project["jobs"] = jobs
        return project
    raise FilmProjectError("Job not found")


def add_soundtrack_track(project: dict[str, Any], track: Mapping[str, Any]) -> dict[str, Any]:
    soundtrack = dict(project.get("soundtrack") or {})
    tracks = [entry for entry in (soundtrack.get("tracks") or []) if isinstance(entry, Mapping)]
    tracks.append(track)
    soundtrack["tracks"] = [
        _normalize_soundtrack_track(entry, index) for index, entry in enumerate(tracks)
    ]
    project["soundtrack"] = soundtrack
    return project


def ensure_scene(project: dict[str, Any], *, title: str = "Scene 1", objective: str = "") -> dict[str, Any]:
    scenes = project.get("scenes") or []
    if scenes:
        return scenes[0]
    return upsert_scene(project, {"title": title, "objective": objective})["scenes"][0]


def scene_for_shot(project: Mapping[str, Any], shot: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    scene_id = shot.get("scene_id")
    for scene in project.get("scenes") or []:
        if isinstance(scene, Mapping) and scene.get("id") == scene_id:
            return scene
    return None

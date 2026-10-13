"""Local, dependency-optional analysis of files a user brings into a production.

Everything here runs on the user's own machine with tools that are already part
of a Maestro install (FFprobe via :mod:`services.editor_projects`), plus a few
optional Python packages the install may or may not have (Pillow, pypdf,
librosa through :mod:`services.audio_analysis`).

Two rules the module never breaks:

1. **Never invent an analysis.** When no local tool can inspect a file, the
   result says so (``available: False`` plus a ``limitations`` reason) instead
   of returning plausible-looking numbers.
2. **Never convert silently.** Format conversion is only ever *planned*
   (:func:`conversion_plan`) for the user to run or approve.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from services.film_projects import (
    DOCUMENT_ROLES,
    AUDIO_EXTENSIONS,
    IMAGE_EXTENSIONS,
    UNSUPPORTED_NATIVE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    FilmProjectError,
    infer_media_kind,
    roles_for_kind,
)

#: Substrings that hint at what a file is for, checked against the file name.
#: Deliberately small and explainable: the UI shows the matched hint as the
#: reason, and the user can always override it.
_ROLE_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("soundtrack", "score", "theme", "music", "banda", "ost"), "soundtrack"),
    (("voice", "voz", "vo_", "_vo", "narration", "narrador"), "character_voice"),
    (("dialogue", "dialogo", "linea", "line_"), "dialogue_audio"),
    (("ambience", "ambient", "ambiente", "atmos", "room_tone"), "ambience"),
    (("sfx", "foley", "efecto", "impact", "whoosh"), "sfx"),
    (("costume", "wardrobe", "vestuario", "traje"), "wardrobe"),
    (("light", "ilumin", "key_", "rim_"), "lighting"),
    (("palette", "paleta", "color_script", "grade"), "palette"),
    (("location", "localizacion", "set_", "scenery", "escenograf"), "location_reference"),
    (("character", "personaje", "hero", "protagon", "cast"), "character_reference"),
    (("prop", "atrezzo", "utileria"), "prop"),
    (("style", "estilo", "look", "moodboard", "referencia"), "style_reference"),
    (("script", "guion", "storyboard", "treatment", "sinopsis"), "script_document"),
    (("footage", "source", "bruto", "raw", "grabacion"), "source_video_edit"),
    (("keep", "conservar", "original"), "source_video_keep"),
)


def _default_role_for(kind: str, *, document_role: str = "script_document") -> str:
    if kind == "image":
        return "character_reference"
    if kind == "audio":
        return "soundtrack"
    if kind == "video":
        return "source_video_edit"
    if kind == "document":
        return document_role if document_role in DOCUMENT_ROLES else DOCUMENT_ROLES[0]
    return "other"


def classify_asset(filename: str, *, role_locked: str = "") -> dict[str, Any]:
    """Suggest a kind + creative role for a file, with the reasoning shown.

    The suggestion is advisory: ``role_confidence`` stays below 1.0 so the UI
    always presents it as a guess the user may correct.
    """
    name = Path(str(filename or "")).name.lower()
    kind = infer_media_kind(name)
    role = _default_role_for(kind)
    confidence = 0.35 if kind != "other" else 0.1
    reason = f"Default role for {kind} files" if kind != "other" else "Unknown file type"

    for tokens, hinted_role in _ROLE_HINTS:
        if not any(token in name for token in tokens):
            continue
        if hinted_role not in roles_for_kind(kind):
            continue
        role = hinted_role
        confidence = 0.6
        reason = f"File name mentions '{next(token for token in tokens if token in name)}'"
        break

    extension = Path(name).suffix
    if kind == "audio" and role == "soundtrack":
        confidence = max(confidence, 0.5)
        reason = reason if confidence > 0.5 else "Audio files default to the soundtrack"
    if extension in UNSUPPORTED_NATIVE_EXTENSIONS:
        confidence = min(confidence, 0.3)
        reason = f"{reason}; {extension} needs conversion before local tools can read it"

    if role_locked:
        role = role_locked
        confidence = 1.0
        reason = "Role confirmed by the user"

    return {
        "kind": kind,
        "role": role,
        "role_confidence": round(float(confidence), 3),
        "role_reason": reason,
        "available_roles": list(dict.fromkeys(roles_for_kind(kind))),
    }


# ── tool availability ───────────────────────────────────────────────────────


def _module_available(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def local_tool_availability() -> dict[str, bool]:
    """Which local tools this install can actually use right now."""
    return {
        "ffprobe": shutil.which("ffprobe") is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "pillow": _module_available("PIL"),
        "numpy": _module_available("numpy"),
        "librosa": _module_available("librosa"),
        "pypdf": _module_available("pypdf") or _module_available("PyPDF2"),
        "soundfile": _module_available("soundfile"),
    }


def missing_requirements() -> list[dict[str, str]]:
    """What is missing locally, why it matters, and how to get it."""
    tools = local_tool_availability()
    requirements: list[dict[str, str]] = []
    if not tools["ffprobe"]:
        requirements.append({
            "tool": "ffprobe",
            "why": "Reading duration, resolution, frame rate and audio streams of imported media.",
            "install": "Install FFmpeg (ships with the Maestro installer; on Linux: apt install ffmpeg).",
        })
    if not tools["ffmpeg"]:
        requirements.append({
            "tool": "ffmpeg",
            "why": "Converting unsupported formats and assembling the final cut.",
            "install": "Install FFmpeg (see app/docs/INSTALLATION.md).",
        })
    if not tools["pillow"]:
        requirements.append({
            "tool": "Pillow",
            "why": "Reading image dimensions and extracting a colour palette for style references.",
            "install": "pip install Pillow (already in app/requirements.txt).",
        })
    if not tools["pypdf"]:
        requirements.append({
            "tool": "pypdf",
            "why": "Extracting text from PDF scripts. Without it, PDFs are catalogued but not read.",
            "install": "pip install pypdf",
        })
    if not tools["librosa"]:
        requirements.append({
            "tool": "librosa",
            "why": "Tempo, beat and section analysis so scenes can be cut to the music.",
            "install": "pip install librosa (already in app/requirements.txt).",
        })
    return requirements


# ── format support and conversion ───────────────────────────────────────────


def format_support(filename: str) -> dict[str, Any]:
    """Will local tools open this file as-is?"""
    extension = Path(str(filename or "")).suffix.lower()
    if extension in UNSUPPORTED_NATIVE_EXTENSIONS:
        return {
            "supported": False,
            "extension": extension,
            "message": (
                f"{extension} is not readable by the local pipeline. The file is kept in the "
                "project and can be converted locally (see the conversion plan)."
            ),
        }
    if extension in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | {".fountain", ".json", ".md", ".srt", ".txt", ".vtt", ".csv"}:
        return {"supported": True, "extension": extension, "message": ""}
    if extension == ".pdf":
        tools = local_tool_availability()
        return {
            "supported": True,
            "extension": extension,
            "message": "" if tools["pypdf"] else "PDF catalogued; install pypdf to read its text.",
        }
    return {
        "supported": False,
        "extension": extension or "(none)",
        "message": "Unrecognised format. The file is stored with the project but not analysed.",
    }


def conversion_plan(filename: str) -> Optional[dict[str, str]]:
    """A local FFmpeg conversion that would make an unsupported file usable."""
    extension = Path(str(filename or "")).suffix.lower()
    source = Path(str(filename or "")).name or "input"
    stem = Path(source).stem or "input"
    if extension in {".heic", ".tif", ".tiff"}:
        return {
            "target_format": "png",
            "reason": f"{extension} images are not decoded by the local image path.",
            "command": f'ffmpeg -i "{source}" -frames:v 1 "{stem}.png"',
        }
    if extension in {".wma", ".aiff", ".aif"}:
        return {
            "target_format": "wav",
            "reason": f"{extension} audio is not guaranteed to be readable by libsndfile.",
            "command": f'ffmpeg -i "{source}" -acodec pcm_s16le "{stem}.wav"',
        }
    if extension in {".wmv", ".flv"}:
        return {
            "target_format": "mp4",
            "reason": f"{extension} video cannot be decoded reliably by the local pipeline.",
            "command": f'ffmpeg -i "{source}" -c:v libx264 -crf 18 -c:a aac "{stem}.mp4"',
        }
    if extension in {".doc", ".docx", ".rtf"}:
        return {
            "target_format": "txt",
            "reason": f"{extension} documents must be exported as plain text to be read locally.",
            "command": "",
        }
    if extension in {".mov", ".mkv", ".avi"}:
        return {
            "target_format": "mp4",
            "reason": f"{extension} works, but remuxing to mp4 is safer for long timelines.",
            "command": f'ffmpeg -i "{source}" -c copy "{stem}.mp4"',
        }
    return None


# ── analysis ────────────────────────────────────────────────────────────────


def _empty_analysis(*, status: str, limitations: Sequence[str], size: int = 0) -> dict[str, Any]:
    return {
        "available": False,
        "status": status,
        "limitations": list(limitations),
        "duration": 0.0,
        "width": 0,
        "height": 0,
        "fps": 0.0,
        "has_audio": False,
        "size": int(size),
        "analyzed_at": time.time(),
        "palette": [],
        "text_excerpt": "",
        "bpm": 0.0,
        "sections": [],
        "beats": [],
    }


def _ffprobe_analysis(path: str, limitations: list[str]) -> Optional[dict[str, Any]]:
    if shutil.which("ffprobe") is None:
        limitations.append("ffprobe is not installed, so media metadata could not be read.")
        return None
    # Reuse the Editor's probe so both features agree on what a file is.
    from services.editor_projects import EditorProjectError, probe_media

    try:
        probe = probe_media(path)
    except (EditorProjectError, OSError, ValueError) as error:
        limitations.append(f"ffprobe could not read this file: {str(error)[:200]}")
        return None
    except Exception as error:  # pragma: no cover - defensive, subprocess failures
        limitations.append(f"ffprobe failed: {str(error)[:200]}")
        return None
    return {
        "available": True,
        "status": "ok",
        "limitations": limitations,
        "duration": float(probe.get("duration") or 0.0),
        "width": int(probe.get("width") or 0),
        "height": int(probe.get("height") or 0),
        "fps": float(probe.get("fps") or 0.0),
        "has_audio": bool(probe.get("has_audio")),
        "size": int(probe.get("size") or 0),
        "analyzed_at": time.time(),
        "palette": [],
        "text_excerpt": "",
        "bpm": 0.0,
        "sections": [],
        "beats": [],
        "kind": probe.get("type"),
        "audio_channels": int(probe.get("audio_channels") or 0),
        "audio_sample_rate": int(probe.get("audio_sample_rate") or 0),
    }


def _pillow_analysis(path: str, limitations: list[str]) -> Optional[dict[str, Any]]:
    if not _module_available("PIL"):
        limitations.append("Pillow is not installed, so image dimensions and palette are unavailable.")
        return None
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
            palette = _dominant_colors(image.convert("RGB"))
            info = {"format": image.format or ""}
        return {
            "width": int(width),
            "height": int(height),
            "palette": palette,
            "format": info["format"],
        }
    except Exception as error:
        limitations.append(f"Image could not be decoded: {str(error)[:200]}")
        return None


def _dominant_colors(image: Any, *, count: int = 6) -> list[str]:
    """Most common colours as ``#rrggbb`` — real pixels, no styling guesswork."""
    try:
        small = image.resize((32, 32))
        quantized = small.quantize(colors=count)
        palette = quantized.getpalette() or []
        counts = sorted(quantized.getcolors() or [], reverse=True)
    except Exception:
        return []
    colors: list[str] = []
    for _, index in counts[:count]:
        base = index * 3
        if base + 2 >= len(palette):
            continue
        red, green, blue = palette[base:base + 3]
        colors.append(f"#{red:02X}{green:02X}{blue:02X}")
    return colors


def _read_document(path: str, *, max_chars: int = 20000) -> tuple[str, list[str]]:
    limitations: list[str] = []
    extension = Path(path).suffix.lower()
    if extension == ".pdf":
        if not _module_available("pypdf") and not _module_available("PyPDF2"):
            limitations.append("pypdf is not installed, so the PDF text was not read.")
            return "", limitations
        try:
            if _module_available("pypdf"):
                from pypdf import PdfReader
            else:  # pragma: no cover - legacy fallback
                from PyPDF2 import PdfReader  # type: ignore

            reader = PdfReader(path)
            chunks: list[str] = []
            total = 0
            for page in reader.pages[:40]:
                text = (page.extract_text() or "").strip()
                if not text:
                    continue
                chunks.append(text)
                total += len(text)
                if total >= max_chars:
                    break
            if not chunks:
                limitations.append("The PDF contained no extractable text (it may be a scan).")
            return "\n\n".join(chunks)[:max_chars], limitations
        except Exception as error:
            limitations.append(f"PDF could not be parsed: {str(error)[:200]}")
            return "", limitations
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(max_chars), limitations
    except OSError as error:
        limitations.append(f"Text could not be read: {str(error)[:200]}")
        return "", limitations


def analyze_asset(
    path: str,
    *,
    kind: Optional[str] = None,
    deep_audio: bool = False,
) -> dict[str, Any]:
    """Describe a file using only local tools. Never guesses.

    ``deep_audio`` opts into the heavier tempo/section pass
    (:mod:`services.audio_analysis`), which loads librosa and may download model
    weights on first use — the Studio UI asks before doing this.
    """
    if not os.path.isfile(path):
        raise FilmProjectError(f"File not found: {os.path.basename(path)}")
    resolved_kind = kind or infer_media_kind(path)
    size = 0
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    limitations: list[str] = []

    if resolved_kind == "image":
        analysis = _empty_analysis(status="unavailable", limitations=limitations, size=size)
        pillow = _pillow_analysis(path, limitations)
        if pillow is not None:
            analysis["available"] = True
            analysis["status"] = "ok"
            analysis["width"] = pillow["width"]
            analysis["height"] = pillow["height"]
            analysis["palette"] = pillow["palette"]
            analysis["format"] = pillow["format"]
        probe = _ffprobe_analysis(path, limitations)
        if probe is not None:
            analysis["available"] = True
            if analysis["status"] != "ok":
                analysis["status"] = "ok"
            for key in ("width", "height", "duration", "size"):
                if not analysis.get(key) and probe.get(key):
                    analysis[key] = probe[key]
        analysis["limitations"] = limitations
        return analysis

    if resolved_kind in ("video", "audio"):
        analysis = _empty_analysis(status="unavailable", limitations=limitations, size=size)
        probe = _ffprobe_analysis(path, limitations)
        if probe is None:
            analysis["status"] = "unavailable"
            analysis["limitations"] = limitations
            return analysis
        analysis.update({
            "available": True,
            "status": "ok",
            "duration": probe["duration"],
            "width": probe["width"],
            "height": probe["height"],
            "fps": probe["fps"],
            "has_audio": probe["has_audio"],
            "size": probe["size"] or size,
            "audio_channels": probe.get("audio_channels", 0),
            "audio_sample_rate": probe.get("audio_sample_rate", 0),
        })
        if resolved_kind == "audio" and deep_audio:
            _merge_deep_audio(analysis, path, limitations)
        analysis["limitations"] = limitations
        return analysis

    if resolved_kind == "document":
        text, doc_limitations = _read_document(path)
        limitations.extend(doc_limitations)
        analysis = _empty_analysis(
            status="ok" if text else "partial",
            limitations=limitations,
            size=size,
        )
        analysis["available"] = bool(text)
        analysis["text_excerpt"] = text
        analysis["document_chars"] = len(text)
        analysis["limitations"] = limitations
        return analysis

    analysis = _empty_analysis(status="unavailable", limitations=limitations, size=size)
    limitations.append("This file type is not analysed locally.")
    analysis["limitations"] = limitations
    return analysis


def _merge_deep_audio(analysis: dict[str, Any], path: str, limitations: list[str]) -> None:
    """Tempo/section analysis through the existing Maestro audio service."""
    if not _module_available("librosa") and not _module_available("numpy"):
        limitations.append(
            "librosa is not installed, so tempo and section analysis were skipped. "
            "Install it to cut scenes to the music."
        )
        return
    try:
        from services import audio_analysis

        result = audio_analysis.analyze(path)
    except Exception as error:  # pragma: no cover - depends on optional runtime
        limitations.append(f"Audio analysis failed: {str(error)[:200]}")
        return
    analysis["bpm"] = float(getattr(result, "bpm", 0.0) or 0.0)
    analysis["sections"] = [
        {
            "start": float(getattr(section, "start", 0.0)),
            "end": float(getattr(section, "end", 0.0)),
            "label": str(getattr(section, "label", "") or ""),
            "energy": float(getattr(section, "energy", 0.0) or 0.0),
        }
        for section in (getattr(result, "sections", None) or [])
    ][:200]
    analysis["beats"] = [
        round(float(getattr(beat, "time", 0.0)), 4)
        for beat in (getattr(result, "beats", None) or [])
    ][:4000]


def register_imported_asset(
    project: dict[str, Any],
    *,
    filename: str,
    name: str = "",
    workspace: str = "",
    origin: str = "upload",
    analyze: bool = False,
    absolute_path: str = "",
    deep_audio: bool = False,
) -> dict[str, Any]:
    """Add a user file to a project, classified and (optionally) analysed.

    The asset is registered even when analysis is impossible — the file stays in
    the project and the missing tool is reported instead of the import failing.
    """
    from services.film_projects import upsert_asset

    classification = classify_asset(filename)
    asset: dict[str, Any] = {
        "name": name or Path(filename).name,
        "filename": filename,
        "kind": classification["kind"],
        "role": classification["role"],
        "role_confidence": classification["role_confidence"],
        "role_reason": classification["role_reason"],
        "origin": origin,
        "workspace": workspace,
        "added_at": time.time(),
    }
    support = format_support(filename)
    conversion = conversion_plan(filename)
    if not support["supported"]:
        asset["notes"] = support["message"]
    if conversion is not None:
        asset["conversions"] = [{
            "target_format": conversion["target_format"],
            "reason": conversion["reason"],
            "command": conversion["command"],
            "status": "suggested",
        }]
    if analyze and absolute_path:
        asset["analysis"] = analyze_asset(
            absolute_path,
            kind=classification["kind"],
            deep_audio=deep_audio,
        )
    upsert_asset(project, asset)
    return project


def conversion_requirement(filename: str) -> Optional[str]:
    """Human-readable reason a file needs conversion, or ``None``."""
    support = format_support(filename)
    return None if support["supported"] else support["message"]


def available_roles(kind: str) -> list[str]:
    return list(dict.fromkeys(roles_for_kind(kind)))

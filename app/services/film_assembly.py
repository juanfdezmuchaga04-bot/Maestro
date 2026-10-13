"""Soundtrack planning and storyboard → Editor assembly.

The Studio does not render video itself.  It compiles the storyboard into a real
:mod:`services.editor_projects` document (tracks, items, transitions, audio
levels, titles) and hands that to the Editor's existing FFmpeg renderer.  That
keeps one render path in the product and means an assembled film opens in the
Editor and stays editable.

Nothing here fabricates media: a shot without a rendered file is reported as
missing rather than inserted into the timeline as a placeholder.
"""
from __future__ import annotations

import hashlib
from typing import Any, Mapping, Optional

from services.film_brief import resolve_asset_path
from services.film_projects import (
    assets_with_roles,
    film_project_duration,
    find_asset,
    find_shot,
    record_decision,
    scene_for_shot,
)
from services.film_planning import storyboard, storyboard_totals

#: Aspect ratio → canvas, matching what the Studio brief offers.
CANVAS_BY_ASPECT: dict[str, tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "2.39:1": (2048, 858),
    "21:9": (2560, 1080),
    "4:3": (1440, 1080),
    "1:1": (1080, 1080),
}

#: Shot transition → Editor transition vocabulary.
_TRANSITION_MAP = {
    "cut": "none",
    "none": "none",
    "dissolve": "dissolve",
    "fade_black": "fade_black",
    "fade_white": "fade_black",
    "whip": "none",
    "match_cut": "none",
}

#: Default gains per soundtrack target, in dB.  A user-supplied track keeps
#: whatever gain it already has; these are only starting points.
_DEFAULT_GAIN_DB = {
    "music": -6.0,
    "dialogue": 0.0,
    "narration": 0.0,
    "sfx": -3.0,
    "ambience": -12.0,
}

_AUDIO_TARGET_ROLES = {
    "music": ("soundtrack", "source_music_keep"),
    "dialogue": ("dialogue_audio",),
    "narration": ("narration_voice",),
    "sfx": ("sfx",),
    "ambience": ("ambience",),
}


def _db_to_gain(db: float) -> float:
    return round(min(4.0, max(0.0, 10 ** (float(db) / 20.0))), 4)


def canvas_for(project: Mapping[str, Any], *, fps: Optional[float] = None) -> dict[str, Any]:
    brief = project.get("brief") or {}
    aspect = str(brief.get("aspect_ratio") or "16:9")
    width, height = CANVAS_BY_ASPECT.get(aspect, CANVAS_BY_ASPECT["16:9"])
    timeline = project.get("timeline") or {}
    settings = timeline.get("assembly_settings") if isinstance(timeline.get("assembly_settings"), Mapping) else {}
    return {
        "width": int(settings.get("width") or width),
        "height": int(settings.get("height") or height),
        "fps": float(fps or settings.get("fps") or 30.0),
        "background": "#000000",
    }


# ── soundtrack ──────────────────────────────────────────────────────────────


def soundtrack_plan(project: Mapping[str, Any]) -> dict[str, Any]:
    """Independent music / dialogue / narration / SFX lanes for the mix.

    The user's own music is never replaced: a track whose role is
    ``source_music_keep`` or ``soundtrack`` is marked ``preserve`` and keeps any
    gain/fade the user already set.
    """
    existing = {
        str(track.get("asset_id") or ""): track
        for track in (project.get("soundtrack") or {}).get("tracks") or []
        if isinstance(track, Mapping)
    }
    tracks: list[dict[str, Any]] = []
    warnings: list[str] = []

    for target, roles in _AUDIO_TARGET_ROLES.items():
        for asset in assets_with_roles(project, roles):
            asset_id = str(asset.get("id") or "")
            saved = existing.get(asset_id)
            gain_db = _DEFAULT_GAIN_DB.get(target, 0.0)
            fade_in = 0.0
            fade_out = 1.5 if target in ("music", "narration") else 0.5
            muted = False
            if isinstance(saved, Mapping) and saved.get("target") == target:
                gain_db = float(saved.get("gain_db", gain_db))
                fade_in = float(saved.get("fade_in_sec", fade_in))
                fade_out = float(saved.get("fade_out_sec", fade_out))
                muted = bool(saved.get("muted"))
            tracks.append({
                "asset_id": asset_id,
                "label": str(asset.get("display_name") or asset.get("name") or ""),
                "target": target,
                "gain_db": round(gain_db, 2),
                "volume": _db_to_gain(gain_db),
                "fade_in_sec": round(fade_in, 3),
                "fade_out_sec": round(fade_out, 3),
                "muted": muted,
                "preserve": True,
                "duration": float((asset.get("analysis") or {}).get("duration") or 0.0),
                "filename": str(asset.get("filename") or ""),
            })

    music_asset = next((track for track in tracks if track["target"] == "music"), None)
    if music_asset is None:
        warnings.append(
            "No music assigned. Import a track and set its role to 'soundtrack' or "
            "'music to preserve' to score the film."
        )
    return {
        "tracks": tracks,
        "warnings": warnings,
        "music_asset_id": music_asset["asset_id"] if music_asset else "",
        "master_gain_db": float((project.get("soundtrack") or {}).get("master_gain_db") or 0.0),
    }


def apply_soundtrack_plan(project: dict[str, Any]) -> dict[str, Any]:
    """Persist the planned soundtrack lanes onto the project document."""
    plan = soundtrack_plan(project)
    project["soundtrack"] = {
        "tracks": [
            {
                "asset_id": track["asset_id"],
                "target": track["target"],
                "label": track["label"],
                "gain_db": track["gain_db"],
                "fade_in_sec": track["fade_in_sec"],
                "fade_out_sec": track["fade_out_sec"],
                "preserve": track["preserve"],
                "muted": track["muted"],
                "start_sec": 0.0,
            }
            for track in plan["tracks"]
        ],
        "sync": music_sync_plan(project),
        "master_gain_db": plan["master_gain_db"],
        "mute_source_music": bool((project.get("soundtrack") or {}).get("mute_source_music")),
    }
    return project


def music_sync_plan(project: Mapping[str, Any]) -> dict[str, Any]:
    """Propose cut points against the music, using real local analysis only."""
    result: dict[str, Any] = {
        "available": False,
        "reason": "",
        "bpm": 0.0,
        "sections": [],
        "boundaries": [],
        "suggested_cuts": [],
    }
    music = next(
        iter(assets_with_roles(project, ("soundtrack", "source_music_keep"))), None
    )
    if music is None:
        result["reason"] = "No music track is assigned to the production."
        return result
    analysis = music.get("analysis") or {}
    if not analysis.get("available"):
        limitations = analysis.get("limitations") or []
        result["reason"] = (
            limitations[0] if limitations
            else "The music has not been analysed yet. Analyse it to sync cuts to tempo."
        )
        return result

    beats = _beat_positions(project)
    sections = [dict(section) for section in analysis.get("sections") or [] if isinstance(section, Mapping)]
    result["bpm"] = float(analysis.get("bpm") or 0.0)
    result["sections"] = sections

    if not beats and not sections:
        result["reason"] = (
            "Analysis found no beats or sections in this track, so cuts cannot be "
            "aligned to the music automatically."
        )
        return result

    boundaries: list[dict[str, Any]] = []
    elapsed = 0.0
    for entry in storyboard(project):
        elapsed += float(entry["duration_sec"])
        boundary: dict[str, Any] = {
            "shot_id": entry["id"],
            "index": entry["index"],
            "planned_sec": round(elapsed, 4),
            "nearest_beat": None,
            "nearest_section": None,
            "delta_sec": None,
            "suggested_sec": round(elapsed, 4),
        }
        if beats:
            nearest = min(beats, key=lambda beat: abs(beat - elapsed))
            boundary["nearest_beat"] = round(nearest, 4)
            boundary["delta_sec"] = round(nearest - elapsed, 4)
            boundary["suggested_sec"] = round(nearest, 4)
        elif sections:
            nearest_section = min(
                sections, key=lambda section: abs(float(section.get("start") or 0.0) - elapsed)
            )
            boundary["nearest_section"] = round(float(nearest_section.get("start") or 0.0), 4)
            boundary["delta_sec"] = round(
                float(nearest_section.get("start") or 0.0) - elapsed, 4
            )
        boundaries.append(boundary)

    result["available"] = True
    result["boundaries"] = boundaries
    result["suggested_cuts"] = [
        {"shot_id": boundary["shot_id"], "start_sec": boundary["suggested_sec"]}
        for boundary in boundaries
    ]
    return result


def _beat_positions(project: Mapping[str, Any]) -> list[float]:
    music = next(iter(assets_with_roles(project, ("soundtrack", "source_music_keep"))), None)
    if music is None:
        return []
    return sorted(
        float(value) for value in (music.get("analysis") or {}).get("beats") or []
    )


def snap_shots_to_music(project: dict[str, Any], *, tolerance_sec: float = 1.5) -> dict[str, Any]:
    """Adjust each shot boundary onto the nearest beat, within tolerance.

    Beats are re-evaluated as the cut grows, so snapping shot 1 cannot leave
    shot 2 aligned to a beat position that no longer exists.  The shot
    *durations* change, so the storyboard stays internally consistent; the report
    states exactly which shots moved and by how much.
    """
    plan = music_sync_plan(project)
    if not plan["available"]:
        return {"changed": [], "reason": plan["reason"]}
    anchors = _beat_positions(project)
    if not anchors:
        anchors = sorted(
            float(section.get("start") or 0.0) for section in plan["sections"]
        )
    changed: list[dict[str, Any]] = []
    elapsed = 0.0
    for entry in storyboard(project):
        shot = find_shot(project, str(entry["id"]))
        if shot is None:
            continue
        duration = float(shot.get("duration_sec") or 0.0)
        target = elapsed + duration
        if anchors:
            nearest = min(anchors, key=lambda anchor: abs(anchor - target))
            delta = nearest - target
            if abs(delta) <= tolerance_sec and abs(delta) > 1e-3:
                new_duration = max(0.5, duration + delta)
                changed.append({
                    "shot_id": entry["id"],
                    "from_sec": round(duration, 4),
                    "to_sec": round(new_duration, 4),
                    "delta_sec": round(delta, 4),
                })
                shot["duration_sec"] = round(new_duration, 4)
                duration = new_duration
        elapsed += duration
    if changed:
        record_decision(
            project,
            "music_sync",
            f"{len(changed)} shot boundary/boundaries snapped to the music.",
            {"changes": changed},
        )
    return {"changed": changed, "reason": ""}


# ── assembly ────────────────────────────────────────────────────────────────


def _output_asset(
    project: Mapping[str, Any],
    *,
    filename: str,
    media_type: str,
    duration: float = 0.0,
    width: int = 0,
    height: int = 0,
    fps: float = 0.0,
    has_audio: bool = True,
) -> dict[str, Any]:
    workspace = str(project.get("workspace") or "default")
    # Stable across processes: an Editor project saved today must still resolve
    # its assets when it is reopened tomorrow. Python's str hash is salted.
    digest = hashlib.md5(f"{workspace}\u0000{media_type}\u0000{filename}".encode("utf-8")).hexdigest()[:16]
    return {
        "id": f"clip-{digest}",
        "name": filename,
        "type": media_type,
        "origin": "output",
        "workspace": workspace,
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "has_audio": has_audio,
    }


def _upload_asset(asset: Mapping[str, Any], media_type: str) -> dict[str, Any]:
    analysis = asset.get("analysis") or {}
    return {
        "id": f"up-{str(asset.get('id') or 'asset')[:40]}",
        "name": str(asset.get("filename") or ""),
        "type": media_type,
        "origin": "upload",
        "duration": float(analysis.get("duration") or 0.0),
        "width": int(analysis.get("width") or 0),
        "height": int(analysis.get("height") or 0),
        "fps": float(analysis.get("fps") or 0.0),
        "has_audio": bool(analysis.get("has_audio") or media_type == "audio"),
    }


def build_editor_project(
    project: Mapping[str, Any],
    *,
    save_root: str,
    uploads_root: str,
    include_subtitles: bool = False,
    include_titles: bool = True,
    title_text: str = "",
    credits_text: str = "",
    fps: Optional[float] = None,
    only_approved: bool = False,
) -> dict[str, Any]:
    """Compile the storyboard into an Editor project (tracks + items + audio)."""
    workspace = str(project.get("workspace") or "default")
    canvas = canvas_for(project, fps=fps)
    assets: dict[str, dict[str, Any]] = {}
    video_items: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    used_shots: list[str] = []

    def resolve(filename: str) -> Optional[str]:
        return resolve_asset_path(
            {"filename": filename},
            uploads_root=uploads_root,
            save_root=save_root,
            workspace=workspace,
        )

    board = storyboard(project)
    timeline_cursor = 0.0
    for entry in board:
        shot = find_shot(project, str(entry["id"])) or {}
        if only_approved and not shot.get("approved"):
            continue
        filename = str(shot.get("output_filename") or "").strip()
        if not filename:
            missing.append({
                "shot_id": str(entry["id"]),
                "index": str(entry["index"]),
                "reason": "no render yet",
            })
            continue
        if resolve(filename) is None:
            missing.append({
                "shot_id": str(entry["id"]),
                "index": str(entry["index"]),
                "reason": f"file '{filename}' not found in the workspace",
            })
            continue
        asset = _output_asset(
            project,
            filename=filename,
            media_type="video",
            duration=float(entry["duration_sec"]),
        )
        assets[asset["id"]] = asset
        scene = scene_for_shot(project, shot) or {}
        video_items.append({
            "id": f"item-{entry['index']:04d}-{asset['id'][-6:]}",
            "asset_id": asset["id"],
            "start": round(timeline_cursor, 4),
            "duration": round(max(1 / 240, float(entry["duration_sec"])), 4),
            "source_in": 0.0,
            "speed": 1.0,
            "volume": 1.0,
            "opacity": 1.0,
            "fade_in": 0.0,
            "fade_out": 0.0,
            "transition_in": "none",
            "transition_out": _TRANSITION_MAP.get(str(entry["transition"]), "none"),
            "fit": "contain",
            "muted": False,
            "disabled": False,
        })
        markers.append({
            "id": f"mark-{entry['index']:04d}",
            "time": round(timeline_cursor, 4),
            "label": f"{entry['index'] + 1}. {entry['objective'][:60] or scene.get('title', 'Shot')}",
            "color": "#f59e0b",
        })
        used_shots.append(str(entry["id"]))
        timeline_cursor += float(entry["duration_sec"])

    total_duration = timeline_cursor

    # ── audio lanes ─────────────────────────────────────────────────────────
    plan = soundtrack_plan(project)
    audio_tracks: list[dict[str, Any]] = []
    for target, label in (
        ("music", "Music"),
        ("dialogue", "Dialogue"),
        ("narration", "Narration"),
        ("sfx", "SFX"),
        ("ambience", "Ambience"),
    ):
        lane_tracks = [track for track in plan["tracks"] if track["target"] == target]
        lane_items: list[dict[str, Any]] = []
        cursor = 0.0
        for track in lane_tracks:
            asset = find_asset(project, track["asset_id"])
            if asset is None:
                continue
            filename = str(asset.get("filename") or "")
            if resolve(filename) is None:
                missing.append({"shot_id": "", "index": "", "reason": f"audio '{filename}' not found"})
                continue
            editor_asset = _upload_asset(asset, "audio")
            assets[editor_asset["id"]] = editor_asset
            duration = float(track.get("duration") or 0.0)
            if duration <= 0:
                duration = max(total_duration, film_project_duration(project), 1.0)
            lane_items.append({
                "id": f"aud-{target}-{editor_asset['id'][-6:]}-{int(cursor)}",
                "asset_id": editor_asset["id"],
                "start": round(cursor, 4),
                "duration": round(duration, 4),
                "source_in": 0.0,
                "speed": 1.0,
                "volume": float(track.get("volume") or 1.0),
                "opacity": 1.0,
                "fade_in": float(track.get("fade_in_sec") or 0.0),
                "fade_out": float(track.get("fade_out_sec") or 0.0),
                "transition_in": "none",
                "transition_out": "none",
                "fit": "contain",
                "muted": bool(track.get("muted")),
                "disabled": False,
            })
            cursor += duration
        if lane_items:
            audio_tracks.append({
                "id": f"audio-{target}",
                "name": label,
                "type": "audio",
                "z_index": 0,
                "muted": False,
                "locked": False,
                "volume": 1.0,
                "items": lane_items,
            })

    # ── subtitle / title lanes ──────────────────────────────────────────────
    text_items: list[dict[str, Any]] = []
    if include_subtitles:
        elapsed = 0.0
        for entry in board:
            dialogue = str(entry["dialogue"] or "").strip()
            if dialogue:
                text_items.append({
                    "id": f"sub-{entry['index']:04d}",
                    "start": round(elapsed, 4),
                    "duration": round(max(0.8, float(entry["duration_sec"])), 4),
                    "text": dialogue[:400],
                    "style": {
                        "x": 0.0, "y": 0.0, "font_family": "Arial", "font_size": 42,
                        "color": "#ffffff", "background_color": "#000000",
                        "background_opacity": 0.4, "text_align": "center",
                    },
                })
            elapsed += float(entry["duration_sec"])
    if include_titles and total_duration > 0:
        heading = title_text or str(project.get("name") or "Untitled production")
        text_items.insert(0, {
            "id": "title-card",
            "start": 0.0,
            "duration": min(3.0, total_duration),
            "text": heading[:200],
            "style": {
                "x": 0.0, "y": 0.0, "font_family": "Arial", "font_size": 72,
                "color": "#ffffff", "background_color": "#000000",
                "background_opacity": 0.0, "text_align": "center",
            },
        })
        if credits_text:
            text_items.append({
                "id": "credit-card",
                "start": round(max(0.0, total_duration - 3.0), 4),
                "duration": min(3.0, total_duration),
                "text": credits_text[:400],
                "style": {
                    "x": 0.0, "y": 0.0, "font_family": "Arial", "font_size": 40,
                    "color": "#ffffff", "background_color": "#000000",
                    "background_opacity": 0.0, "text_align": "center",
                },
            })

    tracks: list[dict[str, Any]] = [
        {
            "id": "video-main",
            "name": "Main video",
            "type": "video",
            "z_index": 0,
            "muted": False,
            "locked": False,
            "volume": 1.0,
            "items": video_items,
        },
        *audio_tracks,
    ]
    # Dialogue is also carried on the video clips' own audio, so a dedicated
    # dialogue lane only appears when the user actually imported dialogue.
    if text_items:
        tracks.append({
            "id": "titles-main",
            "name": "Titles & subtitles",
            "type": "text",
            "z_index": 10,
            "muted": False,
            "locked": False,
            "volume": 1.0,
            "items": text_items,
        })

    sync = music_sync_plan(project)
    markers.extend([
        {
            "id": f"beat-{index:04d}",
            "time": float(boundary["nearest_beat"]),
            "label": f"Beat {index + 1}",
            "color": "#38bdf8",
        }
        for index, boundary in enumerate(sync.get("boundaries") or [])
        if boundary.get("nearest_beat") is not None
    ])

    editor_project = {
        "schema_version": 0,  # the Editor's own normalizer stamps the version
        "id": str((project.get("timeline") or {}).get("editor_project_id") or ""),
        "name": f"{project.get('name') or 'Production'} — cut",
        "workspace": workspace,
        "canvas": canvas,
        "assets": assets,
        "tracks": tracks,
        "markers": markers,
        "export": {
            "quality": "high",
            "codec": "h264",
            "encoder": "auto",
            "include_audio": True,
            "resolution": "canvas",
            "frame_rate": "project",
            "filename": "",
        },
        "exports": [],
    }
    report = {
        "shots_in_timeline": len(used_shots),
        "shots_missing": missing,
        "shot_ids": used_shots,
        "duration_sec": round(total_duration, 4),
        "audio_tracks": [track["name"] for track in audio_tracks],
        "has_subtitles": bool(include_subtitles and text_items),
        "has_titles": bool(include_titles and total_duration > 0),
        "music_sync_available": bool(sync.get("available")),
        "music_sync_reason": sync.get("reason") or "",
        "canvas": canvas,
    }
    return {"editor_project": editor_project, "report": report}


def assembly_readiness(
    project: Mapping[str, Any],
    *,
    save_root: str = "",
    uploads_root: str = "",
    ffmpeg_available: bool = True,
    engine_available: bool = True,
) -> list[dict[str, str]]:
    """What still blocks an assembled, exportable cut."""
    issues: list[dict[str, str]] = []
    totals = storyboard_totals(project)
    if totals["shot_count"] == 0:
        issues.append({
            "code": "no_shots",
            "severity": "blocking",
            "message": "There is no storyboard to assemble yet.",
        })
        return issues
    rendered = totals["with_takes"]
    if rendered == 0:
        issues.append({
            "code": "no_renders",
            "severity": "blocking",
            "message": "No shot has a rendered file yet, so there is nothing to cut together.",
        })
    elif rendered < totals["shot_count"]:
        issues.append({
            "code": "partial_renders",
            "severity": "warning",
            "message": (
                f"{rendered} of {totals['shot_count']} shots are rendered; "
                "the rest will be left out of the cut."
            ),
        })
    if not ffmpeg_available:
        issues.append({
            "code": "ffmpeg_missing",
            "severity": "blocking",
            "message": "FFmpeg is not installed, so the final cut cannot be rendered.",
        })
    if not engine_available:
        issues.append({
            "code": "engine_unavailable",
            "severity": "warning",
            "message": "The generation engine is unavailable here; assembly and export still work on rendered files.",
        })
    if save_root or uploads_root:
        plan = build_editor_project(
            project, save_root=save_root, uploads_root=uploads_root, include_titles=False,
        )
        for entry in plan["report"]["shots_missing"]:
            issues.append({
                "code": "missing_media",
                "severity": "warning",
                "message": f"Shot {entry['index']}: {entry['reason']}.",
            })
    return issues


def edit_fraction(project: Mapping[str, Any]) -> float:
    """Rendered share of the storyboard, used for progress reporting."""
    totals = storyboard_totals(project)
    if not totals["shot_count"]:
        return 0.0
    return round(totals["with_takes"] / totals["shot_count"], 4)

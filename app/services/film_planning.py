"""Director plan → editable storyboard → per-shot production requests.

Director Mode already returns a ``ProductionPlan`` of ``ShotPlan`` objects.  This
module converts that real planner output into the Studio's persistent storyboard
(scenes, shots, continuity bible) and back out again as concrete generation
requests.  It never invents shots: if the plan is empty, the storyboard stays
empty and says so.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Mapping, Optional, Sequence

from services.film_projects import (
    CAMERA_MOVEMENTS,
    SHOT_SIZES,
    FilmProjectError,
    add_job,
    add_take,
    film_project_duration,
    find_asset,
    find_shot,
    record_decision,
    shot_has_render,
    update_job,
    upsert_scene,
    upsert_shot,
)

# ── normalising the planner's free-text camera language ─────────────────────

# Order matters: the most specific phrase must win ("medium wide" is not "wide",
# "extreme close" is not "close").
_SHOT_SIZE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("extreme_close", ("extreme close", "extreme_close", "ecu")),
    ("extreme_wide", ("extreme wide", "extreme_wide", "ews")),
    ("medium_close", ("medium close", "medium_close", "mcu")),
    ("medium_wide", ("medium wide", "medium_wide", "mws")),
    ("over_the_shoulder", ("over the shoulder", "over-the-shoulder", "ots")),
    ("two_shot", ("two shot", "two-shot")),
    ("pov", ("pov", "point of view")),
    ("aerial", ("aerial", "drone shot", "bird's eye")),
    ("insert", ("insert shot", "insert")),
    ("close", ("close up", "close-up", "closeup", "close")),
    ("full", ("full shot", "full body")),
    ("wide", ("wide shot", "wide", "establishing", "long shot")),
    ("medium", ("medium shot", "medium", "mid shot")),
)

_MOVEMENT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("dolly_in", ("dolly in", "dolly-in", "push in", "pushes in", "moves in")),
    ("dolly_out", ("dolly out", "dolly-out", "pull back", "pull out", "pulls back")),
    ("zoom_in", ("zoom in", "zooms in")),
    ("zoom_out", ("zoom out", "zooms out")),
    ("pan_left", ("pan left", "pans left")),
    ("pan_right", ("pan right", "pans right")),
    ("whip_pan", ("whip pan",)),
    ("tilt_up", ("tilt up", "tilts up")),
    ("tilt_down", ("tilt down", "tilts down")),
    ("tracking", ("tracking", "tracks ", "follow shot", "following")),
    ("steadicam", ("steadicam", "gimbal")),
    ("handheld", ("handheld", "hand-held", "shaky")),
    ("crane", ("crane", "jib")),
    ("orbit", ("orbit", "arc shot", "circles")),
    ("static", ("static", "locked", "no movement", "fixed", "still camera", "locked-off")),
)


def shot_size_from_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in SHOT_SIZES:
        return text
    for canonical, phrases in _SHOT_SIZE_PATTERNS:
        if any(phrase in text for phrase in phrases):
            return canonical
    return ""


def camera_movement_from_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in CAMERA_MOVEMENTS:
        return text
    for canonical, phrases in _MOVEMENT_PATTERNS:
        if any(phrase in text for phrase in phrases):
            return canonical
    return ""


# ── reading planner output ──────────────────────────────────────────────────


def _plan_payload(response: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract ``(production_plan, shots, clip_plans)`` from a Director response.

    Two real Director shapes are accepted:

    * a ``ProductionPlan`` of ``ShotPlan`` objects (the V2 planner);
    * the short-film story planner's ``{clips, clip_plans}`` (no audio), whose
      clips carry start/end times, a scene label and dialogue lines.
    """
    if not isinstance(response, Mapping):
        raise FilmProjectError("Director response must be an object")
    plan = response.get("production_plan")
    if not isinstance(plan, Mapping):
        plan = response if isinstance(response.get("shots"), Sequence) else {}
    plan = dict(plan)
    shots = [shot for shot in (plan.get("shots") or []) if isinstance(shot, Mapping)]
    clip_plans = [
        clip for clip in (response.get("clip_plans") or []) if isinstance(clip, Mapping)
    ]
    if not shots and response.get("clips"):
        shots = [
            _shot_from_clip(clip, index)
            for index, clip in enumerate(response.get("clips") or [])
            if isinstance(clip, Mapping)
        ]
        plan.setdefault("skill_type", str(response.get("skill_type") or "short_film"))
        plan.setdefault("title", str(response.get("title") or ""))
        plan.setdefault("global_style", str(response.get("global_style") or ""))
    return plan, [dict(shot) for shot in shots], clip_plans


def _dialogue_beats_from_lines(lines: Any) -> list[dict[str, str]]:
    beats: list[dict[str, str]] = []
    if not isinstance(lines, Sequence) or isinstance(lines, (str, bytes)):
        return beats
    for line in lines:
        if isinstance(line, Mapping):
            text = str(line.get("line") or line.get("text") or "").strip()
            speaker = str(line.get("speaker") or line.get("character") or "").strip()
            if text:
                beats.append({"speaker": speaker, "line": text})
        elif str(line).strip():
            beats.append({"line": str(line).strip()})
    return beats


def _shot_from_clip(clip: Mapping[str, Any], index: int) -> dict[str, Any]:
    """Adapt the short-film story planner's clip into a ShotPlan-shaped dict."""
    try:
        start = float(clip.get("start") or 0.0)
        end = float(clip.get("end") or 0.0)
        duration = end - start
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        duration = 4.0
    label = str(clip.get("section_label") or "").strip()
    hint = str(clip.get("suggested_prompt_hint") or "").strip()
    return {
        "shot_id": f"clip-{index + 1}",
        "index": index,
        "duration_sec": round(duration, 3),
        "scene_goal": hint or f"Shot {index + 1}",
        "narrative_role": label,
        "scene_type": label,
        "subjects_on_screen": [],
        "spatial_setup": hint,
        "environment": "",
        "visual_style": "",
        "lighting": "",
        "mood": "",
        "action_beats": [],
        "dialogue_beats": _dialogue_beats_from_lines(clip.get("dialogue_lines")),
        "camera_plan": {},
        "audio_plan": {},
        "ending_beat": "",
        "constraints": [],
        "continuity_refs": [],
        "metadata": {
            "source": "short_film_story_planner",
            "duration_frames": clip.get("duration_frames"),
            "dominant_speaker": clip.get("dominant_speaker"),
        },
    }


def _subject_names(shot: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for subject in shot.get("subjects_on_screen") or []:
        if isinstance(subject, Mapping):
            name = str(
                subject.get("name")
                or subject.get("display_name")
                or subject.get("role")
                or subject.get("character")
                or subject.get("description")
                or ""
            ).strip()
        else:
            name = str(subject).strip()
        if name and name not in names:
            names.append(name)
    return names


def _dialogue_text(shot: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for beat in shot.get("dialogue_beats") or []:
        if not isinstance(beat, Mapping):
            text = str(beat).strip()
            if text:
                lines.append(text)
            continue
        speaker = str(beat.get("speaker") or beat.get("character") or "").strip()
        line = str(beat.get("line") or beat.get("text") or beat.get("dialogue") or "").strip()
        if not line:
            continue
        lines.append(f"{speaker}: {line}" if speaker else line)
    if not lines:
        return str(shot.get("dialogue") or "").strip()
    return "\n".join(lines)


def _description(shot: Mapping[str, Any]) -> str:
    action = [str(beat).strip() for beat in shot.get("action_beats") or [] if str(beat).strip()]
    pieces = [
        str(shot.get("spatial_setup") or "").strip(),
        str(shot.get("environment") or "").strip(),
        " ".join(action),
        str(shot.get("ending_beat") or "").strip(),
    ]
    return "\n".join(piece for piece in pieces if piece)


def _scene_key(shot: Mapping[str, Any], previous: Optional[Mapping[str, Any]]) -> str:
    """Consecutive shots sharing a scene_type belong to the same scene."""
    scene_type = str(shot.get("scene_type") or "").strip()
    if not scene_type:
        return ""
    if previous is None:
        return scene_type
    if str(previous.get("scene_type") or "").strip() != scene_type:
        return scene_type
    return ""


def _character_matches(name: str, project: Mapping[str, Any]) -> list[str]:
    """Assets whose name identifies this character, case-insensitively."""
    needle = name.strip().lower()
    if not needle:
        return []
    tokens = [token for token in needle.replace("-", " ").split() if len(token) > 3]
    matches: list[str] = []
    for asset in (project.get("assets") or {}).values():
        if not isinstance(asset, Mapping) or asset.get("role") != "character_reference":
            continue
        label = str(asset.get("display_name") or asset.get("name") or asset.get("filename") or "").lower()
        if needle in label or any(token in label for token in tokens):
            matches.append(str(asset.get("id") or ""))
    return matches


def ingest_director_plan(
    project: dict[str, Any],
    response: Mapping[str, Any],
    *,
    imported_from: str = "",
    replace_storyboard: bool = True,
) -> dict[str, Any]:
    """Turn a real Director response into scenes, shots and the continuity bible.

    Returns a report describing exactly what changed, including how many already
    rendered shots were carried over (matched by an identical video prompt) so a
    re-plan never silently discards finished takes.
    """
    plan, plan_shots, clip_plans = _plan_payload(response)
    if not plan_shots:
        raise FilmProjectError("The Director returned no shots to storyboard.")

    existing_shots = [
        shot for shot in (project.get("shots") or []) if isinstance(shot, Mapping)
    ]
    renders_by_prompt = {
        str(shot.get("video_prompt") or "").strip(): dict(shot)
        for shot in existing_shots
        if str(shot.get("video_prompt") or "").strip() and shot.get("takes")
    }

    if replace_storyboard:
        project["scenes"] = []
        project["shots"] = []

    scene_id_by_key: dict[str, str] = {}
    previous: Optional[Mapping[str, Any]] = None
    scene_count = 0
    preserved = 0

    for index, raw_shot in enumerate(plan_shots):
        key = _scene_key(raw_shot, previous)
        if key and key not in scene_id_by_key:
            metadata = raw_shot.get("metadata") if isinstance(raw_shot.get("metadata"), Mapping) else {}
            new_scene_id = uuid.uuid4().hex[:12]
            upsert_scene(project, {
                "id": new_scene_id,
                "title": key.replace("_", " ").strip().title() or f"Scene {scene_count + 1}",
                "objective": str(raw_shot.get("scene_goal") or "").strip(),
                "description": str(raw_shot.get("environment") or "").strip(),
                "time_of_day": str(metadata.get("time_of_day") or ""),
                "mood": str(raw_shot.get("mood") or "").strip(),
            })
            scene_id_by_key[key] = new_scene_id
            scene_count += 1
        previous = raw_shot

        scene_id = ""
        if key:
            scene_id = scene_id_by_key[key]
        elif scene_id_by_key:
            scene_id = next(iter(scene_id_by_key.values()))
        elif project.get("scenes"):
            scene_id = str(project["scenes"][0].get("id") or "")
        else:
            new_scene_id = uuid.uuid4().hex[:12]
            upsert_scene(project, {
                "id": new_scene_id,
                "title": str(plan.get("title") or "Main sequence").strip() or "Main sequence",
                "objective": str(raw_shot.get("scene_goal") or "").strip(),
            })
            scene_id_by_key["__default__"] = new_scene_id
            scene_id = new_scene_id

        camera = raw_shot.get("camera_plan") if isinstance(raw_shot.get("camera_plan"), Mapping) else {}
        audio_plan = raw_shot.get("audio_plan") if isinstance(raw_shot.get("audio_plan"), Mapping) else {}
        clip = clip_plans[index] if index < len(clip_plans) else {}
        video_prompt = str(clip.get("video_prompt") or raw_shot.get("video_prompt") or "").strip()
        image_prompt = str(clip.get("image_prompt") or raw_shot.get("image_prompt") or "").strip()

        payload: dict[str, Any] = {
            "index": index,
            "scene_id": scene_id,
            "objective": str(raw_shot.get("scene_goal") or "").strip(),
            "description": _description(raw_shot),
            "shot_size": shot_size_from_text(camera.get("framing") or raw_shot.get("shot_size")),
            "camera_movement": camera_movement_from_text(camera.get("movement")),
            "duration_sec": raw_shot.get("duration_sec") or 4.0,
            "transition": _transition_from(raw_shot),
            "dialogue": _dialogue_text(raw_shot),
            "elements": _subject_names(raw_shot),
            "lighting": str(raw_shot.get("lighting") or "").strip(),
            "music_notes": _audio_notes(audio_plan),
            "sfx": ", ".join(str(value) for value in (audio_plan.get("effects") or []) if str(value).strip()),
            "continuity_refs": [
                str(value) for value in (raw_shot.get("continuity_refs") or []) if str(value).strip()
            ],
            "image_prompt": image_prompt,
            "video_prompt": video_prompt,
            "source": "director",
            "status": "planned",
            "model_params": {},
            "generation_notes": str(raw_shot.get("narrative_role") or "").strip(),
        }
        preserved_shot = renders_by_prompt.get(video_prompt)
        if preserved_shot:
            payload["takes"] = preserved_shot.get("takes") or []
            payload["output_filename"] = preserved_shot.get("output_filename") or ""
            payload["approved"] = bool(preserved_shot.get("approved"))
            payload["approved_take_index"] = preserved_shot.get("approved_take_index")
            payload["status"] = "approved" if preserved_shot.get("approved") else "generated"
            preserved += 1

        upsert_shot(project, payload)

    unmatched_characters: list[str] = []
    for index, raw_character in enumerate(plan.get("characters") or []):
        if not isinstance(raw_character, Mapping):
            continue
        name = str(raw_character.get("display_name") or raw_character.get("name") or f"Character {index + 1}")
        reference_ids = _character_matches(name, project)
        if not reference_ids:
            unmatched_characters.append(name)
        project.setdefault("bible", {}).setdefault("characters", []).append({
            "id": str(raw_character.get("id") or f"char-{index + 1}"),
            "name": name,
            "display_name": str(raw_character.get("display_name") or ""),
            "physical_description": str(raw_character.get("physical_description") or ""),
            "wardrobe": str(raw_character.get("wardrobe") or ""),
            "voice_description": str(raw_character.get("voice_description") or ""),
            "reference_asset_ids": reference_ids,
        })

    project.setdefault("bible", {}).setdefault("continuity_rules", []).extend(
        str(note) for note in (plan.get("continuity_notes") or []) if str(note).strip()
    )
    if plan.get("global_style"):
        bible = project.setdefault("bible", {})
        bible["style_notes"] = "\n".join(
            part for part in (str(bible.get("style_notes") or "").strip(), str(plan["global_style"]).strip()) if part
        )

    project["plan"] = {
        "director_skill": str(plan.get("skill_type") or response.get("skill_type") or ""),
        "title": str(plan.get("title") or ""),
        "global_style": str(plan.get("global_style") or ""),
        "continuity_notes": [str(note) for note in (plan.get("continuity_notes") or [])],
        "planned_at": time.time(),
        "imported_from": imported_from,
        "production_plan": plan,
    }
    project["status"] = "planned"

    report = {
        "scenes": len(project.get("scenes") or []),
        "shots": len(project.get("shots") or []),
        "renders_preserved": preserved,
        "characters": len((project.get("bible") or {}).get("characters") or []),
        "characters_without_reference": unmatched_characters,
        "planned_duration_sec": round(film_project_duration(project), 3),
    }
    record_decision(
        project,
        "plan_imported",
        f"Director plan imported: {report['shots']} shots over {report['scenes']} scenes.",
        report,
    )
    return report


def _transition_from(shot: Mapping[str, Any]) -> str:
    metadata = shot.get("metadata") if isinstance(shot.get("metadata"), Mapping) else {}
    for key in ("transition", "transition_in", "cut"):
        value = str(metadata.get(key) or "").strip().lower()
        if value in ("cut", "dissolve", "fade_black", "fade_white", "whip", "match_cut"):
            return value
    continuity = str(shot.get("continuity_strategy") or "").strip().lower()
    if continuity == "continuous":
        return "cut"
    return "cut"


def _audio_notes(audio_plan: Mapping[str, Any]) -> str:
    pieces: list[str] = []
    if audio_plan.get("ambience"):
        pieces.append(f"Ambience: {audio_plan['ambience']}")
    if audio_plan.get("vocal_style"):
        pieces.append(f"Vocals: {audio_plan['vocal_style']}")
    if audio_plan.get("mode"):
        pieces.append(f"Audio mode: {audio_plan['mode']}")
    return "\n".join(pieces)


# ── storyboard projection ───────────────────────────────────────────────────


def storyboard(project: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Shots in shooting order, each carrying its scene context."""
    scenes = {
        str(scene.get("id")): scene
        for scene in (project.get("scenes") or [])
        if isinstance(scene, Mapping)
    }
    ordered = sorted(
        (shot for shot in (project.get("shots") or []) if isinstance(shot, Mapping)),
        key=lambda shot: shot.get("index", 0),
    )
    entries: list[dict[str, Any]] = []
    elapsed = 0.0
    for shot in ordered:
        scene = scenes.get(str(shot.get("scene_id") or "")) or {}
        duration = float(shot.get("duration_sec") or 0.0)
        entries.append({
            "id": shot.get("id"),
            "index": shot.get("index"),
            "scene_id": shot.get("scene_id"),
            "scene_title": scene.get("title", ""),
            "objective": shot.get("objective", ""),
            "description": shot.get("description", ""),
            "shot_size": shot.get("shot_size", ""),
            "camera_movement": shot.get("camera_movement", ""),
            "duration_sec": duration,
            "transition": shot.get("transition", "cut"),
            "start_image_asset_id": shot.get("start_image_asset_id", ""),
            "reference_asset_ids": list(shot.get("reference_asset_ids") or []),
            "elements": list(shot.get("elements") or []),
            "dialogue": shot.get("dialogue", ""),
            "music_notes": shot.get("music_notes", ""),
            "sfx": shot.get("sfx", ""),
            "lighting": shot.get("lighting", ""),
            "video_prompt": shot.get("video_prompt", ""),
            "image_prompt": shot.get("image_prompt", ""),
            "model": shot.get("model", ""),
            "status": shot.get("status", "planned"),
            "approved": bool(shot.get("approved")),
            "take_count": len(shot.get("takes") or []),
            "output_filename": shot.get("output_filename", ""),
            "start_time_sec": round(elapsed, 4),
            "end_time_sec": round(elapsed + duration, 4),
        })
        elapsed += duration
    return entries


def storyboard_totals(project: Mapping[str, Any]) -> dict[str, Any]:
    entries = storyboard(project)
    return {
        "shot_count": len(entries),
        "duration_sec": round(sum(entry["duration_sec"] for entry in entries), 3),
        "approved": sum(1 for entry in entries if entry["approved"]),
        "with_prompt": sum(1 for entry in entries if entry["video_prompt"]),
        "with_takes": sum(1 for entry in entries if entry["take_count"]),
        "shots_without_prompt": [entry["id"] for entry in entries if not entry["video_prompt"]],
    }


# ── production requests ─────────────────────────────────────────────────────


def production_requests(
    project: Mapping[str, Any],
    *,
    only_missing: bool = True,
    shot_ids: Optional[Sequence[str]] = None,
) -> list[dict[str, Any]]:
    """Concrete per-shot work items for the existing generation queue."""
    wanted = {str(value) for value in shot_ids} if shot_ids else None
    requests: list[dict[str, Any]] = []
    for entry in storyboard(project):
        shot = find_shot(project, str(entry["id"])) or {}
        if wanted is not None and str(entry["id"]) not in wanted:
            continue
        # Only a take that wrote a file counts as rendered. A failed or
        # cancelled take leaves the shot retryable.
        already_rendered = shot_has_render(shot)
        skip_reason = ""
        if not entry["video_prompt"] and not entry["image_prompt"]:
            skip_reason = "No prompt: plan or write this shot before generating."
        elif only_missing and already_rendered:
            skip_reason = "Already rendered; regenerate explicitly to replace it."
        requests.append({
            "shot_id": entry["id"],
            "index": entry["index"],
            "scene_id": entry["scene_id"],
            "prompt": entry["video_prompt"],
            "image_prompt": entry["image_prompt"],
            "negative_prompt": shot.get("negative_prompt", ""),
            "duration_sec": entry["duration_sec"],
            "shot_size": entry["shot_size"],
            "camera_movement": entry["camera_movement"],
            "start_image_asset_id": entry["start_image_asset_id"],
            "reference_asset_ids": entry["reference_asset_ids"],
            "model": shot.get("model", ""),
            "model_params": dict(shot.get("model_params") or {}),
            "status": entry["status"],
            "already_rendered": already_rendered,
            "skip": bool(skip_reason),
            "skip_reason": skip_reason,
        })
    return requests


def production_readiness(
    project: Mapping[str, Any],
    *,
    model_available: bool = True,
) -> list[dict[str, str]]:
    """Blocking reasons a production run cannot start, based on real state."""
    issues: list[dict[str, str]] = []
    requests = production_requests(project, only_missing=True)
    runnable = [request for request in requests if not request["skip"]]
    if not requests:
        issues.append({
            "code": "empty_storyboard",
            "severity": "blocking",
            "message": "No shots yet. Generate a plan with Director Mode or add shots manually.",
        })
    elif not runnable:
        issues.append({
            "code": "nothing_to_do",
            "severity": "warning",
            "message": "Every shot already has a render. Regenerate individual shots to replace them.",
        })
    if not model_available:
        issues.append({
            "code": "engine_unavailable",
            "severity": "blocking",
            "message": (
                "The local generation engine is not available in this process. Run production on a "
                "machine with an NVIDIA GPU and the Maestro models installed."
            ),
        })
    for request in requests:
        if request["skip"] and request["skip_reason"].startswith("No prompt"):
            issues.append({
                "code": "shot_without_prompt",
                "severity": "blocking",
                "message": f"Shot {request['index'] + 1} has no prompt.",
            })
    return issues


def record_production_job(
    project: dict[str, Any],
    *,
    shot_id: str,
    job_id: str,
    kind: str = "video",
    params: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    shot = find_shot(project, shot_id)
    if shot is None:
        raise FilmProjectError("Shot not found")
    add_job(project, {
        "id": job_id,
        "shot_id": shot_id,
        "kind": kind,
        "status": "queued",
        "params": dict(params or {}),
    })
    shot["status"] = "queued"
    return project


def sync_shot_from_job(
    project: dict[str, Any],
    *,
    job_id: str,
    status: str,
    filename: str = "",
    error: str = "",
) -> dict[str, Any]:
    """Reflect real queue state onto the shot and its take history."""
    job = next(
        (entry for entry in (project.get("jobs") or [])
         if isinstance(entry, Mapping) and entry.get("id") == job_id),
        None,
    )
    if job is None:
        raise FilmProjectError("Job not found in this project")
    shot_id = str(job.get("shot_id") or "")
    update_job(project, job_id, {
        "status": status,
        "output_filename": filename,
        "error": error,
    })
    shot = find_shot(project, shot_id)
    if status == "completed" and filename:
        add_take(project, shot_id, {
            "job_id": job_id,
            "filename": filename,
            "status": "generated",
            "prompt": str((project.get("plan") or {}).get("title") or ""),
        })
    elif status == "failed":
        add_take(project, shot_id, {"job_id": job_id, "status": "failed", "error": error})
    elif status == "cancelled":
        # Cancelling is not a failure: the shot goes back to a state the user can
        # submit again from, instead of looking broken or finished.
        if shot is not None and not shot_has_render(shot):
            shot["status"] = "planned"
            shot["updated_at"] = time.time()
        record_decision(
            project,
            "job_cancelled",
            f"Cancelled the job for shot {int(shot.get('index') or 0) + 1 if shot else '?'}.",
            {"job_id": job_id, "shot_id": shot_id},
        )
    return project


# ── reference assignment & visual continuity ────────────────────────────────


def _resolve_target_shots(
    project: Mapping[str, Any],
    *,
    shot_ids: Sequence[str] = (),
    scene_ids: Sequence[str] = (),
    all_shots: bool = False,
) -> list[dict[str, Any]]:
    shots = [shot for shot in (project.get("shots") or []) if isinstance(shot, dict)]
    if all_shots:
        targets = shots
    else:
        wanted = {str(value) for value in shot_ids if str(value).strip()}
        scenes = {str(value) for value in scene_ids if str(value).strip()}
        targets = [
            shot for shot in shots
            if str(shot.get("id")) in wanted or str(shot.get("scene_id")) in scenes
        ]
    if not targets:
        raise FilmProjectError("No shots matched: choose a shot or a scene to assign references to.")
    return targets


def assign_references(
    project: dict[str, Any],
    *,
    asset_ids: Sequence[str],
    shot_ids: Sequence[str] = (),
    scene_ids: Sequence[str] = (),
    all_shots: bool = False,
    as_start_image: bool = False,
    replace: bool = False,
) -> dict[str, Any]:
    """Attach specific files to specific shots or whole scenes.

    This is the user's explicit association: it always wins over whatever the
    planner guessed, and it is what the production step sends to the model as the
    shot's start frame or reference images.
    """
    wanted_assets: list[str] = []
    for asset_id in asset_ids or []:
        value = str(asset_id or "").strip()
        if not value or value in wanted_assets:
            continue
        asset = find_asset(project, value)
        if asset is None:
            raise FilmProjectError(f"Unknown asset: {value}")
        if str(asset.get("kind")) != "image":
            raise FilmProjectError(
                f"'{asset.get('name')}' is {asset.get('kind')}, not an image, so it cannot be a visual reference."
            )
        wanted_assets.append(value)
    if not wanted_assets:
        raise FilmProjectError("Choose at least one image to assign.")

    targets = _resolve_target_shots(
        project, shot_ids=shot_ids, scene_ids=scene_ids, all_shots=all_shots,
    )
    added = 0
    for shot in targets:
        references = [] if replace else [
            str(value) for value in shot.get("reference_asset_ids") or []
        ]
        start_asset = "" if replace else str(shot.get("start_image_asset_id") or "")
        for asset_id in wanted_assets:
            if as_start_image and not start_asset:
                start_asset = asset_id
                continue
            if asset_id not in references:
                references.append(asset_id)
                added += 1
        shot["reference_asset_ids"] = references
        shot["start_image_asset_id"] = start_asset
        shot["updated_at"] = time.time()

    report = {
        "shots": len(targets),
        "shot_ids": [str(shot.get("id")) for shot in targets],
        "references_added": added,
        "asset_ids": wanted_assets,
        "as_start_image": bool(as_start_image),
        "replaced": bool(replace),
    }
    record_decision(
        project,
        "references_assigned",
        f"{len(wanted_assets)} reference image(s) assigned to {len(targets)} shot(s).",
        report,
    )
    return report


def apply_visual_continuity(project: dict[str, Any], *, replace: bool = False) -> dict[str, Any]:
    """Carry the character bible into every shot that features those characters.

    A shot inherits a character's reference images when the character's name
    appears in the shot's cast/elements, description or dialogue, so the same
    face conditions every shot instead of only the ones the user remembers to
    tag. Characters with no reference image are reported, never substituted.
    """
    bible = project.get("bible") or {}
    characters = [
        character for character in bible.get("characters") or [] if isinstance(character, Mapping)
    ]
    if not characters:
        return {
            "characters": 0,
            "shots_updated": 0,
            "references_added": 0,
            "characters_matched": [],
            "characters_without_reference": [],
            "shots_without_references": [],
            "reason": "The continuity bible has no characters yet. Import a plan that describes the cast.",
        }

    shots_updated = 0
    references_added = 0
    matched: list[str] = []
    without_reference: list[str] = []
    coverage: list[str] = []

    for shot in [entry for entry in (project.get("shots") or []) if isinstance(entry, dict)]:
        haystack = " ".join(
            [str(shot.get("description") or ""), str(shot.get("dialogue") or "")]
            + [str(value) for value in shot.get("elements") or []]
        ).lower()
        references = [] if replace else [str(value) for value in shot.get("reference_asset_ids") or []]
        start_asset = "" if replace else str(shot.get("start_image_asset_id") or "")
        featured: list[str] = []
        for character in characters:
            name = str(character.get("display_name") or character.get("name") or "").strip()
            if not name:
                continue
            tokens = [token for token in name.lower().replace("-", " ").split() if len(token) > 2]
            if not tokens or not any(token in haystack for token in tokens):
                continue
            featured.append(name)
            # A reference listed in the bible but no longer in the project is
            # treated as missing rather than passed to the model.
            asset_ids = [
                str(value) for value in character.get("reference_asset_ids") or []
                if find_asset(project, str(value)) is not None
            ]
            if not asset_ids:
                without_reference.append(name)
                continue
            if name not in matched:
                matched.append(name)
            for asset_id in asset_ids:
                if asset_id and asset_id not in references:
                    references.append(asset_id)
                    references_added += 1
        if featured:
            continuity_refs = [str(value) for value in shot.get("continuity_refs") or []]
            for character in characters:
                name = str(character.get("display_name") or character.get("name") or "")
                character_id = str(character.get("id") or "")
                if name in featured and character_id and character_id not in continuity_refs:
                    continuity_refs.append(character_id)
            shot["continuity_refs"] = continuity_refs
            shot["reference_asset_ids"] = references
            shot["start_image_asset_id"] = start_asset
            shots_updated += 1
        if not references:
            coverage.append(str(shot.get("id")))

    report = {
        "characters": len(characters),
        "shots_updated": shots_updated,
        "references_added": references_added,
        "characters_matched": matched,
        "characters_without_reference": sorted(set(without_reference)),
        "shots_without_references": coverage,
    }
    record_decision(
        project,
        "continuity_applied",
        f"Visual continuity applied to {shots_updated} shot(s) from {len(matched)} character(s).",
        report,
    )
    return report


def reference_files_for_shot(
    project: Mapping[str, Any],
    shot_id: str,
    *,
    resolve,
) -> dict[str, Any]:
    """Resolve a shot's declared references to existing local files."""
    shot = find_shot(project, shot_id)
    if shot is None:
        raise FilmProjectError("Shot not found")
    start: Optional[str] = None
    references: list[str] = []
    missing: list[str] = []

    def resolve_id(asset_id: str) -> Optional[str]:
        asset = find_asset(project, asset_id)
        if asset is None:
            missing.append(asset_id)
            return None
        path = resolve(asset)
        if path is None:
            missing.append(str(asset.get("name") or asset.get("filename") or asset_id))
        return path

    start_asset_id = str(shot.get("start_image_asset_id") or "")
    if start_asset_id:
        start = resolve_id(start_asset_id)
    for asset_id in shot.get("reference_asset_ids") or []:
        path = resolve_id(str(asset_id))
        if path and path != start:
            references.append(path)
    return {"start_image": start, "references": references, "missing": missing}

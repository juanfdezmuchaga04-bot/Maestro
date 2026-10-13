"""Turn a film project into a Director brief and into planner reference inputs.

This is the bridge between the Studio's production document
(:mod:`services.film_projects`) and Director Mode, which already knows how to
write a screenplay, break it into shots and render prompts.  Nothing here calls
a model: it composes the instruction text, resolves which files on disk belong to
which reference slot, and reports what is still missing.
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Mapping, Optional, Sequence

from services.film_projects import (
    AUDIO_ROLES,
    DOCUMENT_ROLES,
    IMAGE_REFERENCE_ROLES,
    VIDEO_ROLES,
    assets_with_roles,
)

APPROACH_DIRECTIVES: dict[str, str] = {
    "faithful": (
        "MODE: FAITHFUL. The user's own material is the film. Reproduce the supplied "
        "references as closely as the models allow: same faces, same wardrobe, same "
        "architecture, same framing ideas. Do not invent characters, locations or story "
        "beats that the brief and files do not support. When a detail is not visible in a "
        "reference, describe it as 'as in the reference file' rather than inventing it."
    ),
    "creative": (
        "MODE: CREATIVE. The material defines tone, palette and intent; you may restructure, "
        "invent supporting characters, locations and shot language freely within the brief's "
        "constraints. Keep the named principal characters recognisable, but the staging is yours."
    ),
    "hybrid": (
        "MODE: HYBRID. Preserve the principal references (the characters, the main location "
        "and any soundtrack the user supplied) exactly, and develop everything else — "
        "secondary characters, extra locations, transitions and beats — creatively within the brief."
    ),
}

#: Role → how the file should be described to the writer.
_ROLE_DESCRIPTIONS = {
    "character_reference": "character identity reference",
    "location_reference": "location reference",
    "set_design": "set design reference",
    "wardrobe": "wardrobe reference",
    "lighting": "lighting reference",
    "palette": "colour palette reference",
    "camera_movement": "camera movement reference",
    "style_reference": "visual style reference",
    "prop": "prop reference",
    "soundtrack": "music for the soundtrack",
    "source_music_keep": "music the user wants preserved as the soundtrack",
    "character_voice": "voice reference for a character",
    "narration_voice": "narration voice reference",
    "dialogue_audio": "recorded dialogue",
    "sfx": "sound effect",
    "ambience": "ambience / room tone",
    "source_video_keep": "source video to preserve unchanged",
    "source_video_edit": "source video to be edited",
    "script_document": "script or document",
    "other": "reference material",
}


def describe_asset(asset: Mapping[str, Any]) -> str:
    role = str(asset.get("role") or "other")
    description = _ROLE_DESCRIPTIONS.get(role, "reference material")
    name = str(asset.get("display_name") or asset.get("name") or asset.get("filename") or "file")
    notes: list[str] = [description]
    if role in IMAGE_REFERENCE_ROLES:
        notes.append(
            "reproduce literally" if asset.get("reproduce_literally")
            else "inspiration only — do not copy it frame-for-frame"
        )
    if role in AUDIO_ROLES and asset.get("keep_original"):
        notes.append("keep this audio, never replace or mute it")
    limitations = list((asset.get("analysis") or {}).get("limitations") or [])
    if limitations:
        notes.append(f"local analysis unavailable ({limitations[0]})")
    return f"- \"{name}\" — {', '.join(notes)}"


def asset_role_summary(project: Mapping[str, Any]) -> str:
    assets = [asset for asset in (project.get("assets") or {}).values() if isinstance(asset, Mapping)]
    if not assets:
        return ""
    lines = [describe_asset(asset) for asset in assets]
    return "\n".join(sorted(lines))


def approach_directive(approach: str) -> str:
    return APPROACH_DIRECTIVES.get(str(approach or "hybrid"), APPROACH_DIRECTIVES["hybrid"])


def compose_director_brief(project: Mapping[str, Any]) -> str:
    """The natural-language instruction handed to Director Mode."""
    brief = project.get("brief") or {}
    parts: list[str] = []
    title = str(project.get("name") or "").strip()
    if title:
        parts.append(f"PRODUCTION: {title}")
    if brief.get("goal"):
        parts.append(f"OBJECTIVE: {brief['goal']}")
    if brief.get("logline"):
        parts.append(f"LOGLINE: {brief['logline']}")
    if brief.get("synopsis"):
        parts.append(f"TREATMENT: {brief['synopsis']}")

    look: list[str] = []
    for label, key in (("Genre", "genre"), ("Tone", "tone"), ("Visual style", "visual_style")):
        if brief.get(key):
            look.append(f"{label}: {brief[key]}")
    if brief.get("aspect_ratio"):
        look.append(f"Aspect ratio: {brief['aspect_ratio']}")
    if brief.get("target_duration_sec"):
        look.append(f"Target duration: {float(brief['target_duration_sec']):.0f} seconds")
    if brief.get("language"):
        look.append(f"Dialogue language: {brief['language']}")
    if brief.get("audience"):
        look.append(f"Audience: {brief['audience']}")
    if look:
        parts.append("LOOK AND FEEL:\n" + "\n".join(look))

    must_keep = [str(value) for value in brief.get("must_keep") or []]
    if must_keep:
        parts.append("MUST KEEP:\n" + "\n".join(f"- {value}" for value in must_keep))
    avoid = [str(value) for value in brief.get("avoid") or []]
    if avoid:
        parts.append("AVOID:\n" + "\n".join(f"- {value}" for value in avoid))

    if brief.get("reference_notes"):
        parts.append(f"REFERENCE NOTES: {brief['reference_notes']}")

    summary = asset_role_summary(project)
    if summary:
        parts.append(
            "SUPPLIED MATERIAL (respect the role the user gave each file):\n" + summary
        )

    script_text = script_excerpt(project)
    if script_text:
        parts.append(f"USER-SUPPLIED SCRIPT OR TREATMENT TEXT:\n{script_text}")

    bible = project.get("bible") or {}
    continuity: list[str] = []
    for character in bible.get("characters") or []:
        if not isinstance(character, Mapping):
            continue
        details = ", ".join(
            value for value in (
                str(character.get("physical_description") or ""),
                str(character.get("wardrobe") or ""),
            ) if value
        )
        continuity.append(f"- {character.get('name')}: {details}".rstrip(": "))
    for location in bible.get("locations") or []:
        if not isinstance(location, Mapping):
            continue
        continuity.append(
            f"- Location {location.get('name')}: {location.get('description') or location.get('lighting') or ''}".rstrip(": ")
        )
    if continuity:
        parts.append("CONTINUITY BIBLE (keep consistent in every shot):\n" + "\n".join(continuity))

    parts.append(approach_directive(project.get("approach", "hybrid")))
    return "\n\n".join(part for part in parts if part.strip())


def script_excerpt(project: Mapping[str, Any], *, limit: int = 12000) -> str:
    """Text the user supplied as a script/document, in import order."""
    chunks: list[str] = []
    for asset in assets_with_roles(project, DOCUMENT_ROLES):
        text = str((asset.get("analysis") or {}).get("text_excerpt") or "").strip()
        if text:
            chunks.append(text)
        if sum(len(chunk) for chunk in chunks) >= limit:
            break
    return "\n\n".join(chunks)[:limit]


# ── resolving assets to real files ──────────────────────────────────────────


def _candidate_paths(asset: Mapping[str, Any], *, uploads_root: str, save_root: str, workspace: str) -> list[str]:
    filename = str(asset.get("filename") or "").strip()
    if not filename:
        return []
    candidates = [str(asset.get("path") or "")]
    if os.path.isabs(filename):
        candidates.append(filename)
    else:
        candidates.append(os.path.join(uploads_root, filename))
        candidates.append(os.path.join(save_root, filename))
        if workspace and workspace != "default":
            candidates.append(os.path.join(save_root, workspace, filename))
        candidates.append(os.path.join(save_root, workspace or "default", "uploads", filename))
    return [candidate for candidate in candidates if candidate]


def resolve_asset_path(
    asset: Mapping[str, Any],
    *,
    uploads_root: str,
    save_root: str,
    workspace: str,
) -> Optional[str]:
    """First existing local file for an asset, or ``None`` when it is missing."""
    for candidate in _candidate_paths(
        asset, uploads_root=uploads_root, save_root=save_root, workspace=workspace
    ):
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def missing_asset_files(
    project: Mapping[str, Any],
    *,
    uploads_root: str,
    save_root: str,
    workspace: str,
) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for asset in (project.get("assets") or {}).values():
        if not isinstance(asset, Mapping):
            continue
        if resolve_asset_path(asset, uploads_root=uploads_root, save_root=save_root, workspace=workspace):
            continue
        missing.append({
            "asset_id": str(asset.get("id") or ""),
            "name": str(asset.get("name") or asset.get("filename") or ""),
            "message": "The file is referenced by the project but not found in the workspace.",
        })
    return missing


def director_reference_inputs(
    project: Mapping[str, Any],
    *,
    uploads_root: str,
    save_root: str,
    workspace: str,
) -> dict[str, Any]:
    """Planner-shaped reference inputs built from the project's assigned roles.

    The output matches the keys Director's planners already consume
    (``reference_image_path``, ``character_ref_paths``, ``location_ref_paths``),
    so it can be passed straight through
    :func:`services.director.reference_inputs.planning_reference_inputs`.
    """
    character_paths: list[str] = []
    character_labels: list[str] = []
    location_paths: list[str] = []
    location_labels: list[str] = []
    soundtrack_path: Optional[str] = None
    voice_path: Optional[str] = None
    source_videos: list[str] = []
    unresolved: list[str] = []

    def resolved(asset: Mapping[str, Any]) -> Optional[str]:
        path = resolve_asset_path(
            asset, uploads_root=uploads_root, save_root=save_root, workspace=workspace
        )
        if path is None:
            unresolved.append(str(asset.get("name") or asset.get("filename") or ""))
        return path

    for asset in (project.get("assets") or {}).values():
        if not isinstance(asset, Mapping):
            continue
        role = str(asset.get("role") or "")
        label = str(asset.get("display_name") or asset.get("name") or asset.get("filename") or "reference")
        if role == "character_reference":
            path = resolved(asset)
            if path:
                character_paths.append(path)
                character_labels.append(label)
        elif role in ("location_reference", "set_design"):
            path = resolved(asset)
            if path:
                location_paths.append(path)
                location_labels.append(label)
        elif role in ("soundtrack", "source_music_keep") and soundtrack_path is None:
            soundtrack_path = resolved(asset)
        elif role in ("character_voice", "narration_voice", "dialogue_audio") and voice_path is None:
            voice_path = resolved(asset)
        elif role in VIDEO_ROLES:
            path = resolved(asset)
            if path:
                source_videos.append(path)

    reference_image = character_paths[0] if character_paths else None
    remaining_characters = character_paths[1:] if reference_image else []
    remaining_labels = character_labels[1:] if reference_image else []

    inputs: dict[str, Any] = {
        "reference_image_path": reference_image,
        "character_ref_paths": remaining_characters,
        "character_ref_labels": remaining_labels,
        "location_ref_paths": location_paths,
        "location_ref_labels": location_labels,
        "unresolved_asset_names": unresolved,
    }
    if soundtrack_path:
        inputs["audio_file"] = soundtrack_path
    if voice_path:
        inputs["voice_reference"] = voice_path
    if source_videos:
        inputs["source_video_paths"] = source_videos
    return inputs


def planner_reference_params(
    project: Mapping[str, Any],
    *,
    uploads_root: str,
    save_root: str,
    workspace: str,
) -> dict[str, Any]:
    """``director_reference_inputs`` normalised by Director's own bridge."""
    from services.director.reference_inputs import planning_reference_inputs

    return planning_reference_inputs(
        director_reference_inputs(
            project, uploads_root=uploads_root, save_root=save_root, workspace=workspace
        )
    )


# ── readiness ───────────────────────────────────────────────────────────────


def brief_readiness(
    project: Mapping[str, Any],
    *,
    uploads_root: str = "",
    save_root: str = "",
    workspace: str = "",
) -> list[dict[str, str]]:
    """What must still be answered before a long production run makes sense."""
    issues: list[dict[str, str]] = []
    brief = project.get("brief") or {}
    if not str(brief.get("logline") or brief.get("synopsis") or brief.get("goal") or "").strip():
        issues.append({
            "code": "brief_missing",
            "severity": "blocking",
            "message": "Describe the idea (logline, treatment or objective) before planning.",
        })
    if not str(brief.get("visual_style") or brief.get("genre") or "").strip():
        issues.append({
            "code": "style_missing",
            "severity": "warning",
            "message": "Add a genre or visual style so the look stays coherent.",
        })
    if not (project.get("assets") or {}):
        issues.append({
            "code": "no_assets",
            "severity": "warning",
            "message": "No files imported yet; the Director will invent everything.",
        })
    if uploads_root or save_root:
        missing = missing_asset_files(
            project, uploads_root=uploads_root, save_root=save_root, workspace=workspace
        )
        for entry in missing:
            issues.append({
                "code": "asset_file_missing",
                "severity": "blocking",
                "message": f"File for '{entry['name']}' is missing from the workspace; re-import it.",
            })
    unassigned = [
        asset for asset in (project.get("assets") or {}).values()
        if isinstance(asset, Mapping) and asset.get("role") == "other"
    ]
    if unassigned:
        issues.append({
            "code": "role_unassigned",
            "severity": "warning",
            "message": (
                f"{len(unassigned)} file(s) have no creative role, so the Director will ignore them."
            ),
        })
    for asset in (project.get("assets") or {}).values():
        if not isinstance(asset, Mapping):
            continue
        for conversion in asset.get("conversions") or []:
            if isinstance(conversion, Mapping) and conversion.get("status") == "suggested":
                issues.append({
                    "code": "conversion_pending",
                    "severity": "warning",
                    "message": (
                        f"'{asset.get('name')}' needs a local conversion to "
                        f"{conversion.get('target_format')} before the pipeline can read it."
                    ),
                })
    return issues


def blocking_issues(issues: Sequence[Mapping[str, str]]) -> list[Mapping[str, str]]:
    return [issue for issue in issues if issue.get("severity") == "blocking"]


def roles_present(project: Mapping[str, Any]) -> list[str]:
    roles: list[str] = []
    for asset in (project.get("assets") or {}).values():
        if isinstance(asset, Mapping):
            role = str(asset.get("role") or "")
            if role and role not in roles:
                roles.append(role)
    return sorted(roles)


def iter_assets(project: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for asset in (project.get("assets") or {}).values():
        if isinstance(asset, Mapping):
            yield asset

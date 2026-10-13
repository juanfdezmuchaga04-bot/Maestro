"""Service layer behind the Studio HTTP routes.

Every Studio operation lives here as a plain function over explicit paths, so it
can be unit tested without importing the application (which pulls in torch,
Gradio and the model registry).  The FastAPI handlers in ``launch.py`` only
translate JSON to these calls and errors back to status codes.

Anything that must touch live runtime state — the generation queue, the Editor
renderer — arrives as an injected callable instead of an import.
"""
from __future__ import annotations

import os
import shutil
import time
from typing import Any, Callable, Mapping, Optional, Sequence

from services import film_assets, film_assembly, film_brief, film_planning
from services.editor_projects import save_editor_project
from services.film_projects import (
    FilmProjectError,
    add_job,
    add_take,
    approve_take,
    create_film_project,
    delete_film_project,
    film_project_summary,
    find_asset,
    find_shot,
    list_film_projects,
    load_film_project,
    move_shot,
    normalize_brief,
    record_decision,
    remove_asset,
    remove_shot,
    save_film_project,
    update_asset,
    update_job,
    upsert_shot,
)

JobLookup = Callable[[str], Optional[Mapping[str, Any]]]


# ── where generation can happen ─────────────────────────────────────────────
# This machine is one possible answer, and not the only one: a peer Maestro on the
# private network with a GPU can render what is queued here (services.remote_engine),
# and so can a ComfyUI server reached over HTTP.  Launch registers a probe for those
# sources at startup, so a modest laptop reports "generation is possible" truthfully
# — naming the computer that will do it — instead of refusing to plan a production
# it cannot render itself.  Only a source that answered recently counts.
GenerationSource = Callable[[], Sequence[Mapping[str, Any]]]
_REMOTE_GENERATION_SOURCES: list[GenerationSource] = []


def register_generation_source(source: GenerationSource) -> GenerationSource:
    """Register a callable returning the remote places that can render now."""
    if not callable(source):
        raise TypeError("A generation source must be callable")
    if source not in _REMOTE_GENERATION_SOURCES:
        _REMOTE_GENERATION_SOURCES.append(source)
    return source


def clear_generation_sources() -> None:
    """Forget every registered source (tests, and an explicit engine reset)."""
    _REMOTE_GENERATION_SOURCES.clear()


def _online_generation_sources() -> list[dict[str, Any]]:
    """Ask each source; a broken one reports nothing instead of breaking the report."""
    found: list[dict[str, Any]] = []
    for source in list(_REMOTE_GENERATION_SOURCES):
        try:
            entries = source() or ()
        except Exception:  # noqa: BLE001 - an unavailable source is simply not evidence
            continue
        for entry in entries:
            if isinstance(entry, Mapping) and entry.get("online"):
                found.append(dict(entry))
    return found


# ── projects ────────────────────────────────────────────────────────────────


def list_projects(save_root: str, workspace: str) -> dict[str, Any]:
    return {"projects": list_film_projects(save_root, workspace)}


def create_project(
    save_root: str,
    workspace: str,
    *,
    name: str = "Untitled production",
    brief: Optional[Mapping[str, Any]] = None,
    approach: str = "hybrid",
) -> dict[str, Any]:
    project = create_film_project(name=name, workspace=workspace, brief=brief, approach=approach)
    return save_film_project(save_root, workspace, project)


def get_project(save_root: str, workspace: str, project_id: str) -> dict[str, Any]:
    return load_film_project(save_root, workspace, project_id)


def save_project(save_root: str, workspace: str, project: Mapping[str, Any]) -> dict[str, Any]:
    return save_film_project(save_root, workspace, project)


def delete_project(save_root: str, workspace: str, project_id: str) -> dict[str, Any]:
    if not delete_film_project(save_root, workspace, project_id):
        raise FileNotFoundError(project_id)
    return {"deleted": project_id}


def _load(save_root: str, workspace: str, project_id: str) -> dict[str, Any]:
    return load_film_project(save_root, workspace, project_id)


def _store(
    project: dict[str, Any],
    save_root: str,
    workspace: str,
    *,
    status: Optional[str] = None,
) -> dict[str, Any]:
    if status:
        project["status"] = status
    return save_film_project(save_root, workspace, project)


def production_readiness(
    save_root: str,
    workspace: str,
    project_id: str,
    *,
    engine_available: Optional[bool] = None,
) -> list[dict[str, str]]:
    """Everything that stands between the project and a generation run."""
    project = _load(save_root, workspace, project_id)
    if engine_available is None:
        engine_available = engine_status()["generation_possible"]
    issues = list(film_brief.brief_readiness(
        project,
        uploads_root=_uploads_root(save_root),
        save_root=save_root,
        workspace=workspace,
    ))
    issues.extend(
        film_planning.production_readiness(project, model_available=bool(engine_available))
    )
    return issues


def project_overview(save_root: str, workspace: str, project_id: str) -> dict[str, Any]:
    """Everything the production screen needs in one round trip."""
    project = _load(save_root, workspace, project_id)
    requirements = film_assets.missing_requirements()
    return {
        "project": project,
        "summary": film_project_summary(project),
        "brief": compose_brief_text(project, save_root, workspace),
        "storyboard": film_planning.storyboard(project),
        "storyboard_totals": film_planning.storyboard_totals(project),
        "soundtrack": film_assembly.soundtrack_plan(project),
        "music_sync": film_assembly.music_sync_plan(project),
        "readiness": production_readiness(save_root, workspace, project_id),
        "engine": engine_status(),
        "requirements": requirements,
        "local_tools": film_assets.local_tool_availability(),
        "asset_roles": {
            kind: film_assets.available_roles(kind)
            for kind in ("image", "video", "audio", "document", "other")
        },
    }


# ── assets ──────────────────────────────────────────────────────────────────


def _uploads_root(save_root: str, uploads_root: str = "") -> str:
    """Where uploaded files land. The app passes its real uploads directory."""
    if uploads_root:
        return uploads_root
    return os.path.join(os.path.dirname(os.path.abspath(save_root)), "uploads")


def import_assets(
    save_root: str,
    workspace: str,
    project_id: str,
    files: Sequence[Mapping[str, Any]],
    *,
    analyze: bool = False,
    deep_audio: bool = False,
    uploads_root: str = "",
) -> dict[str, Any]:
    """Register already-uploaded files against the project.

    Each entry is ``{filename, name?, role?, path?}``.  ``path`` is the absolute
    file on disk when the caller knows it; otherwise the uploads/outputs roots are
    searched.  A role supplied here is treated as a user decision and locked.
    """
    project = _load(save_root, workspace, project_id)
    imported: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for entry in files:
        filename = str((entry or {}).get("filename") or "").strip()
        if not filename:
            rejected.append({"filename": "", "reason": "Missing file name"})
            continue
        absolute = str(entry.get("path") or "").strip()
        if not absolute or not os.path.isfile(absolute):
            absolute = film_brief.resolve_asset_path(
                {"filename": filename},
                uploads_root=_uploads_root(save_root, uploads_root),
                save_root=save_root,
                workspace=workspace,
            ) or ""
        before = set(project.get("assets") or {})
        film_assets.register_imported_asset(
            project,
            filename=filename,
            name=str(entry.get("name") or ""),
            workspace=workspace,
            origin=str(entry.get("origin") or "upload"),
            analyze=analyze,
            absolute_path=absolute,
            deep_audio=deep_audio,
        )
        new_id = next(iter(set(project.get("assets") or {}) - before), None)
        requested_role = str(entry.get("role") or "").strip()
        if new_id and requested_role:
            update_asset(project, new_id, {"role": requested_role})
        if new_id:
            imported.append(project["assets"][new_id])
    record_decision(
        project,
        "assets_imported",
        f"Imported {len(imported)} file(s).",
        {"files": [asset.get("filename") for asset in imported]},
    )
    project["status"] = "brief" if project.get("status") == "draft" else project["status"]
    saved = _store(project, save_root, workspace)
    return {"project": saved, "imported": imported, "rejected": rejected}


def set_asset_role(
    save_root: str,
    workspace: str,
    project_id: str,
    asset_id: str,
    *,
    role: str,
    notes: str = "",
    reproduce_literally: Optional[bool] = None,
    display_name: str = "",
    keep_original: Optional[bool] = None,
) -> dict[str, Any]:
    """Apply the user's correction to an automatic interpretation."""
    project = _load(save_root, workspace, project_id)
    asset = find_asset(project, asset_id)
    if asset is None:
        raise FilmProjectError("Asset not found")
    patch: dict[str, Any] = {"role": role}
    if notes:
        patch["notes"] = notes
    if display_name:
        patch["display_name"] = display_name
    if reproduce_literally is not None:
        patch["reproduce_literally"] = reproduce_literally
    if keep_original is not None:
        patch["keep_original"] = keep_original
    update_asset(project, asset_id, patch)
    record_decision(
        project,
        "asset_role",
        f"'{asset.get('name')}' assigned as {role}.",
        {"asset_id": asset_id, "role": role, "reproduce_literally": bool(reproduce_literally)},
    )
    return _store(project, save_root, workspace)


def analyze_project_assets(
    save_root: str,
    workspace: str,
    project_id: str,
    *,
    asset_ids: Optional[Sequence[str]] = None,
    deep_audio: bool = False,
    uploads_root: str = "",
) -> dict[str, Any]:
    """Run the local analysis pass and record what was and was not possible."""
    project = _load(save_root, workspace, project_id)
    wanted = {str(value) for value in asset_ids} if asset_ids else None
    analyzed: list[dict[str, Any]] = []
    for asset in list((project.get("assets") or {}).values()):
        asset_id = str(asset.get("id") or "")
        if wanted is not None and asset_id not in wanted:
            continue
        absolute = film_brief.resolve_asset_path(
            {"filename": asset.get("filename")},
            uploads_root=_uploads_root(save_root, uploads_root),
            save_root=save_root,
            workspace=workspace,
        )
        if absolute is None:
            analysis = {
                "available": False,
                "status": "unavailable",
                "limitations": ["The file was not found in the workspace."],
            }
        else:
            analysis = film_assets.analyze_asset(
                absolute, kind=str(asset.get("kind") or ""), deep_audio=deep_audio,
            )
        update_asset(project, asset_id, {"analysis": analysis})
        analyzed.append({
            "asset_id": asset_id,
            "name": asset.get("name"),
            "available": bool(analysis.get("available")),
            "limitations": list(analysis.get("limitations") or []),
        })
    return {"project": _store(project, save_root, workspace), "analyzed": analyzed}


def remove_project_asset(
    save_root: str, workspace: str, project_id: str, asset_id: str
) -> dict[str, Any]:
    project = _load(save_root, workspace, project_id)
    remove_asset(project, asset_id)
    record_decision(project, "asset_removed", "A file was removed from the production.", {"asset_id": asset_id})
    return _store(project, save_root, workspace)


# ── brief and references ────────────────────────────────────────────────────


def compose_brief_text(project: Mapping[str, Any], save_root: str, workspace: str) -> str:
    return film_brief.compose_director_brief(project)


def brief_bundle(
    save_root: str,
    workspace: str,
    project_id: str,
    *,
    uploads_root: str = "",
) -> dict[str, Any]:
    """The brief text plus the Director inputs and what is still missing."""
    project = _load(save_root, workspace, project_id)
    uploads = _uploads_root(save_root, uploads_root)
    inputs = film_brief.director_reference_inputs(
        project, uploads_root=uploads, save_root=save_root, workspace=workspace,
    )
    return {
        "brief": film_brief.compose_director_brief(project),
        "reference_inputs": inputs,
        "planner_params": film_brief.planner_reference_params(
            project, uploads_root=uploads, save_root=save_root, workspace=workspace,
        ),
        "readiness": film_brief.brief_readiness(
            project, uploads_root=uploads, save_root=save_root, workspace=workspace,
        ),
        "roles_present": film_brief.roles_present(project),
    }


def update_brief(
    save_root: str,
    workspace: str,
    project_id: str,
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    project = _load(save_root, workspace, project_id)
    merged = dict(project.get("brief") or {})
    merged.update(dict(patch or {}))
    project["brief"] = normalize_brief(merged)
    if patch and patch.get("approach"):
        project["approach"] = str(patch["approach"])
    record_decision(project, "brief", "Creative brief updated.")
    return _store(project, save_root, workspace)


# ── plan / storyboard ───────────────────────────────────────────────────────


def import_plan(
    save_root: str,
    workspace: str,
    project_id: str,
    response: Mapping[str, Any],
    *,
    imported_from: str = "",
) -> dict[str, Any]:
    project = _load(save_root, workspace, project_id)
    report = film_planning.ingest_director_plan(
        project, response, imported_from=imported_from,
    )
    saved = _store(project, save_root, workspace)
    return {
        "project": saved,
        "report": report,
        "storyboard": film_planning.storyboard(saved),
        "storyboard_totals": film_planning.storyboard_totals(saved),
    }


def assign_references(
    save_root: str,
    workspace: str,
    project_id: str,
    *,
    asset_ids: Sequence[str],
    shot_ids: Sequence[str] = (),
    scene_ids: Sequence[str] = (),
    all_shots: bool = False,
    as_start_image: bool = False,
    replace: bool = False,
) -> dict[str, Any]:
    """The user's explicit "use this image for this scene/shot" decision."""
    project = _load(save_root, workspace, project_id)
    report = film_planning.assign_references(
        project,
        asset_ids=asset_ids,
        shot_ids=shot_ids,
        scene_ids=scene_ids,
        all_shots=all_shots,
        as_start_image=as_start_image,
        replace=replace,
    )
    saved = _store(project, save_root, workspace)
    return {"project": saved, "report": report, "storyboard": film_planning.storyboard(saved)}


def apply_continuity(
    save_root: str,
    workspace: str,
    project_id: str,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """Propagate the character bible into every shot that features those people."""
    project = _load(save_root, workspace, project_id)
    report = film_planning.apply_visual_continuity(project, replace=replace)
    saved = _store(project, save_root, workspace)
    return {"project": saved, "report": report, "storyboard": film_planning.storyboard(saved)}


def storyboard_view(
    save_root: str,
    workspace: str,
    project_id: str,
    *,
    only_missing: bool = True,
    shot_ids: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    project = _load(save_root, workspace, project_id)
    return {
        "storyboard": film_planning.storyboard(project),
        "totals": film_planning.storyboard_totals(project),
        "requests": film_planning.production_requests(
            project, only_missing=only_missing, shot_ids=shot_ids,
        ),
        "readiness": production_readiness(save_root, workspace, project_id),
    }


def edit_shot(
    save_root: str,
    workspace: str,
    project_id: str,
    shot_id: str,
    patch: Mapping[str, Any],
) -> dict[str, Any]:
    project = _load(save_root, workspace, project_id)
    if find_shot(project, shot_id) is None:
        raise FilmProjectError("Shot not found")
    upsert_shot(project, {**dict(patch or {}), "id": shot_id})
    saved = _store(project, save_root, workspace)
    return {"project": saved, "storyboard": film_planning.storyboard(saved)}


def add_shot(
    save_root: str,
    workspace: str,
    project_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    project = _load(save_root, workspace, project_id)
    if not project.get("scenes"):
        from services.film_projects import upsert_scene

        upsert_scene(project, {"title": "Scene 1"})
    upsert_shot(project, payload)
    record_decision(project, "shot_added", "A shot was added to the storyboard.")
    saved = _store(project, save_root, workspace)
    return {"project": saved, "storyboard": film_planning.storyboard(saved)}


def delete_shot(save_root: str, workspace: str, project_id: str, shot_id: str) -> dict[str, Any]:
    project = _load(save_root, workspace, project_id)
    if find_shot(project, shot_id) is None:
        raise FilmProjectError("Shot not found")
    remove_shot(project, shot_id)
    saved = _store(project, save_root, workspace)
    return {"project": saved, "storyboard": film_planning.storyboard(saved)}


def reorder_shot(
    save_root: str, workspace: str, project_id: str, shot_id: str, target_index: int
) -> dict[str, Any]:
    project = _load(save_root, workspace, project_id)
    move_shot(project, shot_id, int(target_index))
    saved = _store(project, save_root, workspace)
    return {"project": saved, "storyboard": film_planning.storyboard(saved)}


def approve_shot_take(
    save_root: str,
    workspace: str,
    project_id: str,
    shot_id: str,
    take_index: int,
) -> dict[str, Any]:
    project = _load(save_root, workspace, project_id)
    approve_take(project, shot_id, int(take_index))
    index = int((find_shot(project, shot_id) or {}).get("index") or 0)
    record_decision(
        project,
        "take_approved",
        f"Take {int(take_index) + 1} approved for shot {index + 1}.",
        {"shot_id": shot_id, "take_index": int(take_index)},
    )
    saved = _store(project, save_root, workspace)
    return {"project": saved, "storyboard": film_planning.storyboard(saved)}


def register_take(
    save_root: str,
    workspace: str,
    project_id: str,
    shot_id: str,
    take: Mapping[str, Any],
) -> dict[str, Any]:
    project = _load(save_root, workspace, project_id)
    add_take(project, shot_id, take)
    saved = _store(project, save_root, workspace)
    return {"project": saved, "storyboard": film_planning.storyboard(saved)}


# ── production ──────────────────────────────────────────────────────────────


def production_plan(
    save_root: str,
    workspace: str,
    project_id: str,
    *,
    only_missing: bool = True,
    shot_ids: Optional[Sequence[str]] = None,
    model_available: bool = True,
) -> dict[str, Any]:
    """What would be submitted, and what blocks it. Nothing is executed here."""
    project = _load(save_root, workspace, project_id)
    requests = film_planning.production_requests(
        project, only_missing=only_missing, shot_ids=shot_ids,
    )
    return {
        "requests": requests,
        "runnable": [request for request in requests if not request["skip"]],
        "skipped": [request for request in requests if request["skip"]],
        "readiness": film_planning.production_readiness(project, model_available=model_available),
        "estimated_shots": len(requests),
    }


def record_production_jobs(
    save_root: str,
    workspace: str,
    project_id: str,
    jobs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Persist the queue ids returned by the real generation endpoint."""
    project = _load(save_root, workspace, project_id)
    recorded: list[dict[str, Any]] = []
    for entry in jobs:
        shot_id = str((entry or {}).get("shot_id") or "")
        job_id = str((entry or {}).get("job_id") or "")
        if not shot_id or not job_id:
            continue
        film_planning.record_production_job(
            project,
            shot_id=shot_id,
            job_id=job_id,
            kind=str(entry.get("kind") or "video"),
            params=entry.get("params") if isinstance(entry.get("params"), Mapping) else {},
        )
        recorded.append({"shot_id": shot_id, "job_id": job_id})
    if recorded:
        project["status"] = "production"
        record_decision(
            project, "production_started",
            f"{len(recorded)} shot(s) queued for generation.",
            {"jobs": recorded},
        )
    saved = _store(project, save_root, workspace)
    return {"project": saved, "recorded": recorded}


TERMINAL_JOB_STATES = ("completed", "failed", "cancelled")


def _live_output_filename(live: Mapping[str, Any]) -> str:
    files = live.get("output_files") or []
    if isinstance(files, (list, tuple)) and files:
        return os.path.basename(str(files[0]))
    return ""


def sync_production_jobs(
    save_root: str,
    workspace: str,
    project_id: str,
    job_lookup: JobLookup,
) -> dict[str, Any]:
    """Reconcile recorded queue ids with the live job table.

    Covers both shot generation jobs and the final render (an export is tracked by
    its job id so the finished MP4 filename lands on the export record).
    """
    project = _load(save_root, workspace, project_id)
    updated: list[dict[str, Any]] = []
    exports = list(project.get("exports") or [])
    for job in list(project.get("jobs") or []):
        job_id = str(job.get("id") or "")
        if not job_id or job.get("status") in TERMINAL_JOB_STATES:
            continue
        live = job_lookup(job_id)
        if not live:
            continue
        status = str(live.get("status") or "")
        if status not in TERMINAL_JOB_STATES:
            continue
        filename = _live_output_filename(live)
        error = str(live.get("error") or "")
        if str(job.get("kind")) == "export":
            for record in exports:
                if isinstance(record, dict) and str(record.get("job_id")) == job_id:
                    record["status"] = status
                    record["error"] = error
                    if filename:
                        record["filename"] = filename
                    if status == "completed" and filename:
                        project["status"] = "exported"
            update_job(project, job_id, {
                "status": status,
                "output_filename": filename,
                "error": error,
            })
            updated.append({"job_id": job_id, "status": status, "filename": filename})
            continue
        film_planning.sync_shot_from_job(
            project,
            job_id=job_id,
            status=status,
            filename=filename,
            error=error,
        )
        updated.append({"job_id": job_id, "status": status, "filename": filename})
    project["exports"] = exports
    saved = _store(project, save_root, workspace)
    return {"project": saved, "updated": updated, "storyboard": film_planning.storyboard(saved)}


# ── soundtrack ──────────────────────────────────────────────────────────────


def plan_soundtrack(save_root: str, workspace: str, project_id: str) -> dict[str, Any]:
    project = _load(save_root, workspace, project_id)
    film_assembly.apply_soundtrack_plan(project)
    saved = _store(project, save_root, workspace)
    return {
        "project": saved,
        "soundtrack": film_assembly.soundtrack_plan(saved),
        "music_sync": film_assembly.music_sync_plan(saved),
    }


def sync_cuts_to_music(
    save_root: str,
    workspace: str,
    project_id: str,
    *,
    tolerance_sec: float = 1.5,
    apply_changes: bool = False,
) -> dict[str, Any]:
    project = _load(save_root, workspace, project_id)
    result = film_assembly.snap_shots_to_music(project, tolerance_sec=tolerance_sec)
    saved = _store(project, save_root, workspace) if apply_changes else project
    return {
        "project": saved,
        "changes": result["changed"],
        "reason": result["reason"],
        "applied": bool(apply_changes),
        "music_sync": film_assembly.music_sync_plan(saved),
    }


# ── assembly and export ─────────────────────────────────────────────────────


def assembly_report(
    save_root: str,
    workspace: str,
    project_id: str,
    *,
    engine_available: bool = True,
    uploads_root: str = "",
) -> dict[str, Any]:
    project = _load(save_root, workspace, project_id)
    uploads = _uploads_root(save_root, uploads_root)
    plan = film_assembly.build_editor_project(
        project, save_root=save_root, uploads_root=uploads,
        include_subtitles=False, include_titles=False,
    )
    return {
        "report": plan["report"],
        "readiness": film_assembly.assembly_readiness(
            project,
            save_root=save_root,
            uploads_root=uploads,
            ffmpeg_available=shutil.which("ffmpeg") is not None,
            engine_available=engine_available,
        ),
        "edit_fraction": film_assembly.edit_fraction(project),
        "music_sync": film_assembly.music_sync_plan(project),
    }


def assemble_project(
    save_root: str,
    workspace: str,
    project_id: str,
    *,
    include_subtitles: bool = False,
    include_titles: bool = True,
    title_text: str = "",
    credits_text: str = "",
    only_approved: bool = False,
    width: Optional[int] = None,
    height: Optional[int] = None,
    fps: Optional[float] = None,
    uploads_root: str = "",
) -> dict[str, Any]:
    """Compile the storyboard into a saved Editor project ready to render."""
    project = _load(save_root, workspace, project_id)
    uploads = _uploads_root(save_root, uploads_root)
    settings: dict[str, Any] = {}
    if width:
        settings["width"] = int(width)
    if height:
        settings["height"] = int(height)
    if fps:
        settings["fps"] = float(fps)
    if settings:
        project.setdefault("timeline", {})["assembly_settings"] = settings

    plan = film_assembly.build_editor_project(
        project,
        save_root=save_root,
        uploads_root=uploads,
        include_subtitles=include_subtitles,
        include_titles=include_titles,
        title_text=title_text,
        credits_text=credits_text,
        only_approved=only_approved,
        fps=fps,
    )
    if not plan["report"]["shots_in_timeline"]:
        raise FilmProjectError(
            "Nothing to assemble: no shot in the storyboard has a rendered file yet."
        )
    editor_project = plan["editor_project"]
    # Editing an already-assembled production rewrites the same Editor project so
    # its id (and therefore its export history) stays stable. A blank id lets the
    # Editor's own normaliser mint one.
    editor_project["id"] = str((project.get("timeline") or {}).get("editor_project_id") or "")
    editor_project["schema_version"] = 0  # stamped by the Editor's normaliser
    saved_editor = save_editor_project(save_root, workspace, editor_project)

    project.setdefault("timeline", {})
    project["timeline"]["editor_project_id"] = saved_editor["id"]
    project["timeline"]["last_assembled_at"] = saved_editor["updated_at"]
    project["timeline"]["assembly_report"] = plan["report"]
    project["timeline"]["assembly_settings"] = settings or project["timeline"].get("assembly_settings", {})
    project["status"] = "assembly"
    record_decision(
        project,
        "assembled",
        f"Cut assembled: {plan['report']['shots_in_timeline']} shot(s), "
        f"{plan['report']['duration_sec']:.1f}s.",
        plan["report"],
    )
    saved = _store(project, save_root, workspace)
    return {
        "project": saved,
        "editor_project_id": saved_editor["id"],
        "editor_project": saved_editor,
        "report": plan["report"],
    }


def record_export(
    save_root: str,
    workspace: str,
    project_id: str,
    *,
    filename: str = "",
    job_id: str = "",
    status: str = "queued",
    settings: Optional[Mapping[str, Any]] = None,
    report: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    project = _load(save_root, workspace, project_id)
    exports = list(project.get("exports") or [])
    exports.append({
        "filename": filename,
        "created_at": time.time(),
        "editor_project_id": str((project.get("timeline") or {}).get("editor_project_id") or ""),
        "job_id": job_id,
        "status": status,
        "error": "",
        "settings": dict(settings or {}),
        "report": dict(report or {}),
    })
    project["exports"] = exports
    if filename:
        project["status"] = "exported"
    if job_id:
        # Track the render in the job list so sync_production_jobs can attach the
        # finished MP4 (or the failure) to this export later.
        add_job(project, {
            "id": job_id,
            "shot_id": "",
            "kind": "export",
            "status": "held" if status == "held" else "queued",
            "params": dict(settings or {}),
        })
    record_decision(
        project,
        "export",
        f"Export {'recorded' if filename else 'queued'}: {filename or job_id or 'pending'}.",
        {"filename": filename, "job_id": job_id, "status": status},
    )
    return _store(project, save_root, workspace)


def engine_status() -> dict[str, Any]:
    """Honest report of what can generate, here or on a registered remote engine.

    ``generation_possible`` answers the question the production screens ask — *can
    a render be submitted from this computer and actually run* — and it is true
    when either this machine has a CUDA GPU or a peer Maestro that answered a
    recent probe can render.  The Studio and Films flows delegate to that peer
    automatically (``services.engine_routing``), so a computer without a GPU is
    no longer a computer that cannot produce; it is one that produces elsewhere.
    ``generation_possible_here`` keeps the narrower, local-only answer for the
    screens that are about *this* engine — the local engine panel and the
    capability registry — and ``generation_runs_on`` names, in words, the
    computer the work will actually run on.
    """
    tools = film_assets.local_tool_availability()
    gpu = False
    error = ""
    try:
        import torch  # noqa: PLC0415 - optional runtime dependency

        gpu = bool(torch.cuda.is_available())
    except Exception as exc:  # pragma: no cover - depends on the install
        error = str(exc)[:200]
    remote = _online_generation_sources()
    runs_on = "this computer"
    if not gpu:
        runs_on = ""
        if remote:
            first = remote[0]
            runs_on = str(first.get("label") or first.get("peer_id") or "a remote computer")
    return {
        "gpu": gpu,
        "ffmpeg": bool(tools.get("ffmpeg")),
        "ffprobe": bool(tools.get("ffprobe")),
        "audio_analysis": bool(tools.get("librosa")),
        "image_analysis": bool(tools.get("pillow")),
        "pdf_text": bool(tools.get("pypdf")),
        "detail": error,
        "generation_possible": bool(gpu) or bool(remote),
        "generation_possible_here": bool(gpu),
        "generation_runs_on": runs_on,
        "remote_generation_available": bool(remote),
        "remote_sources": remote,
    }

"""Studio service layer: import, brief, plan, queue sync, assembly, export."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import film_api, film_assets
from services.film_projects import FilmProjectError

DIRECTOR_RESPONSE = {
    "skill_type": "short_film",
    "clip_plans": [
        {"video_prompt": "Wide avenue in the rain.", "image_prompt": "wide"},
        {"video_prompt": "Close on her face.", "image_prompt": "close"},
    ],
    "production_plan": {
        "skill_type": "short_film",
        "title": "Rain City",
        "global_style": "neo-noir",
        "characters": [{"id": "mara", "display_name": "Mara", "physical_description": "short hair"}],
        "continuity_notes": ["Mara keeps the grey coat"],
        "shots": [
            {
                "shot_id": "s1", "index": 0, "duration_sec": 8.0, "scene_goal": "Establish",
                "scene_type": "establishing", "subjects_on_screen": [{"name": "Mara"}],
                "spatial_setup": "wide avenue", "environment": "night rain",
                "visual_style": "neon", "lighting": "practical", "mood": "melancholic",
                "action_beats": ["walks"], "dialogue_beats": [],
                "camera_plan": {"framing": "wide shot", "movement": "dolly in"},
                "audio_plan": {"mode": "music_driven"}, "ending_beat": "stops",
                "metadata": {},
            },
            {
                "shot_id": "s2", "index": 1, "duration_sec": 4.0, "scene_goal": "Decision",
                "scene_type": "establishing", "subjects_on_screen": [{"name": "Mara"}],
                "spatial_setup": "close-up", "environment": "wet neon",
                "visual_style": "shallow", "lighting": "soft", "mood": "resolute",
                "action_beats": ["looks up"],
                "dialogue_beats": [{"speaker": "Mara", "line": "No more waiting."}],
                "camera_plan": {"framing": "close-up", "movement": "static"},
                "audio_plan": {"mode": "dialogue_driven"}, "ending_beat": "black",
                "metadata": {},
            },
        ],
    },
}


class StudioApiTestBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.save_root = self.root / "outputs"
        self.save_root.mkdir()
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.project = film_api.create_project(
            str(self.save_root), "default", name="Rain City",
            brief={"logline": "A courier crosses a flooded city.", "genre": "neo-noir"},
        )
        self.project_id = self.project["id"]

    def add_file(self, name: str, role: str = "", content: bytes = b"data") -> dict:
        (self.uploads / name).write_bytes(content)
        result = film_api.import_assets(
            str(self.save_root), "default", self.project_id,
            [{"filename": name, "role": role}],
            uploads_root=str(self.uploads),
        )
        return result["imported"][0]

    def render_shot(self, index: int, filename: str, duration: float = 4.0) -> str:
        (self.save_root / filename).write_bytes(b"\x00\x00\x00\x18ftypmp42")
        self.reload()
        shot = self.project["shots"][index]
        film_api.edit_shot(
            str(self.save_root), "default", self.project_id, shot["id"],
            {"duration_sec": duration},
        )
        film_api.register_take(
            str(self.save_root), "default", self.project_id, shot["id"],
            {"filename": filename, "job_id": f"job-{index}"},
        )
        self.project = film_api.get_project(str(self.save_root), "default", self.project_id)
        return shot["id"]

    def reload(self):
        self.project = film_api.get_project(str(self.save_root), "default", self.project_id)
        return self.project


class ProjectLifecycleTest(StudioApiTestBase):
    def test_projects_persist_and_reopen(self):
        listed = film_api.list_projects(str(self.save_root), "default")["projects"]
        self.assertEqual([entry["id"] for entry in listed], [self.project_id])
        reopened = film_api.get_project(str(self.save_root), "default", self.project_id)
        self.assertEqual(reopened["brief"]["genre"], "neo-noir")
        self.assertEqual(reopened["name"], "Rain City")

    def test_overview_reports_real_readiness_and_requirements(self):
        with mock.patch.object(film_assets.shutil, "which", return_value=None), \
                mock.patch.object(film_assets, "_module_available", return_value=False):
            overview = film_api.project_overview(str(self.save_root), "default", self.project_id)
        codes = [issue["code"] for issue in overview["readiness"]]
        self.assertIn("no_assets", codes)
        self.assertTrue(overview["requirements"])
        self.assertIn("image", overview["asset_roles"])
        self.assertTrue(overview["asset_roles"]["image"])

    def test_delete_removes_the_project(self):
        film_api.delete_project(str(self.save_root), "default", self.project_id)
        self.assertEqual(film_api.list_projects(str(self.save_root), "default")["projects"], [])
        with self.assertRaises(FileNotFoundError):
            film_api.delete_project(str(self.save_root), "default", self.project_id)


class AssetImportTest(StudioApiTestBase):
    def test_import_classifies_and_records_the_file(self):
        asset = self.add_file("mi_banda_sonora.wav")
        self.assertEqual(asset["kind"], "audio")
        self.assertEqual(asset["role"], "soundtrack")
        self.assertLess(asset["role_confidence"], 1.0)
        self.assertTrue(asset["filename"])

    def test_an_explicit_role_is_locked_as_a_user_decision(self):
        asset = self.add_file("clip.wav", role="dialogue_audio")
        self.assertEqual(asset["role"], "dialogue_audio")
        self.assertTrue(asset["role_locked"])
        self.assertEqual(asset["role_confidence"], 1.0)

    def test_import_records_the_brief_status_transition(self):
        self.assertEqual(self.project["status"], "draft")
        self.add_file("hero.png", role="character_reference")
        self.assertEqual(self.reload()["status"], "brief")

    def test_missing_filenames_are_rejected_with_a_reason(self):
        result = film_api.import_assets(
            str(self.save_root), "default", self.project_id, [{"filename": ""}],
        )
        self.assertEqual(result["imported"], [])
        self.assertEqual(result["rejected"][0]["reason"], "Missing file name")

    def test_analysis_can_be_deferred_then_run(self):
        asset = self.add_file("clip.mp4", role="source_video_edit")
        self.assertEqual(asset["analysis"]["status"], "unknown")
        with mock.patch.object(film_assets, "analyze_asset", return_value={
            "available": False, "status": "unavailable",
            "limitations": ["ffprobe is not installed"],
        }):
            result = film_api.analyze_project_assets(
                str(self.save_root), "default", self.project_id, uploads_root=str(self.uploads),
            )
        self.assertEqual(len(result["analyzed"]), 1)
        self.assertFalse(result["analyzed"][0]["available"])
        stored = self.reload()["assets"][asset["id"]]
        self.assertIn("ffprobe", stored["analysis"]["limitations"][0])

    def test_analysis_reports_a_file_that_vanished(self):
        asset = self.add_file("gone.png", role="character_reference")
        (self.uploads / "gone.png").unlink()
        result = film_api.analyze_project_assets(
            str(self.save_root), "default", self.project_id, uploads_root=str(self.uploads),
        )
        self.assertFalse(result["analyzed"][0]["available"])
        self.assertIn("not found", result["analyzed"][0]["limitations"][0])
        self.assertTrue(asset["id"])

    def test_user_correction_overrides_the_automatic_role(self):
        asset = self.add_file("misterio.png")
        updated = film_api.set_asset_role(
            str(self.save_root), "default", self.project_id, asset["id"],
            role="location_reference", reproduce_literally=False,
            notes="inspiration only, do not copy the framing",
        )
        stored = updated["assets"][asset["id"]]
        self.assertEqual(stored["role"], "location_reference")
        self.assertTrue(stored["role_locked"])
        self.assertFalse(stored["reproduce_literally"])
        self.assertIn("inspiration only", stored["notes"])
        self.assertEqual(updated["decisions"][-1]["kind"], "asset_role")

    def test_removing_a_file_unlinks_it_everywhere(self):
        asset = self.add_file("hero.png", role="character_reference")
        film_api.remove_project_asset(str(self.save_root), "default", self.project_id, asset["id"])
        self.assertEqual(self.reload()["assets"], {})


class BriefBundleTest(StudioApiTestBase):
    def test_bundle_contains_text_inputs_and_pending_requirements(self):
        self.add_file("hero.png", role="character_reference")
        bundle = film_api.brief_bundle(
            str(self.save_root), "default", self.project_id, uploads_root=str(self.uploads),
        )
        self.assertIn("A courier crosses a flooded city.", bundle["brief"])
        self.assertTrue(bundle["reference_inputs"]["reference_image_path"].endswith("hero.png"))
        self.assertEqual(bundle["roles_present"], ["character_reference"])
        self.assertEqual(
            [issue for issue in bundle["readiness"] if issue["severity"] == "blocking"], [],
        )

    def test_missing_file_is_reported_as_blocking(self):
        self.add_file("hero.png", role="character_reference")
        (self.uploads / "hero.png").unlink()
        bundle = film_api.brief_bundle(
            str(self.save_root), "default", self.project_id, uploads_root=str(self.uploads),
        )
        self.assertIn("asset_file_missing", [issue["code"] for issue in bundle["readiness"]])

    def test_brief_updates_are_validated_and_stored(self):
        updated = film_api.update_brief(
            str(self.save_root), "default", self.project_id,
            {"tone": "melancholic", "aspect_ratio": "2.39:1", "must_keep": ["the coat"]},
        )
        self.assertEqual(updated["brief"]["tone"], "melancholic")
        self.assertEqual(updated["brief"]["aspect_ratio"], "2.39:1")
        self.assertEqual(updated["brief"]["must_keep"], ["the coat"])

    def test_invalid_choice_falls_back_instead_of_crashing(self):
        updated = film_api.update_brief(
            str(self.save_root), "default", self.project_id, {"aspect_ratio": "banana"},
        )
        self.assertEqual(updated["brief"]["aspect_ratio"], "16:9")


class PlanAndStoryboardApiTest(StudioApiTestBase):
    def setUp(self):
        super().setUp()
        self.add_file("mara.png", role="character_reference")
        self.reload()

    def test_plan_import_creates_the_storyboard_and_bible(self):
        result = film_api.import_plan(
            str(self.save_root), "default", self.project_id, DIRECTOR_RESPONSE,
            imported_from="short_film",
        )
        self.assertEqual(result["report"]["shots"], 2)
        self.assertEqual(len(result["storyboard"]), 2)
        self.assertEqual(result["project"]["status"], "planned")
        self.assertEqual(result["project"]["bible"]["characters"][0]["name"], "Mara")
        self.assertEqual(result["report"]["characters_without_reference"], [])
        self.assertTrue(result["storyboard"][0]["video_prompt"])

    def test_storyboard_view_returns_requests_and_readiness(self):
        film_api.import_plan(str(self.save_root), "default", self.project_id, DIRECTOR_RESPONSE)
        view = film_api.storyboard_view(str(self.save_root), "default", self.project_id)
        self.assertEqual(len(view["requests"]), 2)
        self.assertEqual(view["totals"]["shot_count"], 2)
        # On a machine without a CUDA GPU the only blocking issue is the honest
        # engine report; nothing about the plan itself may block.
        blocking = [
            issue["code"] for issue in view["readiness"] if issue["severity"] == "blocking"
        ]
        self.assertTrue(set(blocking) <= {"engine_unavailable"}, blocking)

    def test_shots_can_be_edited_added_reordered_and_deleted(self):
        film_api.import_plan(str(self.save_root), "default", self.project_id, DIRECTOR_RESPONSE)
        shots = film_api.storyboard_view(str(self.save_root), "default", self.project_id)["storyboard"]
        target = shots[1]["id"]

        edited = film_api.edit_shot(
            str(self.save_root), "default", self.project_id, target,
            {"duration_sec": 12.0, "camera_movement": "handheld", "video_prompt": "new prompt"},
        )
        updated = next(entry for entry in edited["storyboard"] if entry["id"] == target)
        self.assertEqual(updated["duration_sec"], 12.0)
        self.assertEqual(updated["camera_movement"], "handheld")
        self.assertEqual(updated["video_prompt"], "new prompt")

        moved = film_api.reorder_shot(
            str(self.save_root), "default", self.project_id, target, 0,
        )
        self.assertEqual(moved["storyboard"][0]["id"], target)

        added = film_api.add_shot(
            str(self.save_root), "default", self.project_id, {"objective": "Extra insert"},
        )
        self.assertEqual(added["storyboard"][-1]["objective"], "Extra insert")

        removed = film_api.delete_shot(
            str(self.save_root), "default", self.project_id, added["storyboard"][-1]["id"],
        )
        self.assertEqual(len(removed["storyboard"]), 2)

    def test_editing_an_unknown_shot_is_refused(self):
        with self.assertRaises(FilmProjectError):
            film_api.edit_shot(str(self.save_root), "default", self.project_id, "nope", {"objective": "x"})

    def test_approving_a_take_marks_the_shot_approved(self):
        film_api.import_plan(str(self.save_root), "default", self.project_id, DIRECTOR_RESPONSE)
        self.render_shot(0, "shot_a.mp4")
        shot_id = self.project["shots"][0]["id"]
        result = film_api.approve_shot_take(
            str(self.save_root), "default", self.project_id, shot_id, 0,
        )
        entry = next(item for item in result["storyboard"] if item["id"] == shot_id)
        self.assertTrue(entry["approved"])
        self.assertEqual(result["project"]["decisions"][-1]["kind"], "take_approved")


class ReferenceAndContinuityApiTest(StudioApiTestBase):
    def setUp(self):
        super().setUp()
        self.add_file("mara.png", role="character_reference")
        film_api.import_plan(str(self.save_root), "default", self.project_id, DIRECTOR_RESPONSE)
        self.reload()
        self.asset_id = next(iter(self.project["assets"]))

    def test_references_can_be_assigned_to_a_scene(self):
        scene_id = self.project["shots"][0]["scene_id"]
        result = film_api.assign_references(
            str(self.save_root), "default", self.project_id,
            asset_ids=[self.asset_id], scene_ids=[scene_id],
        )
        self.assertGreaterEqual(result["report"]["shots"], 1)
        stored = self.reload()
        in_scene = [shot for shot in stored["shots"] if shot["scene_id"] == scene_id]
        for shot in in_scene:
            self.assertIn(self.asset_id, shot["reference_asset_ids"])

    def test_a_reference_can_be_made_the_start_frame_of_one_shot(self):
        shot_id = self.project["shots"][0]["id"]
        result = film_api.assign_references(
            str(self.save_root), "default", self.project_id,
            asset_ids=[self.asset_id], shot_ids=[shot_id], as_start_image=True,
        )
        self.assertTrue(result["report"]["as_start_image"])
        stored = self.reload()
        shot = next(shot for shot in stored["shots"] if shot["id"] == shot_id)
        self.assertEqual(shot["start_image_asset_id"], self.asset_id)
        self.assertEqual(stored["decisions"][-1]["kind"], "references_assigned")

    def test_assigning_audio_as_a_visual_reference_is_refused(self):
        track = self.add_file("tema.wav", role="source_music_keep")
        with self.assertRaises(FilmProjectError):
            film_api.assign_references(
                str(self.save_root), "default", self.project_id,
                asset_ids=[track["id"]], all_shots=True,
            )

    def test_continuity_carries_the_bible_into_the_shots(self):
        result = film_api.apply_continuity(str(self.save_root), "default", self.project_id)
        self.assertIn("Mara", result["report"]["characters_matched"])
        stored = self.reload()
        featuring = [
            shot for shot in stored["shots"]
            if "Mara" in " ".join(str(value) for value in shot["elements"])
        ]
        self.assertTrue(featuring)
        for shot in featuring:
            self.assertIn(self.asset_id, shot["reference_asset_ids"])

    def test_continuity_without_a_character_photo_says_so(self):
        film_api.remove_project_asset(str(self.save_root), "default", self.project_id, self.asset_id)
        result = film_api.apply_continuity(str(self.save_root), "default", self.project_id)
        self.assertEqual(result["report"]["characters_without_reference"], ["Mara"])
        for shot in self.reload()["shots"]:
            self.assertEqual(shot["reference_asset_ids"], [])


class ProductionQueueTest(StudioApiTestBase):
    def setUp(self):
        super().setUp()
        film_api.import_plan(str(self.save_root), "default", self.project_id, DIRECTOR_RESPONSE)
        self.reload()

    def test_production_plan_says_what_would_run(self):
        plan = film_api.production_plan(str(self.save_root), "default", self.project_id)
        self.assertEqual(len(plan["runnable"]), 2)
        self.assertEqual(plan["skipped"], [])
        self.assertTrue(plan["runnable"][0]["prompt"])

    def test_rendered_shots_are_not_resubmitted(self):
        self.render_shot(0, "shot_a.mp4")
        plan = film_api.production_plan(str(self.save_root), "default", self.project_id)
        self.assertEqual(len(plan["runnable"]), 1)
        self.assertTrue(plan["skipped"][0]["already_rendered"])

    def test_queue_ids_are_persisted_against_their_shots(self):
        shot_id = self.project["shots"][0]["id"]
        result = film_api.record_production_jobs(
            str(self.save_root), "default", self.project_id,
            [{"shot_id": shot_id, "job_id": "job-99", "kind": "video"}],
        )
        self.assertEqual(result["recorded"], [{"shot_id": shot_id, "job_id": "job-99"}])
        self.assertEqual(result["project"]["status"], "production")
        stored = self.reload()["jobs"][0]
        self.assertEqual(stored["id"], "job-99")
        self.assertEqual(stored["status"], "queued")

    def test_sync_marks_completed_jobs_and_keeps_the_rendered_file(self):
        shot_id = self.project["shots"][0]["id"]
        film_api.record_production_jobs(
            str(self.save_root), "default", self.project_id,
            [{"shot_id": shot_id, "job_id": "job-99"}],
        )
        lookup = lambda job_id: {
            "status": "completed", "output_files": ["/outputs/default/rain_0001.mp4"],
        } if job_id == "job-99" else None
        result = film_api.sync_production_jobs(
            str(self.save_root), "default", self.project_id, lookup,
        )
        self.assertEqual(result["updated"][0]["filename"], "rain_0001.mp4")
        entry = next(item for item in result["storyboard"] if item["id"] == shot_id)
        self.assertEqual(entry["output_filename"], "rain_0001.mp4")
        self.assertEqual(entry["take_count"], 1)

    def test_a_failed_shot_is_offered_again_for_production(self):
        shot_id = self.project["shots"][0]["id"]
        film_api.record_production_jobs(
            str(self.save_root), "default", self.project_id,
            [{"shot_id": shot_id, "job_id": "job-oom"}],
        )
        film_api.sync_production_jobs(
            str(self.save_root), "default", self.project_id,
            lambda job_id: {"status": "failed", "error": "CUDA out of memory"},
        )
        plan = film_api.production_plan(str(self.save_root), "default", self.project_id)
        retryable = [request for request in plan["runnable"] if request["shot_id"] == shot_id]
        self.assertEqual(len(retryable), 1)
        self.assertFalse(retryable[0]["already_rendered"])

    def test_a_cancelled_shot_becomes_runnable_again(self):
        shot_id = self.project["shots"][0]["id"]
        film_api.record_production_jobs(
            str(self.save_root), "default", self.project_id,
            [{"shot_id": shot_id, "job_id": "job-stop"}],
        )
        synced = film_api.sync_production_jobs(
            str(self.save_root), "default", self.project_id,
            lambda job_id: {"status": "cancelled"},
        )
        shot = next(shot for shot in synced["project"]["shots"] if shot["id"] == shot_id)
        self.assertEqual(shot["status"], "planned")
        self.assertEqual(shot["takes"], [])
        plan = film_api.production_plan(str(self.save_root), "default", self.project_id)
        self.assertIn(shot_id, [request["shot_id"] for request in plan["runnable"]])

    def test_sync_records_failures_without_losing_the_shot(self):
        shot_id = self.project["shots"][1]["id"]
        film_api.record_production_jobs(
            str(self.save_root), "default", self.project_id,
            [{"shot_id": shot_id, "job_id": "job-bad"}],
        )
        lookup = lambda job_id: {"status": "failed", "error": "CUDA out of memory"}
        result = film_api.sync_production_jobs(
            str(self.save_root), "default", self.project_id, lookup,
        )
        self.assertEqual(result["updated"][0]["status"], "failed")
        shot = next(shot for shot in result["project"]["shots"] if shot["id"] == shot_id)
        self.assertEqual(shot["status"], "failed")
        self.assertEqual(shot["takes"][0]["error"], "CUDA out of memory")
        self.assertTrue(shot["video_prompt"])

    def test_sync_ignores_jobs_that_are_still_running(self):
        shot_id = self.project["shots"][0]["id"]
        film_api.record_production_jobs(
            str(self.save_root), "default", self.project_id,
            [{"shot_id": shot_id, "job_id": "job-run"}],
        )
        result = film_api.sync_production_jobs(
            str(self.save_root), "default", self.project_id,
            lambda job_id: {"status": "running", "progress": 40},
        )
        self.assertEqual(result["updated"], [])
        self.assertEqual(result["project"]["jobs"][0]["status"], "queued")

    def test_sync_ignores_unknown_jobs(self):
        shot_id = self.project["shots"][0]["id"]
        film_api.record_production_jobs(
            str(self.save_root), "default", self.project_id,
            [{"shot_id": shot_id, "job_id": "job-gone"}],
        )
        result = film_api.sync_production_jobs(
            str(self.save_root), "default", self.project_id, lambda job_id: None,
        )
        self.assertEqual(result["updated"], [])


class SoundtrackApiTest(StudioApiTestBase):
    def test_plan_creates_lanes_and_reports_sync(self):
        self.add_file("tema.wav", role="source_music_keep")
        result = film_api.plan_soundtrack(str(self.save_root), "default", self.project_id)
        self.assertEqual(len(result["soundtrack"]["tracks"]), 1)
        self.assertFalse(result["music_sync"]["available"])
        self.assertTrue(result["music_sync"]["reason"])

    def test_sync_preview_does_not_move_anything_unless_applied(self):
        film_api.import_plan(str(self.save_root), "default", self.project_id, DIRECTOR_RESPONSE)
        self.add_file("tema.wav", role="source_music_keep")
        project = self.reload()
        asset_id = next(iter(project["assets"]))
        project["assets"][asset_id]["analysis"] = {
            "available": True, "status": "ok", "limitations": [], "duration": 30.0,
            "bpm": 120.0, "beats": [7.5, 12.5], "sections": [],
        }
        film_api.save_project(str(self.save_root), "default", project)

        preview = film_api.sync_cuts_to_music(
            str(self.save_root), "default", self.project_id, tolerance_sec=1.0,
        )
        self.assertFalse(preview["applied"])
        self.assertEqual(self.reload()["shots"][0]["duration_sec"], 8.0)

        applied = film_api.sync_cuts_to_music(
            str(self.save_root), "default", self.project_id, tolerance_sec=1.0, apply_changes=True,
        )
        self.assertTrue(applied["applied"])
        self.assertAlmostEqual(self.reload()["shots"][0]["duration_sec"], 7.5)


class AssemblyApiTest(StudioApiTestBase):
    def setUp(self):
        super().setUp()
        film_api.import_plan(str(self.save_root), "default", self.project_id, DIRECTOR_RESPONSE)
        self.reload()
        self.render_shot(0, "shot_a.mp4", duration=8.0)
        self.render_shot(1, "shot_b.mp4", duration=4.0)
        self.reload()

    def test_report_warns_about_a_render_whose_file_vanished(self):
        (self.save_root / "shot_b.mp4").unlink()
        report = film_api.assembly_report(str(self.save_root), "default", self.project_id)
        codes = [issue["code"] for issue in report["readiness"]]
        # The take is still recorded, so this is not "unrendered" — it is a
        # missing file, and it must be reported as exactly that.
        self.assertIn("missing_media", codes)
        self.assertNotIn("no_renders", codes)
        self.assertEqual(report["report"]["shots_in_timeline"], 1)

    def test_assembly_writes_a_real_editor_project(self):
        result = film_api.assemble_project(str(self.save_root), "default", self.project_id)
        editor_id = result["editor_project_id"]
        editor_path = self.save_root / ".maestro_editor" / f"{editor_id}.json"
        self.assertTrue(editor_path.is_file())
        stored = json.loads(editor_path.read_text(encoding="utf-8"))
        video_track = next(track for track in stored["tracks"] if track["type"] == "video")
        self.assertEqual(len(video_track["items"]), 2)
        self.assertAlmostEqual(video_track["items"][1]["start"], 8.0)
        self.assertEqual(result["project"]["status"], "assembly")
        self.assertEqual(result["project"]["timeline"]["editor_project_id"], editor_id)

    def test_reassembling_keeps_the_same_editor_project(self):
        first = film_api.assemble_project(str(self.save_root), "default", self.project_id)
        second = film_api.assemble_project(str(self.save_root), "default", self.project_id)
        self.assertEqual(first["editor_project_id"], second["editor_project_id"])
        editor_files = list((self.save_root / ".maestro_editor").glob("*.json"))
        self.assertEqual(len(editor_files), 1)

    def test_assembly_without_renders_is_refused(self):
        empty = film_api.create_project(str(self.save_root), "default", name="Empty")
        film_api.import_plan(str(self.save_root), "default", empty["id"], DIRECTOR_RESPONSE)
        with self.assertRaises(FilmProjectError):
            film_api.assemble_project(str(self.save_root), "default", empty["id"])

    def test_assembly_settings_are_remembered(self):
        result = film_api.assemble_project(
            str(self.save_root), "default", self.project_id, width=1280, height=720, fps=24,
        )
        self.assertEqual(result["editor_project"]["canvas"]["width"], 1280)
        self.assertEqual(result["project"]["timeline"]["assembly_settings"]["fps"], 24.0)

    def test_titles_and_subtitles_can_be_requested(self):
        result = film_api.assemble_project(
            str(self.save_root), "default", self.project_id,
            include_subtitles=True, credits_text="Hecho con Maestro",
        )
        self.assertTrue(result["report"]["has_subtitles"])
        titles = next(t for t in result["editor_project"]["tracks"] if t["id"] == "titles-main")
        self.assertIn("credit-card", [item["id"] for item in titles["items"]])

    def test_export_records_are_kept_and_move_the_status(self):
        film_api.assemble_project(str(self.save_root), "default", self.project_id)
        saved = film_api.record_export(
            str(self.save_root), "default", self.project_id,
            filename="rain_city_final.mp4", job_id="job-export",
            settings={"codec": "h264"},
        )
        self.assertEqual(saved["status"], "exported")
        self.assertEqual(saved["exports"][0]["filename"], "rain_city_final.mp4")
        self.assertEqual(saved["exports"][0]["editor_project_id"], saved["timeline"]["editor_project_id"])

    def test_a_queued_export_is_tracked_until_the_render_finishes(self):
        film_api.assemble_project(str(self.save_root), "default", self.project_id)
        queued = film_api.record_export(
            str(self.save_root), "default", self.project_id,
            job_id="job-render", status="queued",
        )
        export = queued["exports"][0]
        self.assertEqual(export["status"], "queued")
        self.assertEqual(export["filename"], "")
        self.assertEqual([job["id"] for job in queued["jobs"]], ["job-render"])

        synced = film_api.sync_production_jobs(
            str(self.save_root), "default", self.project_id,
            lambda job_id: {"status": "completed", "output_files": ["/outputs/default/rain_city_cut.mp4"]}
            if job_id == "job-render" else None,
        )
        export = synced["project"]["exports"][0]
        self.assertEqual(export["filename"], "rain_city_cut.mp4")
        self.assertEqual(export["status"], "completed")
        self.assertEqual(synced["project"]["status"], "exported")
        self.assertEqual(synced["updated"], [{"job_id": "job-render", "status": "completed", "filename": "rain_city_cut.mp4"}])

    def test_a_failed_render_is_recorded_on_the_export(self):
        film_api.assemble_project(str(self.save_root), "default", self.project_id)
        film_api.record_export(
            str(self.save_root), "default", self.project_id,
            job_id="job-bad-export", status="queued",
        )
        synced = film_api.sync_production_jobs(
            str(self.save_root), "default", self.project_id,
            lambda job_id: {"status": "failed", "error": "ffmpeg exited 1"},
        )
        export = synced["project"]["exports"][0]
        self.assertEqual(export["status"], "failed")
        self.assertEqual(export["error"], "ffmpeg exited 1")
        self.assertEqual(export["filename"], "")
        self.assertNotEqual(synced["project"]["status"], "exported")

    def test_a_cancelled_render_does_not_mark_the_production_exported(self):
        film_api.assemble_project(str(self.save_root), "default", self.project_id)
        film_api.record_export(
            str(self.save_root), "default", self.project_id,
            job_id="job-stop", status="queued",
        )
        synced = film_api.sync_production_jobs(
            str(self.save_root), "default", self.project_id,
            lambda job_id: {"status": "cancelled"},
        )
        self.assertEqual(synced["project"]["exports"][0]["status"], "cancelled")
        self.assertEqual(synced["project"]["status"], "assembly")

    def test_export_tracking_survives_a_reopen(self):
        film_api.assemble_project(str(self.save_root), "default", self.project_id)
        film_api.record_export(
            str(self.save_root), "default", self.project_id, job_id="job-reopen", status="queued",
        )
        reopened = film_api.get_project(str(self.save_root), "default", self.project_id)
        self.assertEqual(reopened["exports"][0]["job_id"], "job-reopen")
        self.assertEqual(reopened["exports"][0]["status"], "queued")
        self.assertEqual(reopened["jobs"][0]["kind"], "export")

    def test_engine_status_reports_capabilities_without_pretending(self):
        status = film_api.engine_status()
        for key in ("gpu", "ffmpeg", "ffprobe", "generation_possible"):
            self.assertIsInstance(status[key], bool)
        self.assertEqual(status["generation_possible"], status["gpu"])

    def test_edit_progress_is_reported_fractionally(self):
        self.assertEqual(
            film_api.assembly_report(str(self.save_root), "default", self.project_id)["edit_fraction"],
            1.0,
        )


if __name__ == "__main__":
    unittest.main()

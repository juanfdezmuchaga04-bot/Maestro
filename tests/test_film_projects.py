"""Film production project persistence: round-trip, roles, storyboard, recovery."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services.film_projects import (
    FilmProjectError,
    add_job,
    add_take,
    approve_take,
    create_film_project,
    delete_film_project,
    film_project_duration,
    film_project_summary,
    list_film_projects,
    load_film_project,
    move_shot,
    normalize_film_project,
    record_decision,
    remove_asset,
    remove_shot,
    save_film_project,
    update_asset,
    upsert_asset,
    upsert_scene,
    upsert_shot,
)


class FilmProjectStoreTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = temp.name

    def test_new_project_is_a_complete_production_document(self):
        project = create_film_project(name="Rain City", workspace="default")
        self.assertEqual(project["status"], "draft")
        self.assertEqual(project["approach"], "hybrid")
        for key in (
            "brief", "assets", "bible", "scenes", "shots", "soundtrack",
            "timeline", "plan", "jobs", "decisions", "exports",
        ):
            self.assertIn(key, project)
        self.assertEqual(project["brief"]["aspect_ratio"], "16:9")

    def test_project_survives_save_and_reopen(self):
        project = create_film_project(name="Rain City")
        project["brief"]["logline"] = "A courier walks a flooded city at night."
        project["brief"]["genre"] = "neo-noir"
        upsert_scene(project, {"title": "Rain", "objective": "Establish the city"})
        upsert_shot(project, {
            "objective": "Establish the city",
            "duration_sec": 6.5,
            "shot_size": "wide",
            "camera_movement": "dolly_in",
        })
        saved = save_film_project(self.root, "default", project)

        reopened = load_film_project(self.root, "default", project["id"])
        self.assertEqual(reopened["brief"]["logline"], saved["brief"]["logline"])
        self.assertEqual(reopened["brief"]["genre"], "neo-noir")
        self.assertEqual(len(reopened["shots"]), 1)
        self.assertAlmostEqual(reopened["shots"][0]["duration_sec"], 6.5)
        self.assertEqual(reopened["shots"][0]["camera_movement"], "dolly_in")

    def test_workspace_scoping_keeps_projects_separate(self):
        project = save_film_project(self.root, "client-a", create_film_project(name="A", workspace="client-a"))
        self.assertEqual(list_film_projects(self.root, "default"), [])
        listed = list_film_projects(self.root, "client-a")
        self.assertEqual([entry["id"] for entry in listed], [project["id"]])
        with self.assertRaises(FileNotFoundError):
            load_film_project(self.root, "default", project["id"])

    def test_listing_reports_real_production_progress(self):
        project = create_film_project(name="Rain City")
        upsert_asset(project, {"name": "Hero", "kind": "image", "role": "character_reference"})
        upsert_scene(project, {"title": "Rain"})
        upsert_shot(project, {"duration_sec": 4.0})
        upsert_shot(project, {"duration_sec": 3.0})
        save_film_project(self.root, "default", project)

        summary = list_film_projects(self.root, "default")[0]
        self.assertEqual(summary["name"], "Rain City")
        self.assertEqual(summary["asset_count"], 1)
        self.assertEqual(summary["shot_count"], 2)
        self.assertAlmostEqual(summary["planned_duration_sec"], 7.0)

    def test_delete_is_reported_truthfully(self):
        project = create_film_project(name="Rain City")
        save_film_project(self.root, "default", project)
        self.assertTrue(delete_film_project(self.root, "default", project["id"]))
        self.assertFalse(delete_film_project(self.root, "default", project["id"]))

    def test_corrupt_project_files_do_not_break_listing(self):
        project = save_film_project(self.root, "default", create_film_project(name="Good"))
        directory = Path(self.root) / ".maestro_film"
        (directory / "broken.json").write_text("{not json", encoding="utf-8")
        (directory / "notes.txt").write_text("ignore me", encoding="utf-8")
        listed = list_film_projects(self.root, "default")
        self.assertEqual([entry["id"] for entry in listed], [project["id"]])


class FilmProjectAssetTest(unittest.TestCase):
    def test_role_is_inferred_from_kind_but_user_correction_wins(self):
        project = create_film_project()
        upsert_asset(project, {"name": "still.png", "filename": "still.png"})
        asset = next(iter(project["assets"].values()))
        self.assertEqual(asset["kind"], "image")
        self.assertEqual(asset["role"], "character_reference")
        self.assertFalse(asset["reproduce_literally"])

        update_asset(project, asset["id"], {"role": "location_reference", "notes": "rainy alley"})
        corrected = project["assets"][asset["id"]]
        self.assertEqual(corrected["role"], "location_reference")
        self.assertTrue(corrected["role_locked"])
        self.assertEqual(corrected["analysis"]["status"], "unknown")

    def test_partial_update_never_drops_existing_fields(self):
        project = create_film_project()
        upsert_asset(project, {"name": "theme.wav", "filename": "theme.wav", "role": "source_music_keep"})
        asset_id = next(iter(project["assets"]))
        update_asset(project, asset_id, {"notes": "user's own track"})
        asset = project["assets"][asset_id]
        self.assertEqual(asset["role"], "source_music_keep")
        self.assertEqual(asset["notes"], "user's own track")
        self.assertTrue(asset["keep_original"])

    def test_removing_an_asset_unlinks_it_from_shots_soundtrack_and_bible(self):
        project = create_film_project()
        upsert_asset(project, {"name": "hero.png", "filename": "hero.png"})
        asset_id = next(iter(project["assets"]))
        upsert_scene(project, {"title": "Rain"})
        upsert_shot(project, {"start_image_asset_id": asset_id, "reference_asset_ids": [asset_id]})
        project["soundtrack"]["tracks"] = [{"asset_id": asset_id, "target": "music"}]
        project["bible"] = {
            "characters": [{"id": "mara", "name": "Mara", "reference_asset_ids": [asset_id], "voice_asset_ids": [asset_id]}],
            "locations": [{"id": "calle", "name": "Calle", "reference_asset_ids": [asset_id]}],
        }

        remove_asset(project, asset_id)
        self.assertEqual(project["assets"], {})
        self.assertEqual(project["shots"][0]["start_image_asset_id"], "")
        self.assertEqual(project["shots"][0]["reference_asset_ids"], [])
        self.assertEqual(project["soundtrack"]["tracks"], [])
        self.assertEqual(project["bible"]["characters"][0]["reference_asset_ids"], [])
        self.assertEqual(project["bible"]["characters"][0]["voice_asset_ids"], [])
        self.assertEqual(project["bible"]["locations"][0]["reference_asset_ids"], [])

    def test_unknown_asset_fields_survive_a_round_trip(self):
        project = create_film_project()
        upsert_asset(project, {"name": "hero.png", "codex_note": "kept for a future UI"})
        normalized = normalize_film_project(project)
        asset = next(iter(normalized["assets"].values()))
        self.assertEqual(asset["codex_note"], "kept for a future UI")


class FilmProjectStoryboardTest(unittest.TestCase):
    def setUp(self):
        self.project = create_film_project()
        upsert_scene(self.project, {"title": "Scene one", "objective": "Open"})

    def test_shots_are_reindexed_and_reorderable(self):
        upsert_shot(self.project, {"objective": "Wide", "duration_sec": 5})
        upsert_shot(self.project, {"objective": "Close", "duration_sec": 3})
        upsert_shot(self.project, {"objective": "Insert", "duration_sec": 2})
        order = [shot["objective"] for shot in self.project["shots"]]
        self.assertEqual(order, ["Wide", "Close", "Insert"])
        self.assertEqual([shot["index"] for shot in self.project["shots"]], [0, 1, 2])

        move_shot(self.project, self.project["shots"][2]["id"], 0)
        self.assertEqual(
            [shot["objective"] for shot in self.project["shots"]],
            ["Insert", "Wide", "Close"],
        )
        self.assertEqual([shot["index"] for shot in self.project["shots"]], [0, 1, 2])
        self.assertAlmostEqual(film_project_duration(self.project), 10.0)

    def test_upsert_by_id_updates_in_place_without_duplicating(self):
        upsert_shot(self.project, {"id": "shot-a", "objective": "First", "duration_sec": 4})
        upsert_shot(self.project, {"id": "shot-a", "video_prompt": "refined prompt"})
        self.assertEqual(len(self.project["shots"]), 1)
        shot = self.project["shots"][0]
        self.assertEqual(shot["objective"], "First")
        self.assertEqual(shot["video_prompt"], "refined prompt")

    def test_regenerating_one_shot_does_not_reorder_the_others(self):
        upsert_shot(self.project, {"id": "a", "objective": "A"})
        upsert_shot(self.project, {"id": "b", "objective": "B"})
        remove_shot(self.project, "a")
        self.assertEqual([shot["index"] for shot in self.project["shots"]], [0])
        self.assertEqual(self.project["shots"][0]["objective"], "B")

    def test_takes_are_kept_and_one_can_be_approved(self):
        upsert_shot(self.project, {"id": "a", "objective": "A"})
        add_take(self.project, "a", {"filename": "shot_a_v1.mp4", "job_id": "job-1"})
        add_take(self.project, "a", {"filename": "shot_a_v2.mp4", "job_id": "job-2"})
        shot = self.project["shots"][0]
        self.assertEqual([take["index"] for take in shot["takes"]], [0, 1])
        self.assertEqual(shot["status"], "generated")

        approve_take(self.project, "a", 0)
        shot = self.project["shots"][0]
        self.assertTrue(shot["approved"])
        self.assertEqual(shot["approved_take_index"], 0)
        self.assertEqual(shot["output_filename"], "shot_a_v1.mp4")

    def test_failed_take_marks_the_shot_failed_without_losing_history(self):
        upsert_shot(self.project, {"id": "a", "objective": "A"})
        add_take(self.project, "a", {"status": "failed", "error": "CUDA OOM"})
        shot = self.project["shots"][0]
        self.assertEqual(shot["status"], "failed")
        self.assertEqual(len(shot["takes"]), 1)
        self.assertEqual(shot["takes"][0]["error"], "CUDA OOM")

    def test_approving_a_missing_take_is_refused(self):
        upsert_shot(self.project, {"id": "a", "objective": "A"})
        with self.assertRaises(FilmProjectError):
            approve_take(self.project, "a", 0)

    def test_shot_must_belong_to_an_existing_scene(self):
        project = create_film_project()
        project["scenes"] = [{"id": "scene-real", "title": "Real"}]
        project["shots"] = [{"id": "s1", "scene_id": "ghost"}]
        normalized = normalize_film_project(project)
        self.assertEqual(normalized["shots"][0]["scene_id"], "scene-real")

    def test_invalid_durations_are_clamped_not_crashed(self):
        upsert_shot(self.project, {"objective": "Bad", "duration_sec": float("nan")})
        self.assertEqual(self.project["shots"][0]["duration_sec"], 4.0)
        upsert_shot(self.project, {"objective": "Huge", "duration_sec": 10 ** 9})
        self.assertEqual(self.project["shots"][1]["duration_sec"], 600.0)

    def test_invalid_camera_and_transition_fall_back(self):
        upsert_shot(self.project, {"camera_movement": "teleport", "transition": "wipe"})
        shot = self.project["shots"][0]
        self.assertEqual(shot["camera_movement"], "")
        self.assertEqual(shot["transition"], "cut")


class FilmProjectJobAndAuditTest(unittest.TestCase):
    def test_job_state_is_recorded_and_updatable(self):
        from services.film_projects import update_job, add_job

        project = create_film_project()
        add_job(project, {"id": "job-1", "shot_id": "s1", "kind": "video", "status": "queued"})
        update_job(project, "job-1", {"status": "completed", "output_filename": "s1.mp4"})
        self.assertEqual(project["jobs"][0]["status"], "completed")
        self.assertEqual(project["jobs"][0]["output_filename"], "s1.mp4")

    def test_creative_decisions_are_logged_in_order(self):
        project = create_film_project()
        record_decision(project, "approach", "Hybrid: keep the hero's identity")
        record_decision(project, "asset_role", "Photo 2 is a character reference")
        self.assertEqual([entry["kind"] for entry in project["decisions"]], ["approach", "asset_role"])

    def test_summary_counts_generated_and_approved_shots(self):
        project = create_film_project(name="Rain City")
        upsert_scene(project, {"title": "Rain"})
        upsert_shot(project, {"id": "a", "objective": "A"})
        upsert_shot(project, {"id": "b", "objective": "B"})
        add_take(project, "a", {"filename": "a.mp4"})
        approve_take(project, "a", 0)
        summary = film_project_summary(project)
        self.assertEqual(summary["shot_count"], 2)
        self.assertEqual(summary["generated_shot_count"], 1)
        self.assertEqual(summary["approved_shot_count"], 1)

    def test_unsafe_identifiers_are_rejected(self):
        with self.assertRaises(FilmProjectError):
            load_film_project("/tmp", "default", "../escape")

    def test_unsafe_workspace_names_are_rejected(self):
        with self.assertRaises(FilmProjectError):
            create_film_project(workspace="../outside")


if __name__ == "__main__":
    unittest.main()

"""Director plan ingestion, storyboard editing and production requests."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import film_planning
from services.film_projects import (
    FilmProjectError,
    add_take,
    create_film_project,
    find_shot,
    move_shot,
    remove_asset,
    upsert_asset,
)


def director_response() -> dict:
    """Shaped like a real DirectorV2PlanResponse, camera language included."""
    shots = [
        {
            "shot_id": "s1", "index": 0, "duration_sec": 8.0, "skill_type": "shot",
            "scene_goal": "Establish the flooded city",
            "narrative_role": "opening", "scene_type": "establishing",
            "subjects_on_screen": [{"name": "Mara", "role": "courier"}],
            "spatial_setup": "wide view down a flooded avenue",
            "environment": "night, heavy rain, neon signage",
            "visual_style": "wet reflections, high contrast",
            "lighting": "practical neon, rim light",
            "mood": "melancholic",
            "action_beats": ["Mara walks toward camera", "puddle ripples"],
            "dialogue_beats": [],
            "camera_plan": {"framing": "extreme wide shot", "movement": "slow dolly in"},
            "audio_plan": {"mode": "music_driven", "ambience": "rain, distant traffic"},
            "ending_beat": "she stops under the sign",
            "constraints": [], "continuity_refs": ["mara-coat"],
            "metadata": {"time_of_day": "night"},
        },
        {
            "shot_id": "s2", "index": 1, "duration_sec": 5.0, "skill_type": "shot",
            "scene_goal": "Reveal her decision",
            "narrative_role": "turn", "scene_type": "establishing",
            "subjects_on_screen": [{"name": "Mara", "role": "courier"}],
            "spatial_setup": "medium close-up on Mara's face",
            "environment": "neon light on wet skin",
            "visual_style": "shallow depth of field",
            "lighting": "soft key from the sign",
            "mood": "resolute",
            "action_beats": ["she looks up"],
            "dialogue_beats": [{"speaker": "Mara", "line": "No more waiting."}],
            "camera_plan": {"framing": "medium close-up", "movement": "static"},
            "audio_plan": {"mode": "dialogue_driven", "vocal_style": "whispered"},
            "ending_beat": "cut to black",
            "constraints": [], "continuity_refs": ["mara-coat"],
            "metadata": {},
        },
        {
            "shot_id": "s3", "index": 2, "duration_sec": 4.0, "skill_type": "shot",
            "scene_goal": "Departure",
            "narrative_role": "closing", "scene_type": "departure",
            "subjects_on_screen": [],
            "spatial_setup": "insert of the courier bag on the ground",
            "environment": "wet asphalt",
            "visual_style": "macro detail",
            "lighting": "hard practical",
            "mood": "final",
            "action_beats": ["the bag is left behind"],
            "dialogue_beats": [],
            "camera_plan": {"framing": "insert", "movement": "handheld"},
            "audio_plan": {"mode": "ambient_only"},
            "ending_beat": "rain continues",
            "constraints": [], "continuity_refs": [],
            "metadata": {},
        },
    ]
    return {
        "skill_type": "short_film",
        "clip_plans": [
            {"video_prompt": "A courier walks down a flooded avenue at night.", "image_prompt": "wide neon avenue"},
            {"video_prompt": "Close on her face, neon reflection.", "image_prompt": "medium close-up"},
            {"video_prompt": "Insert of a wet courier bag.", "image_prompt": "macro bag on asphalt"},
        ],
        "production_plan": {
            "skill_type": "short_film",
            "title": "Rain City",
            "global_style": "neo-noir, wet neon, shallow focus",
            "total_duration_sec": 17.0,
            "characters": [
                {"id": "mara", "display_name": "Mara", "physical_description": "short dark hair",
                 "wardrobe": "long grey coat", "voice_description": "low, measured"},
            ],
            "continuity_notes": ["Mara keeps the grey coat in every shot"],
            "shots": shots,
        },
    }


class ReferenceAssignmentTest(unittest.TestCase):
    def setUp(self):
        self.project = create_film_project(name="Rain City")
        upsert_asset(self.project, {"id": "mara-photo", "name": "mara.png", "filename": "mara.png", "role": "character_reference"})
        upsert_asset(self.project, {"id": "calle-photo", "name": "calle.jpg", "filename": "calle.jpg", "role": "location_reference"})
        upsert_asset(self.project, {"id": "tema", "name": "tema.wav", "filename": "tema.wav", "role": "source_music_keep"})
        film_planning.ingest_director_plan(self.project, director_response())
        self.shots = self.project["shots"]
        self.scene_id = self.shots[0]["scene_id"]

    def test_an_image_can_be_assigned_to_one_shot(self):
        report = film_planning.assign_references(
            self.project, asset_ids=["mara-photo"], shot_ids=[self.shots[1]["id"]],
        )
        self.assertEqual(report["shots"], 1)
        self.assertIn("mara-photo", self.shots[1]["reference_asset_ids"])
        self.assertEqual(self.shots[0]["reference_asset_ids"], [])
        self.assertEqual(self.project["decisions"][-1]["kind"], "references_assigned")

    def test_an_image_can_be_assigned_to_every_shot_of_a_scene(self):
        in_scene = [shot for shot in self.shots if shot["scene_id"] == self.scene_id]
        self.assertGreater(len(in_scene), 1)
        report = film_planning.assign_references(
            self.project, asset_ids=["calle-photo"], scene_ids=[self.scene_id],
        )
        self.assertEqual(report["shots"], len(in_scene))
        for shot in in_scene:
            self.assertIn("calle-photo", shot["reference_asset_ids"])

    def test_a_reference_can_become_the_start_frame(self):
        film_planning.assign_references(
            self.project, asset_ids=["mara-photo"], shot_ids=[self.shots[0]["id"]],
            as_start_image=True,
        )
        self.assertEqual(self.shots[0]["start_image_asset_id"], "mara-photo")
        self.assertEqual(self.shots[0]["reference_asset_ids"], [])

    def test_assignment_is_idempotent_unless_replacing(self):
        film_planning.assign_references(
            self.project, asset_ids=["mara-photo"], shot_ids=[self.shots[0]["id"]],
        )
        film_planning.assign_references(
            self.project, asset_ids=["mara-photo"], shot_ids=[self.shots[0]["id"]],
        )
        self.assertEqual(self.shots[0]["reference_asset_ids"], ["mara-photo"])

    def test_replacing_clears_what_was_there_before(self):
        film_planning.assign_references(
            self.project, asset_ids=["mara-photo"], shot_ids=[self.shots[0]["id"]], as_start_image=True,
        )
        film_planning.assign_references(
            self.project, asset_ids=["calle-photo"], shot_ids=[self.shots[0]["id"]], replace=True,
        )
        self.assertEqual(self.shots[0]["reference_asset_ids"], ["calle-photo"])
        self.assertEqual(self.shots[0]["start_image_asset_id"], "")

    def test_assigning_to_all_shots_is_possible(self):
        report = film_planning.assign_references(
            self.project, asset_ids=["calle-photo"], all_shots=True,
        )
        self.assertEqual(report["shots"], len(self.shots))

    def test_audio_cannot_be_used_as_a_visual_reference(self):
        with self.assertRaises(FilmProjectError):
            film_planning.assign_references(
                self.project, asset_ids=["tema"], shot_ids=[self.shots[0]["id"]],
            )

    def test_unknown_asset_or_target_is_refused(self):
        with self.assertRaises(FilmProjectError):
            film_planning.assign_references(self.project, asset_ids=["nope"], all_shots=True)
        with self.assertRaises(FilmProjectError):
            film_planning.assign_references(
                self.project, asset_ids=["mara-photo"], shot_ids=["ghost-shot"],
            )
        with self.assertRaises(FilmProjectError):
            film_planning.assign_references(self.project, asset_ids=[], all_shots=True)


class VisualContinuityTest(unittest.TestCase):
    def setUp(self):
        self.project = create_film_project(name="Rain City")
        upsert_asset(self.project, {"id": "mara-photo", "name": "mara.png", "filename": "mara.png", "role": "character_reference"})
        film_planning.ingest_director_plan(self.project, director_response())

    def test_a_character_photo_conditions_every_shot_that_features_them(self):
        report = film_planning.apply_visual_continuity(self.project)
        self.assertIn("Mara", report["characters_matched"])
        self.assertGreaterEqual(report["shots_updated"], 1)
        featuring = [
            shot for shot in self.project["shots"]
            if "Mara" in " ".join(str(value) for value in shot["elements"])
        ]
        self.assertTrue(featuring)
        for shot in featuring:
            self.assertIn("mara-photo", shot["reference_asset_ids"])
            self.assertIn("mara", shot["continuity_refs"])

    def test_a_character_without_a_photo_is_reported_not_invented(self):
        # Removing the file also unlinks it from the bible, so continuity must
        # report the missing reference instead of handing back a dead path.
        remove_asset(self.project, "mara-photo")
        self.assertEqual(self.project["bible"]["characters"][0]["reference_asset_ids"], [])
        report = film_planning.apply_visual_continuity(self.project)
        self.assertEqual(report["characters_matched"], [])
        self.assertEqual(report["characters_without_reference"], ["Mara"])
        for shot in self.project["shots"]:
            self.assertEqual(shot["reference_asset_ids"], [])

    def test_continuity_is_recorded_as_a_decision(self):
        film_planning.apply_visual_continuity(self.project)
        self.assertEqual(self.project["decisions"][-1]["kind"], "continuity_applied")

    def test_the_bible_can_be_used_to_replace_guessed_references(self):
        self.project["shots"][0]["reference_asset_ids"] = ["mara-photo"]
        film_planning.apply_visual_continuity(self.project, replace=True)
        self.assertNotIn("ghost", self.project["shots"][0]["reference_asset_ids"])

    def test_no_bible_characters_says_why_nothing_happened(self):
        self.project["bible"]["characters"] = []
        report = film_planning.apply_visual_continuity(self.project)
        self.assertEqual(report["shots_updated"], 0)
        self.assertIn("continuity bible", report["reason"])

    def test_a_failed_take_leaves_the_shot_retryable(self):
        shot_id = self.project["shots"][0]["id"]
        film_planning.record_production_job(self.project, shot_id=shot_id, job_id="job-fail")
        film_planning.sync_shot_from_job(
            self.project, job_id="job-fail", status="failed", error="CUDA OOM",
        )
        requests = film_planning.production_requests(self.project, only_missing=True)
        request = next(item for item in requests if item["shot_id"] == shot_id)
        self.assertFalse(request["already_rendered"])
        self.assertFalse(request["skip"])

    def test_a_cancelled_job_returns_the_shot_to_a_runnable_state(self):
        shot_id = self.project["shots"][0]["id"]
        film_planning.record_production_job(self.project, shot_id=shot_id, job_id="job-stop")
        film_planning.sync_shot_from_job(self.project, job_id="job-stop", status="cancelled")
        shot = find_shot(self.project, shot_id)
        self.assertEqual(shot["status"], "planned")
        self.assertEqual(shot["takes"], [])
        self.assertEqual(self.project["decisions"][-1]["kind"], "job_cancelled")

    def test_a_completed_take_still_counts_as_rendered(self):
        shot_id = self.project["shots"][0]["id"]
        film_planning.record_production_job(self.project, shot_id=shot_id, job_id="job-ok")
        film_planning.sync_shot_from_job(
            self.project, job_id="job-ok", status="completed", filename="a.mp4",
        )
        requests = film_planning.production_requests(self.project, only_missing=True)
        request = next(item for item in requests if item["shot_id"] == shot_id)
        self.assertTrue(request["already_rendered"])


class PlanIngestionTest(unittest.TestCase):
    def setUp(self):
        self.project = create_film_project(name="Rain City")

    def test_real_planner_output_becomes_scenes_and_shots(self):
        report = film_planning.ingest_director_plan(self.project, director_response())
        self.assertEqual(report["shots"], 3)
        self.assertEqual(report["scenes"], 2)  # scene_type changed for the last shot
        self.assertEqual([shot["objective"] for shot in self.project["shots"]], [
            "Establish the flooded city", "Reveal her decision", "Departure",
        ])
        self.assertEqual([shot["index"] for shot in self.project["shots"]], [0, 1, 2])
        self.assertAlmostEqual(report["planned_duration_sec"], 17.0)

    def test_prompts_come_from_clip_plans_in_order(self):
        film_planning.ingest_director_plan(self.project, director_response())
        self.assertEqual(
            [shot["video_prompt"] for shot in self.project["shots"]],
            [clip["video_prompt"] for clip in director_response()["clip_plans"]],
        )

    def test_camera_language_is_translated_to_the_storyboard_vocabulary(self):
        film_planning.ingest_director_plan(self.project, director_response())
        self.assertEqual(
            [shot["shot_size"] for shot in self.project["shots"]],
            ["extreme_wide", "medium_close", "insert"],
        )
        self.assertEqual(
            [shot["camera_movement"] for shot in self.project["shots"]],
            ["dolly_in", "static", "handheld"],
        )

    def test_dialogue_lighting_and_music_notes_are_preserved(self):
        film_planning.ingest_director_plan(self.project, director_response())
        second = self.project["shots"][1]
        self.assertIn("Mara: No more waiting.", second["dialogue"])
        self.assertEqual(second["lighting"], "soft key from the sign")
        self.assertIn("Ambience", self.project["shots"][0]["music_notes"])

    def test_continuity_bible_is_built_from_the_plan(self):
        upsert_asset(self.project, {"name": "mara.png", "filename": "mara.png", "role": "character_reference"})
        report = film_planning.ingest_director_plan(self.project, director_response())
        characters = self.project["bible"]["characters"]
        self.assertEqual(characters[0]["name"], "Mara")
        self.assertEqual(len(characters[0]["reference_asset_ids"]), 1)
        self.assertEqual(report["characters_without_reference"], [])
        self.assertIn("Mara keeps the grey coat in every shot", self.project["bible"]["continuity_rules"])
        self.assertIn("neo-noir", self.project["bible"]["style_notes"])

    def test_characters_without_a_reference_photo_are_reported(self):
        report = film_planning.ingest_director_plan(self.project, director_response())
        self.assertEqual(report["characters_without_reference"], ["Mara"])

    def test_plan_import_is_recorded_as_a_creative_decision(self):
        film_planning.ingest_director_plan(self.project, director_response())
        self.assertEqual(self.project["decisions"][-1]["kind"], "plan_imported")
        self.assertEqual(self.project["status"], "planned")
        self.assertEqual(self.project["plan"]["director_skill"], "short_film")

    def test_replanning_preserves_rendered_takes_for_identical_prompts(self):
        film_planning.ingest_director_plan(self.project, director_response())
        first_shot_id = self.project["shots"][0]["id"]
        add_take(self.project, first_shot_id, {"filename": "s1_take1.mp4", "job_id": "job-1"})

        report = film_planning.ingest_director_plan(self.project, director_response())
        self.assertEqual(report["renders_preserved"], 1)
        preserved = next(
            shot for shot in self.project["shots"]
            if shot["video_prompt"] == director_response()["clip_plans"][0]["video_prompt"]
        )
        self.assertEqual(len(preserved["takes"]), 1)
        self.assertEqual(preserved["output_filename"], "s1_take1.mp4")

    def test_an_empty_plan_is_refused_instead_of_backfilled(self):
        with self.assertRaises(FilmProjectError):
            film_planning.ingest_director_plan(self.project, {"production_plan": {"shots": []}, "clip_plans": []})
        self.assertEqual(self.project["shots"], [])

    def test_plan_without_clip_plans_still_produces_shots_without_prompts(self):
        response = director_response()
        response["clip_plans"] = []
        film_planning.ingest_director_plan(self.project, response)
        self.assertEqual(len(self.project["shots"]), 3)
        self.assertEqual(film_planning.storyboard_totals(self.project)["with_prompt"], 0)

    def test_short_film_story_planner_output_is_accepted(self):
        response = {
            "skill_type": "short_film",
            "clips": [
                {
                    "start": 0.0, "end": 6.5, "section_label": "establishing",
                    "suggested_prompt_hint": "Rain-slick avenue at night",
                    "duration_frames": 156, "dominant_speaker": None,
                    "dialogue_lines": [],
                },
                {
                    "start": 6.5, "end": 11.0, "section_label": "dialogue",
                    "suggested_prompt_hint": "She decides", "duration_frames": 108,
                    "dominant_speaker": "mara",
                    "dialogue_lines": [{"speaker": "mara", "line": "No more waiting."}],
                },
            ],
            "clip_plans": [
                {"video_prompt": "Wide avenue, rain.", "image_prompt": "wide"},
                {"video_prompt": "Close on her face.", "image_prompt": "close"},
            ],
        }
        report = film_planning.ingest_director_plan(self.project, response)
        self.assertEqual(report["shots"], 2)
        self.assertEqual(self.project["shots"][0]["objective"], "Rain-slick avenue at night")
        self.assertAlmostEqual(self.project["shots"][0]["duration_sec"], 6.5)
        self.assertAlmostEqual(self.project["shots"][1]["duration_sec"], 4.5)
        self.assertIn("mara: No more waiting.", self.project["shots"][1]["dialogue"])
        self.assertEqual(self.project["shots"][1]["video_prompt"], "Close on her face.")
        self.assertEqual(report["scenes"], 2)

    def test_camera_text_normalisation_handles_planner_phrasing(self):
        self.assertEqual(film_planning.shot_size_from_text("slow medium wide shot"), "medium_wide")
        self.assertEqual(film_planning.shot_size_from_text("close"), "close")
        self.assertEqual(film_planning.shot_size_from_text(""), "")
        self.assertEqual(film_planning.camera_movement_from_text("slow push in"), "dolly_in")
        self.assertEqual(film_planning.camera_movement_from_text("locked off"), "static")
        self.assertEqual(film_planning.camera_movement_from_text("dolly_in"), "dolly_in")


class StoryboardTest(unittest.TestCase):
    def setUp(self):
        self.project = create_film_project(name="Rain City")
        film_planning.ingest_director_plan(self.project, director_response())

    def test_storyboard_is_ordered_and_carries_timings(self):
        board = film_planning.storyboard(self.project)
        self.assertEqual([entry["index"] for entry in board], [0, 1, 2])
        self.assertAlmostEqual(board[0]["start_time_sec"], 0.0)
        self.assertAlmostEqual(board[1]["start_time_sec"], 8.0)
        self.assertAlmostEqual(board[2]["end_time_sec"], 17.0)
        self.assertEqual(board[0]["scene_title"], "Establishing")
        self.assertEqual(board[2]["scene_title"], "Departure")

    def test_totals_report_what_is_still_missing(self):
        totals = film_planning.storyboard_totals(self.project)
        self.assertEqual(totals["shot_count"], 3)
        self.assertEqual(totals["with_prompt"], 3)
        self.assertEqual(totals["with_takes"], 0)
        self.assertEqual(totals["shots_without_prompt"], [])

    def test_a_single_shot_can_be_reordered_without_touching_the_others(self):
        board = film_planning.storyboard(self.project)
        move_shot(self.project, board[2]["id"], 0)
        self.assertEqual(
            [entry["objective"] for entry in film_planning.storyboard(self.project)],
            ["Departure", "Establish the flooded city", "Reveal her decision"],
        )

    def test_a_single_shot_can_be_regenerated_in_place(self):
        board = film_planning.storyboard(self.project)
        add_take(self.project, board[0]["id"], {"filename": "take1.mp4"})
        add_take(self.project, board[0]["id"], {"filename": "take2.mp4"})
        shot = find_shot(self.project, board[0]["id"])
        self.assertEqual(len(shot["takes"]), 2)
        board = film_planning.storyboard(self.project)
        self.assertEqual(board[0]["take_count"], 2)
        self.assertEqual(board[1]["take_count"], 0)


class ProductionRequestTest(unittest.TestCase):
    def setUp(self):
        self.project = create_film_project(name="Rain City")
        film_planning.ingest_director_plan(self.project, director_response())

    def test_requests_are_built_for_every_planned_shot(self):
        requests = film_planning.production_requests(self.project)
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(not request["skip"] for request in requests))
        self.assertEqual(requests[0]["prompt"], director_response()["clip_plans"][0]["video_prompt"])
        self.assertAlmostEqual(requests[1]["duration_sec"], 5.0)
        self.assertEqual(requests[1]["shot_size"], "medium_close")

    def test_rendered_shots_are_skipped_unless_explicitly_requested(self):
        shot_id = self.project["shots"][0]["id"]
        add_take(self.project, shot_id, {"filename": "take.mp4"})
        requests = film_planning.production_requests(self.project)
        first = next(request for request in requests if request["shot_id"] == shot_id)
        self.assertTrue(first["skip"])
        self.assertTrue(first["already_rendered"])
        forced = film_planning.production_requests(self.project, only_missing=False)
        self.assertTrue(all(not request["skip"] for request in forced))

    def test_prompts_missing_are_reported_as_skips_not_submitted(self):
        for shot in self.project["shots"]:
            shot["video_prompt"] = ""
            shot["image_prompt"] = ""
        requests = film_planning.production_requests(self.project)
        self.assertTrue(all(request["skip"] for request in requests))
        self.assertIn("No prompt", requests[0]["skip_reason"])

    def test_production_readiness_blocks_on_missing_prompts(self):
        for shot in self.project["shots"]:
            shot["video_prompt"] = ""
            shot["image_prompt"] = ""
        issues = film_planning.production_readiness(self.project)
        codes = [issue["code"] for issue in issues]
        self.assertIn("shot_without_prompt", codes)

    def test_production_readiness_is_honest_when_the_engine_is_absent(self):
        issues = film_planning.production_readiness(self.project, model_available=False)
        engine = [issue for issue in issues if issue["code"] == "engine_unavailable"]
        self.assertEqual(len(engine), 1)
        self.assertEqual(engine[0]["severity"], "blocking")
        self.assertIn("NVIDIA GPU", engine[0]["message"])

    def test_job_records_start_queued_and_follow_real_status(self):
        shot_id = self.project["shots"][0]["id"]
        film_planning.record_production_job(
            self.project, shot_id=shot_id, job_id="job-42", params={"prompt": "x"},
        )
        self.assertEqual(find_shot(self.project, shot_id)["status"], "queued")

        film_planning.sync_shot_from_job(
            self.project, job_id="job-42", status="completed", filename="job-42.mp4",
        )
        shot = find_shot(self.project, shot_id)
        self.assertEqual(shot["status"], "generated")
        self.assertEqual(shot["output_filename"], "job-42.mp4")
        self.assertEqual(self.project["jobs"][0]["status"], "completed")

    def test_a_failed_job_does_not_erase_the_shot(self):
        shot_id = self.project["shots"][1]["id"]
        film_planning.record_production_job(self.project, shot_id=shot_id, job_id="job-7")
        film_planning.sync_shot_from_job(
            self.project, job_id="job-7", status="failed", error="CUDA out of memory",
        )
        shot = find_shot(self.project, shot_id)
        self.assertEqual(shot["status"], "failed")
        self.assertEqual(shot["takes"][0]["error"], "CUDA out of memory")
        self.assertTrue(shot["video_prompt"])

    def test_unknown_job_sync_is_refused(self):
        with self.assertRaises(FilmProjectError):
            film_planning.sync_shot_from_job(self.project, job_id="nope", status="completed")


if __name__ == "__main__":
    unittest.main()

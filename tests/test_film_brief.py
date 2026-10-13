"""Brief composition, approach modes, reference association and readiness."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import film_brief
from services.film_projects import create_film_project, update_asset, upsert_asset


class BriefCompositionTest(unittest.TestCase):
    def setUp(self):
        self.project = create_film_project(name="Rain City")
        self.project["brief"].update({
            "logline": "A courier crosses a flooded city at night.",
            "goal": "A melancholic nocturnal sequence.",
            "genre": "neo-noir",
            "tone": "melancholic, realistic",
            "visual_style": "wet neon reflections, shallow depth of field",
            "target_duration_sec": 90,
            "aspect_ratio": "2.39:1",
            "must_keep": ["the hero's face from image 2"],
            "avoid": ["daylight scenes"],
            "reference_notes": "Architecture from foto_fachada.jpg",
        })

    def test_brief_carries_the_users_intent_verbatim(self):
        text = film_brief.compose_director_brief(self.project)
        self.assertIn("A courier crosses a flooded city at night.", text)
        self.assertIn("neo-noir", text)
        self.assertIn("2.39:1", text)
        self.assertIn("90 seconds", text)
        self.assertIn("the hero's face from image 2", text)
        self.assertIn("daylight scenes", text)
        self.assertIn("Architecture from foto_fachada.jpg", text)

    def test_brief_describes_every_file_with_its_creative_role(self):
        upsert_asset(self.project, {"name": "hero.png", "filename": "hero.png", "role": "character_reference"})
        update_asset(self.project, next(iter(self.project["assets"])), {"reproduce_literally": True})
        upsert_asset(self.project, {"name": "theme.wav", "filename": "theme.wav", "role": "source_music_keep"})
        upsert_asset(self.project, {"name": "foto_fachada.jpg", "filename": "foto_fachada.jpg", "role": "location_reference"})
        text = film_brief.compose_director_brief(self.project)
        self.assertIn("hero.png", text)
        self.assertIn("reproduce literally", text)
        self.assertIn("character identity reference", text)
        self.assertIn("location reference", text)
        self.assertIn("inspiration only", text)

    def test_music_the_user_supplied_is_flagged_as_unreplaceable(self):
        upsert_asset(self.project, {"name": "mi_tema.wav", "filename": "mi_tema.wav", "role": "source_music_keep"})
        text = film_brief.compose_director_brief(self.project)
        self.assertIn("never replace or mute it", text)

    def test_analysis_limitations_are_carried_into_the_brief(self):
        upsert_asset(self.project, {"name": "clip.mp4", "filename": "clip.mp4", "role": "source_video_edit"})
        asset_id = next(iter(self.project["assets"]))
        update_asset(self.project, asset_id, {
            "analysis": {"available": False, "status": "unavailable", "limitations": ["ffprobe is not installed"]},
        })
        text = film_brief.compose_director_brief(self.project)
        self.assertIn("local analysis unavailable", text)

    def test_supplied_script_text_is_included(self):
        upsert_asset(self.project, {"name": "guion.txt", "filename": "guion.txt", "role": "script_document"})
        asset_id = next(iter(self.project["assets"]))
        update_asset(self.project, asset_id, {
            "analysis": {"available": True, "status": "ok", "text_excerpt": "ESCENA 1: llueve."},
        })
        text = film_brief.compose_director_brief(self.project)
        self.assertIn("USER-SUPPLIED SCRIPT", text)
        self.assertIn("ESCENA 1: llueve.", text)

    def test_continuity_bible_is_included(self):
        self.project["bible"]["characters"].append({
            "id": "c1", "name": "Mara", "physical_description": "short dark hair",
            "wardrobe": "long grey coat",
        })
        text = film_brief.compose_director_brief(self.project)
        self.assertIn("CONTINUITY BIBLE", text)
        self.assertIn("Mara", text)
        self.assertIn("long grey coat", text)


class ApproachModeTest(unittest.TestCase):
    def test_each_mode_produces_a_distinct_and_explicit_directive(self):
        project = create_film_project(name="Rain City")
        directives = {}
        for mode in ("faithful", "creative", "hybrid"):
            project["approach"] = mode
            directives[mode] = film_brief.compose_director_brief(project)
        self.assertIn("MODE: FAITHFUL", directives["faithful"])
        self.assertIn("MODE: CREATIVE", directives["creative"])
        self.assertIn("MODE: HYBRID", directives["hybrid"])
        self.assertEqual(len(set(directives.values())), 3)

    def test_faithful_mode_forbids_inventing_story_beats(self):
        self.assertIn("Do not invent characters", film_brief.approach_directive("faithful"))

    def test_unknown_mode_falls_back_to_hybrid(self):
        self.assertEqual(
            film_brief.approach_directive("whatever"),
            film_brief.APPROACH_DIRECTIVES["hybrid"],
        )


class ReferenceAssociationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.uploads = Path(temp.name) / "uploads"
        self.uploads.mkdir()
        self.project = create_film_project(name="Rain City")

    def _add(self, filename, role, **extra):
        (self.uploads / filename).write_bytes(b"data")
        upsert_asset(self.project, {"name": filename, "filename": filename, "role": role, **extra})
        return next(
            asset["id"] for asset in self.project["assets"].values()
            if asset["filename"] == filename
        )

    def test_roles_map_into_directors_existing_reference_slots(self):
        self._add("hero.png", "character_reference")
        self._add("partner.png", "character_reference")
        self._add("calle.jpg", "location_reference")
        self._add("tema.wav", "source_music_keep")
        self._add("voz.wav", "character_voice")

        inputs = film_brief.director_reference_inputs(
            self.project, uploads_root=str(self.uploads), save_root=str(self.uploads), workspace="default",
        )
        self.assertTrue(inputs["reference_image_path"].endswith("hero.png"))
        self.assertEqual(len(inputs["character_ref_paths"]), 1)
        self.assertTrue(inputs["character_ref_paths"][0].endswith("partner.png"))
        self.assertTrue(inputs["location_ref_paths"][0].endswith("calle.jpg"))
        self.assertTrue(inputs["audio_file"].endswith("tema.wav"))
        self.assertTrue(inputs["voice_reference"].endswith("voz.wav"))
        self.assertEqual(inputs["unresolved_asset_names"], [])

    def test_planner_params_pass_through_directors_own_bridge(self):
        self._add("hero.png", "character_reference")
        params = film_brief.planner_reference_params(
            self.project, uploads_root=str(self.uploads), save_root=str(self.uploads), workspace="default",
        )
        self.assertIn("reference_image_path", params)
        self.assertIn("location_ref_paths", params)

    def test_missing_files_are_reported_not_silently_skipped(self):
        upsert_asset(self.project, {"name": "gone.png", "filename": "gone.png", "role": "character_reference"})
        inputs = film_brief.director_reference_inputs(
            self.project, uploads_root=str(self.uploads), save_root=str(self.uploads), workspace="default",
        )
        self.assertIsNone(inputs["reference_image_path"])
        self.assertEqual(inputs["unresolved_asset_names"], ["gone.png"])

    def test_video_source_files_are_surfaced_separately(self):
        self._add("bruto.mp4", "source_video_edit")
        inputs = film_brief.director_reference_inputs(
            self.project, uploads_root=str(self.uploads), save_root=str(self.uploads), workspace="default",
        )
        self.assertTrue(inputs["source_video_paths"][0].endswith("bruto.mp4"))


class ReadinessTest(unittest.TestCase):
    def test_missing_idea_blocks_planning(self):
        project = create_film_project(name="Empty")
        issues = film_brief.brief_readiness(project)
        self.assertIn("brief_missing", [issue["code"] for issue in issues])
        self.assertEqual(len(film_brief.blocking_issues(issues)), 1)

    def test_complete_brief_reports_no_blockers(self):
        project = create_film_project(name="Rain City")
        project["brief"]["logline"] = "A courier crosses a flooded city."
        project["brief"]["genre"] = "neo-noir"
        upsert_asset(project, {"name": "hero.png", "filename": "hero.png", "role": "character_reference"})
        issues = film_brief.brief_readiness(project)
        self.assertEqual(film_brief.blocking_issues(issues), [])

    def test_missing_file_on_disk_is_blocking(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        project = create_film_project(name="Rain City")
        project["brief"]["logline"] = "Something."
        upsert_asset(project, {"name": "gone.png", "filename": "gone.png", "role": "character_reference"})
        issues = film_brief.brief_readiness(
            project, uploads_root=temp.name, save_root=temp.name, workspace="default",
        )
        codes = [issue["code"] for issue in issues]
        self.assertIn("asset_file_missing", codes)

    def test_pending_conversion_is_surfaced_as_a_requirement(self):
        project = create_film_project(name="Rain City")
        project["brief"]["logline"] = "Something."
        upsert_asset(project, {
            "name": "scan.heic", "filename": "scan.heic", "role": "location_reference",
            "conversions": [{"target_format": "png", "status": "suggested", "reason": "needs png"}],
        })
        issues = film_brief.brief_readiness(project)
        self.assertIn("conversion_pending", [issue["code"] for issue in issues])

    def test_assets_without_a_role_are_flagged(self):
        project = create_film_project(name="Rain City")
        project["brief"]["logline"] = "Something."
        upsert_asset(project, {"name": "mystery.bin", "filename": "mystery.bin", "role": "other"})
        issues = film_brief.brief_readiness(project)
        self.assertIn("role_unassigned", [issue["code"] for issue in issues])

    def test_roles_present_lists_what_the_project_has(self):
        project = create_film_project(name="Rain City")
        upsert_asset(project, {"name": "a.png", "filename": "a.png", "role": "lighting"})
        upsert_asset(project, {"name": "b.wav", "filename": "b.wav", "role": "sfx"})
        self.assertEqual(film_brief.roles_present(project), ["lighting", "sfx"])


if __name__ == "__main__":
    unittest.main()

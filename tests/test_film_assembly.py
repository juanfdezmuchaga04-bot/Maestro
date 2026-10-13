"""Soundtrack lanes, music sync and storyboard → Editor assembly."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import film_assembly, film_planning
from services.editor_projects import normalize_editor_project
from services.film_projects import (
    add_take,
    create_film_project,
    update_asset,
    upsert_asset,
)

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
        "shots": [
            {
                "shot_id": "s1", "index": 0, "duration_sec": 8.0,
                "scene_goal": "Establish the city", "scene_type": "establishing",
                "subjects_on_screen": [{"name": "Mara"}],
                "spatial_setup": "wide avenue", "environment": "night rain",
                "visual_style": "neon", "lighting": "practical", "mood": "melancholic",
                "action_beats": ["walks"], "dialogue_beats": [],
                "camera_plan": {"framing": "wide shot", "movement": "dolly in"},
                "audio_plan": {"mode": "music_driven"}, "ending_beat": "stops",
                "constraints": [], "continuity_refs": [], "metadata": {},
            },
            {
                "shot_id": "s2", "index": 1, "duration_sec": 4.0,
                "scene_goal": "Her decision", "scene_type": "establishing",
                "subjects_on_screen": [{"name": "Mara"}],
                "spatial_setup": "close-up", "environment": "wet neon",
                "visual_style": "shallow", "lighting": "soft", "mood": "resolute",
                "action_beats": ["looks up"],
                "dialogue_beats": [{"speaker": "Mara", "line": "No more waiting."}],
                "camera_plan": {"framing": "close-up", "movement": "static"},
                "audio_plan": {"mode": "dialogue_driven"}, "ending_beat": "black",
                "constraints": [], "continuity_refs": [], "metadata": {},
            },
        ],
    },
}


class AssemblyTestBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.save_root = self.root / "outputs"
        self.save_root.mkdir()
        self.project = create_film_project(name="Rain City")
        film_planning.ingest_director_plan(self.project, DIRECTOR_RESPONSE)

    def render_shot(self, index: int, filename: str, duration: float = 4.0) -> str:
        (self.save_root / filename).write_bytes(b"\x00\x00\x00\x18ftypmp42")
        shot = self.project["shots"][index]
        shot["duration_sec"] = duration
        add_take(self.project, shot["id"], {"filename": filename, "job_id": f"job-{index}"})
        shot["approved"] = True
        shot["approved_take_index"] = 0
        shot["status"] = "approved"
        return shot["id"]

    def build(self, **kwargs):
        return film_assembly.build_editor_project(
            self.project,
            save_root=str(self.save_root),
            uploads_root=str(self.uploads),
            **kwargs,
        )


class CanvasTest(unittest.TestCase):
    def test_canvas_follows_the_brief_aspect_ratio(self):
        project = create_film_project(name="X")
        project["brief"]["aspect_ratio"] = "9:16"
        self.assertEqual(film_assembly.canvas_for(project)["width"], 1080)
        project["brief"]["aspect_ratio"] = "2.39:1"
        self.assertEqual(film_assembly.canvas_for(project)["height"], 858)

    def test_manual_assembly_settings_win(self):
        project = create_film_project(name="X")
        project["timeline"]["assembly_settings"] = {"width": 1280, "height": 720, "fps": 24}
        canvas = film_assembly.canvas_for(project)
        self.assertEqual((canvas["width"], canvas["height"], canvas["fps"]), (1280, 720, 24.0))


class SoundtrackTest(AssemblyTestBase):
    def test_roles_become_independent_lanes(self):
        upsert_asset(self.project, {"name": "tema.wav", "filename": "tema.wav", "role": "source_music_keep"})
        upsert_asset(self.project, {"name": "voz.wav", "filename": "voz.wav", "role": "narration_voice"})
        upsert_asset(self.project, {"name": "lluvia.wav", "filename": "lluvia.wav", "role": "ambience"})
        plan = film_assembly.soundtrack_plan(self.project)
        self.assertEqual(
            [track["target"] for track in plan["tracks"]],
            ["music", "narration", "ambience"],
        )
        self.assertTrue(all(track["preserve"] for track in plan["tracks"]))
        self.assertLess(plan["tracks"][0]["gain_db"], 0.0)

    def test_without_music_the_plan_says_so(self):
        plan = film_assembly.soundtrack_plan(self.project)
        self.assertEqual(plan["tracks"], [])
        self.assertEqual(len(plan["warnings"]), 1)
        self.assertIn("No music assigned", plan["warnings"][0])

    def test_user_gain_and_fades_survive_replanning(self):
        upsert_asset(self.project, {"name": "tema.wav", "filename": "tema.wav", "role": "source_music_keep"})
        asset_id = next(iter(self.project["assets"]))
        self.project["soundtrack"]["tracks"] = [{
            "asset_id": asset_id, "target": "music", "gain_db": -18.0,
            "fade_in_sec": 3.0, "fade_out_sec": 4.0,
        }]
        track = film_assembly.soundtrack_plan(self.project)["tracks"][0]
        self.assertEqual(track["gain_db"], -18.0)
        self.assertEqual(track["fade_in_sec"], 3.0)
        self.assertEqual(track["fade_out_sec"], 4.0)

    def test_music_is_never_muted_automatically(self):
        upsert_asset(self.project, {"name": "tema.wav", "filename": "tema.wav", "role": "soundtrack"})
        plan = film_assembly.soundtrack_plan(self.project)
        self.assertFalse(plan["tracks"][0]["muted"])
        self.assertTrue(plan["tracks"][0]["preserve"])

    def test_apply_persists_lanes_and_sync_state(self):
        upsert_asset(self.project, {"name": "tema.wav", "filename": "tema.wav", "role": "soundtrack"})
        film_assembly.apply_soundtrack_plan(self.project)
        self.assertEqual(len(self.project["soundtrack"]["tracks"]), 1)
        self.assertIn("sync", self.project["soundtrack"])


class MusicSyncTest(AssemblyTestBase):
    def _music(self, **analysis):
        upsert_asset(self.project, {"name": "tema.wav", "filename": "tema.wav", "role": "source_music_keep"})
        asset_id = next(iter(self.project["assets"]))
        payload = {"available": True, "status": "ok", "limitations": [], "duration": 60.0}
        payload.update(analysis)
        update_asset(self.project, asset_id, {"analysis": payload})
        return asset_id

    def test_without_analysis_sync_reports_the_real_reason(self):
        self._music(available=False, limitations=["librosa is not installed"])
        plan = film_assembly.music_sync_plan(self.project)
        self.assertFalse(plan["available"])
        self.assertIn("librosa", plan["reason"])

    def test_without_any_music_sync_says_no_track_is_assigned(self):
        plan = film_assembly.music_sync_plan(self.project)
        self.assertIn("No music track", plan["reason"])

    def test_beats_produce_cut_candidates_not_invented_values(self):
        self._music(bpm=120.0, beats=[7.5, 12.0, 11.5, 16.0], sections=[])
        plan = film_assembly.music_sync_plan(self.project)
        self.assertTrue(plan["available"])
        self.assertEqual(plan["bpm"], 120.0)
        self.assertEqual(len(plan["boundaries"]), 2)
        self.assertAlmostEqual(plan["boundaries"][0]["planned_sec"], 8.0)
        self.assertAlmostEqual(plan["boundaries"][0]["nearest_beat"], 7.5)
        self.assertAlmostEqual(plan["boundaries"][0]["delta_sec"], -0.5)

    def test_sections_alone_can_align_scene_changes(self):
        self._music(beats=[], sections=[{"start": 12.0, "end": 30.0, "label": "chorus"}])
        plan = film_assembly.music_sync_plan(self.project)
        self.assertTrue(plan["available"])
        self.assertIsNone(plan["boundaries"][1]["nearest_beat"])
        self.assertAlmostEqual(plan["boundaries"][1]["nearest_section"], 12.0)

    def test_analysis_without_beats_or_sections_is_reported(self):
        self._music(beats=[], sections=[])
        plan = film_assembly.music_sync_plan(self.project)
        self.assertFalse(plan["available"])
        self.assertIn("no beats or sections", plan["reason"])

    def test_snapping_moves_only_boundaries_within_tolerance(self):
        self._music(bpm=120.0, beats=[7.5, 12.0, 11.5, 16.0], sections=[])
        result = film_assembly.snap_shots_to_music(self.project, tolerance_sec=1.0)
        self.assertEqual(len(result["changed"]), 1)
        self.assertEqual(result["changed"][0]["shot_id"], self.project["shots"][0]["id"])
        self.assertAlmostEqual(self.project["shots"][0]["duration_sec"], 7.5)
        self.assertAlmostEqual(self.project["shots"][1]["duration_sec"], 4.0)
        self.assertEqual(self.project["decisions"][-1]["kind"], "music_sync")

    def test_snapping_does_nothing_when_music_cannot_be_analysed(self):
        result = film_assembly.snap_shots_to_music(self.project)
        self.assertEqual(result["changed"], [])
        self.assertTrue(result["reason"])
        self.assertAlmostEqual(self.project["shots"][0]["duration_sec"], 8.0)


class EditorAssemblyTest(AssemblyTestBase):
    def test_only_rendered_shots_reach_the_timeline(self):
        self.render_shot(0, "shot_a.mp4", duration=8.0)
        result = self.build()
        report = result["report"]
        self.assertEqual(report["shots_in_timeline"], 1)
        self.assertEqual(len(report["shots_missing"]), 1)
        self.assertIn("no render yet", report["shots_missing"][0]["reason"])

    def test_missing_files_are_reported_instead_of_placed(self):
        shot_id = self.render_shot(0, "shot_a.mp4")
        (self.save_root / "shot_a.mp4").unlink()
        result = self.build()
        self.assertEqual(result["report"]["shots_in_timeline"], 0)
        self.assertIn("not found", result["report"]["shots_missing"][0]["reason"])
        self.assertTrue(shot_id)

    def test_compiled_project_survives_the_editors_own_normaliser(self):
        self.render_shot(0, "shot_a.mp4", duration=8.0)
        self.render_shot(1, "shot_b.mp4", duration=4.0)
        result = self.build()
        normalized = normalize_editor_project(result["editor_project"], workspace="default")
        video_track = next(track for track in normalized["tracks"] if track["type"] == "video")
        self.assertEqual(len(video_track["items"]), 2)
        self.assertAlmostEqual(video_track["items"][0]["duration"], 8.0)
        self.assertAlmostEqual(video_track["items"][1]["start"], 8.0)
        self.assertEqual(len(normalized["assets"]), 2)
        self.assertEqual(normalized["id"] != "", True)

    def test_asset_ids_are_stable_across_runs(self):
        self.render_shot(0, "shot_a.mp4")
        first = self.build()["editor_project"]["assets"]
        second = self.build()["editor_project"]["assets"]
        self.assertEqual(sorted(first), sorted(second))

    def test_transitions_and_markers_follow_the_storyboard(self):
        self.render_shot(0, "shot_a.mp4", duration=8.0)
        shot = self.project["shots"][1]
        shot["transition"] = "dissolve"
        self.render_shot(1, "shot_b.mp4", duration=4.0)
        result = self.build()
        track = next(t for t in result["editor_project"]["tracks"] if t["id"] == "video-main")
        self.assertEqual(track["items"][1]["transition_out"], "dissolve")
        shot_markers = [m for m in result["editor_project"]["markers"] if m["id"].startswith("mark-")]
        self.assertEqual(len(shot_markers), 2)

    def test_audio_lanes_carry_volume_and_fades(self):
        self.render_shot(0, "shot_a.mp4", duration=8.0)
        self.render_shot(1, "shot_b.mp4", duration=4.0)
        (self.uploads / "tema.wav").write_bytes(b"RIFF")
        upsert_asset(self.project, {
            "name": "tema.wav", "filename": "tema.wav", "role": "source_music_keep",
            "analysis": {"available": True, "status": "ok", "duration": 12.0},
        })
        result = self.build()
        music = next(t for t in result["editor_project"]["tracks"] if t["id"] == "audio-music")
        self.assertEqual(music["items"][0]["volume"], film_assembly._db_to_gain(-6.0))
        self.assertGreater(music["items"][0]["fade_out"], 0.0)
        self.assertIn("Music", result["report"]["audio_tracks"])

    def test_subtitles_and_title_cards_are_optional(self):
        self.render_shot(0, "shot_a.mp4", duration=8.0)
        self.render_shot(1, "shot_b.mp4", duration=4.0)
        plain = self.build(include_subtitles=False, include_titles=False)
        self.assertNotIn("titles-main", [t["id"] for t in plain["editor_project"]["tracks"]])

        titled = self.build(include_subtitles=True, credits_text="Produced with Maestro")
        titles = next(t for t in titled["editor_project"]["tracks"] if t["id"] == "titles-main")
        ids = [item["id"] for item in titles["items"]]
        self.assertIn("title-card", ids)
        self.assertIn("credit-card", ids)
        self.assertIn("sub-0001", ids)
        subtitle = next(item for item in titles["items"] if item["id"] == "sub-0001")
        self.assertIn("No more waiting.", subtitle["text"])

    def test_assembly_reports_music_sync_availability_honestly(self):
        self.render_shot(0, "shot_a.mp4")
        report = self.build()["report"]
        self.assertFalse(report["music_sync_available"])
        self.assertTrue(report["music_sync_reason"])

    def test_only_approved_mode_filters_the_cut(self):
        self.render_shot(0, "shot_a.mp4", duration=8.0)
        (self.save_root / "shot_b.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
        add_take(self.project, self.project["shots"][1]["id"], {"filename": "shot_b.mp4"})
        result = self.build(only_approved=True)
        self.assertEqual(result["report"]["shots_in_timeline"], 1)
        self.assertEqual(result["report"]["shot_ids"], [self.project["shots"][0]["id"]])

    def test_canvas_and_totals_are_reported(self):
        self.render_shot(0, "shot_a.mp4", duration=8.0)
        self.render_shot(1, "shot_b.mp4", duration=4.0)
        report = self.build()["report"]
        self.assertAlmostEqual(report["duration_sec"], 12.0)
        self.assertEqual(report["canvas"]["width"], 1920)

    def test_missing_audio_file_is_reported(self):
        self.render_shot(0, "shot_a.mp4")
        upsert_asset(self.project, {"name": "fantasma.wav", "filename": "fantasma.wav", "role": "soundtrack"})
        result = self.build()
        reasons = " ".join(entry["reason"] for entry in result["report"]["shots_missing"])
        self.assertIn("fantasma.wav", reasons)


class RenderGraphTest(AssemblyTestBase):
    """The assembled project must compile into a real FFmpeg render graph."""

    def compile(self, plan):
        from services.editor_projects import compile_editor_render

        return compile_editor_render(
            plan["editor_project"],
            save_root=str(self.save_root),
            workspace="default",
            uploads_root=str(self.uploads),
            filter_script_path=str(self.root / "filters.txt"),
            output_path=str(self.root / "final.mp4"),
        )

    def test_shots_and_music_compile_into_one_ffmpeg_command(self):
        self.render_shot(0, "shot_a.mp4", duration=8.0)
        self.render_shot(1, "shot_b.mp4", duration=4.0)
        (self.uploads / "tema.wav").write_bytes(b"RIFF")
        upsert_asset(self.project, {
            "name": "tema.wav", "filename": "tema.wav", "role": "source_music_keep",
            "analysis": {"available": True, "status": "ok", "duration": 12.0},
        })
        plan = self.build(include_subtitles=True)
        command, duration = self.compile(plan)
        self.assertIsInstance(command, list)
        self.assertEqual(command[0], "ffmpeg")
        joined = " ".join(command)
        self.assertIn("shot_a.mp4", joined)
        self.assertIn("shot_b.mp4", joined)
        self.assertIn("tema.wav", joined)
        self.assertIn(str(self.root / "final.mp4"), joined)
        self.assertAlmostEqual(duration, 12.0)

    def test_a_shot_without_a_file_is_never_referenced_by_the_render(self):
        self.render_shot(0, "shot_a.mp4", duration=8.0)
        (self.save_root / "shot_b.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
        add_take(self.project, self.project["shots"][1]["id"], {"filename": "shot_b.mp4"})
        (self.save_root / "shot_b.mp4").unlink()
        plan = self.build()
        command, _ = self.compile(plan)
        self.assertIn("shot_a.mp4", " ".join(command))
        self.assertNotIn("shot_b.mp4", " ".join(command))

    def test_an_assembled_empty_cut_is_refused_by_the_renderer(self):
        from services.editor_projects import EditorProjectError

        with self.assertRaises(EditorProjectError):
            self.compile({"editor_project": self.build()["editor_project"]})


class ReadinessTest(AssemblyTestBase):
    def test_empty_storyboard_blocks_assembly(self):
        project = create_film_project(name="Empty")
        issues = film_assembly.assembly_readiness(project)
        self.assertEqual([issue["code"] for issue in issues], ["no_shots"])

    def test_unrendered_storyboard_blocks_assembly(self):
        issues = film_assembly.assembly_readiness(self.project)
        codes = [issue["code"] for issue in issues]
        self.assertIn("no_renders", codes)
        self.assertEqual(
            next(issue for issue in issues if issue["code"] == "no_renders")["severity"],
            "blocking",
        )

    def test_partial_renders_warn_but_do_not_block(self):
        self.render_shot(0, "shot_a.mp4")
        issues = film_assembly.assembly_readiness(
            self.project, save_root=str(self.save_root), uploads_root=str(self.uploads),
        )
        codes = [issue["code"] for issue in issues]
        self.assertIn("partial_renders", codes)
        self.assertNotIn("no_shots", codes)

    def test_missing_ffmpeg_blocks_the_final_render(self):
        self.render_shot(0, "shot_a.mp4")
        self.render_shot(1, "shot_b.mp4")
        issues = film_assembly.assembly_readiness(
            self.project, save_root=str(self.save_root), uploads_root=str(self.uploads),
            ffmpeg_available=False,
        )
        self.assertIn("ffmpeg_missing", [issue["code"] for issue in issues])

    def test_edit_fraction_tracks_progress(self):
        self.assertEqual(film_assembly.edit_fraction(self.project), 0.0)
        self.render_shot(0, "shot_a.mp4")
        self.assertEqual(film_assembly.edit_fraction(self.project), 0.5)


if __name__ == "__main__":
    unittest.main()

"""Asset intake: classification, honest analysis, conversion planning, registration."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import film_assets
from services.film_projects import FilmProjectError, create_film_project


class ClassificationTest(unittest.TestCase):
    def test_kind_is_taken_from_the_extension(self):
        for filename, kind in (
            ("hero.png", "image"),
            ("shot_01.mp4", "video"),
            ("theme.wav", "audio"),
            ("guion.txt", "document"),
            ("archive.zip", "other"),
        ):
            self.assertEqual(film_assets.classify_asset(filename)["kind"], kind, filename)

    def test_name_hints_suggest_a_specific_creative_role(self):
        self.assertEqual(film_assets.classify_asset("movie_soundtrack.wav")["role"], "soundtrack")
        self.assertEqual(film_assets.classify_asset("hero_voice.wav")["role"], "character_voice")
        self.assertEqual(film_assets.classify_asset("rain_ambience.wav")["role"], "ambience")
        self.assertEqual(film_assets.classify_asset("costume_ref.png")["role"], "wardrobe")
        self.assertEqual(film_assets.classify_asset("city_location.jpg")["role"], "location_reference")
        self.assertEqual(film_assets.classify_asset("guion_final.txt")["role"], "script_document")
        self.assertEqual(film_assets.classify_asset("raw_footage.mov")["role"], "source_video_edit")

    def test_role_suggestions_stay_advisory_and_explain_themselves(self):
        result = film_assets.classify_asset("hero_voice.wav")
        self.assertLess(result["role_confidence"], 1.0)
        self.assertIn("voice", result["role_reason"])

    def test_a_locked_role_is_reported_as_user_confirmed(self):
        result = film_assets.classify_asset("theme.wav", role_locked="dialogue_audio")
        self.assertEqual(result["role"], "dialogue_audio")
        self.assertEqual(result["role_confidence"], 1.0)
        self.assertIn("user", result["role_reason"].lower())

    def test_roles_offered_match_the_media_kind(self):
        self.assertIn("character_voice", film_assets.classify_asset("a.wav")["available_roles"])
        self.assertNotIn("character_voice", film_assets.classify_asset("a.png")["available_roles"])

    def test_unsupported_extension_lowers_confidence_and_says_so(self):
        result = film_assets.classify_asset("referencia.heic")
        self.assertIn("conversion", result["role_reason"])
        self.assertLessEqual(result["role_confidence"], 0.3)


class FormatSupportTest(unittest.TestCase):
    def test_supported_formats_need_no_conversion(self):
        self.assertTrue(film_assets.format_support("clip.mp4")["supported"])
        self.assertIsNone(film_assets.conversion_plan("clip.mp4"))

    def test_unsupported_formats_report_a_reason_and_a_local_command(self):
        support = film_assets.format_support("scan.heic")
        self.assertFalse(support["supported"])
        self.assertIn("heic", support["message"].lower())
        plan = film_assets.conversion_plan("scan.heic")
        self.assertEqual(plan["target_format"], "png")
        self.assertTrue(plan["command"].startswith("ffmpeg -i"))

    def test_audio_and_video_conversions_are_planned_not_run(self):
        self.assertEqual(film_assets.conversion_plan("song.wma")["target_format"], "wav")
        self.assertEqual(film_assets.conversion_plan("tape.flv")["target_format"], "mp4")
        self.assertIn("pcm_s16le", film_assets.conversion_plan("song.wma")["command"])

    def test_documents_that_are_not_text_explain_the_manual_step(self):
        plan = film_assets.conversion_plan("guion.docx")
        self.assertEqual(plan["target_format"], "txt")
        self.assertIn("plain text", plan["reason"])


class AnalysisHonestyTest(unittest.TestCase):
    """Without local tools the analysis must refuse to invent numbers."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.media = self.root / "clip.mp4"
        self.media.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    def _without_tools(self):
        return (
            mock.patch.object(film_assets.shutil, "which", return_value=None),
            mock.patch.object(film_assets, "_module_available", return_value=False),
        )

    def test_media_without_ffprobe_reports_unavailable_with_a_reason(self):
        which, module = self._without_tools()
        with which, module:
            analysis = film_assets.analyze_asset(str(self.media))
        self.assertFalse(analysis["available"])
        self.assertEqual(analysis["status"], "unavailable")
        self.assertEqual(analysis["duration"], 0.0)
        self.assertEqual(analysis["width"], 0)
        self.assertTrue(any("ffprobe" in note for note in analysis["limitations"]))

    def test_media_with_ffprobe_maps_real_probe_fields(self):
        fake_probe = {
            "name": "clip.mp4", "type": "video", "duration": 12.5, "width": 1920,
            "height": 1080, "fps": 24.0, "has_audio": True, "audio_channels": 2,
            "audio_sample_rate": 48000, "size": 4096,
        }
        with mock.patch.object(film_assets.shutil, "which", return_value="/usr/bin/ffprobe"), \
                mock.patch("services.editor_projects.probe_media", return_value=fake_probe):
            analysis = film_assets.analyze_asset(str(self.media))
        self.assertTrue(analysis["available"])
        self.assertEqual(analysis["width"], 1920)
        self.assertAlmostEqual(analysis["duration"], 12.5)
        self.assertTrue(analysis["has_audio"])
        self.assertEqual(analysis["audio_sample_rate"], 48000)

    def test_ffprobe_failure_is_reported_not_hidden(self):
        with mock.patch.object(film_assets.shutil, "which", return_value="/usr/bin/ffprobe"), \
                mock.patch("services.editor_projects.probe_media", side_effect=OSError("broken pipe")):
            analysis = film_assets.analyze_asset(str(self.media))
        self.assertFalse(analysis["available"])
        self.assertTrue(any("broken pipe" in note for note in analysis["limitations"]))

    def test_text_documents_are_actually_read(self):
        script = self.root / "guion.txt"
        script.write_text("ESCENA 1\nLlueve sobre la ciudad.\n", encoding="utf-8")
        analysis = film_assets.analyze_asset(str(script))
        self.assertTrue(analysis["available"])
        self.assertIn("Llueve sobre la ciudad", analysis["text_excerpt"])
        self.assertEqual(analysis["document_chars"], len("ESCENA 1\nLlueve sobre la ciudad.\n"))

    def test_scanned_pdf_without_text_is_reported_as_partial(self):
        pdf = self.root / "guion.pdf"
        pdf.write_bytes(b"%PDF-1.4\n%scan")
        with mock.patch.object(film_assets, "_module_available", return_value=False):
            analysis = film_assets.analyze_asset(str(pdf))
        self.assertFalse(analysis["available"])
        self.assertTrue(any("pypdf" in note for note in analysis["limitations"]))

    def test_missing_file_is_an_error_not_an_empty_analysis(self):
        with self.assertRaises(FilmProjectError):
            film_assets.analyze_asset(str(self.root / "missing.mp4"))

    def test_deep_audio_without_librosa_says_the_feature_is_skipped(self):
        track = self.root / "theme.wav"
        track.write_bytes(b"RIFF")
        fake_probe = {"duration": 30.0, "width": 0, "height": 0, "fps": 0.0,
                      "has_audio": True, "size": 4, "type": "audio",
                      "audio_channels": 2, "audio_sample_rate": 44100}
        with mock.patch.object(film_assets.shutil, "which", return_value="/usr/bin/ffprobe"), \
                mock.patch("services.editor_projects.probe_media", return_value=fake_probe), \
                mock.patch.object(film_assets, "_module_available", return_value=False):
            analysis = film_assets.analyze_asset(str(track), kind="audio", deep_audio=True)
        self.assertTrue(analysis["available"])
        self.assertEqual(analysis["bpm"], 0.0)
        self.assertTrue(any("librosa" in note for note in analysis["limitations"]))

    def test_missing_requirements_explain_what_is_lost(self):
        with mock.patch.object(film_assets.shutil, "which", return_value=None), \
                mock.patch.object(film_assets, "_module_available", return_value=False):
            requirements = film_assets.missing_requirements()
        tools = {entry["tool"] for entry in requirements}
        self.assertEqual(tools, {"ffprobe", "ffmpeg", "Pillow", "pypdf", "librosa"})
        for entry in requirements:
            self.assertTrue(entry["why"])
            self.assertTrue(entry["install"])


class RegisterAssetTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = create_film_project()

    def test_import_records_the_file_even_when_it_cannot_be_analysed(self):
        target = self.root / "clip.mp4"
        target.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        with mock.patch.object(film_assets.shutil, "which", return_value=None), \
                mock.patch.object(film_assets, "_module_available", return_value=False):
            film_assets.register_imported_asset(
                self.project, filename="clip.mp4", absolute_path=str(target), analyze=True,
            )
        asset = next(iter(self.project["assets"].values()))
        self.assertEqual(asset["kind"], "video")
        self.assertEqual(asset["role"], "source_video_edit")
        self.assertFalse(asset["analysis"]["available"])
        self.assertTrue(asset["analysis"]["limitations"])

    def test_import_offers_a_conversion_plan_for_unsupported_files(self):
        film_assets.register_imported_asset(self.project, filename="referencia.heic")
        asset = next(iter(self.project["assets"].values()))
        self.assertFalse(film_assets.format_support(asset["filename"])["supported"])
        self.assertEqual(asset["conversions"][0]["status"], "suggested")
        self.assertEqual(asset["conversions"][0]["target_format"], "png")
        self.assertTrue(asset["notes"])

    def test_import_suggests_the_role_but_leaves_it_correctable(self):
        film_assets.register_imported_asset(self.project, filename="mi_banda_sonora.wav")
        asset = next(iter(self.project["assets"].values()))
        self.assertEqual(asset["role"], "soundtrack")
        self.assertFalse(asset["role_locked"])
        self.assertLess(asset["role_confidence"], 1.0)

    def test_analysis_is_skipped_unless_requested(self):
        film_assets.register_imported_asset(self.project, filename="still.png")
        asset = next(iter(self.project["assets"].values()))
        self.assertEqual(asset["analysis"]["status"], "unknown")
        self.assertFalse(asset["analysis"]["available"])

    def test_conversion_requirement_surfaces_the_message(self):
        self.assertIsNone(film_assets.conversion_requirement("clip.mp4"))
        message = film_assets.conversion_requirement("scan.heic")
        self.assertIn("not readable", message)


if __name__ == "__main__":
    unittest.main()

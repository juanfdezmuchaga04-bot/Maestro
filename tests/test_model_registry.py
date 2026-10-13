"""Model registry: unknown stays unknown, and a claim without evidence fails."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services.model_registry import (  # noqa: E402
    EVIDENCE_REQUIRED_FIELDS,
    HuggingFaceSource,
    ModelRecord,
    ModelRegistry,
    ModelRegistryError,
    Resource,
    from_json,
    to_json,
)


CHECKED = "2026-10-13T00:00:00Z"


def record(**changes):
    """A minimal, honest record: only the identity fields are known."""
    fields = {
        "model_id": "huggingface:acme/wonder-video",
        "name": "acme/wonder-video",
        "source": "huggingface",
        "repo_id": "acme/wonder-video",
        "url": "https://huggingface.co/acme/wonder-video",
    }
    fields.update(changes)
    return ModelRecord(**fields)


def evidence(field_name, source="https://huggingface.co/acme/wonder-video", **extra):
    from services.model_registry import Evidence

    extra.setdefault("checked_at", CHECKED)
    return Evidence(field=field_name, source=source, **extra)


class EvidenceTest(unittest.TestCase):
    def test_evidence_needs_a_source_and_a_date(self):
        with self.assertRaises(ModelRegistryError):
            evidence("license", source="")
        with self.assertRaises(ModelRegistryError):
            evidence("license", checked_at="")

    def test_a_non_iso_date_is_refused(self):
        with self.assertRaises(ModelRegistryError):
            evidence("license", checked_at="last tuesday")


class RecordTest(unittest.TestCase):
    def test_identity_fields_are_required(self):
        for attribute in ("model_id", "name", "source", "repo_id", "url"):
            with self.subTest(attribute=attribute):
                with self.assertRaises(ModelRegistryError):
                    record(**{attribute: ""})

    def test_an_unknown_source_or_task_is_refused(self):
        with self.assertRaises(ModelRegistryError):
            record(source="a-friend-told-me")
        with self.assertRaises(ModelRegistryError):
            record(tasks=("draw_something",))

    def test_the_shared_vocabulary_is_enforced(self):
        with self.assertRaises(ModelRegistryError):
            record(formats=("exe",))
        with self.assertRaises(ModelRegistryError):
            record(precisions=("float128",))
        with self.assertRaises(ModelRegistryError):
            record(accelerators=("quantum",))
        with self.assertRaises(ModelRegistryError):
            record(engine="wishful-thinking")

    def test_unknown_is_the_default_and_stays_unknown(self):
        item = record()
        self.assertEqual(item.tasks, ())
        self.assertEqual(item.accelerators, ())
        self.assertEqual(item.engine, "")
        self.assertEqual(item.license, "")
        self.assertIsNone(item.requires_auth)
        self.assertIsNone(item.download_available)
        self.assertIsNone(item.remote_inference_available)
        for name in ("download_bytes", "disk_bytes", "ram_bytes", "vram_bytes"):
            self.assertEqual(item.resource(name), Resource())
        self.assertEqual(item.last_checked_at, "")

    def test_a_positive_claim_without_evidence_is_refused(self):
        for field_name in ("download_available", "requires_auth", "accelerators",
                           "formats", "remote_inference_available", "precisions",
                           "vram_bytes", "ram_bytes", "download_bytes", "engine"):
            with self.subTest(field=field_name):
                if field_name in ("download_bytes", "ram_bytes", "vram_bytes"):
                    value = Resource(value=10, basis="documented")
                elif field_name == "accelerators":
                    value = ("cpu",)
                elif field_name == "formats":
                    value = ("safetensors",)
                elif field_name == "precisions":
                    value = ("int8",)
                elif field_name == "engine":
                    value = "wangp"
                else:
                    value = True
                with self.assertRaises(ModelRegistryError):
                    record(**{field_name: value})

    def test_the_same_claim_with_evidence_is_accepted(self):
        item = record(
            tasks=("text_to_video",),
            accelerators=("cuda",),
            download_available=True,
            vram_bytes=Resource(value=8 * 1024**3, basis="documented"),
            evidence=(evidence("accelerators"), evidence("download_available"),
                      evidence("vram_bytes")),
        )
        self.assertTrue(item.has_evidence("accelerators"))
        self.assertEqual(item.evidence_for("vram_bytes")[0].checked_at, CHECKED)

    def test_an_estimate_is_the_records_own_arithmetic(self):
        item = record(vram_bytes=Resource(value=4 * 1024**3, basis="estimated"))
        self.assertEqual(item.vram_bytes.basis, "estimated")

    def test_resource_rules(self):
        with self.assertRaises(ModelRegistryError):
            Resource(value=1, basis="unknown")
        with self.assertRaises(ModelRegistryError):
            Resource(value=-1, basis="measured")
        with self.assertRaises(ModelRegistryError):
            Resource(value=1.5, basis="measured")
        with self.assertRaises(ModelRegistryError):
            Resource(basis="measured")
        with self.assertRaises(ModelRegistryError):
            Resource(value=1, basis="probably")

    def test_a_licence_without_evidence_is_refused(self):
        with self.assertRaises(ModelRegistryError):
            record(license="apache-2.0")
        item = record(license="apache-2.0", evidence=(evidence("license"),))
        self.assertEqual(item.license, "apache-2.0")

    def test_records_are_immutable_and_gain_evidence_by_copy(self):
        item = record()
        with self.assertRaises(Exception):
            item.name = "other"
        updated = item.with_evidence(evidence("formats"))
        self.assertEqual(item.evidence, ())
        self.assertEqual(len(updated.evidence), 1)

    def test_last_checked_at_is_the_newest_evidence_date(self):
        older = evidence("formats", checked_at="2026-01-01T00:00:00Z")
        newer = evidence("license", checked_at="2026-10-13T00:00:00Z")
        self.assertEqual(record(evidence=(older, newer)).last_checked_at, "2026-10-13")

    def test_round_trip_through_dict_and_json(self):
        item = record(
            tasks=("text_to_video",),
            accelerators=("cuda",),
            download_available=True,
            vram_bytes=Resource(value=24 * 1024**3, basis="documented",
                                source="https://huggingface.co/acme/wonder-video"),
            evidence=(evidence("accelerators"), evidence("download_available"),
                      evidence("vram_bytes")),
        )
        self.assertEqual(ModelRecord.from_dict(item.to_dict()), item)
        self.assertEqual(from_json(to_json([item])), [item])

    def test_a_bad_json_payload_raises_a_named_error(self):
        with self.assertRaises(ModelRegistryError):
            from_json("not json")
        with self.assertRaises(ModelRegistryError):
            from_json(json.dumps({"records": []}))


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.registry = ModelRegistry()
        self.cpu = record(model_id="local:tiny-llm", name="tiny-llm", source="local",
                          repo_id="tiny-llm", url="file:///models/tiny-llm",
                          tasks=("transcription",),
                          accelerators=("cpu",),
                          download_available=True,
                          download_bytes=Resource(value=2 * 1024**3, basis="documented"),
                          evidence=(evidence("accelerators"), evidence("download_available"),
                                    evidence("download_bytes")))
        self.gpu = record(model_id="huggingface:acme/h3-video", name="acme/h3-video",
                          repo_id="acme/h3-video",
                          url="https://huggingface.co/acme/h3-video",
                          tasks=("text_to_video",),
                          accelerators=("cuda",),
                          engine="wangp",
                          download_available=True,
                          download_bytes=Resource(value=40 * 1024**3, basis="documented"),
                          evidence=(evidence("accelerators"), evidence("download_available"),
                                    evidence("download_bytes"), evidence("engine")))
        self.registry.add(self.cpu)
        self.registry.add(self.gpu)

    def test_duplicate_and_missing_ids_are_named(self):
        with self.assertRaises(ModelRegistryError):
            self.registry.add(self.cpu)
        replaced = record(model_id=self.cpu.model_id, name=self.cpu.name,
                          source="local", repo_id=self.cpu.repo_id, url=self.cpu.url)
        self.registry.replace(replaced)
        with self.assertRaises(ModelRegistryError):
            self.registry.replace(record(model_id="local:not-registered",
                                         name="x", source="local", repo_id="x",
                                         url="file:///x"))
        with self.assertRaises(ModelRegistryError):
            self.registry.get("nope")
        with self.assertRaises(ModelRegistryError):
            self.registry.remove("nope")

    def test_find_matches_only_verified_facts(self):
        self.assertEqual([item.model_id for item in self.registry.find(task="text_to_video")],
                         ["huggingface:acme/h3-video"])
        self.assertEqual([item.model_id for item in self.registry.find(accelerator="cpu")],
                         ["local:tiny-llm"])
        # Unknown is not a match: the video model never declared CPU support.
        self.assertEqual(self.registry.find(accelerator="mps"), [])
        self.assertEqual([item.model_id for item in self.registry.find(engine="wangp")],
                         ["huggingface:acme/h3-video"])
        self.assertEqual([item.model_id for item in self.registry.find(source="local")],
                         ["local:tiny-llm"])

    def test_find_respects_a_size_budget_and_never_guesses_a_size(self):
        self.assertEqual(
            [item.model_id for item in self.registry.find(max_download_bytes=4 * 1024**3)],
            ["local:tiny-llm"],
        )
        guessed = record(model_id="huggingface:acme/unknown-size", name="acme/unknown-size",
                         repo_id="acme/unknown-size",
                         url="https://huggingface.co/acme/unknown-size",
                         download_available=True,
                         evidence=(evidence("download_available"),))
        self.registry.add(guessed)
        ids = [item.model_id for item in self.registry.find(max_download_bytes=99 * 1024**3)]
        self.assertNotIn("huggingface:acme/unknown-size", ids)

    def test_find_refuses_a_filter_it_does_not_understand(self):
        with self.assertRaises(ModelRegistryError):
            self.registry.find(task="render_everything")
        with self.assertRaises(ModelRegistryError):
            self.registry.find(accelerator="gpu")
        with self.assertRaises(ModelRegistryError):
            self.registry.find(source="telegram")

    def test_task_coverage_is_reported_for_the_interface(self):
        coverage = self.registry.tasks()
        self.assertEqual(coverage["transcription"], ["local:tiny-llm"])
        self.assertEqual(coverage["text_to_video"], ["huggingface:acme/h3-video"])

    def test_save_and_load_round_trip_and_a_corrupt_file_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "models.json")
            self.registry.save(path)
            reloaded = ModelRegistry.load_file(path)
            self.assertEqual([item.model_id for item in reloaded.list()],
                             [item.model_id for item in self.registry.list()])
            Path(path).write_text("{not json", encoding="utf-8")
            self.assertEqual(len(ModelRegistry.load_file(path)), 0)


class _Response:
    def __init__(self, payload, status=200):
        self.status = status
        self.body = json.dumps(payload).encode("utf-8")

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class HuggingFaceSourceTest(unittest.TestCase):
    def source(self, payload, status=200):
        calls = []

        def transport(method, url, **kwargs):
            calls.append((method, url))
            return _Response(payload, status=status)

        return HuggingFaceSource(transport=transport), calls

    def test_tasks_once_from_declared_tags_only(self):
        adapter, _ = self.source({
            "id": "acme/wonder-video",
            "pipeline_tag": "text-to-video",
            "tags": ["diffusers", "image-to-video"],
            "siblings": [{"rfilename": "model.safetensors"}],
        })
        item = adapter.fetch("acme/wonder-video", checked_at=CHECKED)
        self.assertEqual(item.tasks, ("image_to_video", "text_to_video"))

    def test_a_name_that_sounds_like_a_task_is_not_a_task(self):
        adapter, _ = self.source({
            "id": "acme/awesome-text-to-video-9000",
            "tags": ["diffusers"],
            "siblings": [{"rfilename": "model.safetensors"}],
        })
        item = adapter.fetch("acme/awesome-text-to-video-9000", checked_at=CHECKED)
        self.assertEqual(item.tasks, ())

    def test_a_repository_never_claims_remote_inference(self):
        adapter, _ = self.source({
            "id": "acme/wonder-video",
            "pipeline_tag": "text-to-video",
            "siblings": [{"rfilename": "model.safetensors"}],
        })
        item = adapter.fetch("acme/wonder-video", checked_at=CHECKED)
        self.assertIsNone(item.remote_inference_available)
        self.assertFalse(item.has_evidence("remote_inference_available"))

    def test_formats_precisions_and_gating_come_from_the_report(self):
        adapter, _ = self.source({
            "id": "acme/h3-quant",
            "pipeline_tag": "text-to-video",
            "gated": True,
            "cardData": {"license": "apache-2.0"},
            "downloads": 1200,
            "likes": 42,
            "lastModified": "2026-09-01T10:00:00.000Z",
            "siblings": [
                {"rfilename": "model-fp8.safetensors"},
                {"rfilename": "model-int8.safetensors"},
                {"rfilename": "README.md"},
            ],
        })
        item = adapter.fetch("acme/h3-quant", checked_at=CHECKED)
        self.assertEqual(item.formats, ("safetensors",))
        self.assertEqual(item.precisions, ("fp8", "int8"))
        self.assertEqual(item.weight_files, ("model-fp8.safetensors", "model-int8.safetensors"))
        self.assertTrue(item.requires_auth)
        self.assertEqual(item.license, "apache-2.0")
        self.assertEqual(item.maintenance["downloads"], 1200)
        for field_name in ("formats", "precisions", "license", "requires_auth",
                           "download_available"):
            self.assertTrue(item.has_evidence(field_name), field_name)
        self.assertEqual(item.last_checked_at, "2026-10-13")

    def test_a_hub_error_is_reported_with_its_status(self):
        adapter, _ = self.source({"error": "not found"}, status=404)
        with self.assertRaises(ModelRegistryError) as raised:
            adapter.fetch("acme/missing", checked_at=CHECKED)
        self.assertIn("404", str(raised.exception))

    def test_every_evidence_required_field_can_be_satisfied_by_the_adapter(self):
        # The list is the contract: a field that requires evidence must have a
        # way to obtain it, or the rule above would be unsatisfiable.
        self.assertIn("download_available", EVIDENCE_REQUIRED_FIELDS)
        self.assertIn("accelerators", EVIDENCE_REQUIRED_FIELDS)


if __name__ == "__main__":
    unittest.main(verbosity=2)

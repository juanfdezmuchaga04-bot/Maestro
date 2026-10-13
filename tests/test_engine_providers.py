"""Engine registry: capability resolution, local-first preference, honesty."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services.engine_providers import (  # noqa: E402
    CAPABILITIES,
    EngineProvider,
    EngineProviderError,
    EngineRegistry,
    build_registry,
    capability_spec,
    describe_capabilities,
)


class CatalogueTest(unittest.TestCase):
    def test_every_capability_has_a_spec(self):
        self.assertTrue(CAPABILITIES)
        for name in CAPABILITIES:
            spec = capability_spec(name)
            self.assertIn(spec["task"], {"image", "video", "audio", "text", "processing"})
            self.assertIn(spec["media"], {"image", "video", "audio", "text", "any"})
        self.assertEqual(len(describe_capabilities()), len(CAPABILITIES))

    def test_unknown_capability_is_refused_with_the_known_list(self):
        with self.assertRaises(EngineProviderError) as ctx:
            capability_spec("teleport")
        self.assertIn("image_generation", str(ctx.exception))


class ProviderTest(unittest.TestCase):
    def test_kind_capabilities_and_key_rules_are_enforced(self):
        with self.assertRaises(EngineProviderError):
            EngineProvider("x", "X", "mystery", ("image_generation",))
        with self.assertRaises(EngineProviderError):
            EngineProvider("x", "X", "local_engine", ("nope",))
        with self.assertRaises(EngineProviderError):
            EngineProvider("x", "X", "comfyui", ("image_generation",), requires_api_key=True)
        with self.assertRaises(EngineProviderError):
            EngineProvider("x", "X", "local_engine", ("image_generation", "image_generation"))

    def test_a_provider_may_implement_only_some_capabilities(self):
        provider = EngineProvider("p", "P", "local_engine", ("image_generation",))
        self.assertTrue(provider.supports("image_generation"))
        self.assertFalse(provider.supports("voice_generation"))
        self.assertNotIn("voice_generation", provider.describe()["capabilities"])


class RegistryTest(unittest.TestCase):
    """A local engine, a remote node, and a paid API with its own capability."""

    def setUp(self):
        self.registry = EngineRegistry()
        self.registry.register(EngineProvider(
            "local:maestro", "Local", "local_engine",
            ("image_generation", "auxiliary_processing")))
        self.registry.register(EngineProvider(
            "node:desktop", "Desktop", "compute_node",
            ("image_generation", "text_to_video"), local=False, node_id="desktop"))
        # ``image_to_video`` exists *only* behind the paid API: that is how we
        # prove commercial adapters are opt-in and never silently required.
        self.registry.register(EngineProvider(
            "paid:vendor", "Vendor API", "commercial_api",
            ("image_to_video",), local=False, requires_api_key=True))

    def test_duplicate_registration_is_refused(self):
        with self.assertRaises(EngineProviderError):
            self.registry.register(EngineProvider("local:maestro", "Dup", "local_engine", ()))

    def test_capability_map_lists_every_provider_that_implements_it(self):
        mapping = self.registry.capabilities(include_commercial=False)
        self.assertEqual(mapping["image_generation"], ["local:maestro", "node:desktop"])
        self.assertEqual(mapping["voice_generation"], [])
        self.assertEqual(mapping["image_to_video"], [])

    def test_local_engine_wins_over_a_remote_node(self):
        self.assertEqual(
            self.registry.resolve("image_generation").provider_id, "local:maestro")

    def test_commercial_provider_is_never_chosen_without_opt_in(self):
        with self.assertRaises(EngineProviderError) as ctx:
            self.registry.resolve("image_to_video")
        message = str(ctx.exception)
        self.assertIn("image to video", message)
        self.assertIn("no local provider implements it", message)
        self.assertIn("none is required or configured", message)
        chosen = self.registry.resolve("image_to_video", allow_commercial=True)
        self.assertEqual(chosen.provider_id, "paid:vendor")

    def test_a_disconnected_node_stops_receiving_work(self):
        with self.assertRaises(EngineProviderError) as ctx:
            self.registry.resolve("text_to_video", node_ids=["local"])
        self.assertIn("No available engine", str(ctx.exception))
        chosen = self.registry.resolve("text_to_video", node_ids=["local", "desktop"])
        self.assertEqual(chosen.provider_id, "node:desktop")

    def test_explicit_preference_is_honoured_or_explained(self):
        chosen = self.registry.resolve("image_generation", preferred="node:desktop")
        self.assertEqual(chosen.provider_id, "node:desktop")

        with self.assertRaises(EngineProviderError) as ctx:
            self.registry.resolve("image_generation", preferred="paid:vendor")
        self.assertIn("does not implement image_generation", str(ctx.exception))

        with self.assertRaises(EngineProviderError) as ctx:
            self.registry.resolve("image_to_video", preferred="paid:vendor")
        self.assertIn("local-only mode", str(ctx.exception))

        with self.assertRaises(EngineProviderError) as ctx:
            self.registry.resolve("image_generation", preferred="ghost")
        self.assertIn("Unknown provider", str(ctx.exception))

    def test_unavailable_capability_raises_instead_of_falling_back(self):
        with self.assertRaises(EngineProviderError) as ctx:
            self.registry.resolve("voice_generation")
        self.assertIn("no local provider", str(ctx.exception))

    def test_availability_report_marks_what_is_missing(self):
        report = {item["capability"]: item for item in self.registry.availability()}
        self.assertTrue(report["image_generation"]["available"])
        self.assertEqual(report["image_generation"]["providers"],
                         ["local:maestro", "node:desktop"])
        self.assertFalse(report["voice_generation"]["available"])
        self.assertEqual(report["voice_generation"]["providers"], [])
        self.assertTrue(report["image_generation"]["needs_gpu"])

    def test_unregistering_a_node_removes_only_its_provider(self):
        removed = self.registry.unregister_kind("compute_node")
        self.assertEqual(removed, ["node:desktop"])
        with self.assertRaises(EngineProviderError):
            self.registry.resolve("text_to_video")
        self.assertEqual(
            self.registry.resolve("image_generation").provider_id, "local:maestro")


class BuildRegistryTest(unittest.TestCase):
    def test_local_engine_is_registered_even_with_no_capabilities(self):
        registry = build_registry()
        described = registry.list()
        self.assertEqual(len(described), 1)
        self.assertEqual(described[0]["provider_id"], "local:maestro")
        self.assertEqual(described[0]["capabilities"], [])
        self.assertTrue(registry.status()["local_only"])

    def test_registry_is_composed_from_measured_state_only(self):
        registry = build_registry(
            local_capabilities=["image_generation"],
            local_models=["local-model"],
            comfy_servers=[{
                "base_url": "http://192.168.1.10:8188",
                "capabilities": ["video_extension"],
                "object_info_nodes": 42,
                "templates": ["image_txt2img"],
            }],
            nodes=[{
                "node_id": "desktop",
                "label": "Editing PC",
                "capabilities": ["text_to_video"],
                "online": True,
                "models": ["wan"],
            }],
        )
        ids = [p["provider_id"] for p in registry.list()]
        self.assertEqual(ids, [
            "local:maestro",
            "node:desktop",
            "comfyui:http://192.168.1.10:8188",
        ])
        self.assertEqual(registry.resolve("text_to_video").provider_id, "node:desktop")
        self.assertEqual(
            registry.resolve("video_extension").provider_id,
            "comfyui:http://192.168.1.10:8188",
        )
        comfy = registry.get("comfyui:http://192.168.1.10:8188").describe()
        self.assertEqual(comfy["detail"]["object_info_nodes"], 42)
        self.assertEqual(comfy["detail"]["templates"], ["image_txt2img"])

    def test_a_comfy_server_with_no_mapped_capability_claims_only_generic_work(self):
        registry = build_registry(
            comfy_servers=[{"base_url": "http://10.0.0.9:8188", "capabilities": []}])
        provider = registry.get("comfyui:http://10.0.0.9:8188")
        self.assertEqual(provider.capabilities, ("auxiliary_processing",))
        self.assertFalse(provider.supports("image_generation"))

    def test_entries_without_an_address_or_node_id_are_skipped_not_guessed(self):
        registry = build_registry(
            comfy_servers=[{"capabilities": ["image_generation"]}],
            nodes=[{"label": "no id", "capabilities": ["image_generation"]}],
        )
        self.assertEqual([p["provider_id"] for p in registry.list()], ["local:maestro"])

    def test_status_reports_local_only_mode_and_provider_detail(self):
        registry = build_registry(local_capabilities=["image_generation"])
        status = registry.status()
        self.assertIn("providers", status)
        self.assertIn("availability", status)
        self.assertTrue(status["local_only"])
        self.assertEqual(status["providers"][0]["kind"], "local_engine")

    def test_registering_a_commercial_adapter_flips_local_only_off(self):
        registry = build_registry(
            commercial=[{
                "provider_id": "paid:vendor",
                "capabilities": ["image_to_video"],
                "requires_api_key": True,
            }])
        self.assertFalse(registry.status()["local_only"])
        # It is still not used by default.
        with self.assertRaises(EngineProviderError):
            registry.resolve("image_to_video")


if __name__ == "__main__":
    unittest.main()

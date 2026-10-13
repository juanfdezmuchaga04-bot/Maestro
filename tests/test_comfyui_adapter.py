"""ComfyUI adapter against a stub server speaking the real ComfyUI HTTP API.

The stub implements the documented endpoints (/system_stats, /object_info,
/prompt, /history/{id}, /queue, /interrupt) so the adapter is exercised over a
real socket with real JSON.  No GPU, no model, no ComfyUI install is required —
and equally, nothing here claims a generation happened: the stub only proves the
protocol handling.
"""
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services.comfyui_adapter import (  # noqa: E402
    ComfyUIAdapter,
    ComfyUIError,
    bind_template,
    default_templates_dir,
    list_templates,
    load_template,
    template_supported,
    validate_base_url,
)

NODE_CLASSES = [
    "KSampler",
    "CheckpointLoaderSimple",
    "EmptyLatentImage",
    "CLIPTextEncode",
    "VAEDecode",
    "SaveImage",
]

DEFAULT_GRAPH = {
    "3": {"class_type": "KSampler", "inputs": {"seed": 0, "steps": 20, "model": ["4", 0]}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "x.safetensors"}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "p", "images": ["8", 0]}},
}
DEFAULT_MANIFEST = {
    "template_id": "t",
    "version": "1",
    "capabilities": ["image_generation"],
    "inputs": {"prompt": {"node": "3", "field": "prompt", "required": True}},
    "outputs": {"images": {"node": "9", "kind": "image"}},
}


class StubComfy(BaseHTTPRequestHandler):
    """A faithful-enough ComfyUI: rejects graphs whose nodes it does not have."""

    node_classes = list(NODE_CLASSES)
    submitted: list = []
    interrupts = 0
    deleted: list = []
    history_ready = True

    def log_message(self, *args):  # silence the test output
        return

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/system_stats":
            return self._json({
                "system": {"comfyui_version": "0.3.40", "python_version": "3.11.9"},
                "devices": [{"name": "cuda:0", "type": "cuda", "vram_total": 24, "vram_free": 20}],
            })
        if path == "/object_info":
            info = {cls: {"input": {"required": {}}} for cls in type(self).node_classes}
            info["CheckpointLoaderSimple"] = {
                "input": {"required": {"ckpt_name": [["a.safetensors", "b.safetensors"]]}}
            }
            return self._json(info)
        if path == "/queue":
            return self._json({"queue_running": [], "queue_pending": []})
        if path.startswith("/history/"):
            prompt_id = path.rsplit("/", 1)[-1]
            if not type(self).history_ready:
                return self._json({})
            return self._json({
                prompt_id: {
                    "status": {"status_str": "success", "completed": True, "messages": []},
                    "outputs": {"9": {"images": [{"filename": "p_00001_.png", "subfolder": "", "type": "output"}]}},
                }
            })
        return self._json({"error": "not found"}, code=404)

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if path == "/prompt":
            graph = payload.get("prompt") or {}
            missing = [n.get("class_type") for n in graph.values()
                       if n.get("class_type") not in type(self).node_classes]
            if missing:
                return self._json({"error": "missing nodes", "node_errors": {"0": missing}}, code=400)
            type(self).submitted.append(payload)
            return self._json({"prompt_id": f"pid{len(type(self).submitted)}", "number": 1})
        if path == "/interrupt":
            type(self).interrupts += 1
            return self._json({})
        if path == "/queue":
            type(self).deleted.extend(payload.get("delete") or [])
            return self._json({})
        return self._json({"error": "not found"}, code=404)


class AdapterTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubComfy)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        StubComfy.node_classes = list(NODE_CLASSES)
        StubComfy.submitted = []
        StubComfy.interrupts = 0
        StubComfy.deleted = []
        StubComfy.history_ready = True


class UrlTest(AdapterTestCase):
    def test_scheme_and_credentials_are_refused(self):
        self.assertEqual(validate_base_url("http://127.0.0.1:8188/"), "http://127.0.0.1:8188")
        for bad in ("ftp://x", "file:///tmp", "http://u:p@h:1", "", "notaurl"):
            with self.assertRaises(ComfyUIError):
                validate_base_url(bad)


class DiscoverTest(AdapterTestCase):
    def test_connection_reports_measured_devices(self):
        client = ComfyUIAdapter.__new__(ComfyUIAdapter)
        from services.comfyui_adapter import ComfyUIClient
        client.client = ComfyUIClient(self.base_url)
        health = client.client.check_connection()
        self.assertTrue(health["reachable"])
        self.assertEqual(health["comfyui_version"], "0.3.40")
        self.assertEqual(health["devices"][0]["type"], "cuda")

    def test_capabilities_come_from_node_classes_not_the_manifest(self):
        adapter = ComfyUIAdapter(self.base_url, templates_dir=default_templates_dir())
        described = adapter.describe()
        self.assertIn("image_generation", described["capabilities"])
        self.assertEqual(described["object_info_nodes"], len(NODE_CLASSES))
        self.assertEqual(described["models"], ["a.safetensors", "b.safetensors"])
        # This stub exposes the image classes only, so the video template must be
        # refused — with the exact classes the server is missing, not silently.
        self.assertNotIn("text_to_video", described["capabilities"])
        self.assertEqual(
            [t["template_id"] for t in described["unsupported_templates"]],
            ["wan22_ti2v_5b_txt2video"],
        )
        missing = described["unsupported_templates"][0]["missing_node_classes"]
        for expected in ("UNETLoader", "Wan22ImageToVideoLatent", "CreateVideo", "SaveVideo"):
            with self.subTest(node=expected):
                self.assertIn(expected, missing)

    def test_a_server_without_the_nodes_advertises_nothing(self):
        StubComfy.node_classes = ["SomeOtherNode"]
        adapter = ComfyUIAdapter(self.base_url, templates_dir=default_templates_dir())
        described = adapter.describe()
        self.assertEqual(described["capabilities"], [])
        unsupported = {t["template_id"]: t for t in described["unsupported_templates"]}
        self.assertEqual(sorted(unsupported), ["image_txt2img", "wan22_ti2v_5b_txt2video"])
        for template_id, entry in unsupported.items():
            with self.subTest(template=template_id):
                self.assertTrue(entry["missing_node_classes"])
        self.assertIn("KSampler", unsupported["image_txt2img"]["missing_node_classes"])

    def test_unreachable_server_raises_instead_of_pretending(self):
        adapter = ComfyUIAdapter("http://127.0.0.1:1", templates_dir=default_templates_dir())
        with self.assertRaises(ComfyUIError):
            adapter.describe()

    def test_template_supported_reports_the_exact_missing_classes(self):
        template = load_template(default_templates_dir(), "image_txt2img")
        ok, missing = template_supported(template, {"KSampler": {}})
        self.assertFalse(ok)
        self.assertIn("SaveImage", missing)


class BindTest(AdapterTestCase):
    def test_declared_inputs_are_substituted_and_undeclared_ones_rejected(self):
        template = load_template(default_templates_dir(), "image_txt2img")
        graph = bind_template(
            template, {"prompt": "a lighthouse", "steps": 12, "ckpt_name": "a.safetensors"}
        )
        self.assertEqual(graph["6"]["inputs"]["text"], "a lighthouse")
        self.assertEqual(graph["4"]["inputs"]["ckpt_name"], "a.safetensors")
        self.assertEqual(graph["3"]["inputs"]["steps"], 12)
        # defaults apply where declared
        self.assertEqual(graph["5"]["inputs"]["width"], 1024)
        with self.assertRaises(ComfyUIError) as ctx:
            bind_template(template, {"not_an_input": 1, "ckpt_name": "a.safetensors"})
        self.assertIn("not_an_input", str(ctx.exception))

    def test_required_input_without_a_default_is_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "t"
            directory.mkdir()
            (directory / "manifest.json").write_text(json.dumps(DEFAULT_MANIFEST), encoding="utf-8")
            (directory / "workflow.json").write_text(json.dumps(DEFAULT_GRAPH), encoding="utf-8")
            template = load_template(tmp, "t")
            with self.assertRaises(ComfyUIError) as ctx:
                bind_template(template, {})
            self.assertIn("requires input", str(ctx.exception))

    def test_binding_does_not_mutate_the_shipped_template(self):
        template = load_template(default_templates_dir(), "image_txt2img")
        before = json.dumps(template.graph, sort_keys=True)
        bind_template(template, {"prompt": "changed", "ckpt_name": "a.safetensors"})
        self.assertEqual(json.dumps(template.graph, sort_keys=True), before)

    def test_unknown_and_traversing_template_ids_are_refused(self):
        for bad in ("../etc", "nope", "a/b", ".hidden"):
            with self.assertRaises(ComfyUIError):
                load_template(default_templates_dir(), bad)

    def test_cycles_are_refused_before_submission(self):
        graph = {
            "1": {"class_type": "KSampler", "inputs": {"model": ["2", 0]}},
            "2": {"class_type": "VAEDecode", "inputs": {"samples": ["1", 0]}},
        }
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "cyc"
            directory.mkdir()
            (directory / "manifest.json").write_text(
                json.dumps({"template_id": "cyc", "version": "1", "capabilities": ["image_generation"],
                            "inputs": {}, "outputs": {}}), encoding="utf-8")
            (directory / "workflow.json").write_text(json.dumps(graph), encoding="utf-8")
            adapter = ComfyUIAdapter(self.base_url, templates_dir=tmp)
            with self.assertRaises(ComfyUIError) as ctx:
                adapter.submit("cyc", {})
            self.assertIn("cycle", str(ctx.exception))
        self.assertEqual(StubComfy.submitted, [])


class RunTest(AdapterTestCase):
    def setUp(self):
        super().setUp()
        self.adapter = ComfyUIAdapter(self.base_url, templates_dir=default_templates_dir())

    def test_submit_sends_a_dependency_ordered_graph(self):
        result = self.adapter.submit("image_txt2img", {"prompt": "x", "ckpt_name": "a.safetensors"})
        self.assertEqual(result["prompt_id"], "pid1")
        self.assertEqual(result["template_id"], "image_txt2img")
        payload = StubComfy.submitted[0]
        self.assertEqual(payload["extra_data"]["maestro_template"], "image_txt2img")
        order = list(payload["prompt"].keys())
        # CheckpointLoaderSimple must precede the nodes that consume it.
        self.assertLess(order.index("4"), order.index("3"))

    def test_comfyui_rejection_is_surfaced_with_node_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "wrong"
            directory.mkdir()
            (directory / "manifest.json").write_text(
                json.dumps({"template_id": "wrong", "version": "1", "capabilities": ["image_generation"],
                            "inputs": {}, "outputs": {}}), encoding="utf-8")
            (directory / "workflow.json").write_text(
                json.dumps({"1": {"class_type": "NotInstalled", "inputs": {}}}), encoding="utf-8")
            adapter = ComfyUIAdapter(self.base_url, templates_dir=tmp)
            with self.assertRaises(ComfyUIError) as ctx:
                adapter.submit("wrong", {})
            self.assertIn("rejected", str(ctx.exception))

    def test_progress_reflects_history_then_queue(self):
        result = self.adapter.submit("image_txt2img", {"prompt": "x", "ckpt_name": "a.safetensors"})
        prompt_id = result["prompt_id"]
        StubComfy.history_ready = False
        self.assertEqual(self.adapter.progress(prompt_id)["state"], "unknown")
        StubComfy.history_ready = True
        self.assertEqual(self.adapter.progress(prompt_id)["state"], "completed")

    def test_results_lists_the_files_the_server_reported(self):
        result = self.adapter.submit("image_txt2img", {"prompt": "x", "ckpt_name": "a.safetensors"})
        results = self.adapter.results(result["prompt_id"])
        self.assertTrue(results["completed"])
        self.assertEqual(results["files"][0]["filename"], "p_00001_.png")
        self.assertEqual(results["files"][0]["node_id"], "9")

    def test_cancel_interrupts_only_when_the_prompt_is_running(self):
        result = self.adapter.submit("image_txt2img", {"prompt": "x", "ckpt_name": "a.safetensors"})
        prompt_id = result["prompt_id"]
        StubComfy.history_ready = False  # nothing in history, nothing in the queue
        outcome = self.adapter.cancel(prompt_id)
        self.assertEqual(outcome["action"], "none")
        self.assertEqual(StubComfy.interrupts, 0)

    def test_cancel_deletes_a_pending_prompt(self):
        result = self.adapter.submit("image_txt2img", {"prompt": "x", "ckpt_name": "a.safetensors"})
        prompt_id = result["prompt_id"]
        StubComfy.history_ready = False
        original_queue = StubComfy.do_GET

        def queue_with_pending(handler):
            if urlparse(handler.path).path == "/queue":
                return handler._json({"queue_running": [], "queue_pending": [["", prompt_id]]})
            return original_queue(handler)

        StubComfy.do_GET = queue_with_pending
        try:
            outcome = self.adapter.cancel(prompt_id)
        finally:
            StubComfy.do_GET = original_queue
        self.assertEqual(outcome["action"], "deleted_from_queue")
        self.assertIn(prompt_id, StubComfy.deleted)


class LibraryTest(unittest.TestCase):
    def test_every_shipped_template_is_loadable_and_declares_a_known_capability(self):
        from services.engine_providers import CAPABILITIES
        templates = list_templates(default_templates_dir())
        self.assertTrue(templates)
        for template in templates:
            self.assertTrue(template.capabilities)
            for capability in template.capabilities:
                self.assertIn(capability, CAPABILITIES)
            self.assertTrue(template.inputs)
            for name, spec in template.inputs.items():
                self.assertIn(str(spec.get("node")), template.graph, f"{template.template_id}:{name}")


if __name__ == "__main__":
    unittest.main()

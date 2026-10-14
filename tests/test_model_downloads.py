"""Model library: complete inventory, honest access data, and downloads that work on any CPU.

No network: a fake opener answers as the Hugging Face API (``?blobs=true``) and its
file endpoint (with HTTP ranges), so every rule is checked deterministically:

* the committed manifest matches app/defaults and every family handler;
* every model maps to a family with a licence, and every access route names its
  kind, source and confidence;
* plans pick one file per group of alternative precisions, never an NVIDIA-only
  format by default on other hardware, and place files where Maestro looks;
* downloads verify size and SHA-256, resume, refuse gated repos without a token,
  require licence acknowledgement, check disk space, cancel cleanly, never send the
  token to another host, and skip what is already installed.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from services import model_access, model_downloads as md  # noqa: E402

Usage = namedtuple("Usage", "total used free")


class FakeHub:
    """Serves /api/models/<repo>?blobs=true and /<repo>/resolve/<rev>/<path>, with ranges."""

    def __init__(self, files, gated=(), missing=()):
        self.files = files  # {(repo, path): bytes}
        self.gated = set(gated)
        self.missing = set(missing)
        self.calls = []
        self.block = None  # threading.Event to pause streaming
        self.corrupt = set()

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        m = re.match(r"https://huggingface\.co/api/models/([^/]+/[^/?]+)(?:/revision/[^?]+)?\?blobs=true", url)
        if m:
            repo = m.group(1)
            if repo in self.missing:
                return md._Response(404, {}, io.BytesIO(b"{}"))
            pass
            siblings = [{"rfilename": p, "size": len(b), "lfs": {"size": len(b), "sha256": hashlib.sha256(b).hexdigest()}}
                        for (r, p), b in self.files.items() if r == repo]
            body = {"id": repo, "sha": "abc123", "gated": "manual" if repo in self.gated else False, "private": False,
                    "cardData": {"license": "apache-2.0"}, "siblings": siblings}
            return md._Response(200, {}, io.BytesIO(json.dumps(body).encode()))
        m = re.match(r"https://huggingface\.co/([^/]+/[^/]+)/resolve/([^/]+)/(.+)$", url)
        if m:
            if m.group(1) in self.gated and "Authorization" not in headers:
                return md._Response(401, {}, io.BytesIO(b""))
            data = self.files.get((m.group(1), m.group(3)))
            if data is None:
                return md._Response(404, {}, io.BytesIO(b""))
            if url in self.corrupt:
                data = b"X" * len(data)
            start = 0
            status = 200
            if "Range" in headers:
                start = int(headers["Range"].split("=")[1].rstrip("-"))
                status = 206
            stream = io.BytesIO(data[start:])
            if self.block is not None:
                event = self.block
                inner = stream

                class Slow:
                    def read(self, n=-1):
                        chunk = inner.read(4)
                        if chunk:
                            event.wait(5)
                        return chunk

                    def close(self):
                        pass

                stream = Slow()
            return md._Response(status, {}, stream)
        raise AssertionError(url)


def manifest_with(models=None, llm=None, extras=None):
    return {"models": models or {}, "llm": llm or {}, "extras": extras or {}, "shared": []}


def model(name="Wan2.1 Text2video 14B", urls=(), te=(), companions=(), loras=(), modality="video"):
    return {"name": name, "architecture": "t2v", "description": "", "visible": True, "modality": modality,
            "family": "wan", "URLs": list(urls), "URLs2": [], "preload_URLs": [], "VAE_URLs": [],
            "text_encoder_URLs": list(te), "text_encoder_folder": "umt5-xxl", "loras": list(loras),
            "lora_dir": "loras/wan", "modules": [], "companions": list(companions)}


HF = "https://huggingface.co/DeepBeepMeep/Wan2.1/resolve/main/"


class ManifestTests(unittest.TestCase):
    def test_manifest_is_current_and_mirrors_wgp_handlers(self):
        done = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_model_manifest.py"), "--check"],
                              capture_output=True, text=True, timeout=600)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        source = (ROOT / "app" / "wgp.py").read_text(encoding="utf-8")
        handlers = re.search(r"^family_handlers = (\[.*?\])", source, re.M).group(1)
        sys.path.insert(0, str(ROOT / "scripts"))
        import build_model_manifest
        self.assertEqual(json.loads(handlers.replace("'", '"')), build_model_manifest.FAMILY_HANDLERS)

    def test_every_default_model_is_in_the_catalogue(self):
        manifest = md.load_manifest()
        defaults = {p.stem for p in (ROOT / "app" / "defaults").glob("*.json")}
        self.assertEqual(set(manifest["models"]), defaults)
        self.assertEqual(manifest["handler_errors"], [])
        self.assertGreaterEqual(len(manifest["llm"]), 10)

    def test_every_item_has_family_licence_and_sourced_routes(self):
        for item in md.catalog(accelerator="cpu"):
            with self.subTest(item=item["id"]):
                self.assertIn(item["family"], model_access.FAMILIES)
                self.assertTrue(item["access"]["license"])
        for family, data in model_access.FAMILIES.items():
            for route in data["routes"]:
                self.assertIn(route["route"], model_access.ROUTES, family)
            for backend in data["local"]:
                self.assertIn(backend, model_access.LOCAL_BACKENDS, family)
        for key, route in model_access.ROUTES.items():
            self.assertIn(route["kind"], model_access.ROUTE_KINDS, key)
            self.assertTrue(route["source"].startswith("https://"), key)
            self.assertIn(route["confidence"], {"official", "secondary", "unverified"}, key)

    def test_territory_and_non_commercial_licences_need_acknowledgement(self):
        cat = {i["id"]: i for i in md.catalog(accelerator="cpu")}
        self.assertTrue(cat["hunyuan_1_5_t2v"]["license_ack_required"])
        self.assertTrue(cat["minimax_h3"]["license_ack_required"])
        self.assertTrue(cat["flux"]["license_ack_required"])
        self.assertEqual(cat["z_image"]["license_ack_required"], [])
        self.assertTrue(cat["flux_schnell"]["access"]["free_api"])
        self.assertFalse(cat["chrono_edit"]["access"]["free_api"])  # base-model routes do not cover derivatives


class PlanTests(unittest.TestCase):
    def test_one_file_per_alternative_group_and_hardware_aware(self):
        plan = md.plan("t2v", accelerator="cpu")
        transformers = [f for f in plan["files"] if f["role"] == "transformer"]
        self.assertEqual(len(transformers), 1)
        self.assertIn("mbf16", transformers[0]["target"])
        compact = md.plan("t2v", accelerator="cpu", profile="compact")
        self.assertIn("int8", [f for f in compact["files"] if f["role"] == "transformer"][0]["target"])
        targets = [f["target"] for f in plan["files"]]
        self.assertIn("ckpts/umt5-xxl/models_t5_umt5-xxl-enc-bf16.safetensors", targets)
        self.assertIn("ckpts/umt5-xxl/spiece.model", targets)

    def test_nvidia_only_formats_are_flagged_off_cuda(self):
        cpu = {i["id"]: i for i in md.catalog(accelerator="cpu")}
        self.assertFalse(cpu["qwen_image_edit_plus_20B_nunchaku_r128_fp4"]["portable_here"])
        self.assertTrue(cpu["qwen_image_edit_plus_20B"]["portable_here"])
        cuda = {i["id"]: i for i in md.catalog(accelerator="cuda")}
        self.assertTrue(cuda["qwen_image_edit_plus_20B_nunchaku_r128_fp4"]["portable_here"])
        for f in md.plan("minimax_h3", accelerator="cpu")["files"]:
            if f["role"] == "transformer":
                self.assertNotIn("fp8", f["target"])

    def test_modules_referenced_by_name_are_resolved(self):
        roles = [f["role"] for f in md.plan("vace_lynx_14B", accelerator="cpu")["files"]]
        self.assertEqual(roles.count("module"), 2)

    def test_llm_and_whisper_targets(self):
        plan = md.plan("llm:unsloth/Qwen3.5-2B-GGUF")
        self.assertTrue(plan["files"][0]["target"].startswith("ckpts/llm/Qwen3.5-2B/"))
        whisper = md.plan("extra:whisper_small")
        self.assertIn("models--Systran--faster-whisper-small", whisper["files"][0]["target"])


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = self.tmp.name
        self.weights = b"W" * 1000
        self.te = b"T" * 300
        self.tok = b"tok"
        self.hub = FakeHub({
            ("DeepBeepMeep/Wan2.1", "wan_bf16.safetensors"): self.weights,
            ("DeepBeepMeep/Wan2.1", "umt5-xxl/te_bf16.safetensors"): self.te,
            ("DeepBeepMeep/Wan2.1", "umt5-xxl/spiece.model"): self.tok,
            ("Gated/Model", "g.safetensors"): b"G" * 10,
            ("Systran/faster-whisper-small", "model.bin"): b"M" * 50,
        }, gated={"Gated/Model"})
        self.manifest = manifest_with(
            models={
                "wan_test": model(urls=[HF + "wan_bf16.safetensors", HF + "wan_quanto_int8.safetensors",
                                        HF + "wan_nvfp4.safetensors"],
                                  te=[HF + "umt5-xxl/te_bf16.safetensors"],
                                  companions=[{"repo": "DeepBeepMeep/Wan2.1", "folder": "umt5-xxl", "target": "",
                                               "files": ["spiece.model"], "revision": ""}]),
                "gated_test": model(name="Gated 1B", urls=["https://huggingface.co/Gated/Model/resolve/main/g.safetensors"]),
                "hunyuan_test": model(name="Hunyuan Video 1.5 8B", urls=[HF + "wan_bf16.safetensors"]),
            },
            extras={"whisper_small": {"name": "whisper", "modality": "text",
                                      "hf_cache": {"repo": "Systran/faster-whisper-small", "folder": "whisper",
                                                   "files": ["model.bin"]}}})

    def tearDown(self):
        self.tmp.cleanup()

    def manager(self, **kwargs):
        hub = md.HubClient(self.hub, token=kwargs.pop("token", ""))
        return md.DownloadManager(self.app, hub=hub, opener=self.hub, manifest=self.manifest,
                                  start_threads=kwargs.pop("start_threads", True), **kwargs)

    def test_download_verifies_and_places_files(self):
        m = self.manager()
        job = m.wait(m.start("wan_test", accelerator="cpu")["id"], 10)
        self.assertEqual(job["state"], "succeeded", job)
        with open(os.path.join(self.app, "ckpts", "wan_bf16.safetensors"), "rb") as h:
            self.assertEqual(h.read(), self.weights)
        self.assertTrue(os.path.isfile(os.path.join(self.app, "ckpts", "umt5-xxl", "te_bf16.safetensors")))
        self.assertTrue(os.path.isfile(os.path.join(self.app, "ckpts", "umt5-xxl", "spiece.model")))
        self.assertTrue(all(entry["verified"] for entry in job["log"]))
        self.assertEqual(job["bytes_done"], len(self.weights) + len(self.te) + len(self.tok))
        downloaded = [u for u, _ in self.hub.calls if "/resolve/" in u]
        self.assertFalse(any("nvfp4" in u or "int8" in u for u in downloaded))
        again = m.wait(m.start("wan_test")["id"], 10)
        self.assertEqual(again["state"], "succeeded")
        self.assertEqual(len([u for u, _ in self.hub.calls if "/resolve/" in u]), len(downloaded))

    def test_resume_uses_range(self):
        part = os.path.join(self.app, "ckpts", "wan_bf16.safetensors.part")
        os.makedirs(os.path.dirname(part))
        with open(part, "wb") as h:
            h.write(self.weights[:400])
        m = self.manager()
        job = m.wait(m.start("wan_test")["id"], 10)
        self.assertEqual(job["state"], "succeeded", job)
        ranged = [h for u, h in self.hub.calls if u.endswith("wan_bf16.safetensors")]
        self.assertEqual(ranged[0].get("Range"), "bytes=400-")
        with open(os.path.join(self.app, "ckpts", "wan_bf16.safetensors"), "rb") as h:
            self.assertEqual(h.read(), self.weights)

    def test_checksum_mismatch_fails_and_discards(self):
        self.hub.corrupt.add(HF + "wan_bf16.safetensors")
        m = self.manager()
        job = m.wait(m.start("wan_test")["id"], 10)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "checksum_mismatch")
        self.assertFalse(os.path.exists(os.path.join(self.app, "ckpts", "wan_bf16.safetensors")))
        self.assertFalse(os.path.exists(os.path.join(self.app, "ckpts", "wan_bf16.safetensors.part")))

    def test_gated_repo_needs_token_and_token_stays_on_hub(self):
        m = self.manager()
        job = m.wait(m.start("gated_test")["id"], 10)
        self.assertEqual(job["error"]["code"], "not_free_to_download")
        m2 = self.manager(token="hf_secret")
        job = m2.wait(m2.start("gated_test")["id"], 10)
        self.assertEqual(job["state"], "succeeded", job)
        file_calls = [h for u, h in self.hub.calls if "/resolve/" in u and "Gated" in u]
        self.assertEqual(file_calls[-1]["Authorization"], "Bearer hf_secret")
        self.assertEqual(md.HubClient(self.hub, token="hf_secret").auth("https://cdn-lfs.example.com/x"), {})

    def test_licence_acknowledgement(self):
        self.manifest["models"]["hunyuan_1_5_t2v"] = self.manifest["models"].pop("hunyuan_test")
        m = self.manager()
        with self.assertRaises(md.DownloadError) as ctx:
            m.start("hunyuan_1_5_t2v")
        self.assertEqual(ctx.exception.code, "license_ack_required")
        self.assertIn("European Union", ctx.exception.message)
        job = m.wait(m.start("hunyuan_1_5_t2v", accept_license=True)["id"], 10)
        self.assertEqual(job["state"], "succeeded", job)

    def test_disk_space_checked(self):
        m = self.manager(disk_usage=lambda path: Usage(10, 10, 100))
        job = m.wait(m.start("wan_test")["id"], 10)
        self.assertEqual(job["error"]["code"], "disk_full")

    def test_cancel_keeps_partial_file(self):
        self.hub.block = threading.Event()
        m = self.manager()
        job = m.start("wan_test")
        for _ in range(500):
            if m.get(job["id"])["bytes_done"] > 0:
                break
            threading.Event().wait(0.01)
        m.cancel(job["id"])
        self.hub.block.set()
        done = m.wait(job["id"], 10)
        self.assertEqual(done["state"], "cancelled")
        self.assertTrue(os.path.exists(os.path.join(self.app, "ckpts", "wan_bf16.safetensors.part")))

    def test_whisper_lands_in_hub_cache_layout(self):
        m = self.manager()
        job = m.wait(m.start("extra:whisper_small")["id"], 10)
        self.assertEqual(job["state"], "succeeded", job)
        base = os.path.join(self.app, "ckpts", "whisper", "models--Systran--faster-whisper-small")
        self.assertTrue(os.path.isfile(os.path.join(base, "snapshots", "abc123", "model.bin")))
        with open(os.path.join(base, "refs", "main")) as h:
            self.assertEqual(h.read(), "abc123")

    def test_unknown_and_missing_repo(self):
        m = self.manager()
        with self.assertRaises(md.DownloadError):
            m.start("nope")
        self.hub.missing.add("DeepBeepMeep/Wan2.1")
        report = m.inspect(md.plan("wan_test", manifest=self.manifest))
        self.assertFalse(report["free_to_download"])


try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from services import model_library_api
except Exception:  # pragma: no cover
    FastAPI = None


@unittest.skipIf(FastAPI is None, "fastapi not installed")
class ApiTests(unittest.TestCase):
    def test_library_and_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = md.DownloadManager(tmp, start_threads=False, hub=md.HubClient(FakeHub({}), token=""))
            app = FastAPI()
            app.include_router(model_library_api.create_router(lambda: manager))
            client = TestClient(app)
            data = client.get("/api/v1/model-library?accelerator=cpu").json()
            self.assertEqual(data["hardware"]["accelerator"], "cpu")
            self.assertGreater(len(data["items"]), 200)
            plan = client.get("/api/v1/model-library/plan", params={"id": "t2v", "accelerator": "cpu"}).json()
            self.assertFalse(plan["files"][0]["present"])
            refused = client.post("/api/v1/model-library/downloads", json={"id": "hunyuan_1_5_t2v"})
            self.assertEqual(refused.status_code, 409)
            self.assertEqual(client.get("/api/v1/model-library/plan", params={"id": "nope"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()

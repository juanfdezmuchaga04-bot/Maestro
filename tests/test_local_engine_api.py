"""The local engine over HTTP, including one full clip round trip (spec §8, §18).

The last test is the point of this whole layer: submit a generation from the API,
run the queue, and get a real video file back with a URL the phone can play. The
ComfyUI side of it is the stub server, which is labelled as such everywhere it
appears: it proves the protocol and the wiring, not that a model ran.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from comfy_stub import FAKE_MP4, RunningStub, reset_stub  # noqa: E402
from services import comfy_jobs, generation_queue as queue, local_engine_api, queue_worker  # noqa: E402
from test_local_engine import hardware_stub  # noqa: E402


class RouterTestCase(unittest.IsolatedAsyncioTestCase):
    hardware: dict = {}

    async def asyncSetUp(self):
        reset_stub()
        queue_worker.clear_handlers()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "queue")
        self.out_dir = os.path.join(self._tmp.name, "workspace")
        os.makedirs(self.out_dir, exist_ok=True)
        self.nodes_root = os.path.join(self._tmp.name, "nodes")
        self.runs: list = []
        self.downloads: list = []

        probe = lambda: dict(self.hardware or hardware_stub())  # noqa: E731
        app = FastAPI()
        app.include_router(
            local_engine_api.create_router(
                queue_root=self.root,
                compute_nodes_root=self.nodes_root,
                hardware_probe=probe,
                out_dir=lambda: self.out_dir,
                file_url=lambda path: local_engine_api.make_file_url(path, "default"),
                runner=lambda command, cwd, timeout: (self.runs.append(command) or (0, "ok", "")),
                downloader=self._downloader,
            )
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://maestro.test"
        )

    async def asyncTearDown(self):
        queue_worker.clear_handlers()
        await self.client.aclose()
        self._tmp.cleanup()

    def _downloader(self, url: str, destination: str) -> int:
        """Stand in for the HTTP fetch, writing a real (if tiny) MP4 container."""
        self.downloads.append(url)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "wb") as handle:
            handle.write(FAKE_MP4)
        return len(FAKE_MP4)


class TestHardwareAndCatalogue(RouterTestCase):
    async def test_hardware_endpoint_measures_and_recommends(self):
        self.hardware = hardware_stub(vram_mb=8589, ram_gb=32.0, free_gb=300.0)
        response = await self.client.get("/api/v1/engines/local/hardware")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["hardware"]["primary_gpu"]["name"], "NVIDIA GeForce RTX 4060")
        self.assertEqual(body["selection"]["model"]["model_id"], "wan22_ti2v_5b")
        self.assertEqual(body["engine"]["kind"], "comfyui")
        self.assertFalse(body["engine"]["cloud"])
        self.assertFalse(body["policy"]["credits"])
        self.assertFalse(body["policy"]["commercial_providers"])
        evaluated = {entry["model_id"] for entry in body["evaluated_but_not_selected"]}
        self.assertIn("ltx2", evaluated)
        catalogue = {entry["model_id"] for entry in body["catalogue"]}
        self.assertIn("wan22_ti2v_5b", catalogue)
        self.assertIn("wan22_t2v_a14b", catalogue)

    async def test_hardware_endpoint_refuses_to_pick_a_model_on_a_gpu_less_machine(self):
        self.hardware = hardware_stub(gpu=False)
        body = (await self.client.get("/api/v1/engines/local/hardware")).json()
        self.assertIsNone(body["selection"]["model"])
        self.assertTrue(body["selection"]["blockers"])


class TestSetup(RouterTestCase):
    async def test_setup_lists_commands_and_files_without_running_anything(self):
        self.hardware = hardware_stub(vram_mb=8589, free_gb=300.0)
        body = (await self.client.get("/api/v1/engines/local/setup")).json()
        self.assertEqual([step["id"] for step in body["steps"]][:4], ["checkout", "venv", "torch", "requirements"])
        self.assertEqual(len(body["downloads"]), 3)
        self.assertAlmostEqual(body["download_gb"], 18.15, places=1)
        self.assertEqual(body["present"], {"comfyui_checkout": False, "venv": False})
        self.assertEqual(self.runs, [])  # a plan never executes anything

    async def test_install_needs_confirmation(self):
        self.hardware = hardware_stub(vram_mb=8589, free_gb=300.0)
        response = await self.client.post("/api/v1/engines/local/setup/install", json={})
        body = response.json()
        self.assertFalse(body["started"])
        self.assertIn("explicitly confirmed", body["reason"])
        self.assertEqual(self.runs, [])
        self.assertEqual(self.downloads, [])

    async def test_confirmed_install_runs_and_reports_each_step(self):
        self.hardware = hardware_stub(vram_mb=8589, free_gb=300.0)
        response = await self.client.post(
            "/api/v1/engines/local/setup/install",
            json={"confirm": True, "download_models": True},
        )
        body = response.json()
        self.assertTrue(body["started"])
        self.assertTrue(body["completed"])
        self.assertEqual(len(self.runs), 4)
        self.assertEqual(len(self.downloads), 3)
        statuses = {step["id"]: step["status"] for step in body["steps"]}
        self.assertEqual(statuses["models"], "done")
        self.assertEqual(statuses["server"], "manual")
        self.assertEqual(statuses["functional_test"], "pending_action")


class TestFunctionalTestAndRegister(RouterTestCase):
    async def test_the_functional_test_returns_a_clip_with_a_playable_url(self):
        self.hardware = hardware_stub(vram_mb=8589, free_gb=300.0)
        with RunningStub() as stub:
            response = await self.client.post(
                "/api/v1/engines/local/functional-test",
                json={"base_url": stub.base_url, "prompt": "a paper boat", "poll_interval": 0.01},
            )
        body = response.json()
        self.assertTrue(body["passed"], body.get("reason"))
        self.assertTrue(os.path.isfile(body["clip_path"]))
        self.assertTrue(body["clip_url"].startswith("/api/v1/file/"))
        self.assertIn("workspace=default", body["clip_url"])
        self.assertEqual(body["container"], "mp4/mov")

    async def test_a_response_that_is_not_a_video_is_rejected_as_a_failure(self):
        """The proof is the file itself: an HTML error page is not a generated clip."""
        self.hardware = hardware_stub(vram_mb=8589, free_gb=300.0)
        original = self._downloader

        def html_downloader(url: str, destination: str) -> int:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, "wb") as handle:
                handle.write(b"<html>proxy error</html>" * 100)
            return 2200

        router = local_engine_api.create_router(
            queue_root=self.root,
            compute_nodes_root=self.nodes_root,
            hardware_probe=lambda: dict(self.hardware),
            out_dir=lambda: self.out_dir,
            downloader=html_downloader,
        )
        app = FastAPI()
        app.include_router(router)
        with RunningStub() as stub:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://maestro.test"
            ) as client:
                body = (
                    await client.post(
                        "/api/v1/engines/local/functional-test",
                        json={"base_url": stub.base_url, "poll_interval": 0.01},
                    )
                ).json()
        self.assertFalse(body["passed"])
        self.assertIn("not a usable video", body["reason"])
        self.assertIsNotNone(original)

    async def test_the_functional_test_fails_cleanly_when_there_is_no_engine(self):
        self.hardware = hardware_stub(vram_mb=8589, free_gb=300.0)
        body = (
            await self.client.post(
                "/api/v1/engines/local/functional-test",
                json={"base_url": "http://127.0.0.1:1", "poll_interval": 0.01},
            )
        ).json()
        self.assertFalse(body["passed"])
        self.assertIn("not reachable", body["reason"])

    async def test_registration_without_a_passed_test_is_refused(self):
        self.hardware = hardware_stub(vram_mb=8589, free_gb=300.0)
        with RunningStub() as stub:
            body = (
                await self.client.post("/api/v1/engines/local/register", json={"base_url": stub.base_url})
            ).json()
        self.assertFalse(body["registered"])
        self.assertTrue(body["functional_test_required"])
        self.assertIn("only after a real clip", body["reason"])

    async def test_registering_after_a_passed_test_creates_the_node(self):
        from services import compute_nodes

        self.hardware = hardware_stub(vram_mb=8589, free_gb=300.0)
        with RunningStub() as stub:
            tested = await self.client.post(
                "/api/v1/engines/local/functional-test",
                json={"base_url": stub.base_url, "poll_interval": 0.01},
            )
            self.assertTrue(tested.json()["passed"], tested.json().get("reason"))
            response = await self.client.post(
                "/api/v1/engines/local/register", json={"base_url": stub.base_url}
            )
        body = response.json()
        self.assertTrue(body["registered"], body)
        self.assertEqual(body["node"]["kind"], "comfyui")
        self.assertEqual(body["node"]["address"], stub.base_url)
        self.assertTrue(body["node"]["token"])
        self.assertIn("text_to_video", body["node"]["capabilities"])
        stored = compute_nodes.get_node(self.nodes_root, body["node"]["node_id"])
        self.assertEqual(stored["node_id"], body["node"]["node_id"])
        self.assertIn("verified_by", body)


class TestQueueJobRoundTrip(RouterTestCase):
    async def test_a_queued_clip_runs_and_comes_back_as_a_real_file(self):
        """The whole point: queue a generation from the API and get a real clip back."""
        self.hardware = hardware_stub(vram_mb=8589, free_gb=300.0)
        with RunningStub() as stub:
            comfy_jobs.register_comfyui_handler(out_dir=self.out_dir)
            queued = await self.client.post(
                "/api/v1/engines/local/queue-job",
                json={
                    "prompt": "a paper boat drifting down a small stream",
                    "base_url": stub.base_url,
                    "project_id": "prod-1",
                    "seconds": 5,
                    "width": 256,
                    "height": 256,
                    "length": 9,
                    "steps": 4,
                    "dedupe_key": "shot-sh-1",
                },
            )
            self.assertEqual(queued.status_code, 200, queued.text)
            queued_body = queued.json()
            self.assertTrue(queued_body["created"])
            self.assertIn("waits instead of failing", queued_body["note"])

            # The queue runs it exactly as the runner would.
            from services.production_queue_api import create_router as queue_router

            qapp = FastAPI()
            qapp.include_router(queue_router(root_provider=lambda: self.root))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=qapp), base_url="http://maestro.test"
            ) as qclient:
                run = await qclient.post("/api/v1/production/queue/run", json={"limit": 2})
                self.assertEqual(run.json()["outcomes"], {"completed": 1}, run.text)

        job = queue.job(self.root, queued_body["job"]["job_id"])
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["external_job_id"], "pid1")
        clip = job["outputs"][0]
        self.assertTrue(os.path.isfile(clip))
        self.assertEqual(os.path.dirname(clip), os.path.realpath(self.out_dir))
        self.assertEqual(job["params"]["width"], 256)
        self.assertEqual(job["params"]["length"], 9)
        self.assertEqual(job["progress"]["percent"], None)

    async def test_a_duplicate_submission_does_not_queue_a_second_clip(self):
        self.hardware = hardware_stub(vram_mb=8589, free_gb=300.0)
        payload = {"prompt": "x", "dedupe_key": "shot-sh-9"}
        first = await self.client.post("/api/v1/engines/local/queue-job", json=payload)
        second = await self.client.post("/api/v1/engines/local/queue-job", json=payload)
        self.assertTrue(first.json()["created"])
        self.assertFalse(second.json()["created"])
        self.assertEqual(len(queue.list_jobs(self.root)), 1)

    async def test_queueing_needs_no_running_engine(self):
        """The phone can queue work while the rendering computer is off."""
        self.hardware = hardware_stub(vram_mb=8589, free_gb=300.0)
        response = await self.client.post(
            "/api/v1/engines/local/queue-job", json={"prompt": "a lighthouse at dusk"}
        )
        body = response.json()
        self.assertTrue(body["created"])
        job = queue.job(self.root, body["job"]["job_id"])
        self.assertEqual(job["state"], "pending")
        self.assertEqual(job["engine"], "comfyui")
        self.assertEqual(job["task"], "video")
        self.assertEqual(job["params"]["length"], 121)  # 5 s at 24 fps


if __name__ == "__main__":
    unittest.main()

"""Delegating generation to another Maestro, over a real socket.

The modest-computer path: this machine keeps the projects, the storyboard and the
durable queue, and another Maestro that has the GPU renders the work.  These tests
drive the whole seam for real — a peer FastAPI app served by uvicorn with the
**real** production-queue router and the **real** access gate, reached over HTTP
with a bearer token — because the properties that matter here only exist on the
wire:

* a delegated job comes back as a **file on this machine**, byte for byte;
* a peer that is off (or slow) **defers** the job: no attempt consumed, no error,
  nothing lost;
* a wrong token is reported as such instead of being retried forever;
* progress shown here is the progress the **peer's own engine** reported;
* a cancelled job is only acknowledged once the peer really stopped;
* a dropped transfer resumes from where it stopped (`Range`), and a caller can
  never name a file on the peer — only an index of what that job produced.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import traceback
import unittest
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import access_gate  # noqa: E402
from services import engine_providers  # noqa: E402
from services import generation_queue as queue  # noqa: E402
from services import production_queue_api  # noqa: E402
from services import queue_worker  # noqa: E402
from services import remote_engine  # noqa: E402

TOKEN = "peer-token-9000"
PEER_ID = "desk-1"


def fake_mp4(size: int = 3 * 1024 * 1024) -> bytes:
    """Bytes that are structurally an MP4, and big enough to need several chunks."""
    header = (32).to_bytes(4, "big") + b"ftypisom" + b"\x00\x00\x02\x00isomiso2"
    return header + b"\x00" * max(0, size - len(header))


def build_peer_app(
    *,
    queue_root: str,
    token: str = TOKEN,
    capabilities: tuple[str, ...] = ("text_to_video", "image_generation"),
    generation_possible: bool = True,
    models: tuple[str, ...] = ("wan22_ti2v_5b",),
) -> FastAPI:
    """A stand-in for the other computer: its engines report + its real queue."""
    app = FastAPI()

    @app.get("/api/v1/engines")
    def engines_report():
        available = set(capabilities)
        return {
            "availability": [
                {**entry, "available": entry["id"] in available, "providers": []}
                for entry in engine_providers.describe_capabilities()
            ],
            "local_engine": {
                "models": list(models),
                "capabilities": sorted(available),
                "generation_possible": generation_possible,
            },
        }

    app.include_router(production_queue_api.create_router(root_provider=lambda: queue_root))
    gate = access_gate.AccessGate(
        settings_dir=os.path.join(queue_root, "..", "peer-settings"),
        environ={"MAESTRO_ACCESS_TOKEN": token} if token else {},
    )
    access_gate.install_access_gate(app, gate)
    return app


class LivePeer:
    """Serve a peer app on a free port for the duration of a test."""

    def __init__(self, *, queue_root: str, **kwargs) -> None:
        self.queue_root = queue_root
        self.app = build_peer_app(queue_root=queue_root, **kwargs)
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.port = 0

    def __enter__(self) -> "LivePeer":
        config = uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.server.started and self.server.servers:
                break
            time.sleep(0.02)
        else:  # pragma: no cover - a failure here is an environment problem
            raise RuntimeError("the test peer did not start")
        self.port = int(self.server.servers[0].sockets[0].getsockname()[1])
        return self

    def __exit__(self, *exc) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=15)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def finish_remote_job_eventually(
    peer_root: str,
    *,
    dedupe_key: str,
    path: str = "",
    percent: float | None = None,
    state: str = "completed",
    error: str = "",
    delay: float = 0.15,
    hold_after_report: float = 0.0,
) -> threading.Thread:
    """Stand in for the peer's engine: finish its job the way a node would."""

    def worker() -> None:
        deadline = time.time() + 30
        while time.time() < deadline:
            match = next(
                (
                    job
                    for job in queue.list_jobs(peer_root, include_terminal=False)
                    if job.get("dedupe_key") == dedupe_key
                ),
                None,
            )
            if match is not None:
                try:
                    time.sleep(delay)
                    job_id = str(match["job_id"])
                    # A real peer's worker claims the job before its engine runs it.
                    queue.claim_next(peer_root, worker="peer-worker")
                    queue.set_state(peer_root, job_id, "running", worker="peer-worker")
                    if state == "completed":
                        if percent is not None:
                            queue.report_progress(
                                peer_root, job_id, percent=percent, step=3, total_steps=12,
                                note="sampling step 3 of 12",
                            )
                            # Stay mid-render for a moment so the delegation really has
                            # to poll and forward this measurement, not just read a
                            # finished record.
                            time.sleep(hold_after_report)
                        queue.complete(peer_root, job_id, [path])
                    else:
                        queue.fail(
                            peer_root, job_id, error or "the peer's engine failed",
                            retryable=False,
                        )
                except Exception:  # a broken stand-in must not look like a slow peer
                    traceback.print_exc()
                return
            time.sleep(0.02)
        # A stand-in that silently does nothing turns into a confusing timeout in
        # the test that used it, so it says why it gave up.
        print(f"peer stand-in gave up waiting for a job with dedupe key {dedupe_key!r}")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


def watch_progress(root: str, job_id: str) -> tuple[threading.Thread, list]:
    """Sample what this machine's job shows, until it ends."""
    seen: list[tuple[str, object]] = []

    def watch() -> None:
        for _ in range(400):
            try:
                record = queue.job(root, job_id)
            except FileNotFoundError:  # pragma: no cover - defensive
                return
            seen.append((str(record["state"]), record["progress"].get("percent")))
            if record["state"] in {"completed", "failed", "cancelled"}:
                return
            time.sleep(0.01)

    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    return thread, seen


class PeerStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "peers")

    def add(self, **overrides):
        payload = {
            "peer_id": PEER_ID,
            "label": "Desktop with the GPU",
            "base_url": "http://desk.local:7860",
            "token": TOKEN,
            "capabilities": ["text_to_video"],
        }
        payload.update(overrides)
        return remote_engine.add_peer(self.root, payload)

    def test_a_peer_is_described_without_ever_handing_its_token_back(self):
        described = self.add()
        self.assertEqual(described["peer_id"], PEER_ID)
        self.assertEqual(described["engine_id"], "node:desk-1")
        self.assertTrue(described["token_set"])
        self.assertNotIn("token", described)
        listed = remote_engine.list_peers(self.root)
        self.assertEqual(len(listed), 1)
        self.assertNotIn("token", listed[0])
        # The token is available to the transport, and only there.
        self.assertEqual(remote_engine.peer_credentials(self.root, PEER_ID), ("http://desk.local:7860", TOKEN))
        self.assertNotIn(TOKEN, str(listed))

    def test_the_store_holds_the_token_in_a_file_only_its_owner_can_read(self):
        self.add()
        path = os.path.join(self.root, remote_engine.PEERS_FILENAME)
        self.assertTrue(os.path.isfile(path))
        if os.name != "nt":
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_ids_addresses_and_capabilities_are_validated(self):
        with self.assertRaises(remote_engine.RemoteEngineError):
            self.add(peer_id="bad id/../etc")
        with self.assertRaises(remote_engine.RemoteEngineError):
            self.add(base_url="ftp://desk.local")
        with self.assertRaises(remote_engine.RemoteEngineError):
            self.add(base_url="http://user:pass@desk.local")
        with self.assertRaises(remote_engine.RemoteEngineError):
            self.add(capabilities=["teleportation"])
        self.assertEqual(remote_engine.list_peers(self.root), [])

    def test_a_peer_can_be_updated_and_removed(self):
        self.add()
        updated = remote_engine.update_peer(
            self.root, PEER_ID, {"base_url": "http://desk.tailnet.ts.net", "token": "second-token"}
        )
        self.assertEqual(updated["base_url"], "http://desk.tailnet.ts.net")
        self.assertEqual(remote_engine.peer_credentials(self.root, PEER_ID)[1], "second-token")
        removed = remote_engine.remove_peer(self.root, PEER_ID)
        self.assertEqual(removed["removed"], PEER_ID)
        self.assertEqual(remote_engine.list_peers(self.root), [])
        with self.assertRaises(FileNotFoundError):
            remote_engine.get_peer(self.root, PEER_ID)

    def test_a_peer_is_a_provider_only_while_a_probe_is_fresh(self):
        self.add()
        self.assertEqual(remote_engine.provider_entries(self.root)[0]["online"], False)
        self.assertEqual(remote_engine.generation_sources(self.root), [])
        remote_engine.remember_probe(
            self.root,
            PEER_ID,
            {"at": time.time(), "reachable": True, "capabilities": ["text_to_video"]},
        )
        entry = remote_engine.provider_entries(self.root)[0]
        self.assertTrue(entry["online"])
        self.assertEqual(entry["node_id"], PEER_ID)
        self.assertFalse(entry["local"])
        source = remote_engine.generation_sources(self.root)[0]
        self.assertEqual(source["engine_id"], "node:desk-1")
        self.assertEqual(source["kind"], "remote_maestro")
        # A stale probe is not evidence.
        stale = time.time() - remote_engine.PEER_ONLINE_TTL - 1
        remote_engine.remember_probe(
            self.root, PEER_ID, {"at": stale, "reachable": True, "capabilities": ["text_to_video"]}
        )
        self.assertFalse(remote_engine.provider_entries(self.root)[0]["online"])
        self.assertEqual(remote_engine.generation_sources(self.root), [])

    def test_measured_capabilities_replace_only_undeclared_ones(self):
        self.add(capabilities=[])  # nothing declared: the probe fills it in
        probe = {
            "at": time.time(),
            "reachable": True,
            "capabilities": ["text_to_video", "image_generation"],
            "models": ["wan22_ti2v_5b"],
        }
        remote_engine.remember_probe(self.root, PEER_ID, probe)
        measured = remote_engine.list_peers(self.root)[0]
        self.assertEqual(measured["capabilities_source"], "probe")
        self.assertEqual(measured["capabilities"], ["text_to_video", "image_generation"])
        self.assertEqual(measured["models"], ["wan22_ti2v_5b"])
        # A user's own declaration is never overwritten by a probe.
        remote_engine.update_peer(self.root, PEER_ID, {"capabilities": ["text_to_video"]})
        remote_engine.remember_probe(self.root, PEER_ID, {**probe, "capabilities": ["upscaling"]})
        declared = remote_engine.list_peers(self.root)[0]
        self.assertEqual(declared["capabilities"], ["text_to_video"])
        self.assertEqual(declared["capabilities_source"], "declared")


class OutputsEndpointTest(unittest.IsolatedAsyncioTestCase):
    """The peer's side of the transfer: index in, file out, never a path."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "queue")
        self.app = build_peer_app(queue_root=self.root)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://peer.test",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        self.addAsyncCleanup(self.client.aclose)
        self.clip = os.path.join(self._tmp.name, "clip.mp4")
        with open(self.clip, "wb") as handle:
            handle.write(fake_mp4(64 * 1024))
        queue.submit(
            self.root,
            {
                "job_id": "peer-job-1",
                "task": "video",
                "engine": "comfyui",
                "dedupe_key": "maestro:job-1",
                "params": {"prompt": "a paper boat"},
            },
        )

    async def complete(self, job_id: str = "peer-job-1") -> None:
        queue.claim_next(self.root, worker="peer-worker")
        queue.set_state(self.root, job_id, "running", worker="peer-worker")
        queue.complete(self.root, job_id, [self.clip])

    async def test_the_gate_refuses_an_anonymous_machine_caller(self):
        anonymous = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://peer.test"
        )
        self.addAsyncCleanup(anonymous.aclose)
        response = await anonymous.get("/api/v1/production/queue/jobs/peer-job-1/outputs")
        self.assertEqual(response.status_code, 401)

    async def test_outputs_are_listed_by_index_without_leaking_paths(self):
        await self.complete()
        response = await self.client.get("/api/v1/production/queue/jobs/peer-job-1/outputs")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["state"], "completed")
        self.assertEqual(len(payload["outputs"]), 1)
        entry = payload["outputs"][0]
        self.assertEqual(entry["index"], 0)
        self.assertEqual(entry["name"], "clip.mp4")
        self.assertEqual(entry["bytes"], os.path.getsize(self.clip))
        self.assertTrue(entry["present"])
        self.assertNotIn(self._tmp.name, response.text)

    async def test_a_file_comes_back_and_a_range_asks_for_the_rest(self):
        await self.complete()
        size = os.path.getsize(self.clip)
        whole = await self.client.get("/api/v1/production/queue/jobs/peer-job-1/outputs/0")
        self.assertEqual(whole.status_code, 200)
        self.assertEqual(len(whole.content), size)
        self.assertEqual(whole.content[:8], fake_mp4(8)[:8])
        partial = await self.client.get(
            "/api/v1/production/queue/jobs/peer-job-1/outputs/0",
            headers={"Range": f"bytes={size - 100}-"},
        )
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(len(partial.content), 100)
        self.assertEqual(partial.headers["content-range"], f"bytes {size - 100}-{size - 1}/{size}")
        self.assertEqual(partial.content, whole.content[-100:])

    async def test_an_impossible_range_is_refused_instead_of_silently_completed(self):
        await self.complete()
        response = await self.client.get(
            "/api/v1/production/queue/jobs/peer-job-1/outputs/0",
            headers={"Range": f"bytes={os.path.getsize(self.clip) + 10}-"},
        )
        self.assertEqual(response.status_code, 416)

    async def test_an_unknown_job_or_index_is_a_404(self):
        await self.complete()
        missing_job = await self.client.get("/api/v1/production/queue/jobs/nope/outputs")
        self.assertEqual(missing_job.status_code, 404)
        missing_index = await self.client.get("/api/v1/production/queue/jobs/peer-job-1/outputs/7")
        self.assertEqual(missing_index.status_code, 404)
        wrong_type = await self.client.get("/api/v1/production/queue/jobs/peer-job-1/outputs/abc")
        self.assertEqual(wrong_type.status_code, 422)


class DelegationTest(unittest.TestCase):
    """The whole seam: a queued job, a peer, and a file that ends up here."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.peers_root = os.path.join(self._tmp.name, "peers")
        self.peer_root = os.path.join(self._tmp.name, "peer-queue")
        self.root = os.path.join(self._tmp.name, "local-queue")
        self.out_dir = os.path.join(self._tmp.name, "workspace")
        os.makedirs(self.out_dir, exist_ok=True)
        queue_worker.clear_handlers()
        self.addCleanup(queue_worker.clear_handlers)

    def configure_peer(self, **overrides) -> dict:
        payload = {
            "peer_id": PEER_ID,
            "label": "Desktop with the GPU",
            "base_url": "http://127.0.0.1:1",
            "token": TOKEN,
            "capabilities": ["text_to_video", "image_generation"],
        }
        payload.update(overrides)
        return remote_engine.add_peer(self.peers_root, payload)

    def submit(self, *, job_id: str = "job-1", params: dict | None = None, peer_id: str = PEER_ID):
        return queue.submit(
            self.root,
            {
                "job_id": job_id,
                "project_id": "prod-1",
                "shot_id": "sh-1",
                "task": "video",
                "engine": f"node:{peer_id}",
                "model": "wan22_ti2v_5b",
                "params": params if params is not None else {"prompt": "a paper boat"},
                "cancel_supported": True,
            },
        )

    def delegate(self):
        registered = remote_engine.register_remote_handlers(
            peers_root=self.peers_root, out_dir=self.out_dir,
            poll_interval=0.05, poll_timeout=25.0, timeout=5.0,
        )
        self.assertEqual(registered, [f"node:{PEER_ID}"])
        return queue_worker.run_once(self.root, worker="laptop")

    def test_a_job_runs_on_the_peer_and_its_file_lands_here(self):
        clip = os.path.join(self._tmp.name, "peer-clip.mp4")
        payload = fake_mp4(2 * 1024 * 1024)
        with open(clip, "wb") as handle:
            handle.write(payload)
        with LivePeer(queue_root=self.peer_root) as peer:
            self.configure_peer(base_url=peer.base_url)
            self.submit()
            finish_remote_job_eventually(self.peer_root, dedupe_key="maestro:job-1", path=clip)

            report = self.delegate()

        self.assertEqual(report["outcome"], "completed", report.get("reason"))
        record = queue.job(self.root, "job-1")
        self.assertEqual(record["state"], "completed")
        self.assertEqual(len(record["outputs"]), 1)
        produced = record["outputs"][0]
        self.assertTrue(os.path.isfile(produced), produced)
        self.assertEqual(os.path.dirname(produced), os.path.realpath(self.out_dir))
        self.assertTrue(os.path.basename(produced).startswith("job-1_"))
        with open(produced, "rb") as handle:
            self.assertEqual(handle.read(), payload)
        # The peer's own job id is recorded, so its queue can be inspected.
        peer_jobs = queue.list_jobs(self.peer_root)
        self.assertEqual(record["external_job_id"], peer_jobs[0]["job_id"])
        self.assertEqual(len(peer_jobs), 1)
        # The note is about the transfer, because that is what really happened here.
        self.assertIn("retrieved", str(record["progress"]["note"]))
        # No partial file was left behind.
        self.assertFalse([name for name in os.listdir(self.out_dir) if name.endswith(".part")])

    def test_the_peer_reported_progress_is_forwarded_and_nothing_is_invented(self):
        clip = os.path.join(self._tmp.name, "peer-clip.mp4")
        with open(clip, "wb") as handle:
            handle.write(fake_mp4(4096))
        with LivePeer(queue_root=self.peer_root) as peer:
            self.configure_peer(base_url=peer.base_url)
            self.submit(job_id="job-progress")
            finish_remote_job_eventually(
                self.peer_root, dedupe_key="maestro:job-progress", path=clip,
                percent=25.0, hold_after_report=0.6,
            )
            # Watch this machine's job while the peer is mid-render: whatever the
            # peer measured has to show up here, and nothing else may appear.
            watcher, seen = watch_progress(self.root, "job-progress")
            report = self.delegate()
            watcher.join(timeout=5)

        self.assertEqual(report["outcome"], "completed", report.get("reason"))
        self.assertIn(("running", 25.0), seen)
        # Before the job finished, the only percentage it ever carried is the one
        # the peer's engine produced (nothing measured yet reads as no percentage).
        mid = [percent for state, percent in seen if state not in {"completed", "failed", "cancelled"}]
        self.assertTrue(mid)
        self.assertEqual(set(mid), {25.0, None})
        progress = queue.job(self.root, "job-progress")["progress"]
        self.assertEqual(progress["step"], 3)
        self.assertEqual(progress["total_steps"], 12)
        self.assertEqual(progress["source"], "reported")
        # A completed job reads 100 because the queue's own rule for a finished
        # job says so, not because anyone here measured that percentage.
        self.assertEqual(progress["percent"], 100)

    def test_the_download_resumes_from_a_partial_file(self):
        clip = os.path.join(self._tmp.name, "peer-clip.mp4")
        payload = fake_mp4(256 * 1024)
        with open(clip, "wb") as handle:
            handle.write(payload)
        with LivePeer(queue_root=self.peer_root) as peer:
            self.configure_peer(base_url=peer.base_url)
            self.submit(job_id="job-part", params={"prompt": "x"})
            finish_remote_job_eventually(self.peer_root, dedupe_key="maestro:job-part", path=clip)
            remote_engine.register_remote_handlers(
                peers_root=self.peers_root, out_dir=self.out_dir, poll_interval=0.05, timeout=5.0
            )
            # Pretend a first attempt died half way through.
            destination = os.path.join(self.out_dir, "job-part_peer-clip.mp4")
            with open(f"{destination}.part", "wb") as handle:
                handle.write(payload[: len(payload) // 2])
            report = queue_worker.run_once(self.root, worker="laptop")
        self.assertEqual(report["outcome"], "completed", report.get("reason"))
        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), payload)
        self.assertFalse(os.path.exists(f"{destination}.part"))

    def test_a_computer_that_is_off_defers_the_job_instead_of_failing_it(self):
        self.configure_peer(base_url="http://127.0.0.1:1")
        self.submit()
        report = self.delegate()
        self.assertEqual(report["outcome"], "deferred", report.get("reason"))
        self.assertIn("Cannot reach Maestro", report["reason"])
        self.assertIn("stays queued", report["reason"])
        record = queue.job(self.root, "job-1")
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["attempts"], 0)
        self.assertEqual(record["errors"], [])
        self.assertEqual(record["deferrals"], 1)
        self.assertEqual(len(queue.list_jobs(self.root)), 1)

    def test_a_peer_whose_numbers_disagree_does_not_put_a_number_on_screen(self):
        # A source that says "90%" and "step 1 of 100" is contradicting itself.  The
        # percentage the engine reported is forwarded; the step count is dropped,
        # because the queue would otherwise re-derive 1% from it and display a
        # figure nobody measured.
        clip = os.path.join(self._tmp.name, "peer-clip.mp4")
        with open(clip, "wb") as handle:
            handle.write(fake_mp4(4096))
        with LivePeer(queue_root=self.peer_root) as peer:
            self.configure_peer(base_url=peer.base_url)
            self.submit(job_id="job-conflict")
            finish_remote_job_eventually(
                self.peer_root, dedupe_key="maestro:job-conflict", path=clip,
                percent=90.0, hold_after_report=0.6,
            )
            watcher, seen = watch_progress(self.root, "job-conflict")
            report = self.delegate()
            watcher.join(timeout=5)
        self.assertEqual(report["outcome"], "completed", report.get("reason"))
        progress = queue.job(self.root, "job-conflict")["progress"]
        # The percentage the peer reported is what this machine showed while it
        # ran; the 1% its contradictory step count implies never appeared.
        mid = {percent for state, percent in seen if state not in {"completed", "failed", "cancelled"}}
        self.assertIn(90.0, mid)
        self.assertNotIn(1.0, mid)
        self.assertIsNone(progress["step"])
        self.assertIsNone(progress["total_steps"])

    def test_a_wrong_token_is_reported_instead_of_retried_forever(self):
        with LivePeer(queue_root=self.peer_root) as peer:
            self.configure_peer(base_url=peer.base_url, token="not-the-peers-token")
            self.submit()
            report = self.delegate()
        self.assertEqual(report["outcome"], "failed", report.get("reason"))
        record = queue.job(self.root, "job-1")
        self.assertIn("refused this machine's credential", record["errors"][-1])
        self.assertIn("MAESTRO_ACCESS_TOKEN", record["errors"][-1])
        self.assertEqual(record["attempts"], 1)
        self.assertEqual(record["state"], "failed")

    def test_a_finished_peer_job_is_adopted_instead_of_rendered_twice(self):
        clip = os.path.join(self._tmp.name, "peer-clip.mp4")
        payload = fake_mp4(8192)
        with open(clip, "wb") as handle:
            handle.write(payload)
        with LivePeer(queue_root=self.peer_root) as peer:
            self.configure_peer(base_url=peer.base_url)
            self.submit(job_id="job-adopt")
            # The previous attempt already rendered it on the peer and died before
            # downloading: the job exists there, completed, with the same dedupe key.
            queue.submit(
                self.peer_root,
                {
                    "job_id": "peer-old-1",
                    "task": "video",
                    "engine": "comfyui",
                    "dedupe_key": "maestro:job-adopt",
                    "params": {"prompt": "a paper boat"},
                },
            )
            queue.claim_next(self.peer_root, worker="peer-worker")
            queue.set_state(self.peer_root, "peer-old-1", "running", worker="peer-worker")
            queue.complete(self.peer_root, "peer-old-1", [clip])
            report = self.delegate()
        self.assertEqual(report["outcome"], "completed", report.get("reason"))
        record = queue.job(self.root, "job-adopt")
        self.assertEqual(record["external_job_id"], "peer-old-1")
        # Nothing new was queued on the peer for the same shot.
        self.assertEqual(len(queue.list_jobs(self.peer_root)), 1)

    def test_a_peer_that_fails_the_job_reports_its_own_reason(self):
        with LivePeer(queue_root=self.peer_root) as peer:
            self.configure_peer(base_url=peer.base_url)
            self.submit(job_id="job-fail")
            finish_remote_job_eventually(
                self.peer_root, dedupe_key="maestro:job-fail", state="failed",
                error="ComfyUI ran out of VRAM at step 12",
            )
            report = self.delegate()
        self.assertEqual(report["outcome"], "failed", report.get("reason"))
        self.assertIn("out of VRAM", queue.job(self.root, "job-fail")["errors"][-1])

    def test_a_job_the_peer_certifies_as_cancelled_is_acknowledged(self):
        with LivePeer(queue_root=self.peer_root) as peer:
            self.configure_peer(base_url=peer.base_url)
            self.submit(job_id="job-cancel")
            remote_engine.register_remote_handlers(
                peers_root=self.peers_root, out_dir=self.out_dir,
                poll_interval=0.05, poll_timeout=25.0, timeout=5.0,
            )

            def ask_to_stop() -> None:
                for _ in range(200):
                    try:
                        if queue.job(self.root, "job-cancel")["state"] == "running":
                            break
                    except FileNotFoundError:  # pragma: no cover - defensive
                        pass
                    time.sleep(0.02)
                queue.cancel(self.root, "job-cancel", reason="the user stopped this shot")

            stopper = threading.Thread(target=ask_to_stop, daemon=True)
            stopper.start()
            # Nothing on the peer ever finishes this job, so the only way out is
            # the stop request — which must be confirmed by the peer, not assumed.
            report = queue_worker.run_once(self.root, worker="laptop")
            stopper.join(timeout=5)

        self.assertEqual(report["outcome"], "cancelled", report.get("reason"))
        self.assertEqual(queue.job(self.root, "job-cancel")["state"], "cancelled")
        peer_states = [job["state"] for job in queue.list_jobs(self.peer_root)]
        self.assertEqual(peer_states, ["cancelled"])

    def test_a_job_that_names_no_configured_peer_is_not_sent_anywhere(self):
        # Defence in depth: a runner can outlive the peer it was registered for
        # (the peer was removed while its job was still queued).  The job must fail
        # with that reason here, not be sent to whatever the store says now.
        self.configure_peer()
        self.submit(job_id="job-orphan", peer_id="desk-gone")
        remote_engine.register_remote_handlers(
            peers_root=self.peers_root, out_dir=self.out_dir, engines=("node:desk-gone",)
        )
        report = queue_worker.run_once(self.root, worker="laptop")
        self.assertEqual(report["outcome"], "failed", report.get("reason"))
        record = queue.job(self.root, "job-orphan")
        self.assertIn("is not configured", record["errors"][-1])
        self.assertEqual(record["attempts"], 1)
        self.assertEqual(record["state"], "failed")

    def test_a_capability_the_peer_does_not_report_stops_the_job_here(self):
        with LivePeer(queue_root=self.peer_root, capabilities=("image_generation",)) as peer:
            self.configure_peer(base_url=peer.base_url, capabilities=["text_to_video"])
            self.submit(job_id="job-cap", params={"prompt": "x", "capability": "text_to_video"})
            report = self.delegate()
        self.assertEqual(report["outcome"], "failed", report.get("reason"))
        self.assertIn("does not report", queue.job(self.root, "job-cap")["errors"][-1])
        self.assertEqual(queue.list_jobs(self.peer_root), [])


class JobRoutingTest(unittest.TestCase):
    def test_the_engine_string_decides_the_peer(self):
        self.assertEqual(remote_engine.peer_id_from_engine("node:desk-1"), "desk-1")
        self.assertEqual(remote_engine.peer_id_from_engine("node:desk-1/../etc"), "")
        self.assertEqual(remote_engine.peer_id_from_engine("node:"), "")
        self.assertEqual(remote_engine.peer_id_from_engine("comfyui"), "")
        self.assertEqual(
            remote_engine.peer_id_from_job({"engine": "node:desk-1"}), "desk-1"
        )
        self.assertEqual(
            remote_engine.peer_id_from_job({"engine": "comfyui", "params": {"peer_id": "desk-1"}}),
            "desk-1",
        )
        self.assertEqual(remote_engine.peer_id_from_job({"engine": "comfyui"}), "")

    def test_local_only_keys_are_not_sent_to_the_peer(self):
        payload = remote_engine.remote_payload(
            {
                "job_id": "job-1",
                "project_id": "prod-1",
                "task": "video",
                "engine": "node:desk-1",
                "params": {"prompt": "x", "base_url": "http://127.0.0.1:8188", "peer_id": "desk-1"},
            },
            {"engine": "comfyui"},
            "maestro:job-1",
        )
        self.assertEqual(payload["engine"], "comfyui")
        self.assertEqual(payload["dedupe_key"], "maestro:job-1")
        self.assertNotIn("base_url", payload["params"])
        self.assertNotIn("peer_id", payload["params"])
        self.assertTrue(payload["cancel_supported"])


if __name__ == "__main__":
    unittest.main()

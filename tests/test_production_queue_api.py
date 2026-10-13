"""The production queue over HTTP, not just as a module (spec §10 / Prueba G).

These tests drive the real router with a real queue file on disk, so they cover
what the mobile client actually talks to: submit, claim, progress, cancellation,
completion, recovery, and the refusal cases (unknown job, illegal output path,
unknown setting, missing runner).
"""
from __future__ import annotations

import ast
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import generation_queue as queue  # noqa: E402
from services import production_queue_api  # noqa: E402
from services import queue_worker as worker  # noqa: E402


class QueueApiTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "queue")
        self.out_dir = os.path.join(self._tmp.name, "out")
        os.makedirs(self.out_dir, exist_ok=True)
        worker.clear_handlers()
        app = FastAPI()
        app.include_router(production_queue_api.create_router(root_provider=lambda: self.root))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://maestro.test"
        )

    async def asyncTearDown(self):
        worker.stop_worker(timeout=2.0)
        worker.clear_handlers()
        await self.client.aclose()
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def artifact(self, name="shot.mp4", content=b"frames"):
        path = os.path.join(self.out_dir, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    async def submit(self, job_id="job-1", **extra):
        payload = {
            "job_id": job_id,
            "project_id": "prod-1",
            "scene_id": "sc-1",
            "shot_id": "sh-1",
            "task": "video",
            "engine": "test-engine",
            "model": "model-a",
            "params": {"seed": 1},
            "inputs": ["/tmp/reference.png"],
        }
        payload.update(extra)
        response = await self.client.post("/api/v1/production/queue/jobs", json=payload)
        return response

    async def claimed(self, job_id="job-1", **extra):
        await self.submit(job_id=job_id, **extra)
        claim = await self.client.post("/api/v1/production/queue/claim", json={"worker": "node-1"})
        self.assertEqual(claim.status_code, 200, claim.text)
        self.assertEqual(claim.json()["claimed"]["job_id"], job_id)
        return claim.json()["claimed"]


class TestQueueReading(QueueApiTestCase):
    async def test_empty_queue_reports_zero_work_and_no_worker(self):
        response = await self.client.get("/api/v1/production/queue")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["jobs"], [])
        self.assertEqual(set(body["summary"]["counts"]), set(queue.STATES))
        self.assertEqual(body["summary"]["counts"]["pending"], 0)
        self.assertFalse(body["worker"]["running"])
        self.assertIn("only when the engine reported one", body["progress_note"])

    async def test_submitted_job_is_listed_and_readable(self):
        created = await self.submit()
        self.assertEqual(created.status_code, 200)
        self.assertTrue(created.json()["created"])
        listing = (await self.client.get("/api/v1/production/queue")).json()
        self.assertEqual([job["job_id"] for job in listing["jobs"]], ["job-1"])
        self.assertEqual(listing["summary"]["counts"]["pending"], 1)
        job = (await self.client.get("/api/v1/production/queue/jobs/job-1")).json()
        self.assertEqual(job["state"], "pending")
        self.assertEqual(job["params"], {"seed": 1})
        self.assertIsNone(job["progress"]["percent"])
        self.assertEqual(job["progress"]["source"], "none")

    async def test_unknown_job_is_404_and_filters_work(self):
        missing = await self.client.get("/api/v1/production/queue/jobs/nope")
        self.assertEqual(missing.status_code, 404)
        await self.submit(job_id="job-1", project_id="prod-1")
        await self.submit(job_id="job-2", project_id="prod-2")
        only_two = await self.client.get("/api/v1/production/queue", params={"project_id": "prod-2"})
        self.assertEqual([job["job_id"] for job in only_two.json()["jobs"]], ["job-2"])
        bad_state = await self.client.get("/api/v1/production/queue", params={"state": "nonsense"})
        self.assertEqual(bad_state.status_code, 400)

    async def test_events_endpoint_has_a_trail_and_a_job_filter(self):
        await self.submit()
        await self.client.post("/api/v1/production/queue/jobs/job-1/cancel", json={"reason": "x"})
        response = await self.client.get("/api/v1/production/queue/events")
        kinds = [event["kind"] for event in response.json()["events"]]
        self.assertIn("submitted", kinds)
        self.assertIn("cancelled", kinds)
        one = await self.client.get("/api/v1/production/queue/events", params={"job_id": "job-1"})
        self.assertTrue(one.json()["events"])


class TestQueueWrites(QueueApiTestCase):
    async def test_draft_is_not_claimable_until_moved_to_pending(self):
        await self.submit(job_id="job-1", start="held")
        self.assertEqual(queue.job(self.root, "job-1")["state"], "draft")
        claim = await self.client.post("/api/v1/production/queue/claim", json={})
        self.assertIsNone(claim.json()["claimed"])
        moved = await self.client.post(
            "/api/v1/production/queue/jobs/job-1/state", json={"state": "pending"})
        self.assertEqual(moved.status_code, 200)
        claim = await self.client.post("/api/v1/production/queue/claim", json={})
        self.assertEqual(claim.json()["claimed"]["job_id"], "job-1")

    async def test_duplicate_submission_returns_the_active_job(self):
        first = await self.submit(job_id="job-1", dedupe_key="shot-sh-1")
        second = await self.submit(job_id="job-2", dedupe_key="shot-sh-1")
        self.assertTrue(first.json()["created"])
        self.assertFalse(second.json()["created"])
        self.assertEqual(second.json()["job"]["job_id"], "job-1")
        listing = (await self.client.get("/api/v1/production/queue")).json()
        self.assertEqual(len(listing["jobs"]), 1)

    async def test_invalid_submissions_are_rejected_with_a_reason(self):
        unknown_task = await self.submit(task="teleport")
        self.assertEqual(unknown_task.status_code, 400)
        self.assertIn("teleport", unknown_task.json()["detail"])
        no_engine = await self.submit(engine="")
        self.assertEqual(no_engine.status_code, 400)
        self.assertIn("engine", no_engine.json()["detail"])

    async def test_settings_round_trip_and_unknown_key_refusal(self):
        defaults = (await self.client.get("/api/v1/production/queue/settings")).json()
        self.assertEqual(defaults["settings"]["max_concurrent"], 1)
        bad = await self.client.put(
            "/api/v1/production/queue/settings", json={"max_concurrency": 4})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("max_concurrency", bad.json()["detail"])
        good = await self.client.put(
            "/api/v1/production/queue/settings",
            json={"max_concurrent": 3, "memory_ceiling_mb": 12000, "max_retries": 2})
        self.assertEqual(good.json()["max_concurrent"], 3)
        self.assertEqual(good.json()["memory_ceiling_mb"], 12000)

    async def test_link_attaches_the_engine_job_id(self):
        await self.submit()
        linked = await self.client.post(
            "/api/v1/production/queue/jobs/job-1/link", json={"external_job_id": "prompt-42"})
        self.assertEqual(linked.json()["external_job_id"], "prompt-42")
        job = (await self.client.get("/api/v1/production/queue/jobs/job-1")).json()
        self.assertEqual(job["external_job_id"], "prompt-42")


class TestQueueProgressAndCompletion(QueueApiTestCase):
    async def test_progress_is_recorded_when_reported(self):
        await self.claimed()
        await self.client.post(
            "/api/v1/production/queue/jobs/job-1/state", json={"state": "running"})
        response = await self.client.post(
            "/api/v1/production/queue/jobs/job-1/progress",
            json={"percent": 40, "note": "step 4 of 10"})
        progress = response.json()["progress"]
        self.assertEqual(progress["percent"], 40)
        self.assertEqual(progress["source"], "reported")
        self.assertEqual(progress["note"], "step 4 of 10")

    async def test_engine_without_progress_keeps_percent_null(self):
        await self.claimed(job_id="job-quiet", progress_supported=False)
        response = await self.client.post(
            "/api/v1/production/queue/jobs/job-quiet/progress", json={"percent": 42})
        progress = response.json()["progress"]
        self.assertIsNone(progress["percent"])
        self.assertEqual(progress["source"], "none")
        self.assertIn("no detailed progress", progress["note"])

    async def test_out_of_range_percent_is_rejected(self):
        await self.claimed()
        response = await self.client.post(
            "/api/v1/production/queue/jobs/job-1/progress", json={"percent": "half"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("percent", response.json()["detail"])

    async def test_completion_requires_files_that_exist(self):
        await self.client.post(
            "/api/v1/production/queue/jobs/job-1/state", json={"state": "pending"})
        await self.claimed()
        missing = await self.client.post(
            "/api/v1/production/queue/jobs/job-1/complete",
            json={"outputs": ["/does/not/exist.mp4"]})
        self.assertEqual(missing.status_code, 400)
        self.assertIn("does not exist", missing.json()["detail"])
        self.assertEqual(queue.job(self.root, "job-1")["state"], "preparing")

        path = self.artifact()
        done = await self.client.post(
            "/api/v1/production/queue/jobs/job-1/complete", json={"outputs": [path]})
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.json()["state"], "completed")
        self.assertEqual(done.json()["outputs"], [path])

    async def test_completion_without_outputs_is_refused(self):
        await self.claimed()
        response = await self.client.post(
            "/api/v1/production/queue/jobs/job-1/complete", json={"outputs": []})
        self.assertEqual(response.status_code, 400)
        self.assertIn("must list the file", response.json()["detail"])

    async def test_failure_records_the_message_and_retry_is_bounded(self):
        await self.client.put(
            "/api/v1/production/queue/settings", json={"max_retries": 1})
        await self.claimed()
        failed = await self.client.post(
            "/api/v1/production/queue/jobs/job-1/fail",
            json={"error": "engine ran out of VRAM", "retryable": True})
        body = failed.json()
        self.assertEqual(body["state"], "pending")  # one automatic retry remains
        self.assertEqual(body["attempts"], 1)
        self.assertIn("engine ran out of VRAM", body["errors"][-1])
        await self.claimed(job_id="job-1")
        final = await self.client.post(
            "/api/v1/production/queue/jobs/job-1/fail",
            json={"error": "engine ran out of VRAM again", "retryable": True})
        self.assertEqual(final.json()["state"], "failed")
        no_retries = await self.client.post("/api/v1/production/queue/jobs/job-1/retry")
        self.assertEqual(no_retries.status_code, 400)
        self.assertIn("retry", no_retries.json()["detail"])


class TestQueueCancellation(QueueApiTestCase):
    async def test_pending_job_cancels_immediately(self):
        await self.submit()
        response = await self.client.post(
            "/api/v1/production/queue/jobs/job-1/cancel", json={"reason": "changed my mind"})
        body = response.json()
        self.assertTrue(body["cancelled"])
        self.assertEqual(body["action"], "cancelled_before_start")
        self.assertEqual(body["job"]["state"], "cancelled")

    async def test_engine_that_cannot_stop_only_records_the_request(self):
        await self.claimed(cancel_supported=False)
        response = await self.client.post(
            "/api/v1/production/queue/jobs/job-1/cancel", json={"reason": "stop"})
        body = response.json()
        self.assertFalse(body["cancelled"])
        self.assertEqual(body["action"], "requested")
        self.assertTrue(body["job"]["cancel_requested"])
        # Still held by the runner until it confirms.
        self.assertEqual(queue.job(self.root, "job-1")["state"], "preparing")
        acknowledged = await self.client.post(
            "/api/v1/production/queue/jobs/job-1/acknowledge-cancel")
        self.assertEqual(acknowledged.json()["state"], "cancelled")

    async def test_cancelling_a_finished_job_changes_nothing(self):
        await self.claimed()
        await self.client.post(
            "/api/v1/production/queue/jobs/job-1/complete", json={"outputs": [self.artifact()]})
        response = await self.client.post("/api/v1/production/queue/jobs/job-1/cancel", json={})
        body = response.json()
        self.assertFalse(body["cancelled"])
        self.assertEqual(body["action"], "none")
        self.assertEqual(body["job"]["state"], "completed")

    async def test_cancelled_job_can_be_requeued_by_hand(self):
        await self.submit()
        await self.client.post("/api/v1/production/queue/jobs/job-1/cancel", json={})
        retried = await self.client.post("/api/v1/production/queue/jobs/job-1/retry")
        self.assertEqual(retried.json()["state"], "pending")


class TestQueueRunning(QueueApiTestCase):
    async def test_run_executes_through_a_registered_handler(self):
        path = self.artifact()

        def handler(job, context):
            context.set_state("running")
            context.report(step=5, total_steps=10, note="half way")
            return [path]

        worker.register_handler("test-engine", handler)
        await self.submit()
        response = await self.client.post(
            "/api/v1/production/queue/run", json={"limit": 4, "worker": "node-1"})
        body = response.json()
        self.assertEqual(body["ran"], 1)
        self.assertEqual(body["outcomes"], {"completed": 1})
        job = queue.job(self.root, "job-1")
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["outputs"], [path])
        self.assertEqual(job["worker"], "node-1")
        self.assertEqual(job["progress"]["percent"], 100)

    async def test_run_reports_a_missing_runner_instead_of_pretending(self):
        await self.submit(job_id="job-1", engine="engine-nobody-runs")
        response = await self.client.post("/api/v1/production/queue/run", json={"limit": 2})
        body = response.json()
        self.assertEqual(body["outcomes"], {"no_handler": 1})
        job = queue.job(self.root, "job-1")
        self.assertEqual(job["state"], "failed")
        self.assertIn("No runner is registered", job["errors"][-1])

    async def test_worker_start_stop_over_http(self):
        path = self.artifact()
        worker.register_handler("test-engine", lambda job, context: [path])
        await self.submit()
        started = await self.client.post(
            "/api/v1/production/queue/worker/start", json={"interval": 0.2})
        self.assertEqual(started.status_code, 200)
        self.assertTrue(started.json()["started"])
        again = await self.client.post("/api/v1/production/queue/worker/start", json={})
        self.assertFalse(again.json()["started"])
        stopped = await self.client.post("/api/v1/production/queue/worker/stop")
        self.assertTrue(stopped.json()["stopped"])
        status = (await self.client.get("/api/v1/production/queue")).json()["worker"]
        self.assertFalse(status["running"])


class TestRecoveryOverHttp(QueueApiTestCase):
    def _age_job(self, job_id: str, *, seconds: float) -> None:
        """Rewrite a job's last-seen stamp, as if its worker had died mid-run."""
        state = queue.read_queue(self.root)
        state["jobs"][job_id]["updated_at"] = time.time() - seconds
        queue._save_queue(self.root, state)  # noqa: SLF001 - simulating a crash

    async def test_interrupted_job_is_requeued_by_recover(self):
        await self.client.put(
            "/api/v1/production/queue/settings",
            json={"stale_after_seconds": 1.0, "max_retries": 1})
        await self.submit()
        queue.claim_next(self.root, worker="dead-node")
        # Make the claim look abandoned (a worker that stopped speaking long ago).
        self._age_job("job-1", seconds=3600)
        response = await self.client.post("/api/v1/production/queue/recover")
        body = response.json()
        self.assertEqual(body["requeued"], ["job-1"])
        self.assertEqual(queue.job(self.root, "job-1")["state"], "pending")
        self.assertEqual(queue.job(self.root, "job-1")["attempts"], 1)

    async def test_recover_on_start_logs_and_never_deletes_work(self):
        await self.client.put(
            "/api/v1/production/queue/settings",
            json={"stale_after_seconds": 1.0, "max_retries": 0})
        await self.submit()
        queue.claim_next(self.root, worker="dead-node")
        self._age_job("job-1", seconds=3600)
        lines: list[str] = []
        result = production_queue_api.recover_on_start(self.root, print_line=lines.append)
        self.assertEqual(result["failed"], ["job-1"])
        self.assertTrue(lines and "Recovered after restart" in lines[0])
        self.assertEqual(queue.job(self.root, "job-1")["state"], "failed")
        self.assertEqual(len(queue.list_jobs(self.root)), 1)

    async def test_prune_keeps_active_jobs_and_trims_finished_ones(self):
        await self.submit(job_id="job-done", priority=5)
        await self.submit(job_id="job-live")
        path = self.artifact()
        # The priority-5 job is claimed first, so it is the one that finishes.
        self.assertEqual(queue.claim_next(self.root, worker="w")["claimed"]["job_id"], "job-done")
        queue.complete(self.root, "job-done", [path])
        self.assertEqual(queue.job(self.root, "job-live")["state"], "pending")
        # Only now bound the history, so the explicit prune is the thing tested.
        await self.client.put("/api/v1/production/queue/settings", json={"keep_terminal": 0})
        response = await self.client.post("/api/v1/production/queue/prune")
        self.assertEqual(response.json()["removed"], ["job-done"])
        remaining = [job["job_id"] for job in queue.list_jobs(self.root)]
        self.assertEqual(remaining, ["job-live"])


class TestApplicationWiring(unittest.TestCase):
    """The router must really be mounted on the app, and must not shadow anything.

    Parsed rather than imported: ``app/launch.py`` pulls in torch, Gradio and the
    model registry, which is exactly what the rest of this file avoids. This is
    the same AST-inspection approach the repo already uses for launch.py routes.
    """

    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parents[1]
        cls.source = (cls.root / "app" / "launch.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def app_route_paths(self) -> set[str]:
        paths = set()
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
                        if getattr(decorator.func.value, "id", "") == "api" and decorator.args:
                            if isinstance(decorator.args[0], ast.Constant):
                                paths.add(str(decorator.args[0].value))
        return paths

    def router_paths(self) -> list[str]:
        router = production_queue_api.create_router(root_provider=lambda: "/tmp/queue-wiring")
        return [route.path for route in router.routes]

    def test_router_is_included_and_recovery_runs_at_startup(self):
        includes = [
            ast.unparse(node) for node in self.tree.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "include_router"
        ]
        self.assertTrue(
            any("production_queue_api.create_router" in call for call in includes),
            f"the queue router is not mounted on the app: {includes}",
        )
        self.assertIn(
            'api.add_event_handler("startup", _recover_generation_queue_on_start)',
            self.source,
        )

    def test_queue_root_is_under_the_settings_directory(self):
        self.assertIn('os.path.join(_app_dir, "settings", "generation_queue")', self.source)

    def test_the_module_level_calls_come_after_the_names_they_use(self):
        """A forward reference here would be a NameError at import, not a warning.

        ``launch.py`` cannot be imported in a dependency-light environment, so the
        order is asserted on the parsed source instead.
        """
        defined = {
            node.name: node.lineno
            for node in self.tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("_film_queue_root", defined)
        self.assertIn("_recover_generation_queue_on_start", defined)
        call_lines: dict[str, int] = {}
        for node in self.tree.body:
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if isinstance(call.func, ast.Attribute):
                call_lines[call.func.attr] = node.lineno
        self.assertLess(defined["_film_queue_root"], call_lines["include_router"])
        self.assertLess(defined["_recover_generation_queue_on_start"], call_lines["add_event_handler"])

    def test_no_route_is_shadowed_by_the_router(self):
        app_paths = self.app_route_paths()
        overlapping = sorted(set(self.router_paths()) & app_paths)
        self.assertEqual(overlapping, [])

    def test_router_exposes_the_whole_documented_surface(self):
        paths = set(self.router_paths())
        # 19 queue operations plus the two endpoints a peer Maestro uses to hand
        # a delegated job's artefacts back (services.remote_engine).
        self.assertEqual(len(paths), 21)
        for expected in (
            "/api/v1/production/queue",
            "/api/v1/production/queue/jobs",
            "/api/v1/production/queue/jobs/{job_id}",
            "/api/v1/production/queue/jobs/{job_id}/cancel",
            "/api/v1/production/queue/jobs/{job_id}/acknowledge-cancel",
            "/api/v1/production/queue/jobs/{job_id}/outputs",
            "/api/v1/production/queue/jobs/{job_id}/outputs/{index}",
            "/api/v1/production/queue/recover",
            "/api/v1/production/queue/worker/start",
        ):
            self.assertIn(expected, paths)

    def test_ui_client_calls_only_paths_that_exist(self):
        """Every path the panel builds must be one the backend actually serves.

        ``${...}`` is kept as a ``{x}`` placeholder so templated paths (a job's
        id, then a sub-action) are compared in full, not merely by prefix. A
        trailing placeholder is dropped only when the remainder is itself a real
        route, which is how the client's optional query string is expressed.
        """
        client = (self.root / "ui" / "src" / "api" / "queue.ts").read_text(encoding="utf-8")
        templated = re.sub(r"\$\{[^}]*\}", "{x}", client)
        called = set(re.findall(r"[`'](/api/v1/production/queue[^`'? ]*)", templated))
        self.assertGreaterEqual(len(called), 11)
        known = {re.sub(r"\{[^}]*\}", "{x}", path).rstrip("/") for path in self.router_paths()}
        for path in called:
            normalized = re.sub(r"\{[^}]*\}", "{x}", path).rstrip("/")
            candidates = [normalized]
            if normalized.endswith("{x}"):
                candidates.append(normalized[: -len("{x}")].rstrip("/"))
            self.assertTrue(
                any(candidate in known for candidate in candidates),
                f"the UI calls {path}, which the backend does not serve",
            )


if __name__ == "__main__":
    unittest.main()

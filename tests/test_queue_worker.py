"""Queue worker: routing, honesty rules, limits, cancellation and the thread.

The tests that matter most are the ones asserting the worker *refuses* to
invent success: no handler, no artefacts, or a path that does not exist must all
end as a recorded failure, never as a completed job.
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import generation_queue as queue  # noqa: E402
from services import queue_worker  # noqa: E402
from services.queue_worker import (  # noqa: E402
    HandlerUnavailable,
    JobCancelled,
    clear_handlers,
    drain,
    handler_names,
    register_handler,
    run_once,
    start_worker,
    stop_worker,
    unregister_handler,
    worker_status,
)


class WorkerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "queue")
        self.out_dir = os.path.join(self._tmp.name, "out")
        os.makedirs(self.out_dir, exist_ok=True)
        clear_handlers()

    def tearDown(self):
        stop_worker(timeout=2.0)
        clear_handlers()
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def artifact(self, name="shot.mp4", content=b"data"):
        path = os.path.join(self.out_dir, name)
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def add(self, job_id="job-1", *, engine="local:maestro", task="video", now=100.0, **extra):
        payload = {
            "job_id": job_id,
            "project_id": "prod-1",
            "scene_id": "sc-1",
            "shot_id": "sh-1",
            "task": task,
            "engine": engine,
            "model": "model-a",
            "params": {"seed": 1},
            "inputs": ["/tmp/input.png"],
        }
        payload.update(extra)
        return queue.submit(self.root, payload, now=now)


class TestHandlerRegistry(WorkerTestCase):
    def test_register_and_unregister(self):
        register_handler("local:maestro", lambda job, ctx: [])
        self.assertEqual(handler_names(), ["local:maestro"])
        self.assertTrue(unregister_handler("local:maestro"))
        self.assertEqual(handler_names(), [])

    def test_duplicate_registration_is_refused_unless_replaced(self):
        register_handler("local:maestro", lambda job, ctx: [])
        with self.assertRaises(ValueError):
            register_handler("local:maestro", lambda job, ctx: [])
        register_handler("local:maestro", lambda job, ctx: [], replace=True)
        self.assertEqual(handler_names(), ["local:maestro"])

    def test_empty_engine_and_non_callable_are_rejected(self):
        with self.assertRaises(ValueError):
            register_handler("   ", lambda job, ctx: [])
        with self.assertRaises(TypeError):
            register_handler("local:maestro", "not-callable")


class TestRunOnce(WorkerTestCase):
    def test_idle_queue_reports_idle_not_a_run(self):
        report = run_once(self.root)
        self.assertFalse(report["claimed"])
        self.assertEqual(report["outcome"], "idle")

    def test_missing_handler_fails_the_job_without_pretending(self):
        self.add(engine="comfyui")
        report = run_once(self.root)
        self.assertEqual(report["outcome"], "no_handler")
        record = queue.job(self.root, "job-1")
        self.assertEqual(record["state"], "failed")
        self.assertIn("comfyui", record["errors"][-1])
        self.assertIn("No runner is registered", record["errors"][-1])
        # The attempt is counted, but nothing is scheduled to run again: a
        # machine without the engine will not grow one by retrying.
        self.assertEqual(record["attempts"], 1)
        self.assertFalse(any(
            event["kind"] == "retry_scheduled" for event in queue.events(self.root, job_id="job-1")
        ))

    def test_successful_handler_records_real_outputs(self):
        path = self.artifact()

        def handler(job, ctx):
            ctx.set_state("running")
            ctx.set_state("finalizing")
            return [path]

        register_handler("local:maestro", handler)
        self.add(job_id="job-1")
        report = run_once(self.root, worker="desk-1")
        self.assertEqual(report["outcome"], "completed")
        record = queue.job(self.root, "job-1")
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["outputs"], [path])
        self.assertEqual(record["worker"], "desk-1")
        self.assertIsNotNone(record["started_at"])
        self.assertIsNotNone(record["finished_at"])

    def test_handler_without_output_is_a_failure(self):
        register_handler("local:maestro", lambda job, ctx: [])
        self.add(job_id="job-1")
        report = run_once(self.root)
        self.assertEqual(report["outcome"], "failed")
        record = queue.job(self.root, "job-1")
        self.assertIn("no output file", record["errors"][-1])
        # Retryable, so the job went back to pending (max_retries defaults to 1).
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["attempts"], 1)

    def test_output_that_does_not_exist_is_not_a_completion(self):
        register_handler("local:maestro", lambda job, ctx: ["/nope/missing.mp4"])
        self.add(job_id="job-1")
        report = run_once(self.root)
        self.assertEqual(report["outcome"], "failed")
        record = queue.job(self.root, "job-1")
        self.assertEqual(record["state"], "failed")
        self.assertIn("/nope/missing.mp4", record["errors"][-1])

    def test_relative_output_path_is_rejected(self):
        register_handler("local:maestro", lambda job, ctx: ["shot.mp4"])
        self.add(job_id="job-1")
        report = run_once(self.root)
        self.assertEqual(report["outcome"], "failed")
        self.assertIn("does not exist on disk", queue.job(self.root, "job-1")["errors"][-1])

    def test_exception_in_handler_is_recorded_with_its_message(self):
        def handler(job, ctx):
            raise RuntimeError("CUDA out of memory")

        register_handler("local:maestro", handler)
        self.add(job_id="job-1")
        report = run_once(self.root)
        self.assertEqual(report["outcome"], "failed")
        self.assertIn("CUDA out of memory", report["reason"])
        record = queue.job(self.root, "job-1")
        self.assertIn("RuntimeError: CUDA out of memory", record["errors"][-1])

    def test_explicit_handlers_mapping_excludes_global_registry(self):
        register_handler("local:maestro", lambda job, ctx: [self.artifact()])
        self.add(job_id="job-1")
        report = run_once(self.root, handlers={})
        self.assertEqual(report["outcome"], "no_handler")

    def test_progress_is_forwarded_only_when_the_handler_reports_it(self):
        path = self.artifact()

        def quiet(job, ctx):
            return [path]

        register_handler("local:maestro", quiet)
        self.add(job_id="job-1")
        run_once(self.root)
        record = queue.job(self.root, "job-1")
        self.assertIsNone(record["progress"]["percent"])
        self.assertEqual(record["progress"]["source"], "none")

        self.add(job_id="job-2")
        seen = []

        def loud(job, ctx):
            seen.append(ctx.report(step=2, total_steps=8, note="step 2 of 8")["progress"])
            return [path]

        register_handler("local:maestro", loud, replace=True)
        run_once(self.root)
        # A step count the engine gave us is a measurement: 2 of 8 steps.
        self.assertEqual(seen[0]["percent"], 25)
        self.assertEqual(seen[0]["step"], 2)
        self.assertEqual(seen[0]["total_steps"], 8)
        self.assertEqual(seen[0]["source"], "reported")
        # Finishing a job the engine reported progress for means 100%, and only
        # because this job declared it reports progress at all.
        record = queue.job(self.root, "job-2")
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["progress"]["percent"], 100)

    def test_engine_that_reports_no_progress_keeps_percent_none(self):
        path = self.artifact()

        def handler(job, ctx):
            ctx.report(percent=42, note="ignored")
            return [path]

        register_handler("local:maestro", handler)
        queue.configure(self.root, {"max_concurrent": 1})
        queue.submit(self.root, {
            "job_id": "job-quiet", "task": "video", "engine": "no-progress-engine",
            "progress_supported": False,
        }, now=100.0)
        register_handler("no-progress-engine", handler)
        run_once(self.root)
        record = queue.job(self.root, "job-quiet")
        self.assertIsNone(record["progress"]["percent"])
        self.assertEqual(record["progress"]["source"], "none")
        self.assertIn("no detailed progress", record["progress"]["note"])

    def test_priority_decides_which_job_runs_first(self):
        register_handler("local:maestro", lambda job, ctx: [self.artifact()])
        self.add(job_id="low", priority=0, created_at=1.0)
        self.add(job_id="high", priority=10, created_at=2.0)
        run_once(self.root)
        self.assertEqual(queue.job(self.root, "high")["state"], "completed")
        self.assertEqual(queue.job(self.root, "low")["state"], "pending")

    def test_duplicate_submission_runs_once(self):
        register_handler("local:maestro", lambda job, ctx: [self.artifact()])
        first = self.add(job_id="job-1", dedupe_key="shot-sh-1")
        second = self.add(job_id="job-2", dedupe_key="shot-sh-1")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["job"]["job_id"], "job-1")
        result = drain(self.root)
        self.assertEqual(result["ran"], 1)

    def test_concurrency_limit_is_respected(self):
        def handler(job, ctx):
            raise JobCancelled()

        register_handler("local:maestro", handler)
        queue.configure(self.root, {"max_concurrent": 1})
        # Claim one job and leave it running, then ask for another.
        self.add(job_id="job-1")
        claimed = queue.claim_next(self.root, worker="w")
        self.assertTrue(claimed["claimed"])
        second = queue.claim_next(self.root, worker="w")
        self.assertIsNone(second["claimed"])
        self.assertIn("Concurrency limit", second["reason"])

    def test_memory_ceiling_skips_a_job_that_would_not_fit(self):
        queue.configure(self.root, {"memory_ceiling_mb": 1000, "max_concurrent": 3})
        self.add(job_id="big", memory_mb=900, created_at=1.0)
        self.add(job_id="small", memory_mb=200, created_at=2.0)
        first = queue.claim_next(self.root, worker="w")
        self.assertEqual(first["claimed"]["job_id"], "big")
        # 900 MB is in use; the 200 MB job would exceed the 1000 MB ceiling.
        second = queue.claim_next(self.root, worker="w")
        self.assertIsNone(second["claimed"])
        self.assertIn("memory ceiling", second["reason"])
        self.assertEqual(queue.job(self.root, "small")["state"], "pending")

    def test_job_above_the_ceiling_is_refused_at_submission(self):
        queue.configure(self.root, {"memory_ceiling_mb": 500})
        with self.assertRaises(queue.GenerationQueueError):
            self.add(job_id="huge", memory_mb=4000)

    def test_bounded_retries_then_failure(self):
        calls = {"n": 0}

        def flaky(job, ctx):
            calls["n"] += 1
            raise RuntimeError(f"attempt {calls['n']} failed")

        register_handler("local:maestro", flaky)
        queue.configure(self.root, {"max_retries": 2, "retry_delay_seconds": 0.0})
        self.add(job_id="job-1")
        outcomes = [run_once(self.root)["outcome"] for _ in range(3)]
        self.assertEqual(outcomes, ["failed", "failed", "failed"])
        record = queue.job(self.root, "job-1")
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["attempts"], 3)
        self.assertEqual(len(record["errors"]), 3)


class TestCancellation(WorkerTestCase):
    def test_handler_that_stops_on_request_acknowledges_the_cancellation(self):
        path = self.artifact()

        def handler(job, ctx):
            # This engine declared it cannot stop itself, so the request stays a
            # request until the runner confirms it really stopped.
            queue.cancel(self.root, job["job_id"], reason="user asked")
            self.assertTrue(ctx.cancelled())
            return [path]

        # Reported artefacts are withheld: the work was cancelled, not finished.
        self.add(job_id="job-1", cancel_supported=False)
        report = run_once(self.root, handlers={"local:maestro": handler})
        self.assertEqual(report["outcome"], "cancelled")
        record = queue.job(self.root, "job-1")
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(record["outputs"], [])

    def test_handler_raising_job_cancelled_is_recorded_as_cancelled(self):
        def handler(job, ctx):
            queue.cancel(self.root, job["job_id"], reason="user asked")
            raise JobCancelled()

        self.add(job_id="job-2", cancel_supported=False)
        report = run_once(self.root, handlers={"local:maestro": handler})
        self.assertEqual(report["outcome"], "cancelled")
        record = queue.job(self.root, "job-2")
        self.assertEqual(record["state"], "cancelled")
        self.assertEqual(record["errors"], [])

    def test_job_cancelled_between_claim_and_start(self):
        """The narrow race the branch exists for: cancelled just after the claim."""
        self.add(job_id="job-1")
        claimed = dict(queue.job(self.root, "job-1"), cancel_requested=True)
        called = {"n": 0}

        def handler(job, ctx):  # pragma: no cover - must never be reached
            called["n"] += 1
            return [self.artifact()]

        with patch.object(queue_worker.queue, "claim_next",
                          return_value={"claimed": claimed, "reason": "", "waiting": 0}), \
                patch.object(queue_worker.queue, "job", return_value=claimed):
            report = run_once(self.root, handlers={"local:maestro": handler})
        self.assertEqual(report["outcome"], "cancelled_before_start")
        self.assertEqual(called["n"], 0)
        self.assertEqual(queue.job(self.root, "job-1")["state"], "cancelled")

    def test_job_cancelled_while_pending_never_claims_a_handler(self):
        self.add(job_id="job-1")
        result = queue.cancel(self.root, "job-1", reason="changed my mind")
        self.assertTrue(result["cancelled"])
        report = run_once(self.root)
        self.assertFalse(report["claimed"])
        self.assertEqual(report["outcome"], "idle")


class TestRecoveryAndDrain(WorkerTestCase):
    def test_drain_reports_honest_outcome_counts(self):
        self.add(job_id="job-1", engine="engine-a")
        self.add(job_id="job-2", engine="engine-b")
        register_handler("engine-a", lambda job, ctx: [self.artifact("a.mp4")])
        register_handler("engine-b", lambda job, ctx: [self.artifact("b.mp4")])
        result = drain(self.root, limit=10)
        self.assertEqual(result["ran"], 2)
        self.assertEqual(result["outcomes"], {"completed": 2})

    def test_drain_stops_at_the_limit(self):
        for index in range(5):
            self.add(job_id=f"job-{index}")
        register_handler("local:maestro", lambda job, ctx: [self.artifact()])
        result = drain(self.root, limit=2)
        self.assertEqual(result["ran"], 2)

    def test_interrupted_job_is_recovered_when_the_worker_starts(self):
        queue.configure(self.root, {"stale_after_seconds": 1.0, "max_retries": 1})
        self.add(job_id="job-1")
        claimed = queue.claim_next(self.root, worker="old-worker", now=time.time() - 60)
        self.assertEqual(claimed["claimed"]["state"], "preparing")
        started = start_worker(self.root, interval=5.0)
        self.assertTrue(started["started"])
        self.assertEqual(started["recovered"]["requeued"], ["job-1"])
        self.assertEqual(queue.job(self.root, "job-1")["state"], "pending")

    def test_start_worker_is_idempotent(self):
        first = start_worker(self.root, interval=5.0)
        second = start_worker(self.root, interval=5.0)
        self.assertTrue(first["started"])
        self.assertFalse(second["started"])
        self.assertIn("already running", second["reason"])
        stopped = stop_worker()
        self.assertTrue(stopped["stopped"])

    def test_background_worker_runs_a_job_and_stop_is_clean(self):
        path = self.artifact()
        register_handler("local:maestro", lambda job, ctx: [path])
        self.add(job_id="job-1")
        start_worker(self.root, interval=0.2)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if queue.job(self.root, "job-1")["state"] == "completed":
                break
            time.sleep(0.05)
        self.assertEqual(queue.job(self.root, "job-1")["state"], "completed")
        stopped = stop_worker()
        self.assertTrue(stopped["stopped"])
        self.assertFalse(worker_status()["running"])
        self.assertGreaterEqual(worker_status()["ticks"], 1)
        self.assertEqual(worker_status()["last_outcome"], "completed")


class TestErrorTypes(unittest.TestCase):
    def test_error_types_are_distinct(self):
        self.assertTrue(issubclass(HandlerUnavailable, RuntimeError))
        self.assertTrue(issubclass(JobCancelled, RuntimeError))


if __name__ == "__main__":
    unittest.main()

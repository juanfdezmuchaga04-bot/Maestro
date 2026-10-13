"""The ComfyUI engine through the persistent queue (spec §10, §18).

The two behaviours this file exists to pin down:

* **A computer that is off is not a failure.**  The job is deferred — no attempt
  consumed, no error recorded — and it is still there, waiting, when the engine
  comes back.
* **A clip or nothing.**  The job completes with a downloaded, verified video in
  the workspace, and the progress it reports is ComfyUI's own state, never a
  percentage somebody invented.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from comfy_stub import RunningStub, StubComfy, reset_stub  # noqa: E402
from services import comfy_jobs, comfy_models, generation_queue as queue, queue_worker  # noqa: E402


class QueueTestCase(unittest.TestCase):
    def setUp(self):
        reset_stub()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "queue")
        self.out_dir = os.path.join(self._tmp.name, "workspace")
        os.makedirs(self.out_dir, exist_ok=True)
        queue_worker.clear_handlers()

    def tearDown(self):
        queue_worker.clear_handlers()

    def submit(self, job_id="job-1", *, engine="comfyui", params=None, **extra):
        payload = {
            "job_id": job_id,
            "project_id": "prod-1",
            "shot_id": "sh-1",
            "task": "video",
            "engine": engine,
            "model": "wan22_ti2v_5b",
            "params": params if params is not None else {"prompt": "a paper boat"},
            "cancel_supported": True,
        }
        payload.update(extra)
        return queue.submit(self.root, payload, now=100.0)


class DeferTest(QueueTestCase):
    def test_a_held_job_can_be_deferred_without_consuming_an_attempt(self):
        self.submit()
        queue.claim_next(self.root, worker="node-1", now=101.0)
        deferred = queue.defer(self.root, "job-1", reason="the computer is off", now=102.0)
        self.assertEqual(deferred["state"], "pending")
        self.assertEqual(deferred["deferrals"], 1)
        self.assertEqual(deferred["attempts"], 0)
        self.assertEqual(deferred["errors"], [])
        self.assertIn("computer is off", deferred["deferred_reason"])
        self.assertGreater(deferred["available_at"], 102.0)

    def test_a_deferred_job_is_not_claimed_until_its_delay_passes(self):
        self.submit()
        queue.claim_next(self.root, worker="node-1", now=101.0)
        deferred = queue.defer(self.root, "job-1", reason="off", delay_seconds=60.0, now=102.0)
        self.assertIsNone(queue.claim_next(self.root, worker="node-1", now=110.0)["claimed"])
        later = queue.claim_next(self.root, worker="node-1", now=float(deferred["available_at"]) + 1)
        self.assertEqual(later["claimed"]["job_id"], "job-1")

    def test_deferring_records_its_own_event_and_leaves_no_failure(self):
        self.submit()
        queue.claim_next(self.root, worker="node-1", now=101.0)
        queue.defer(self.root, "job-1", reason="off", now=102.0)
        kinds = [event["kind"] for event in queue.events(self.root, job_id="job-1")]
        self.assertIn("deferred", kinds)
        self.assertNotIn("failed", kinds)
        summary = queue.summary(self.root, now=103.0)
        self.assertEqual(summary["deferred"], 1)
        self.assertEqual(summary["counts"]["pending"], 1)
        self.assertEqual(summary["deferred_detail"][0]["job_id"], "job-1")

    def test_only_a_held_job_can_be_deferred(self):
        self.submit()
        with self.assertRaises(queue.GenerationQueueError):
            queue.defer(self.root, "job-1", reason="not held yet")

    def test_the_worker_reports_a_deferral_as_deferred(self):
        def handler(job, context):
            context.defer("the node is away")
            return []

        self.submit()
        report = queue_worker.run_once(self.root, handlers={"comfyui": handler})
        self.assertEqual(report["outcome"], "deferred")
        self.assertEqual(report["deferrals"], 1)
        record = queue.job(self.root, "job-1")
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["attempts"], 0)

    def test_the_worker_honours_a_terminal_state_the_handler_set_itself(self):
        def handler(job, context):
            context.fail("this cannot be fixed by retrying", retryable=False)
            return []

        self.submit()
        report = queue_worker.run_once(self.root, handlers={"comfyui": handler})
        self.assertEqual(report["outcome"], "failed")
        self.assertTrue(report["decided_by_handler"])
        record = queue.job(self.root, "job-1")
        self.assertEqual(record["state"], "failed")
        self.assertEqual(len(record["errors"]), 1)
        self.assertFalse(any(e["kind"] == "retry_scheduled" for e in queue.events(self.root, job_id="job-1")))


class ComfyJobTest(QueueTestCase):
    def test_a_job_runs_on_comfyui_and_comes_back_as_a_real_clip(self):
        with RunningStub() as stub:
            self.submit(params={
                "prompt": "a paper boat drifting down a small stream",
                "base_url": stub.base_url,
                "width": 256,
                "height": 256,
                "length": 9,
                "steps": 4,
            })
            registered = comfy_jobs.register_comfyui_handler(out_dir=self.out_dir)
            self.assertEqual(registered, ["comfyui"])
            report = queue_worker.run_once(self.root, worker="desk-1")

        self.assertEqual(report["outcome"], "completed", report.get("reason"))
        record = queue.job(self.root, "job-1")
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["external_job_id"], "pid1")
        self.assertEqual(len(record["outputs"]), 1)
        clip = record["outputs"][0]
        self.assertTrue(os.path.isfile(clip), clip)
        self.assertEqual(os.path.dirname(clip), os.path.realpath(self.out_dir))
        with open(clip, "rb") as handle:
            self.assertEqual(handle.read(12)[4:8], b"ftyp")  # a real container, not a renamed text file
        # Real progress only: ComfyUI's HTTP API reports no percentage.
        self.assertIsNone(record["progress"]["percent"])
        self.assertEqual(record["progress"]["source"], "none")
        self.assertIn("clip retrieved", record["progress"]["note"])

    def test_a_job_that_names_one_server_runs_through_that_engine_id(self):
        with RunningStub() as stub:
            comfy_jobs.register_comfyui_handler(
                engines=("comfyui", f"comfyui:{stub.base_url}"), out_dir=self.out_dir
            )
            self.submit(engine=f"comfyui:{stub.base_url}")
            report = queue_worker.run_once(self.root)
            self.assertEqual(report["outcome"], "completed", report.get("reason"))
        self.assertEqual(queue.job(self.root, "job-1")["engine"], f"comfyui:{stub.base_url}")

    def test_an_unreachable_computer_keeps_the_job_pending(self):
        self.submit(params={"prompt": "a paper boat", "base_url": "http://127.0.0.1:1"})
        comfy_jobs.register_comfyui_handler(out_dir=self.out_dir)
        report = queue_worker.run_once(self.root)
        self.assertEqual(report["outcome"], "deferred")
        self.assertIn("did not answer", report["reason"])
        record = queue.job(self.root, "job-1")
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["attempts"], 0)
        self.assertEqual(record["errors"], [])
        self.assertEqual(len(queue.list_jobs(self.root)), 1)

    def test_a_missing_model_file_fails_with_the_exact_file_names(self):
        with RunningStub() as stub:
            StubComfy.files = {"diffusion_models": [], "text_encoders": [], "vae": []}
            try:
                self.submit(params={"prompt": "x", "base_url": stub.base_url})
                comfy_jobs.register_comfyui_handler(out_dir=self.out_dir)
                report = queue_worker.run_once(self.root)
            finally:
                reset_stub()
        self.assertEqual(report["outcome"], "failed")
        record = queue.job(self.root, "job-1")
        self.assertIn("wan2.2_ti2v_5B_fp16.safetensors", record["errors"][-1])
        self.assertIn("ComfyUI/models/diffusion_models", record["errors"][-1])
        self.assertEqual(record["state"], "failed")
        self.assertEqual(record["attempts"], 1)

    def test_a_server_without_the_nodes_fails_with_the_missing_classes(self):
        with RunningStub() as stub:
            StubComfy.node_classes = ["KSampler"]
            try:
                self.submit(params={"prompt": "x", "base_url": stub.base_url})
                comfy_jobs.register_comfyui_handler(out_dir=self.out_dir)
                report = queue_worker.run_once(self.root)
            finally:
                reset_stub()
        self.assertEqual(report["outcome"], "failed")
        self.assertIn("SaveVideo", queue.job(self.root, "job-1")["errors"][-1])

    def test_a_job_without_a_prompt_is_refused_instead_of_guessed(self):
        with RunningStub() as stub:
            self.submit(params={"base_url": stub.base_url})
            comfy_jobs.register_comfyui_handler(out_dir=self.out_dir)
            report = queue_worker.run_once(self.root)
        self.assertEqual(report["outcome"], "failed")
        self.assertIn("no prompt", queue.job(self.root, "job-1")["errors"][-1])

    def test_the_handler_defers_instead_of_failing_when_the_render_is_slow(self):
        with RunningStub() as stub, tempfile.TemporaryDirectory() as tmp:
            StubComfy.finished = False
            try:
                job = {"job_id": "job-slow", "params": {"prompt": "x", "base_url": stub.base_url},
                       "node_id": "", "engine": "comfyui"}

                class Context:
                    def __init__(self):
                        self.deferred = None

                    def fail(self, *args, **kwargs):  # pragma: no cover - not expected
                        raise AssertionError("should not fail")

                    def defer(self, reason, **kwargs):
                        self.deferred = reason

                    def link(self, *args, **kwargs):
                        return {}

                    def set_state(self, *args, **kwargs):
                        return {}

                    def report(self, **kwargs):
                        return {}

                    def cancelled(self):
                        return False

                context = Context()
                outputs = comfy_jobs.run_job(
                    job, context, out_dir=tmp, poll_timeout=0.05, poll_interval=0.01,
                    sleep=lambda seconds: None,
                )
            finally:
                reset_stub()
        self.assertEqual(outputs, [])
        self.assertIn("still working", context.deferred)

    def test_the_engine_is_not_routed_when_no_handler_is_registered(self):
        with RunningStub() as stub:
            self.submit(params={"prompt": "x", "base_url": stub.base_url})
            report = queue_worker.run_once(self.root)
        self.assertEqual(report["outcome"], "no_handler")
        self.assertEqual(queue.job(self.root, "job-1")["state"], "failed")

    def test_the_catalogue_model_answers_for_the_default_job_engine(self):
        self.assertEqual(comfy_jobs.ENGINE, "comfyui")
        model = comfy_models.get_model("wan22_ti2v_5b")
        self.assertEqual(model.templates, ("wan22_ti2v_5b_txt2video",))
        self.assertIn("unet_name", model.template_params)
        payload = json.dumps(model.describe())
        self.assertIn("Apache-2.0", payload)
        self.assertIn("1280x704", payload)


if __name__ == "__main__":
    unittest.main()

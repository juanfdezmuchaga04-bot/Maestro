"""Generation queue: states, priorities, retries, cancellation, recovery.

The progress tests are the important ones: this module must never invent a
percentage, and a job whose engine reports nothing must keep ``percent = None``
from submission to completion.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services.generation_queue import (  # noqa: E402
    ACTIVE_STATES,
    RUNNING_STATES,
    STATES,
    TERMINAL_STATES,
    GenerationQueueError,
    acknowledge_cancel,
    cancel,
    claim_next,
    complete,
    configure,
    events,
    fail,
    job,
    link_external_job,
    list_jobs,
    prune,
    read_queue,
    recover,
    report_progress,
    retry,
    set_state,
    settings,
    submit,
    summary,
    system_memory_mb,
)


class QueueTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "queue")

    def tearDown(self):
        self._tmp.cleanup()

    def add(self, job_id="job-1", *, task="video", engine="local:maestro", now=100.0, **extra):
        payload = {
            "job_id": job_id,
            "project_id": "prod-1",
            "scene_id": "sc-1",
            "shot_id": "sh-1",
            "task": task,
            "engine": engine,
            "model": "wan-2.2",
            "params": {"duration": 5},
            "inputs": ["/tmp/in.png"],
            **extra,
        }
        return submit(self.root, payload, now=now)

    def queue_file(self):
        return Path(self.root, "queue.json")

    def run_to_running(self, job_id="job-1", now=200.0):
        """Submit (if needed), claim and mark running."""
        if self.is_empty():
            self.add(job_id)
        claimed = claim_next(self.root, worker="w1", now=now)
        self.assertIsNotNone(claimed["claimed"], "nothing was claimable")
        set_state(self.root, job_id, "running", now=now + 1)
        return claimed

    def is_empty(self):
        return not read_queue(self.root)["jobs"]


class ContractTest(QueueTestCase):
    def test_states_and_their_partition_are_what_the_spec_asks_for(self):
        self.assertEqual(
            STATES,
            ("draft", "pending", "preparing", "running", "finalizing",
             "completed", "failed", "cancelled"),
        )
        self.assertEqual(set(ACTIVE_STATES) - set(STATES), set())
        self.assertEqual(set(RUNNING_STATES) - set(ACTIVE_STATES), set())
        self.assertEqual(set(TERMINAL_STATES) - set(STATES), set())

    def test_a_job_cannot_be_submitted_without_an_engine_or_a_known_task(self):
        with self.assertRaises(GenerationQueueError) as ctx:
            submit(self.root, {"job_id": "a", "task": "video"})
        self.assertIn("must name the engine", str(ctx.exception))
        with self.assertRaises(GenerationQueueError) as ctx:
            submit(self.root, {"job_id": "a", "engine": "local:x", "task": "sculpture"})
        self.assertIn("Unknown task", str(ctx.exception))

    def test_settings_reject_unknown_keys_and_keep_concurrency_usable(self):
        with self.assertRaises(GenerationQueueError) as ctx:
            configure(self.root, {"concurrency": 2})
        self.assertIn("concurrency", str(ctx.exception))
        updated = configure(self.root, {"max_concurrent": 0})
        self.assertEqual(updated["max_concurrent"], 1)
        self.assertEqual(settings(self.root)["memory_ceiling_mb"], 0)


class SubmitTest(QueueTestCase):
    def test_every_field_the_spec_requires_is_stored(self):
        self.add(memory_mb=4096, priority=3)
        stored = job(self.root, "job-1")
        for field in (
            "project_id", "scene_id", "shot_id", "task", "engine", "model", "params",
            "inputs", "node_id", "state", "attempts", "max_retries", "memory_mb",
            "outputs", "errors", "progress", "created_at", "updated_at",
        ):
            self.assertIn(field, stored, field)
        self.assertEqual(stored["state"], "pending")
        self.assertEqual(stored["attempts"], 0)
        self.assertEqual(stored["memory_mb"], 4096)
        self.assertEqual(stored["priority"], 3)

    def test_a_held_job_starts_as_a_draft_and_is_not_runnable(self):
        self.add(job_id="draft-1", draft=True)
        self.assertEqual(job(self.root, "draft-1")["state"], "draft")
        self.assertIsNone(claim_next(self.root, now=200.0)["claimed"])
        set_state(self.root, "draft-1", "pending", now=201.0)
        self.assertIsNotNone(claim_next(self.root, now=202.0)["claimed"])

    def test_an_accidental_double_submit_returns_the_existing_job(self):
        first = submit(self.root, {
            "job_id": "a", "task": "video", "engine": "local:maestro",
            "shot_id": "sh-1", "dedupe_key": "shot:sh-1",
        }, now=10.0)
        self.assertTrue(first["created"])
        second = submit(self.root, {
            "job_id": "b", "task": "video", "engine": "local:maestro",
            "shot_id": "sh-1", "dedupe_key": "shot:sh-1",
        }, now=11.0)
        self.assertFalse(second["created"])
        self.assertEqual(second["job"]["job_id"], "a")
        self.assertIn("already active", second["reason"])
        self.assertEqual(len(list_jobs(self.root)), 1)

    def test_the_same_key_can_be_used_again_once_the_job_is_finished(self):
        submit(self.root, {"job_id": "a", "task": "video", "engine": "local:maestro",
                           "dedupe_key": "k"}, now=10.0)
        output = os.path.join(self._tmp.name, "out.mp4")
        Path(output).write_bytes(b"x")
        self.run_to_running("a", now=20.0)
        complete(self.root, "a", [output], now=30.0)
        again = submit(self.root, {"job_id": "b", "task": "video", "engine": "local:maestro",
                                   "dedupe_key": "k"}, now=40.0)
        self.assertTrue(again["created"])

    def test_a_job_over_the_memory_ceiling_is_refused_at_submission(self):
        configure(self.root, {"memory_ceiling_mb": 2048})
        with self.assertRaises(GenerationQueueError) as ctx:
            self.add(memory_mb=4096)
        self.assertIn("memory ceiling", str(ctx.exception))


class ClaimingTest(QueueTestCase):
    def test_priority_wins_then_arrival_order(self):
        configure(self.root, {"max_concurrent": 3})
        submit(self.root, {"job_id": "low", "task": "video", "engine": "e"}, now=1.0)
        submit(self.root, {"job_id": "high", "task": "video", "engine": "e", "priority": 5}, now=2.0)
        submit(self.root, {"job_id": "high-late", "task": "video", "engine": "e", "priority": 5}, now=3.0)
        claimed = []
        for step in range(3):
            result = claim_next(self.root, now=100.0 + step)
            claimed.append(result["claimed"]["job_id"] if result["claimed"] else None)
        self.assertEqual(claimed, ["high", "high-late", "low"])

    def test_concurrency_is_enforced_and_reported(self):
        configure(self.root, {"max_concurrent": 2})
        for index in range(3):
            submit(self.root, {"job_id": f"j{index}", "task": "video", "engine": "e"}, now=1.0)
        self.assertIsNotNone(claim_next(self.root, now=10.0)["claimed"])
        self.assertIsNotNone(claim_next(self.root, now=10.0)["claimed"])
        blocked = claim_next(self.root, now=10.0)
        self.assertIsNone(blocked["claimed"])
        self.assertIn("Concurrency limit reached (2/2)", blocked["reason"])
        self.assertEqual(blocked["waiting"], 1)

    def test_a_job_that_would_exceed_the_memory_ceiling_is_skipped_not_starved(self):
        configure(self.root, {"memory_ceiling_mb": 8000, "max_concurrent": 3})
        submit(self.root, {"job_id": "big", "task": "video", "engine": "e",
                           "memory_mb": 6000, "priority": 9}, now=1.0)
        submit(self.root, {"job_id": "small", "task": "video", "engine": "e",
                           "memory_mb": 1000, "priority": 1}, now=2.0)
        submit(self.root, {"job_id": "medium", "task": "video", "engine": "e",
                           "memory_mb": 2000, "priority": 0}, now=3.0)
        first = claim_next(self.root, now=10.0)
        self.assertEqual(first["claimed"]["job_id"], "big")
        second = claim_next(self.root, now=11.0)
        self.assertEqual(second["claimed"]["job_id"], "small")
        # 7000 MB is in use, so the remaining 2000 MB job does not fit: it is
        # skipped and the reason names the ceiling instead of failing silently.
        third = claim_next(self.root, now=12.0)
        self.assertIsNone(third["claimed"])
        self.assertIn("memory ceiling", third["reason"])
        self.assertIn("1000 MB", third["reason"])
        self.assertEqual(third["waiting"], 1)

    def test_the_retry_delay_keeps_a_requeued_job_waiting(self):
        configure(self.root, {"retry_delay_seconds": 60.0})
        self.run_to_running("job-1", now=200.0)
        fail(self.root, "job-1", "OOM", now=201.0)
        self.assertEqual(job(self.root, "job-1")["state"], "pending")
        self.assertIsNone(claim_next(self.root, now=210.0)["claimed"])
        self.assertIsNotNone(claim_next(self.root, now=300.0)["claimed"])


class TransitionTest(QueueTestCase):
    def test_illegal_transitions_are_refused(self):
        self.run_to_running()
        with self.assertRaises(GenerationQueueError) as ctx:
            set_state(self.root, "job-1", "draft", now=300.0)
        self.assertIn("Illegal transition running", str(ctx.exception))
        output = os.path.join(self._tmp.name, "o.mp4")
        Path(output).write_bytes(b"x")
        complete(self.root, "job-1", [output], now=301.0)
        with self.assertRaises(GenerationQueueError):
            set_state(self.root, "job-1", "running", now=302.0)

    def test_unknown_states_are_refused(self):
        self.add()
        with self.assertRaises(GenerationQueueError) as ctx:
            set_state(self.root, "job-1", "exploded", now=2.0)
        self.assertIn("Unknown state", str(ctx.exception))

    def test_state_timings_and_duration_are_recorded(self):
        self.add()
        claim_next(self.root, now=150.0)
        set_state(self.root, "job-1", "running", now=160.0)
        set_state(self.root, "job-1", "finalizing", now=170.0)
        output = os.path.join(self._tmp.name, "o.mp4")
        Path(output).write_bytes(b"x")
        finished = complete(self.root, "job-1", [output], now=180.0)
        self.assertEqual(finished["started_at"], 160.0)
        self.assertEqual(finished["finished_at"], 180.0)
        self.assertEqual(finished["duration_seconds"], 20.0)

    def test_an_unknown_job_is_a_clear_error(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            job(self.root, "ghost")
        self.assertIn("ghost", str(ctx.exception))


class ProgressHonestyTest(QueueTestCase):
    def test_a_job_starts_with_no_percentage_at_all(self):
        self.add()
        self.assertIsNone(job(self.root, "job-1")["progress"]["percent"])
        self.assertEqual(job(self.root, "job-1")["progress"]["source"], "none")

    def test_moving_through_states_never_invents_a_percentage(self):
        self.add()
        claim_next(self.root, now=200.0)
        set_state(self.root, "job-1", "running", now=201.0)
        set_state(self.root, "job-1", "finalizing", now=202.0)
        for step in (200.0, 201.0, 202.0):
            record = job(self.root, "job-1")
            self.assertIsNone(record["progress"]["percent"], record["state"])
            self.assertEqual(record["progress"]["source"], "none")

    def test_an_engine_that_reports_nothing_keeps_percent_none_even_if_one_is_sent(self):
        self.add(job_id="silent", progress_supported=False)
        claim_next(self.root, now=200.0)
        set_state(self.root, "silent", "running", now=201.0)
        updated = report_progress(self.root, "silent", percent=42, note="encoding", now=202.0)
        self.assertIsNone(updated["progress"]["percent"])
        self.assertEqual(updated["progress"]["source"], "none")
        # The engine's own words are kept, but they never read as a measurement.
        self.assertIn("encoding", updated["progress"]["note"])
        self.assertIn("no detailed progress", updated["progress"]["note"])

    def test_a_reported_percentage_from_the_engine_is_kept(self):
        self.add()
        claim_next(self.root, now=200.0)
        set_state(self.root, "job-1", "running", now=201.0)
        updated = report_progress(self.root, "job-1", percent=37.4, note="denoising", now=202.0)
        self.assertEqual(updated["progress"]["percent"], 37)
        self.assertEqual(updated["progress"]["source"], "reported")
        self.assertEqual(updated["progress"]["note"], "denoising")
        self.assertEqual(updated["progress"]["reported_at"], 202.0)

    def test_a_steps_based_engine_yields_a_truthful_percentage(self):
        self.add()
        claim_next(self.root, now=200.0)
        set_state(self.root, "job-1", "running", now=201.0)
        report_progress(self.root, "job-1", total_steps=20, now=202.0)
        updated = report_progress(self.root, "job-1", step=5, now=203.0)
        self.assertEqual(updated["progress"]["percent"], 25)
        self.assertEqual(updated["progress"]["step"], 5)
        self.assertEqual(updated["progress"]["total_steps"], 20)

    def test_completion_does_not_fabricate_a_percentage_either(self):
        self.add()
        claim_next(self.root, now=200.0)
        output = os.path.join(self._tmp.name, "o.mp4")
        Path(output).write_bytes(b"x")
        finished = complete(self.root, "job-1", [output], now=210.0)
        self.assertEqual(finished["state"], "completed")
        self.assertIsNone(finished["progress"]["percent"])
        self.assertEqual(finished["progress"]["source"], "none")

    def test_completion_keeps_a_reported_percentage(self):
        self.add()
        claim_next(self.root, now=200.0)
        set_state(self.root, "job-1", "running", now=201.0)
        report_progress(self.root, "job-1", percent=80, now=202.0)
        output = os.path.join(self._tmp.name, "o.mp4")
        Path(output).write_bytes(b"x")
        finished = complete(self.root, "job-1", [output], now=210.0)
        self.assertEqual(finished["progress"]["percent"], 100)

    def test_progress_is_refused_for_a_finished_job(self):
        self.add()
        claim_next(self.root, now=200.0)
        output = os.path.join(self._tmp.name, "o.mp4")
        Path(output).write_bytes(b"x")
        complete(self.root, "job-1", [output], now=210.0)
        with self.assertRaises(GenerationQueueError) as ctx:
            report_progress(self.root, "job-1", percent=10, now=211.0)
        self.assertIn("cannot report progress", str(ctx.exception))


class CompletionTest(QueueTestCase):
    def test_a_completion_without_a_real_file_is_refused(self):
        self.run_to_running()
        with self.assertRaises(GenerationQueueError) as ctx:
            complete(self.root, "job-1", [], now=300.0)
        self.assertIn("must list the file", str(ctx.exception))
        with self.assertRaises(GenerationQueueError) as ctx:
            complete(self.root, "job-1", ["/does/not/exist.mp4"], now=300.0)
        self.assertIn("does not exist on disk", str(ctx.exception))
        self.assertEqual(job(self.root, "job-1")["state"], "running")

    def test_a_real_file_completes_the_job_and_is_recorded(self):
        self.run_to_running()
        output = os.path.join(self._tmp.name, "shot.mp4")
        Path(output).write_bytes(b"video")
        finished = complete(self.root, "job-1", [output], now=300.0)
        self.assertEqual(finished["state"], "completed")
        self.assertEqual(finished["outputs"], [output])
        self.assertEqual(list_jobs(self.root, include_terminal=False), [])


class FailureTest(QueueTestCase):
    def test_a_retryable_failure_is_requeued_with_the_attempt_counted(self):
        self.run_to_running("job-1", now=200.0)
        after = fail(self.root, "job-1", "CUDA out of memory", now=210.0)
        self.assertEqual(after["state"], "pending")
        self.assertEqual(after["attempts"], 1)
        self.assertEqual(after["errors"], ["CUDA out of memory"])
        self.assertIsNone(after["finished_at"])
        kinds = [event["kind"] for event in events(self.root, job_id="job-1")]
        self.assertIn("retry_scheduled", kinds)
        self.assertIn("failed", kinds)

    def test_retries_are_limited(self):
        configure(self.root, {"max_retries": 1})
        self.run_to_running("job-1", now=200.0)
        fail(self.root, "job-1", "first", now=210.0)          # attempt 1 -> retry
        self.assertEqual(job(self.root, "job-1")["state"], "pending")
        claim_next(self.root, now=220.0)
        set_state(self.root, "job-1", "running", now=221.0)
        final = fail(self.root, "job-1", "second", now=230.0)  # attempt 2 -> spent
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["attempts"], 2)
        self.assertEqual(final["errors"], ["first", "second"])

    def test_a_non_retryable_failure_fails_immediately(self):
        self.run_to_running()
        final = fail(self.root, "job-1", "model missing", retryable=False, now=300.0)
        self.assertEqual(final["state"], "failed")
        self.assertEqual(final["attempts"], 1)

    def test_automatic_retry_can_be_turned_off(self):
        configure(self.root, {"auto_retry": False})
        self.run_to_running()
        final = fail(self.root, "job-1", "boom", now=300.0)
        self.assertEqual(final["state"], "failed")

    def test_manual_retry_requeues_and_respects_the_limit(self):
        self.run_to_running()
        fail(self.root, "job-1", "boom", retryable=False, now=300.0)
        self.assertEqual(job(self.root, "job-1")["state"], "failed")
        requeued = retry(self.root, "job-1", now=310.0)
        self.assertEqual(requeued["state"], "pending")
        self.assertIsNotNone(claim_next(self.root, now=320.0)["claimed"])

    def test_manual_retry_is_refused_when_retries_are_spent_or_the_job_is_running(self):
        configure(self.root, {"max_retries": 0})
        self.run_to_running()
        fail(self.root, "job-1", "boom", retryable=False, now=300.0)
        with self.assertRaises(GenerationQueueError) as ctx:
            retry(self.root, "job-1", now=310.0)
        self.assertIn("already used its 0 retry", str(ctx.exception))
        self.add(job_id="other")
        claim_next(self.root, now=320.0)
        with self.assertRaises(GenerationQueueError) as ctx:
            retry(self.root, "other", now=330.0)
        self.assertIn("Only a failed or cancelled job", str(ctx.exception))


class CancellationTest(QueueTestCase):
    def test_a_job_that_never_started_is_cancelled_immediately(self):
        self.add()
        outcome = cancel(self.root, "job-1", reason="changed my mind", now=50.0)
        self.assertTrue(outcome["cancelled"])
        self.assertEqual(outcome["action"], "cancelled_before_start")
        self.assertEqual(job(self.root, "job-1")["state"], "cancelled")

    def test_a_running_job_is_only_cancelled_when_the_engine_can_stop_it(self):
        self.add(job_id="hard", cancel_supported=False)
        claim_next(self.root, now=200.0)
        set_state(self.root, "hard", "running", now=201.0)
        outcome = cancel(self.root, "hard", reason="stop", now=202.0)
        self.assertFalse(outcome["cancelled"])
        self.assertEqual(outcome["action"], "requested")
        self.assertIn("still running", outcome["reason"])
        self.assertEqual(job(self.root, "hard")["state"], "running")
        self.assertTrue(job(self.root, "hard")["cancel_requested"])
        # The runner confirms, and only then does the job become cancelled.
        stopped = acknowledge_cancel(self.root, "hard", now=203.0)
        self.assertEqual(stopped["state"], "cancelled")

    def test_a_running_job_with_cancellation_support_is_cancelled(self):
        self.run_to_running()
        outcome = cancel(self.root, "job-1", now=300.0)
        self.assertTrue(outcome["cancelled"])
        self.assertEqual(outcome["action"], "cancelled_while_running")

    def test_cancelling_a_finished_job_is_a_no_op_with_an_explanation(self):
        self.add()
        cancel(self.root, "job-1", now=50.0)
        outcome = cancel(self.root, "job-1", now=60.0)
        self.assertEqual(outcome["action"], "none")
        self.assertIn("already cancelled", outcome["reason"])

    def test_a_cancelled_job_can_be_retried(self):
        self.add()
        cancel(self.root, "job-1", now=50.0)
        self.assertEqual(retry(self.root, "job-1", now=60.0)["state"], "pending")


class RecoveryTest(QueueTestCase):
    def test_an_abandoned_running_job_is_requeued_with_the_attempt_counted(self):
        configure(self.root, {"stale_after_seconds": 60.0, "max_retries": 2})
        self.run_to_running("job-1", now=200.0)
        report = recover(self.root, now=1_000.0)
        self.assertEqual(report["requeued"], ["job-1"])
        self.assertEqual(report["failed"], [])
        record = job(self.root, "job-1")
        self.assertEqual(record["state"], "pending")
        self.assertEqual(record["attempts"], 1)
        self.assertEqual(record["worker"], "")
        self.assertTrue(any("Interrupted" in error for error in record["errors"]))
        self.assertIsNotNone(claim_next(self.root, now=1_010.0)["claimed"])

    def test_an_abandoned_job_without_retries_left_fails_instead_of_looping(self):
        configure(self.root, {"stale_after_seconds": 60.0, "max_retries": 0})
        self.run_to_running("job-1", now=200.0)
        report = recover(self.root, now=1_000.0)
        self.assertEqual(report["failed"], ["job-1"])
        self.assertEqual(job(self.root, "job-1")["state"], "failed")

    def test_a_fresh_running_job_is_left_alone(self):
        configure(self.root, {"stale_after_seconds": 600.0})
        self.run_to_running("job-1", now=200.0)
        report = recover(self.root, now=210.0)
        self.assertEqual(report["requeued"], [])
        self.assertEqual(report["failed"], [])
        self.assertEqual(job(self.root, "job-1")["state"], "running")

    def test_abandoned_drafts_are_reported_but_never_deleted(self):
        configure(self.root, {"stale_after_seconds": 60.0})
        self.add(job_id="d1", draft=True)
        report = recover(self.root, now=1_000.0)
        self.assertEqual(report["abandoned_drafts"], ["d1"])
        self.assertEqual(job(self.root, "d1")["state"], "draft")
        self.assertIn("Nothing was deleted", report["detail"])

    def test_recovery_is_idempotent_after_it_has_run(self):
        configure(self.root, {"stale_after_seconds": 60.0, "max_retries": 3})
        self.run_to_running("job-1", now=200.0)
        recover(self.root, now=1_000.0)
        again = recover(self.root, now=1_001.0)
        self.assertEqual(again["requeued"], [])
        self.assertEqual(job(self.root, "job-1")["attempts"], 1)


class PersistenceTest(QueueTestCase):
    def test_the_queue_survives_a_restart_and_keeps_paths_not_contents(self):
        self.add(memory_mb=1024)
        claim_next(self.root, now=200.0)
        set_state(self.root, "job-1", "running", now=201.0)
        report_progress(self.root, "job-1", percent=12, now=202.0)
        # A fresh read is what a restarted backend sees.
        reread = read_queue(self.root)
        record = reread["jobs"]["job-1"]
        self.assertEqual(record["state"], "running")
        self.assertEqual(record["progress"]["percent"], 12)
        self.assertEqual(record["memory_mb"], 1024)
        # The queue holds paths and metadata only — never file contents.
        self.assertEqual(record["inputs"], ["/tmp/in.png"])
        self.assertNotIn("base64", json.dumps(record).lower())

    def test_a_corrupt_queue_file_starts_clean_instead_of_crashing(self):
        self.add()
        Path(self.root, "queue.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(read_queue(self.root)["jobs"], {})
        self.assertEqual(settings(self.root)["max_concurrent"], 1)
        self.assertTrue(self.add(job_id="job-2")["created"])

    def test_linking_the_engine_job_id_is_persisted(self):
        self.add()
        linked = link_external_job(self.root, "job-1", "abc123", now=150.0)
        self.assertEqual(linked["external_job_id"], "abc123")
        self.assertEqual(job(self.root, "job-1")["external_job_id"], "abc123")


class HousekeepingTest(QueueTestCase):
    def test_history_is_trimmed_to_the_configured_bound(self):
        configure(self.root, {"keep_terminal": 2, "max_concurrent": 5})
        output = os.path.join(self._tmp.name, "o.mp4")
        Path(output).write_bytes(b"x")
        for index in range(4):
            submit(self.root, {"job_id": f"j{index}", "task": "video", "engine": "e"}, now=1.0)
            claim_next(self.root, now=10.0 + index)
            set_state(self.root, f"j{index}", "running", now=11.0 + index)
            complete(self.root, f"j{index}", [output], now=12.0 + index)
        kept = list_jobs(self.root, state="completed")
        self.assertEqual([record["job_id"] for record in kept], ["j2", "j3"])

    def test_pruning_never_touches_active_work(self):
        configure(self.root, {"keep_terminal": 0})
        self.add(job_id="active", now=100.0)
        output = os.path.join(self._tmp.name, "o.mp4")
        Path(output).write_bytes(b"x")
        submit(self.root, {"job_id": "done", "task": "video", "engine": "e"}, now=1.0)
        claim_next(self.root, now=3.0, worker="w")
        set_state(self.root, "done", "running", now=4.0)
        complete(self.root, "done", [output], now=5.0)
        # ``complete`` already trims, and ``prune`` is a safe no-op afterwards.
        prune(self.root, now=6.0)
        self.assertEqual([record["job_id"] for record in list_jobs(self.root)], ["active"])
        self.assertEqual(job(self.root, "active")["state"], "pending")
        self.assertIsNotNone(claim_next(self.root, now=200.0)["claimed"])

    def test_the_event_log_is_bounded_and_filterable(self):
        configure(self.root, {"event_limit": 10})
        for index in range(12):
            submit(self.root, {"job_id": f"j{index}", "task": "video", "engine": "e"}, now=float(index))
        log = events(self.root)
        self.assertLessEqual(len(log), 10)
        self.assertEqual(log[-1]["job_id"], "j11")
        self.assertTrue(all(event["kind"] == "submitted" for event in log))

    def test_each_job_keeps_its_own_event_trail(self):
        self.run_to_running()
        trail = events(self.root, job_id="job-1")
        # claimed comes before the state move it causes, so the trail reads in
        # the order things happened: submitted, claimed, preparing, running.
        self.assertEqual([event["kind"] for event in trail],
                         ["submitted", "claimed", "state", "state"])
        states = [event["detail"] for event in trail if event["kind"] == "state"]
        self.assertEqual(states, ["preparing", "running"])
        # The trail is the job's, not a filtered slice of the queue log.
        self.assertTrue(all(event["job_id"] == "job-1" for event in trail))
        with self.assertRaises(FileNotFoundError):
            events(self.root, job_id="ghost")

    def test_summary_reports_the_queues_real_state(self):
        configure(self.root, {"max_concurrent": 2, "memory_ceiling_mb": 8000})
        self.add(job_id="a", memory_mb=1000)
        submit(self.root, {"job_id": "b", "task": "video", "engine": "e"}, now=101.0)
        claim_next(self.root, now=200.0)
        report = summary(self.root, now=300.0)
        self.assertEqual(report["counts"]["preparing"], 1)
        self.assertEqual(report["counts"]["pending"], 1)
        self.assertEqual(report["running"], 1)
        self.assertEqual(report["max_concurrent"], 2)
        self.assertEqual(report["declared_memory_running_mb"], 1000)
        self.assertEqual(report["memory_ceiling_mb"], 8000)
        # The oldest job still waiting was submitted at t=101.
        self.assertEqual(report["waiting_seconds"], 199.0)
        self.assertIn("only when the engine reported one", report["progress_note"])

    def test_system_memory_is_measured_or_reported_as_unknown(self):
        value = system_memory_mb()
        self.assertTrue(value is None or value > 0)


if __name__ == "__main__":
    unittest.main()

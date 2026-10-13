"""Compute nodes: registration, heartbeat auth, routing, parked work recovery."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services.compute_nodes import (  # noqa: E402
    DEFAULT_OFFLINE_AFTER,
    ComputeNodeError,
    assign_job,
    get_node,
    heartbeat,
    jobs_for_node,
    list_nodes,
    nodes_with_capability,
    orphaned_jobs,
    recover_node,
    register_node,
    release_job,
    remove_node,
    select_node,
    summary,
    update_node,
)
from services.engine_providers import EngineProviderError  # noqa: E402


class NodeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def register(self, node_id="desktop", *, now=0.0, **kwargs):
        payload = {
            "node_id": node_id,
            "label": node_id.title(),
            "capabilities": ["text_to_video"],
            "max_concurrent_jobs": 1,
            **kwargs,
        }
        return register_node(self.root, payload, now=now)


class RegistrationTest(NodeTestCase):
    def test_registration_returns_a_token_once_and_stores_only_its_hash(self):
        node = self.register()
        token = node["token"]
        self.assertTrue(token)
        self.assertIn("never shown again", node["token_notice"])
        on_disk = Path(self.root, "compute_nodes.json").read_text()
        self.assertNotIn(token, on_disk)
        self.assertIn("token_hash", json.loads(on_disk)["nodes"]["desktop"])
        self.assertNotIn("token", list(get_node(self.root, "desktop").keys()))

    def test_a_node_may_only_declare_known_capabilities(self):
        with self.assertRaises(ComputeNodeError) as ctx:
            self.register(capabilities=["time_travel"])
        self.assertIn("image_generation", str(ctx.exception))

    def test_node_ids_are_validated(self):
        for bad in ("", "../etc", "a/b", "x" * 80, " with space"):
            with self.assertRaises(ComputeNodeError):
                register_node(self.root, {"node_id": bad, "capabilities": []})
            self.assertFalse(os.path.exists(os.path.join(self.root, "..", "etc")))

    def test_re_registration_rotates_the_token_and_keeps_the_created_at(self):
        first = self.register()
        second = self.register(now=100.0)
        self.assertNotEqual(first["token"], second["token"])
        self.assertEqual(second["created_at"], first["created_at"])
        with self.assertRaises(PermissionError):
            heartbeat(self.root, "desktop", first["token"], now=100.0)

    def test_a_corrupt_registry_starts_clean_instead_of_crashing(self):
        with open(os.path.join(self.root, "compute_nodes.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(list_nodes(self.root), [])
        self.register()

    def test_a_node_is_online_only_while_its_heartbeat_is_fresh(self):
        self.register(now=1000.0)
        self.assertTrue(get_node(self.root, "desktop", now=1000.0)["online"])
        later = 1000.0 + DEFAULT_OFFLINE_AFTER + 1
        node = get_node(self.root, "desktop", now=later)
        self.assertFalse(node["online"])
        self.assertEqual(node["state"], "offline")
        self.assertGreater(node["seconds_since_seen"], DEFAULT_OFFLINE_AFTER)


class HeartbeatTest(NodeTestCase):
    def test_heartbeat_requires_the_registration_token(self):
        node = self.register(now=0.0)
        with self.assertRaises(PermissionError):
            heartbeat(self.root, "desktop", "wrong-token", now=10.0)
        with self.assertRaises(FileNotFoundError):
            heartbeat(self.root, "ghost", "whatever", now=10.0)
        updated = heartbeat(self.root, "desktop", node["token"], now=10.0)
        self.assertTrue(updated["online"])
        self.assertEqual(updated["last_seen"], 10.0)

    def test_a_heartbeat_can_report_fresh_stats(self):
        node = self.register(now=0.0, models=["old"], max_concurrent_jobs=1)
        heartbeat(
            self.root, "desktop", node["token"], now=10.0,
            stats={
                "models": ["wan", "ltx"],
                "max_concurrent_jobs": 3,
                "hardware": {"gpu": "RTX 4090", "vram_gb": 24},
                "storage": {"free_bytes": 500_000_000_000},
            },
        )
        refreshed = get_node(self.root, "desktop", now=10.0)
        self.assertEqual(refreshed["models"], ["wan", "ltx"])
        self.assertEqual(refreshed["max_concurrent_jobs"], 3)
        self.assertEqual(refreshed["hardware"]["gpu"], "RTX 4090")

    def test_control_surface_can_correct_capabilities(self):
        self.register(now=0.0, capabilities=["text_to_video"])
        updated = update_node(self.root, "desktop", {"capabilities": ["image_generation"]})
        self.assertEqual(updated["capabilities"], ["image_generation"])
        with self.assertRaises(FileNotFoundError):
            update_node(self.root, "ghost", {"capabilities": []})


class RoutingTest(NodeTestCase):
    def test_selection_requires_a_declared_and_reachable_capability(self):
        self.register("desktop", now=0.0)
        self.register("phone", now=0.0, capabilities=["image_generation"], local=False)
        with self.assertRaises(EngineProviderError) as ctx:
            select_node(self.root, "voice_generation", now=1.0)
        self.assertIn("voice_generation", str(ctx.exception))
        with self.assertRaises(EngineProviderError):
            select_node(self.root, "text_to_video", now=1.0 + DEFAULT_OFFLINE_AFTER + 1)
        chosen = select_node(self.root, "text_to_video", now=1.0)
        self.assertEqual(chosen["node_id"], "desktop")

    def test_the_local_node_is_preferred_and_never_guessed_from_a_name(self):
        self.register("remote-box", now=0.0, local=False)
        self.register("local", now=0.0, local=True)
        chosen = select_node(self.root, "text_to_video", now=1.0)
        self.assertEqual(chosen["node_id"], "local")
        self.assertTrue(chosen["local"])

    def test_a_busy_node_is_skipped_rather_than_overloaded(self):
        self.register("desktop", now=0.0, max_concurrent_jobs=1)
        assign_job(self.root, "job-1", "desktop", capability="text_to_video", now=1.0)
        with self.assertRaises(EngineProviderError) as ctx:
            select_node(self.root, "text_to_video", now=1.0)
        self.assertIn("at capacity", str(ctx.exception))
        release_job(self.root, "job-1", outcome="completed")
        chosen = select_node(self.root, "text_to_video", now=1.0)
        self.assertEqual(chosen["capacity_remaining"], 1)

    def test_nodes_with_capability_can_include_offline_ones_on_demand(self):
        self.register("desktop", now=0.0)
        later = 0.0 + DEFAULT_OFFLINE_AFTER + 10
        self.assertEqual(nodes_with_capability(self.root, "text_to_video", now=later), [])
        self.assertEqual(
            len(nodes_with_capability(self.root, "text_to_video", now=later, online_only=False)), 1)


class AssignmentTest(NodeTestCase):
    def test_assigning_to_an_online_node_is_pending(self):
        self.register("desktop", now=0.0)
        assignment = assign_job(
            self.root, "job-1", "desktop",
            capability="text_to_video", task="shot", now=1.0)
        self.assertEqual(assignment["state"], "pending")
        self.assertEqual(assignment["node_id"], "desktop")

    def test_assigning_to_a_node_that_is_away_parks_the_job(self):
        self.register("desktop", now=0.0)
        assignment = assign_job(
            self.root, "job-1", "desktop", capability="text_to_video",
            now=DEFAULT_OFFLINE_AFTER + 100)
        self.assertEqual(assignment["state"], "waiting_node")

    def test_a_node_cannot_be_given_work_it_did_not_declare(self):
        self.register("desktop", now=0.0, capabilities=["image_generation"])
        with self.assertRaises(ComputeNodeError) as ctx:
            assign_job(self.root, "job-1", "desktop", capability="voice_generation", now=1.0)
        self.assertIn("did not declare", str(ctx.exception))

    def test_releasing_a_job_records_the_outcome(self):
        self.register("desktop", now=0.0)
        assign_job(self.root, "job-1", "desktop", capability="text_to_video", now=1.0)
        released = release_job(self.root, "job-1", outcome="failed", error="OOM")
        self.assertEqual(released["state"], "failed")
        self.assertEqual(released["error"], "OOM")
        self.assertEqual(jobs_for_node(self.root, "desktop"), [])
        self.assertEqual(len(jobs_for_node(self.root, "desktop", include_terminal=True)), 1)
        with self.assertRaises(ComputeNodeError):
            release_job(self.root, "job-1", outcome="exploded")

    def test_parked_work_is_listed_while_the_node_is_away(self):
        self.register("desktop", now=0.0)
        assign_job(self.root, "job-1", "desktop", capability="text_to_video", now=1.0)
        later = 0.0 + DEFAULT_OFFLINE_AFTER + 10
        orphans = orphaned_jobs(self.root, now=later)
        self.assertEqual([item["job_id"] for item in orphans], ["job-1"])
        self.assertEqual(orphaned_jobs(self.root, now=1.0), [])

    def test_a_returning_node_gets_its_parked_work_back_with_an_attempt_count(self):
        node = self.register("desktop", now=0.0)
        # Parked: the node was already away when the job was accepted.
        assign_job(self.root, "job-1", "desktop", capability="text_to_video",
                   now=DEFAULT_OFFLINE_AFTER + 100)
        # In flight: accepted while online, then the node vanished mid-run.
        assign_job(self.root, "job-2", "desktop", capability="text_to_video", now=1.0)
        release_job(self.root, "job-2", outcome="running")
        # Finished: must never be resurrected.
        assign_job(self.root, "job-3", "desktop", capability="text_to_video", now=1.0)
        release_job(self.root, "job-3", outcome="completed")

        before = {item["job_id"]: item["state"] for item in jobs_for_node(
            self.root, "desktop", include_terminal=True)}
        self.assertEqual(before, {"job-1": "waiting_node", "job-2": "running", "job-3": "completed"})

        heartbeat(self.root, "desktop", node["token"], now=10_000.0)
        recovery = recover_node(self.root, "desktop", now=10_000.0)
        self.assertEqual([item["job_id"] for item in recovery["resumed"]], ["job-1", "job-2"])
        self.assertTrue(all(item["state"] == "pending" for item in recovery["resumed"]))
        self.assertEqual([item["attempts"] for item in recovery["resumed"]], [1, 1])
        self.assertIn("resubmit them", recovery["detail"])
        after = {item["job_id"]: item for item in jobs_for_node(
            self.root, "desktop", include_terminal=True)}
        self.assertEqual(after["job-3"]["state"], "completed")
        self.assertEqual(after["job-3"]["attempts"], 0)
        # Nothing is left to hand back a second time.
        self.assertEqual(recover_node(self.root, "desktop", now=10_001.0)["count"], 0)

    def test_removing_a_node_parks_its_work_instead_of_losing_it(self):
        self.register("desktop", now=0.0)
        assign_job(self.root, "job-1", "desktop", capability="text_to_video", now=1.0)
        result = remove_node(self.root, "desktop")
        self.assertEqual(result, {"removed": "desktop", "parked_jobs": 1})
        with self.assertRaises(FileNotFoundError):
            get_node(self.root, "desktop")
        # Re-registering hands the work straight back.
        self.register("desktop", now=2.0)
        self.assertEqual(recover_node(self.root, "desktop", now=2.0)["count"], 1)


class PersistenceTest(NodeTestCase):
    def test_state_survives_a_fresh_read(self):
        node = self.register("desktop", now=0.0, models=["wan"])
        assign_job(self.root, "job-1", "desktop", capability="text_to_video", now=1.0)
        again = get_node(self.root, "desktop", now=1.0)
        self.assertEqual(again["models"], ["wan"])
        self.assertEqual(len(jobs_for_node(self.root, "desktop")), 1)
        self.assertEqual(heartbeat(self.root, "desktop", node["token"], now=2.0)["node_id"], "desktop")

    def test_summary_reports_the_mobile_header_view(self):
        self.register("desktop", now=0.0)
        self.register("phone", now=0.0, capabilities=["image_generation"], local=False)
        assign_job(self.root, "job-1", "desktop", capability="text_to_video", now=1.0)
        report = summary(self.root, now=1.0)
        self.assertEqual(report["node_count"], 2)
        self.assertEqual(report["online_count"], 2)
        self.assertEqual(report["active_assignments"], 1)
        self.assertEqual(
            report["capabilities_online"], ["image_generation", "text_to_video"])
        offline = summary(self.root, now=0.0 + DEFAULT_OFFLINE_AFTER + 5)
        self.assertEqual(offline["online_count"], 0)
        self.assertEqual(offline["waiting_for_node"], 0)


if __name__ == "__main__":
    unittest.main()

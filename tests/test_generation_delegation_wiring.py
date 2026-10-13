"""The Studio/Films delegation seam in ``launch.py``.

``app/launch.py`` cannot be imported here — it pulls in Gradio, torch and the
model registry — but the part that decides *where a job runs* is plain Python
over measured state, and this repository already has a pattern for exercising
those pieces: extract the exact function objects from the source with :mod:`ast`
and run them (``tests/test_image_runtime_regressions.py`` does the same for the
model pipeline). So the helpers below are **executed**, not grepped:

* ``_generation_route`` / ``_generation_route_or_refuse`` — local first, a peer
  when this machine cannot generate, a free public engine on the internet when
  there is no peer either, and a 409 that names what is missing;
* ``_delegated_queue_payload`` — the durable queue job that carries the request
  to the peer (or to the public engine), including the engine it is asked to run;
* ``_delegation_out_dir`` — the result must land in the job's own workspace.

The last test locks in the *call sites*: those are inside functions that need the
running application, so they are asserted statically rather than executed. That
is deliberate and is the honest limit of what can be checked outside the app.
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import engine_routing  # noqa: E402

LAUNCH = Path(__file__).resolve().parents[1] / "app" / "launch.py"

# Every helper this file executes, plus the module-level constants they read.
_HELPERS = (
    "_local_generation_possible",
    "_generation_route",
    "_generation_route_or_refuse",
    "_public_sources",
    "_delegation_out_dir",
    "_generation_queue_task",
    "_delegated_queue_payload",
)
_CONSTANTS = ("_DELEGATION_POLL_SECONDS", "_DELEGATION_PHASES")


class _Refused(Exception):
    """Stands in for FastAPI's HTTPException, keeping its ``detail``."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _EngineStub:
    """``film_api_service`` as the helpers see it: one measured boolean."""

    def __init__(self, generation_possible_here: bool) -> None:
        self.generation_possible_here = generation_possible_here

    def engine_status(self) -> dict:
        return {"generation_possible_here": self.generation_possible_here}


class _PeersStub:
    """``remote_engine_service``: the peers this machine remembers."""

    def __init__(self, peers: list[dict]) -> None:
        self.peers = peers

    def list_peers(self, _root: str) -> list[dict]:
        return list(self.peers)


class _PublicCloudStub:
    """``public_cloud_service``: the free public engines connected right now."""

    class PublicCloudError(RuntimeError):
        pass

    def __init__(self, sources: list[dict]) -> None:
        self.sources = sources

    def connected_sources(self, _root: str) -> list[dict]:
        return list(self.sources)


def load_helpers(
    *, local_gpu: bool, peers: list[dict], workspace_root: str, public_sources: list[dict] | None = None
) -> dict:
    """Execute the real helpers from launch.py with stubbed neighbours."""
    tree = ast.parse(LAUNCH.read_text(encoding="utf-8"))
    nodes = [
        node
        for node in tree.body
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _HELPERS)
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in _CONSTANTS
                for target in node.targets
            )
        )
    ]
    found = {
        node.name
        for node in nodes
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = set(_HELPERS) - found
    if missing:
        raise AssertionError(f"launch.py no longer defines: {', '.join(sorted(missing))}")

    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    namespace = {
        "os": os,
        "time": __import__("time"),
        "print": print,
        "HTTPException": _Refused,
        "engine_routing_service": engine_routing,
        "film_api_service": _EngineStub(local_gpu),
        "remote_engine_service": _PeersStub(peers),
        "public_cloud_service": _PublicCloudStub(list(public_sources or [])),
        "_remote_peers_root": lambda: "/nonexistent/peers",
        "_public_cloud_root": lambda: "/nonexistent/public",
        "_workspace_dir": lambda workspace="": os.path.join(workspace_root, workspace or "default"),
    }
    exec(compile(module, str(LAUNCH), "exec"), namespace)  # noqa: S102 - the point of the test
    for name in _HELPERS:
        namespace[name] = namespace[name]
    return namespace


def peer(peer_id: str = "desk-1", *, online: bool = True, capabilities=("text_to_video",)) -> dict:
    return {
        "peer_id": peer_id,
        "label": "PC with the RTX",
        "base_url": f"http://{peer_id}.tailnet.ts.net",
        "capabilities": list(capabilities),
        "online": online,
    }


def public_source(
    provider_id: str = "hf-space:owner/space",
    *,
    online: bool = True,
    capabilities=("text_to_video",),
    accepts_media: bool = False,
) -> dict:
    return {
        "kind": "public_cloud",
        "provider_id": provider_id,
        "label": f"{provider_id} (public Hugging Face Space)",
        "base_url": "https://owner-space.hf.space",
        "hardware": "zero-a10g",
        "capabilities": list(capabilities),
        "accepts_media": accepts_media,
        "online": online,
        "free": True,
    }


class GenerationRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace_root = self._tmp.name

    def helpers(
        self, *, local_gpu: bool, peers: list[dict], public_sources: list[dict] | None = None
    ) -> dict:
        return load_helpers(
            local_gpu=local_gpu,
            peers=peers,
            public_sources=public_sources,
            workspace_root=self.workspace_root,
        )

    def test_a_machine_with_a_gpu_runs_the_job_here(self):
        helpers = self.helpers(local_gpu=True, peers=[peer()])
        route = helpers["_generation_route"]({"model_type": "h3", "generation_mode": "video"})
        self.assertEqual(route["target"], engine_routing.LOCAL_TARGET)
        self.assertEqual(route["engine"], engine_routing.LOCAL_ENGINE)

    def test_a_machine_without_a_gpu_sends_the_job_to_the_peer(self):
        helpers = self.helpers(local_gpu=False, peers=[peer()])
        route = helpers["_generation_route"]({"model_type": "h3", "generation_mode": "video"})
        self.assertEqual(route["target"], engine_routing.REMOTE_TARGET)
        self.assertEqual(route["engine"], "node:desk-1")

    def test_a_machine_with_neither_a_gpu_nor_a_peer_uses_public_compute(self):
        # The whole point of the third tier: a fresh install with no GPU and no
        # second computer still renders, on a free public engine connected by itself.
        helpers = self.helpers(local_gpu=False, peers=[], public_sources=[public_source()])
        route = helpers["_generation_route"]({"model_type": "h3", "generation_mode": "video"})
        self.assertEqual(route["target"], engine_routing.PUBLIC_TARGET)
        self.assertEqual(route["engine"], "public:text_to_video")
        self.assertEqual(route["public_provider_id"], "hf-space:owner/space")
        self.assertEqual(route["public_fidelity"], engine_routing.PUBLIC_FIDELITY)
        # A peer is still preferred when one exists: it replays the recipe, the
        # public engine only approximates it.
        preferred = self.helpers(local_gpu=False, peers=[peer()], public_sources=[public_source()])
        self.assertEqual(
            preferred["_generation_route"]({"model_type": "h3"})["target"],
            engine_routing.REMOTE_TARGET,
        )

    def test_a_local_gpu_still_wins_over_public_compute(self):
        helpers = self.helpers(local_gpu=True, peers=[], public_sources=[public_source()])
        route = helpers["_generation_route"]({"model_type": "h3"})
        self.assertEqual(route["target"], engine_routing.LOCAL_TARGET)

    def test_a_refusal_names_public_compute_too(self):
        helpers = self.helpers(local_gpu=False, peers=[], public_sources=[])
        with self.assertRaises(_Refused) as caught:
            helpers["_generation_route_or_refuse"]({"model_type": "h3"})
        detail = caught.exception.detail
        self.assertIn("no CUDA GPU", detail)
        self.assertIn("Public compute is not connected", detail)

    def test_an_unrunnable_request_is_refused_with_its_reason(self):
        helpers = self.helpers(local_gpu=False, peers=[])
        with self.assertRaises(_Refused) as caught:
            helpers["_generation_route_or_refuse"]({"model_type": "h3", "generation_mode": "video"})
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("no CUDA GPU", caught.exception.detail)

    def test_a_forced_local_job_never_delegates_onward(self):
        helpers = self.helpers(local_gpu=False, peers=[peer()])
        route = helpers["_generation_route"]({"model_type": "h3"}, force_local=True)
        self.assertEqual(route["target"], engine_routing.NO_TARGET)
        self.assertEqual(route["peer_id"], "")
        self.assertIn("cannot run it locally", route["reason"])

    def test_a_forced_local_job_with_a_gpu_runs_here(self):
        helpers = self.helpers(local_gpu=True, peers=[peer()])
        route = helpers["_generation_route"]({"model_type": "h3"}, force_local=True)
        self.assertEqual(route["target"], engine_routing.LOCAL_TARGET)

    def test_a_broken_engine_probe_is_read_as_no_local_gpu(self):
        helpers = self.helpers(local_gpu=False, peers=[peer()])

        def explode() -> dict:
            raise RuntimeError("torch is having a bad day")

        helpers["film_api_service"].engine_status = explode
        route = helpers["_generation_route"]({"model_type": "h3"})
        self.assertFalse(route["local_generation_possible"])
        self.assertEqual(route["target"], engine_routing.REMOTE_TARGET)


class DelegatedPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace_root = self._tmp.name
        self.helpers = load_helpers(
            local_gpu=False, peers=[peer()], workspace_root=self.workspace_root
        )

    def test_a_public_payload_names_the_capability_and_the_provider(self):
        job = {
            "id": "pub12345",
            "workspace": "films",
            "params": {"model_type": "h3", "prompt": "a paper boat", "video_length": 48, "fps": 24},
        }
        route = {
            "target": engine_routing.PUBLIC_TARGET,
            "engine": "public:text_to_video",
            "peer_id": "",
            "capability": "text_to_video",
            "public_provider_id": "hf-space:owner/space",
        }
        payload = self.helpers["_delegated_queue_payload"](job, route)
        self.assertEqual(payload["engine"], "public:text_to_video")
        self.assertEqual(payload["task"], "video")
        self.assertEqual(payload["params"]["public_provider"], "hf-space:owner/space")
        self.assertEqual(payload["params"]["capability"], "text_to_video")
        self.assertEqual(payload["params"]["workspace"], "films")
        # A public Space has no Maestro engine to ask for, so none is named: the job
        # names the capability and the runner maps it onto that Space's own inputs.
        self.assertNotIn("remote_engine", payload["params"])

    def test_the_payload_names_the_peer_engine_and_keeps_the_studio_params(self):
        job = {
            "id": "abc12345",
            "workspace": "films",
            "params": {"model_type": "h3", "prompt": "a paper boat", "num_inference_steps": 8},
        }
        route = {"engine": "node:desk-1", "peer_id": "desk-1", "capability": "text_to_video",
                 "remote_engine": engine_routing.REMOTE_ENGINE}
        payload = self.helpers["_delegated_queue_payload"](job, route)
        self.assertEqual(payload["engine"], "node:desk-1")
        self.assertEqual(payload["task"], "video")
        self.assertEqual(payload["model"], "h3")
        self.assertEqual(payload["dedupe_key"], "maestro:abc12345")
        self.assertTrue(payload["cancel_supported"])
        # A delegated render is not local work, so the local memory ceiling does
        # not apply to it.
        self.assertEqual(payload["memory_mb"], 0)
        params = payload["params"]
        self.assertEqual(params["remote_engine"], engine_routing.REMOTE_ENGINE)
        self.assertEqual(params["peer_id"], "desk-1")
        self.assertEqual(params["capability"], "text_to_video")
        self.assertEqual(params["workspace"], "films")
        # The Studio's own settings travel untouched; the peer runs the same job.
        self.assertEqual(params["prompt"], "a paper boat")
        self.assertEqual(params["num_inference_steps"], 8)

    def test_image_and_audio_requests_use_their_own_queue_task(self):
        route = {"engine": "node:desk-1", "peer_id": "desk-1", "capability": ""}
        image = self.helpers["_delegated_queue_payload"](
            {"id": "i", "workspace": "w", "params": {"model_type": "flux", "generation_mode": "image"}},
            {**route, "capability": "image_generation"},
        )
        self.assertEqual(image["task"], "image")
        audio = self.helpers["_delegated_queue_payload"](
            {"id": "a", "workspace": "w", "params": {"generation_mode": "audio"}},
            {**route, "capability": "audio_generation"},
        )
        self.assertEqual(audio["task"], "audio")

    def test_the_result_directory_follows_the_job_workspace_and_is_absolute(self):
        directory = self.helpers["_delegation_out_dir"](
            {"params": {"workspace": "second"}, "workspace": "second"}
        )
        self.assertTrue(os.path.isabs(directory))
        self.assertTrue(directory.endswith(os.path.join(self.workspace_root, "second")))

    def test_a_job_without_a_workspace_uses_the_active_one(self):
        directory = self.helpers["_delegation_out_dir"]({"params": {}})
        self.assertTrue(os.path.isabs(directory))
        self.assertTrue(directory.endswith(os.path.join(self.workspace_root, "default")))


class WiringLockInTests(unittest.TestCase):
    """The call sites, asserted statically: they need the running application.

    Nothing here proves runtime behaviour — the executed tests above do that for
    the helpers. This states, in code, which parts of the app must keep asking
    the router, so a later edit cannot quietly drop the delegation.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = ast.parse(LAUNCH.read_text(encoding="utf-8"))

    def function(self, name: str) -> ast.AST:
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        raise AssertionError(f"launch.py no longer defines {name}")

    def names_used(self, node: ast.AST) -> set[str]:
        return {
            child.func.id
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }

    def names_referenced(self, node: ast.AST) -> set[str]:
        """Every bare name the function reads, whether it calls it or passes it on."""
        return {
            child.id
            for child in ast.walk(node)
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
        }

    def attributes_used(self, node: ast.AST) -> set[str]:
        return {
            child.func.attr
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
        }

    def test_both_submission_paths_ask_the_router_and_refuse_honestly(self):
        for name in ("_prepare_generation_submission", "_enqueue_deferred_generation_preparation"):
            with self.subTest(function=name):
                self.assertIn("_generation_route_or_refuse", self.names_used(self.function(name)))

    def test_every_generation_worker_start_goes_through_the_router(self):
        # Both job-creation paths start their runner through the one helper that
        # knows how to delegate; the held-job dispatcher runs it inline.
        for name in ("_prepare_generation_submission", "_enqueue_deferred_generation_preparation"):
            with self.subTest(function=name):
                self.assertIn("_start_generation_worker", self.names_used(self.function(name)))
        self.assertIn("_run_generation_job_now", self.names_used(self.function("_run_held_studio_jobs")))

    def test_the_films_production_flow_submits_through_the_routed_path(self):
        # Films hands every shot to the same submission entry point Studio uses,
        # which is where the router decides — and where a request nothing can run
        # is refused with its reason. The film flow must not grow its own runner.
        produce = self.function("film_produce")
        self.assertIn("_prepare_generation_submission", self.names_used(produce))
        self.assertNotIn("_run_generation", self.names_used(produce))

    def test_an_enhanced_prompt_retry_goes_through_the_router_too(self):
        # Enhancing a prompt and then retrying is an ordinary generation request
        # on a computer that may not be able to generate, so it must not bypass
        # the router (nor start a local render that is known to fail).
        retry = self.function("retry_enhanced_job")
        self.assertIn("_generation_route", self.names_used(retry))
        self.assertIn("_start_generation_worker", self.names_used(retry))
        self.assertNotIn("_run_generation", self.names_used(retry))

    def test_a_request_asks_the_peers_again_before_it_is_routed(self):
        # Opening the interface and pressing Generate is the whole flow on a
        # computer without a GPU: both submission entry points refresh first, so a
        # peer that was switched on after this machine is not missed.
        for name in ("generate", "_prepare_generation_submission"):
            with self.subTest(function=name):
                self.assertIn(
                    "_refresh_remote_peers_if_needed", self.names_used(self.function(name))
                )
        refresh = self.function("_refresh_remote_peers_if_needed")
        # It only ever probes when this machine cannot generate and no peer is
        # known to be online — never a delay on a machine that has its own GPU.
        self.assertIn("_local_generation_possible", self.names_used(refresh))
        self.assertIn("to_thread", self.attributes_used(refresh))

    def test_the_peers_are_kept_fresh_and_prepared_without_being_asked(self):
        hooks = {
            node.args[-1].id
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_event_handler"
            and node.args
            and isinstance(node.args[-1], ast.Name)
        }
        self.assertIn("_probe_remote_peers_on_start", hooks)
        self.assertIn("_start_peer_refresh_loop", hooks)
        # The peer's own worker is what renders a delegated job, so handing work
        # over starts it there; that is what "without touching the other computer"
        # means in practice.
        self.assertIn(
            "ensure_peer_worker", self.attributes_used(self.function("_run_delegated_generation"))
        )
        self.assertIn(
            "ensure_peer_worker", self.attributes_used(self.function("_probe_remote_peers_on_start"))
        )

    def test_both_remote_runners_hand_over_through_the_one_queue_path(self):
        # A peer render and a public render differ in exactly one way — who prepares
        # the other side — so both must reach the durable queue through the same
        # helper. Otherwise one of them could quietly stop deferring work.
        hand_over = self.function("_hand_over_to_queue")
        self.assertIn("_delegated_queue_payload", self.names_used(hand_over))
        self.assertIn("_mirror_queue_job", self.names_used(hand_over))
        self.assertIn("submit", self.attributes_used(hand_over))
        for name in ("_run_delegated_generation", "_run_public_generation"):
            with self.subTest(runner=name):
                self.assertIn("_hand_over_to_queue", self.names_used(self.function(name)))
        # A peer is prepared before it is handed work; a public service is nobody's
        # computer and there is nothing to prepare.
        self.assertIn(
            "ensure_peer_worker", self.attributes_used(self.function("_run_delegated_generation"))
        )
        self.assertNotIn(
            "ensure_peer_worker", self.attributes_used(self.function("_run_public_generation"))
        )

    def test_public_compute_is_registered_and_kept_fresh_without_being_asked(self):
        hooks = {
            node.args[-1].id
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_event_handler"
            and node.args
            and isinstance(node.args[-1], ast.Name)
        }
        self.assertIn("_connect_public_cloud_on_start", hooks)
        self.assertIn("_start_public_cloud_refresh_loop", hooks)
        self.assertIn("_register_public_generation_queue_handler", hooks)
        # The runners are registered for every capability the tier can serve, before
        # anything is connected: the handler decides at run time and defers otherwise.
        register = self.function("_register_public_generation_queue_handler")
        self.assertIn(
            "register_public_handlers", self.attributes_used(register)
        )
        self.assertIn("_delegation_out_dir", self.names_referenced(register))

    def test_a_request_looks_for_public_compute_before_it_is_routed(self):
        # The first Generate on a fresh install has to reach a free public engine
        # without anyone pressing anything.
        refresh = self.function("_refresh_remote_peers_if_needed")
        self.assertIn("_public_sources", self.names_used(refresh))
        self.assertIn("_connect_public_cloud_now", self.names_referenced(refresh))
        self.assertIn("to_thread", self.attributes_used(refresh))
        # ...and the search is rate limited, so pressing Generate never pays for a
        # fresh sweep of the internet on every request.
        connect = self.function("_connect_public_cloud_now")
        self.assertIn("_PUBLIC_CONNECT_MIN_INTERVAL", self.names_referenced(connect))

    def test_the_peer_engine_handler_is_registered_with_the_startup_hooks(self):
        hooks = {
            node.args[-1].id
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_event_handler"
            and node.args
            and isinstance(node.args[-1], ast.Name)
        }
        self.assertIn("_register_local_generation_queue_handler", hooks)
        self.assertIn("_resume_delegated_jobs", hooks)
        # The handler is a local runner: it must not delegate onward, or two
        # peers could hand the same job to each other in a circle.
        handler = self.function("_run_queued_generation_locally")
        self.assertFalse({"choosing", "_run_delegated_generation"} & self.names_used(handler))
        self.assertIn("_force_local_generation", ast.unparse(handler))

    def test_the_upload_surface_is_the_router_the_peer_transfer_uses(self):
        # The media a request brings is posted to the peer's own upload sessions, so
        # the endpoints the transport calls must be the ones the application serves:
        # one definition, in services.upload_api, served here where the old handlers
        # were (the same arrangement the production queue and the downloads API use).
        source = LAUNCH.read_text(encoding="utf-8")
        self.assertIn("upload_api_service.create_router", source)
        self.assertLess(
            source.index("upload_api_service.create_router"),
            source.index("production_queue_api.create_router(root_provider=_film_queue_root)"),
        )
        for name in (
            "upload_limits",
            "upload_session_create",
            "upload_session_status",
            "upload_session_chunk",
            "upload_session_complete",
            "upload_session_cancel",
            "upload_sessions_cleanup",
            "_read_bounded_chunk",
        ):
            with self.subTest(handler=name):
                self.assertEqual(
                    [
                        node.name
                        for node in ast.walk(self.tree)
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and node.name == name
                    ],
                    [],
                    f"launch.py defines {name} instead of serving the router",
                )

    def test_remote_handlers_download_into_the_jobs_own_workspace(self):
        # The peer runner is registered with the workspace resolver (a callable),
        # not one fixed directory, so a delegated result lands where the job's
        # own gallery entry is served from.
        for name in ("_register_remote_engine_runner", "remote_peers_add", "remote_peers_update"):
            with self.subTest(function=name):
                self.assertIn("_delegation_out_dir", self.names_referenced(self.function(name)))


if __name__ == "__main__":
    unittest.main()

"""Public compute: a free engine on the internet, connected without being asked.

The tier these tests cover is the one that has to work on a computer with no GPU
and no second machine, so nothing here may assume a service, a key or an account
exists.  The HTTP layer is therefore injected: a fake transport answers as the Hub,
as a public Gradio Space's ``/info``, and as its event stream — which is exactly the
shape of the real thing (verified against a live Space), without any test depending
on the internet being up.

What is protected here:

* **Discovery is measured.**  Only Spaces the Hub reports as ``RUNNING`` on free
  ZeroGPU hardware are candidates; a paused, erroring or CPU-only Space is not, and
  a Space whose name merely sounds right is never assumed to render anything.
* **The endpoint is read, not guessed.**  A Space's own ``/info`` decides which of
  its endpoints runs the capability: Gradio's internal event callbacks (which also
  take a string, and would report success while rendering nothing), endpoints that
  return text, and endpoints demanding a file the request does not have are all
  excluded before any ordering is applied.
* **The request is mapped honestly.**  A frame count is never sent as seconds, a
  value outside the engine's own declared range is brought inside it *and reported*,
  and a required input nothing can fill stops the render by name.
* **No invented output.**  A Space that answers with JSON, HTML or nothing fails
  with its own reason; an image result that is not an image never becomes a file.
* **Unavailable is not failed.**  A spent free quota, a reclaimed GPU or a Space
  that broke mid-render defers the job (and falls through to another public engine
  first), instead of failing work the user asked for.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import public_cloud  # noqa: E402

HUB = public_cloud.HUB_API
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 32


def space(
    space_id: str,
    *,
    hardware: str = "zero-a10g",
    stage: str = "RUNNING",
    likes: int = 10,
    sdk: str = "gradio",
) -> dict:
    return {
        "id": space_id,
        "sdk": sdk,
        "likes": likes,
        "subdomain": space_id.replace("/", "-").replace(".", "-").replace("_", "-").lower(),
        "runtime": {"stage": stage, "hardware": {"current": hardware}},
    }


def parameter(
    name: str,
    *,
    default: Any = None,
    has_default: bool = True,
    kind: str = "str",
    component: str = "Textbox",
    description: str = "",
) -> dict:
    return {
        "label": name,
        "parameter_name": name,
        "parameter_has_default": has_default,
        "parameter_default": default,
        "python_type": {"type": kind, "description": description},
        "component": component,
    }


FILE_TYPE = "dict(path: str | None (Path to a local file), url: str | None)"


def image_return() -> list[dict]:
    return [{"label": "Result", "python_type": {"type": FILE_TYPE, "description": "a file"}}]


def text_return() -> list[dict]:
    return [{"label": "Result", "python_type": {"type": "str", "description": ""}}]


def video_endpoint(
    *,
    api_name: str = "/text_to_video",
    prompt_default: str = "",
    required_file: bool = False,
) -> dict:
    parameters = [
        parameter("prompt", default=prompt_default),
        parameter("negative_prompt", default=""),
        parameter("seed", default=0, kind="int"),
        parameter("steps", default=30, kind="float"),
    ]
    if required_file:
        parameters.insert(0, parameter("image_path", has_default=False, kind=FILE_TYPE))
    return {"parameters": parameters, "returns": image_return(), "show_api": True}


class FakeSpace:
    """A public Space as the network sees it: an info page and an event stream."""

    def __init__(
        self,
        space_id: str,
        *,
        endpoints: Optional[Mapping[str, Any]] = None,
        result: Any = None,
        stream: str = "",
        status: int = 200,
        file_body: bytes = PNG,
        file_type: str = "image/png",
        upload_path: str = "/tmp/gradio/abc/frame.png",
        info_status: int = 200,
    ) -> None:
        self.space_id = space_id
        self.subdomain = space_id.replace("/", "-").lower()
        self.base_url = f"https://{self.subdomain}.hf.space"
        self.endpoints = dict(endpoints if endpoints is not None else {"/generate": video_endpoint(api_name="/generate")})
        self.result = result
        self.stream = stream
        self.status = status
        self.file_body = file_body
        self.file_type = file_type
        self.upload_path = upload_path
        self.info_status = info_status
        self.calls: list[tuple[str, str, Any]] = []
        self.info_requests = 0


class FakeNetwork:
    """The transport: a Hub, a set of Spaces, and a file server — nothing else."""

    def __init__(self, spaces: list[dict], sites: list[FakeSpace]) -> None:
        self.spaces = spaces
        self.sites = {site.subdomain: site for site in sites}
        self.urls: list[str] = []

    def _hub(self, url: str) -> public_cloud.HttpResponse:
        payload = [s for s in self.spaces if s["id"] in url or "filter" in url]
        # The Hub returns the whole (already sorted) listing for each query.
        return public_cloud.HttpResponse(200, {}, json.dumps(payload).encode())

    def __call__(
        self,
        method: str,
        url: str,
        *,
        data: Optional[bytes] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: float = 15.0,
    ) -> public_cloud.HttpResponse:
        self.urls.append(url)
        if url.startswith(HUB):
            return self._hub(url)
        host = url.split("//", 1)[-1].split("/", 1)[0]
        site = self.sites.get(host.split(".")[0])
        if site is None:
            raise OSError(f"no such public Space: {host}")
        site.calls.append((method, url, data))
        path = "/" + url.split("//", 1)[-1].split("/", 1)[1]
        if path == f"{public_cloud.GRADIO}/info":
            site.info_requests += 1
            if site.info_status != 200:
                return public_cloud.HttpResponse(site.info_status, {}, b"{}")
            return public_cloud.HttpResponse(
                200, {}, json.dumps({"named_endpoints": site.endpoints}).encode()
            )
        if method == "POST" and path == f"{public_cloud.GRADIO}/upload":
            return public_cloud.HttpResponse(200, {}, json.dumps([site.upload_path]).encode())
        if method == "POST" and path.startswith(f"{public_cloud.GRADIO}/call/"):
            if site.status != 200:
                return public_cloud.HttpResponse(site.status, {}, b'{"detail":"nope"}')
            return public_cloud.HttpResponse(200, {}, b'{"event_id":"evt-1"}')
        if method == "GET" and "/call/" in path:
            if site.stream:
                body = site.stream.encode()
            else:
                result = site.result
                if result is None:
                    result = [{"path": "/tmp/gradio/x/out.png", "url": f"{site.base_url}{public_cloud.GRADIO}/file=/tmp/x/out.png"}]
                body = ("event: complete\ndata: " + json.dumps(result) + "\n\n").encode()
            return public_cloud.HttpResponse(200, {}, body)
        if method == "GET" and f"{public_cloud.GRADIO}/file=" in path:
            return public_cloud.HttpResponse(
                200, {"content-type": site.file_type}, site.file_body
            )
        return public_cloud.HttpResponse(404, {}, b"{}")


class Context:
    """``queue_worker.JobContext`` as a public job uses it, recording everything."""

    def __init__(self, *, cancelled: bool = False) -> None:
        self.states: list[str] = []
        self.notes: list[str] = []
        self.links: list[str] = []
        self.deferred: list[str] = []
        self._cancelled = cancelled

    def set_state(self, state: str) -> None:
        self.states.append(state)

    def report(self, *, percent=None, step=None, total_steps=None, note: str = "") -> None:
        if note:
            self.notes.append(note)

    def link(self, external_job_id: str) -> None:
        self.links.append(external_job_id)

    def defer(self, reason: str, *, delay_seconds=None) -> None:
        self.deferred.append(reason)

    def cancelled(self) -> bool:
        return self._cancelled


def job(capability: str, **params: Any) -> dict:
    return {
        "engine": public_cloud.engine_for(capability),
        "params": {"capability": capability, **params},
    }


class DiscoveryTests(unittest.TestCase):
    def test_only_running_spaces_on_free_hardware_are_candidates(self):
        spaces = [
            space("good/free", likes=5),
            space("slow/cpu", hardware="cpu-basic", likes=99),
            space("paused/one", stage="PAUSED", likes=50),
            space("broken/one", stage="RUNTIME_ERROR", likes=40),
        ]
        found = public_cloud.discover(("text-to-video",), transport=FakeNetwork(spaces, []))
        self.assertEqual([entry["space_id"] for entry in found], ["good/free"])
        self.assertEqual(found[0]["hardware"], "zero-a10g")

    def test_a_non_gradio_space_is_not_a_candidate(self):
        spaces = [space("streamlit/app", sdk="streamlit")]
        self.assertEqual(public_cloud.discover(("text-to-video",), transport=FakeNetwork(spaces, [])), [])

    def test_the_most_liked_running_space_comes_first(self):
        spaces = [space("quiet/one", likes=2), space("busy/one", likes=200)]
        found = public_cloud.discover(("text-to-video",), transport=FakeNetwork(spaces, []))
        self.assertEqual([entry["space_id"] for entry in found], ["busy/one", "quiet/one"])

    def test_a_hub_that_cannot_be_reached_finds_nothing_instead_of_raising(self):
        def transport(*args, **kwargs):
            raise OSError("no internet")

        self.assertEqual(public_cloud.discover(("text-to-video",), transport=transport), [])


class EndpointChoiceTests(unittest.TestCase):
    def choose(self, endpoints: Mapping[str, Any], capability: str) -> dict:
        return public_cloud.choose_endpoint({"named_endpoints": endpoints}, capability)

    def test_the_endpoint_named_after_the_capability_wins(self):
        chosen = self.choose(
            {
                "/generate": video_endpoint(api_name="/generate"),
                "/text_to_video": video_endpoint(api_name="/text_to_video"),
            },
            "text_to_video",
        )
        self.assertEqual(chosen["api_name"], "/text_to_video")
        self.assertTrue(chosen["exact"])

    def test_gradios_own_event_callbacks_are_never_used(self):
        # These take a string too, so without this they look like renderers.
        chosen = self.choose(
            {
                "/_on_user_loras_change": {"parameters": [parameter("text")], "returns": image_return()},
                "/lambda_2": {"parameters": [parameter("text")], "returns": image_return()},
                "/prepare_user_loras": {"parameters": [parameter("text")], "returns": image_return()},
                "/generate": video_endpoint(api_name="/generate"),
            },
            "text_to_video",
        )
        self.assertEqual(chosen["api_name"], "/generate")

    def test_an_endpoint_that_returns_text_is_not_a_renderer(self):
        with self.assertRaises(public_cloud.PublicCloudError):
            self.choose(
                {"/build_prompt": {"parameters": [parameter("prompt", has_default=False)], "returns": text_return()}},
                "text_to_video",
            )

    def test_an_endpoint_that_demands_a_file_is_skipped_for_a_text_request(self):
        with self.assertRaises(public_cloud.PublicCloudError):
            self.choose(
                {"/generate": video_endpoint(api_name="/generate", required_file=True)},
                "text_to_video",
            )

    def test_a_request_bringing_a_frame_needs_an_endpoint_that_can_receive_it(self):
        with self.assertRaises(public_cloud.PublicCloudError):
            self.choose(
                {"/text_to_video": video_endpoint(api_name="/text_to_video")},
                "image_to_video",
            )
        chosen = self.choose(
            {"/image_to_video": video_endpoint(api_name="/image_to_video", required_file=True)},
            "image_to_video",
        )
        self.assertTrue(chosen["accepts_media"])

    def test_a_space_with_nothing_usable_says_so(self):
        with self.assertRaises(public_cloud.PublicCloudError) as caught:
            self.choose({"/health": {"parameters": [], "returns": []}}, "text_to_video")
        self.assertIn("cannot run this request", str(caught.exception))


class DataMappingTests(unittest.TestCase):
    def endpoint(self, *parameters: dict) -> dict:
        return {"parameters": list(parameters), "returns": image_return()}

    def test_the_prompt_and_the_obvious_settings_cross_over(self):
        data = public_cloud.build_data(
            self.endpoint(
                parameter("prompt", has_default=False),
                parameter("negative_prompt", default=""),
                parameter("seed", default=0, kind="int"),
                parameter("num_inference_steps", default=20, kind="float"),
                parameter("width", default=512, kind="float"),
                parameter("height", default=512, kind="float"),
            ),
            {
                "prompt": "a paper boat",
                "negative_prompt": "blurry",
                "seed": 7,
                "num_inference_steps": 12,
                "resolution": "768x448",
                "capability": "image_generation",
            },
        )
        self.assertEqual(data, ["a paper boat", "blurry", 7, 12, 768, 448])

    def test_a_frame_count_is_never_sent_as_seconds(self):
        endpoint = self.endpoint(parameter("prompt", has_default=False), parameter("duration", default=5, kind="float"))
        # 48 frames at 24 fps is two seconds, and two seconds is what may be sent.
        self.assertEqual(
            public_cloud.build_data(endpoint, {"prompt": "x", "video_length": 48, "fps": 24, "capability": "text_to_video"}),
            ["x", 2],
        )
        # Without a frame rate nothing can be derived, so the Space's own default stays.
        self.assertEqual(
            public_cloud.build_data(endpoint, {"prompt": "x", "video_length": 90, "capability": "text_to_video"}),
            ["x", 5],
        )

    def test_a_value_outside_the_engines_own_range_is_brought_inside_and_reported(self):
        notes: list[str] = []
        data = public_cloud.build_data(
            self.endpoint(
                parameter("prompt", has_default=False),
                parameter("steps", default=30, kind="float", description="numeric value between 10 and 60"),
            ),
            {"prompt": "x", "num_inference_steps": 4, "capability": "image_generation"},
            notes=notes,
        )
        self.assertEqual(data, ["x", 10])
        self.assertEqual(len(notes), 1)
        self.assertIn("steps", notes[0])

    def test_a_fixed_seed_turns_the_randomizer_off(self):
        endpoint = self.endpoint(
            parameter("prompt", has_default=False),
            parameter("seed", default=0, kind="int"),
            parameter("randomize_seed", default=True, kind="bool"),
        )
        self.assertEqual(
            public_cloud.build_data(endpoint, {"prompt": "x", "seed": 5, "capability": "image_generation"}),
            ["x", 5, False],
        )
        self.assertEqual(
            public_cloud.build_data(endpoint, {"prompt": "x", "capability": "image_generation"}),
            ["x", 0, True],
        )

    def test_a_closed_choice_is_told_which_mode_it_is_in(self):
        mode = parameter("mode", default="text-to-video", kind="Literal['text-to-video', 'image-to-video']")
        self.assertEqual(
            public_cloud.build_data(
                self.endpoint(parameter("prompt", has_default=False), mode),
                {"prompt": "x", "capability": "image_to_video"},
            ),
            ["x", "image-to-video"],
        )

    def test_a_required_input_nothing_can_fill_stops_the_render_by_name(self):
        with self.assertRaises(public_cloud.PublicCloudError) as caught:
            public_cloud.build_data(
                self.endpoint(parameter("prompt", has_default=False), parameter("aspect_ratio", has_default=False)),
                {"prompt": "x", "capability": "image_generation"},
            )
        self.assertIn("aspect_ratio", str(caught.exception))


class ConnectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def test_it_connects_to_a_running_public_space_on_its_own(self) -> None:
        site = FakeSpace("owner/space")
        network = FakeNetwork([space("owner/space")], [site])
        result = public_cloud.connect(self.root, capabilities=["text_to_video"], transport=network)
        self.assertEqual([entry["space_id"] for entry in result["connected"]], ["owner/space"])
        # The endpoint came from the Space's own report, not from its name.
        self.assertEqual(result["connected"][0]["endpoints"]["text_to_video"]["api_name"], "/generate")
        # It was remembered, so the next call does not search again.
        state = public_cloud.load_state(self.root)
        self.assertIn("hf-space:owner/space", state["providers"])
        after = network.urls.copy()
        public_cloud.connect(self.root, capabilities=["text_to_video"], transport=network)
        self.assertEqual(network.urls, after)

    def test_a_space_that_cannot_run_the_capability_is_skipped_for_the_next_one(self) -> None:
        broken = FakeSpace("first/one", endpoints={"/health": {"parameters": [], "returns": []}})
        good = FakeSpace("second/one", endpoints={"/text_to_video": video_endpoint()})
        network = FakeNetwork(
            [space("first/one", likes=100), space("second/one", likes=5)], [broken, good]
        )
        result = public_cloud.connect(self.root, capabilities=["text_to_video"], transport=network)
        self.assertEqual([entry["space_id"] for entry in result["connected"]], ["second/one"])
        self.assertEqual(result["attempts"][0]["outcome"], "not_usable")

    def test_an_endpoint_named_after_the_capability_beats_a_more_liked_generate(self) -> None:
        liked = FakeSpace("liked/one", endpoints={"/generate": video_endpoint(api_name="/generate")})
        exact = FakeSpace("exact/one", endpoints={"/text_to_video": video_endpoint()})
        network = FakeNetwork([space("liked/one", likes=500), space("exact/one", likes=1)], [liked, exact])
        result = public_cloud.connect(self.root, capabilities=["text_to_video"], transport=network)
        self.assertEqual([entry["space_id"] for entry in result["connected"]], ["exact/one"])

    def test_nothing_running_is_reported_instead_of_connected(self) -> None:
        network = FakeNetwork([space("paused/one", stage="PAUSED")], [])
        result = public_cloud.connect(self.root, capabilities=["text_to_video"], transport=network)
        self.assertEqual(result["connected"], [])
        self.assertEqual(result["attempts"][0]["outcome"], "nothing_running")

    def test_a_space_that_is_not_answering_is_not_a_source(self) -> None:
        # The Hub says it is running, but the Space itself does not answer: it is not
        # a place a render can run, and saying otherwise would fail the user's job.
        site = FakeSpace("owner/space", info_status=503)
        network = FakeNetwork([space("owner/space")], [site])
        result = public_cloud.connect(self.root, capabilities=["text_to_video"], transport=network)
        self.assertEqual(result["connected"], [])
        self.assertEqual(result["attempts"][0]["outcome"], "not_usable")
        self.assertEqual(public_cloud.connected_sources(self.root), [])

    def test_an_offline_space_disappears_from_the_sources(self) -> None:
        site = FakeSpace("owner/space")
        network = FakeNetwork([space("owner/space")], [site])
        public_cloud.connect(self.root, capabilities=["text_to_video"], transport=network)
        self.assertEqual(len(public_cloud.connected_sources(self.root)), 1)
        # Long past the freshness window, the connection is no longer evidence.
        self.assertEqual(
            public_cloud.connected_sources(self.root, now=__import__("time").time() + 10_000), []
        )

    def test_a_space_can_be_added_by_hand_and_is_still_probed(self) -> None:
        site = FakeSpace("myspace/one", endpoints={"/text_to_video": video_endpoint()})
        network = FakeNetwork([], [site])
        added = public_cloud.add_space(
            self.root, "myspace/one", capability="text_to_video", transport=network
        )
        self.assertTrue(added["provider"]["online"])
        self.assertEqual(
            [entry["provider_id"] for entry in public_cloud.connected_sources(self.root)],
            ["hf-space:myspace/one"],
        )

    def test_an_unknown_capability_is_refused(self) -> None:
        with self.assertRaises(public_cloud.PublicCloudError):
            public_cloud.add_space(self.root, "myspace/one", capability="teleportation")

    def test_forgetting_is_honest_about_what_it_changed(self) -> None:
        site = FakeSpace("owner/space")
        network = FakeNetwork([space("owner/space")], [site])
        public_cloud.connect(self.root, capabilities=["text_to_video"], transport=network)
        result = public_cloud.forget_provider(self.root, "hf-space:owner/space")
        self.assertIn("changes nothing outside this computer", result["note"])
        with self.assertRaises(FileNotFoundError):
            public_cloud.forget_provider(self.root, "hf-space:owner/space")

    def test_the_provider_entries_are_shaped_for_the_engine_registry(self) -> None:
        site = FakeSpace("owner/space")
        network = FakeNetwork([space("owner/space")], [site])
        public_cloud.connect(self.root, capabilities=["text_to_video"], transport=network)
        entry = public_cloud.provider_entries(self.root)[0]
        self.assertEqual(entry["provider_id"], "hf-space:owner/space")
        self.assertEqual(entry["capabilities"], ["text_to_video"])
        self.assertTrue(entry["online"])
        self.assertFalse(entry["requires_key"])


class RunJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.out = os.path.join(self.root, "workspace")
        os.makedirs(self.out, exist_ok=True)

    def connect(self, sites: list[FakeSpace], **kwargs: Any) -> FakeNetwork:
        spaces = [space(site.space_id, likes=kwargs.pop("likes", 10)) for site in sites]
        network = FakeNetwork(spaces, sites)
        public_cloud.connect(self.root, capabilities=["text_to_video"], transport=network)
        return network

    def test_a_render_is_downloaded_into_the_workspace_and_validated(self) -> None:
        site = FakeSpace("owner/space", endpoints={"/text_to_video": video_endpoint()}, file_body=MP4, file_type="video/mp4")
        network = self.connect([site])
        context = Context()
        files = public_cloud.run_job(
            self.root, job("text_to_video", prompt="a paper boat"), context,
            out_dir=self.out, transport=network,
        )
        self.assertEqual(len(files), 1)
        self.assertTrue(os.path.isfile(files[0]))
        self.assertTrue(files[0].startswith(os.path.abspath(self.out)))
        self.assertTrue(files[0].endswith(".mp4"))
        with open(files[0], "rb") as handle:
            self.assertEqual(handle.read(4), b"\x00\x00\x00\x18")
        self.assertEqual(context.states[:2], ["preparing", "running"])
        self.assertIn("finalizing", context.states)
        self.assertIn("public:owner/space", context.links)
        self.assertEqual(context.deferred, [])
        # What was actually sent is the mapped request, not Maestro's parameter set.
        post = [call for call in site.calls if call[0] == "POST"][0]
        sent = json.loads(post[2].decode())["data"]
        self.assertEqual(sent[0], "a paper boat")

    def test_a_broken_space_falls_through_to_the_next_one(self) -> None:
        # The first choice is the more liked Space, and it breaks without a reason —
        # the case a public tier has to survive, because it is somebody else's server.
        broken = FakeSpace(
            "first/one",
            endpoints={"/text_to_video": video_endpoint()},
            stream="event: error\ndata: null\n\n",
        )
        good = FakeSpace(
            "second/one",
            endpoints={"/text_to_video": video_endpoint()},
            file_body=MP4,
            file_type="video/mp4",
        )
        network = self.connect([broken, good])
        context = Context()
        files = public_cloud.run_job(
            self.root, job("text_to_video", prompt="x"), context, out_dir=self.out, transport=network
        )
        self.assertEqual(len(files), 1)
        self.assertIn("Trying second/one instead", context.notes)
        self.assertIn("Preparing on first/one", " ".join(context.notes))

    def test_a_spent_free_quota_defers_the_job_instead_of_failing_it(self) -> None:
        site = FakeSpace(
            "owner/space",
            stream=(
                "event: error\ndata: "
                + json.dumps({"error": "You have exceeded your ZeroGPU quota (90s requested vs. 0s left)."})
                + "\n\n"
            ),
        )
        network = self.connect([site])
        context = Context()
        files = public_cloud.run_job(
            self.root, job("text_to_video", prompt="x"), context, out_dir=self.out, transport=network
        )
        self.assertEqual(files, [])
        self.assertEqual(len(context.deferred), 1)
        self.assertIn("quota", context.deferred[0])

    def test_a_service_that_refuses_right_now_defers_the_job(self) -> None:
        site = FakeSpace("owner/space", status=429)
        network = self.connect([site])
        context = Context()
        self.assertEqual(
            public_cloud.run_job(
                self.root, job("text_to_video", prompt="x"), context, out_dir=self.out, transport=network
            ),
            [],
        )
        self.assertEqual(len(context.deferred), 1)

    def test_nothing_connected_defers_instead_of_failing(self) -> None:
        context = Context()
        files = public_cloud.run_job(
            self.root, job("text_to_video", prompt="x"), context,
            out_dir=self.out, transport=FakeNetwork([], []),
        )
        self.assertEqual(files, [])
        self.assertIn("keeps looking", context.deferred[0])

    def test_an_error_page_is_never_recorded_as_a_render(self) -> None:
        site = FakeSpace("owner/space", file_body=b"<html>gateway error</html>", file_type="text/html")
        network = self.connect([site])
        context = Context()
        with self.assertRaises(public_cloud.PublicCloudError) as caught:
            public_cloud.run_job(
                self.root, job("text_to_video", prompt="x"), context, out_dir=self.out, transport=network
            )
        self.assertIn("text instead of media", str(caught.exception))
        self.assertEqual(os.listdir(self.out), [])

    def test_an_image_result_is_not_accepted_as_a_video(self) -> None:
        site = FakeSpace("owner/space", file_body=PNG, file_type="image/png")
        network = self.connect([site])
        with self.assertRaises(public_cloud.PublicCloudError):
            public_cloud.run_job(
                self.root, job("text_to_video", prompt="x"), Context(), out_dir=self.out, transport=network
            )
        self.assertEqual(os.listdir(self.out), [])

    def test_cancellation_is_acknowledged_while_the_public_gpu_is_working(self) -> None:
        site = FakeSpace("owner/space")
        network = self.connect([site])
        context = Context(cancelled=True)
        with self.assertRaises(Exception) as caught:
            public_cloud.run_job(
                self.root, job("text_to_video", prompt="x"), context, out_dir=self.out, transport=network
            )
        self.assertEqual(type(caught.exception).__name__, "JobCancelled")

    def test_a_request_bringing_a_frame_uploads_it_before_the_render(self) -> None:
        frame = os.path.join(self.root, "frame.png")
        with open(frame, "wb") as handle:
            handle.write(PNG)
        site = FakeSpace(
            "owner/space",
            endpoints={"/image_to_video": video_endpoint(api_name="/image_to_video", required_file=True)},
            file_body=MP4,
            file_type="video/mp4",
        )
        spaces = [space("owner/space")]
        network = FakeNetwork(spaces, [site])
        public_cloud.connect(self.root, capabilities=["image_to_video"], transport=network)
        files = public_cloud.run_job(
            self.root,
            job("image_to_video", prompt="x", image_start=frame),
            Context(),
            out_dir=self.out,
            transport=network,
        )
        self.assertEqual(len(files), 1)
        uploaded = [call for call in site.calls if call[1].endswith("/upload")]
        self.assertEqual(len(uploaded), 1)
        sent = json.loads([call for call in site.calls if call[0] == "POST"][-1][2].decode())["data"]
        self.assertEqual(sent[0]["path"], site.upload_path)
        self.assertEqual(sent[0]["meta"]["_type"], "gradio.FileData")


class HandlerRegistrationTests(unittest.TestCase):
    def test_a_runner_exists_for_every_capability_the_tier_serves(self) -> None:
        from services import queue_worker

        self.addCleanup(queue_worker.clear_handlers)
        registered = public_cloud.register_public_handlers(
            "/nonexistent", out_dir=lambda _job: "/tmp"
        )
        self.assertEqual(
            sorted(registered),
            sorted(public_cloud.engine_for(name) for name in public_cloud.SERVED_CAPABILITIES),
        )
        self.assertEqual(sorted(queue_worker.handler_names()), sorted(registered))
        self.assertEqual(
            sorted(public_cloud.unregister_public_handlers()), sorted(registered)
        )
        self.assertEqual(queue_worker.handler_names(), [])


class RouterIntegrationTests(unittest.TestCase):
    """The tier as the router sees it: a third place, never the first."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def source(self) -> dict:
        site = FakeSpace("owner/space")
        network = FakeNetwork([space("owner/space")], [site])
        public_cloud.connect(self.root, capabilities=["text_to_video"], transport=network)
        return public_cloud.connected_sources(self.root)[0]

    def test_the_source_carries_what_the_router_needs(self) -> None:
        source = self.source()
        self.assertEqual(source["kind"], "public_cloud")
        self.assertTrue(source["online"])
        self.assertTrue(source["free"])
        self.assertFalse(source["requires_key"])
        self.assertEqual(source["capabilities"], ["text_to_video"])
        self.assertEqual(source["fidelity"], "public_approximation")


if __name__ == "__main__":
    unittest.main()

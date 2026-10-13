"""Where a generation request runs: this computer, a peer, or a public engine.

The rule these tests protect is the one the whole delegation feature rests on:

* a machine that can generate **always** generates locally — nothing is sent away
  while the local engine could have done it;
* a machine that cannot generate is not a dead end: the request is addressed to a
  peer that *reported* it can render, then to a free public engine on the internet
  that Maestro connected to by itself, and only then refused with a reason naming
  what is missing — never sent somewhere on the strength of a name or a hope;
* a peer is preferred over a public engine, because a peer runs this same pipeline
  and a public Space runs its own model;
* a request carrying media from this computer only goes to an engine that reported
  somewhere to put that media. Refusing that is honest; sending it would render a
  different video for a reason the user did not cause.

The decision is a pure function, so none of this needs a GPU, a peer or the app.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services import engine_routing  # noqa: E402


def peer(
    peer_id: str = "desk-1",
    *,
    online: bool = True,
    capabilities: tuple[str, ...] = ("text_to_video", "image_generation"),
    label: str = "Desktop with the GPU",
) -> dict:
    return {
        "peer_id": peer_id,
        "label": label,
        "base_url": f"http://{peer_id}.tailnet.ts.net",
        "capabilities": list(capabilities),
        "online": online,
        "engine_id": f"node:{peer_id}",
    }


def public_source(
    provider_id: str = "hf-space:owner/space",
    *,
    online: bool = True,
    capabilities: tuple[str, ...] = ("text_to_video", "image_generation"),
    accepts_media: bool = False,
    label: str = "owner/space (public Hugging Face Space)",
) -> dict:
    return {
        "kind": "public_cloud",
        "provider_id": provider_id,
        "label": label,
        "base_url": "https://owner-space.hf.space",
        "hardware": "zero-a10g",
        "capabilities": list(capabilities),
        "accepts_media": accepts_media,
        "online": online,
        "free": True,
        "fidelity": "public_approximation",
    }


class CapabilityForRequestTests(unittest.TestCase):
    def test_video_becomes_text_to_video_unless_a_frame_is_supplied(self):
        self.assertEqual(
            engine_routing.capability_for_request({"model_type": "h3", "generation_mode": "video"}),
            "text_to_video",
        )
        self.assertEqual(
            engine_routing.capability_for_request(
                {"model_type": "h3", "generation_mode": "video", "image_start": "frame.png"}
            ),
            "image_to_video",
        )

    def test_image_and_audio_requests_use_their_own_capability(self):
        self.assertEqual(
            engine_routing.capability_for_request({"generation_mode": "image"}), "image_generation"
        )
        self.assertEqual(
            engine_routing.capability_for_request({"generation_mode": "image", "image_mode": 2}),
            "inpainting",
        )
        self.assertEqual(
            engine_routing.capability_for_request({"generation_mode": "audio"}), "audio_generation"
        )
        self.assertEqual(
            engine_routing.capability_for_request(
                {"generation_mode": "audio", "voice_reference": "voice.wav"}
            ),
            "voice_generation",
        )

    def test_a_request_that_declares_nothing_asks_for_nothing_specific(self):
        self.assertEqual(engine_routing.capability_for_request({"sfx_mode": True}), "")


class LocalMediaTests(unittest.TestCase):
    def test_media_inputs_are_named_in_a_stable_order(self):
        found = engine_routing.local_media_inputs(
            {"image_refs": ["a.png"], "video_guide": "clip.mp4", "prompt": "text only"}
        )
        self.assertEqual(found, ["image_refs", "video_guide"])

    def test_empty_media_values_do_not_count(self):
        self.assertEqual(
            engine_routing.local_media_inputs({"image_refs": [], "video_guide": "   "}), []
        )


class ChooseTargetTests(unittest.TestCase):
    def choose(self, **kwargs):
        payload = {
            "capability": "text_to_video",
            "local_generation_possible": False,
            "peers": [],
            "request_params": {"model_type": "h3"},
            "public_sources": [],
        }
        payload.update(kwargs)
        return engine_routing.choose_target(**payload)

    def test_a_local_gpu_always_wins_over_an_online_peer(self):
        route = self.choose(local_generation_possible=True, peers=[peer()])
        self.assertEqual(route["target"], engine_routing.LOCAL_TARGET)
        self.assertEqual(route["engine"], engine_routing.LOCAL_ENGINE)
        self.assertEqual(route["peer_id"], "")

    def test_without_a_local_gpu_the_job_goes_to_a_peer_that_can_render(self):
        route = self.choose(peers=[peer()])
        self.assertEqual(route["target"], engine_routing.REMOTE_TARGET)
        self.assertEqual(route["engine"], "node:desk-1")
        self.assertEqual(route["peer_id"], "desk-1")
        self.assertEqual(route["remote_engine"], engine_routing.REMOTE_ENGINE)
        self.assertEqual(route["candidates"], ["desk-1"])
        self.assertIn("desk-1", route["reason"])

    def test_a_stale_probe_does_not_make_a_peer_a_target(self):
        route = self.choose(peers=[peer(online=False)])
        self.assertEqual(route["target"], engine_routing.NO_TARGET)
        self.assertIn("probe", route["reason"])

    def test_a_peer_that_reports_no_gpu_capability_cannot_take_the_job(self):
        route = self.choose(peers=[peer(capabilities=("auxiliary_processing",))])
        self.assertEqual(route["target"], engine_routing.NO_TARGET)
        self.assertIn("no capability that renders", route["reason"])

    def test_a_peer_that_does_not_report_the_needed_capability_is_not_used(self):
        route = self.choose(capability="image_to_video", peers=[peer(capabilities=("text_to_video",))])
        self.assertEqual(route["target"], engine_routing.NO_TARGET)
        self.assertIn("image_to_video", route["reason"])
        self.assertIn("text_to_video", route["reason"])

    def test_a_request_without_a_declared_capability_accepts_any_renderer(self):
        route = self.choose(capability="", peers=[peer(capabilities=("image_generation",))])
        self.assertEqual(route["target"], engine_routing.REMOTE_TARGET)
        self.assertEqual(route["peer_id"], "desk-1")

    def test_nothing_configured_is_refused_by_naming_the_missing_gpu(self):
        route = self.choose()
        self.assertEqual(route["target"], engine_routing.NO_TARGET)
        self.assertIn("no CUDA GPU", route["reason"])
        self.assertIn("no remote computer is configured", route["reason"])

    def test_media_from_this_computer_is_delegated_and_named_for_transfer(self):
        # The peer cannot read a path that only exists here, so the request's own
        # media is sent with the job.  The route says what has to travel, and the
        # reason tells the person reading it that this will happen.
        route = self.choose(
            peers=[peer()],
            request_params={"model_type": "h3", "image_start": "only-here.png"},
        )
        self.assertEqual(route["target"], engine_routing.REMOTE_TARGET)
        self.assertEqual(route["media_to_transfer"], ["image_start"])
        self.assertIn("image_start", route["reason"])
        self.assertIn("sent to that computer first", route["reason"])

    def test_a_request_without_media_says_nothing_about_transferring_it(self):
        route = self.choose(peers=[peer()], request_params={"model_type": "h3"})
        self.assertEqual(route["media_to_transfer"], [])
        self.assertNotIn("sent to that computer first", route["reason"])

    def test_a_local_gpu_still_lists_the_media_it_will_use_here(self):
        route = self.choose(
            local_generation_possible=True,
            peers=[peer()],
            request_params={"model_type": "h3", "image_start": "here.png"},
        )
        self.assertEqual(route["target"], engine_routing.LOCAL_TARGET)
        self.assertEqual(route["media_to_transfer"], ["image_start"])

    def test_the_first_online_renderer_wins_deterministically(self):
        route = self.choose(
            peers=[peer("desk-2", label="Second"), peer("desk-1", label="First")]
        )
        self.assertEqual(route["peer_id"], "desk-2")

    def test_an_offline_renderer_does_not_hide_an_online_one(self):
        route = self.choose(peers=[peer("desk-off", online=False), peer("desk-on")])
        self.assertEqual(route["peer_id"], "desk-on")


class PublicTierTests(unittest.TestCase):
    """The third tier: a free public engine, and the rules for using it."""

    def choose(self, **kwargs):
        payload = {
            "capability": "text_to_video",
            "local_generation_possible": False,
            "peers": [],
            "request_params": {"model_type": "h3"},
            "public_sources": [public_source()],
        }
        payload.update(kwargs)
        return engine_routing.choose_target(**payload)

    def test_without_a_gpu_and_without_a_peer_the_job_goes_to_a_public_engine(self):
        route = self.choose()
        self.assertEqual(route["target"], engine_routing.PUBLIC_TARGET)
        self.assertEqual(route["engine"], "public:text_to_video")
        self.assertEqual(route["public_provider_id"], "hf-space:owner/space")
        self.assertEqual(route["public_fidelity"], engine_routing.PUBLIC_FIDELITY)
        self.assertEqual(route["capability"], "text_to_video")
        self.assertIn("owner/space", route["reason"])
        self.assertIn(engine_routing.PUBLIC_FIDELITY, route["reason"])
        self.assertIn("connected to by itself", route["reason"])

    def test_a_local_gpu_still_wins_and_a_peer_still_wins(self):
        self.assertEqual(
            self.choose(local_generation_possible=True)["target"], engine_routing.LOCAL_TARGET
        )
        with_peer = self.choose(peers=[peer()])
        self.assertEqual(with_peer["target"], engine_routing.REMOTE_TARGET)
        self.assertEqual(with_peer["public_provider_id"], "")
        self.assertEqual(with_peer["engine"], "node:desk-1")

    def test_a_peer_that_cannot_take_the_capability_steps_aside_for_a_public_engine(self):
        # The peer is online but does not report the capability, so the public tier is
        # the honest answer instead of a refusal.
        route = self.choose(peers=[peer(capabilities=("image_generation",))])
        self.assertEqual(route["target"], engine_routing.PUBLIC_TARGET)
        self.assertIn("No remote computer reports", route["reason"])

    def test_a_public_engine_that_is_not_online_is_not_a_target(self):
        route = self.choose(public_sources=[public_source(online=False)])
        self.assertEqual(route["target"], engine_routing.NO_TARGET)
        self.assertIn("Public compute answered before", route["reason"])
        self.assertIn("may find one", route["reason"])
        # Nothing configured at all is a different sentence, and says what to do.
        nothing = self.choose(public_sources=[])
        self.assertIn("Public compute is not connected either", nothing["reason"])
        self.assertIn("keeps looking", nothing["reason"])

    def test_a_public_engine_that_does_not_report_the_capability_is_not_used(self):
        route = self.choose(
            capability="image_to_video",
            public_sources=[public_source(capabilities=("image_generation",))],
        )
        self.assertEqual(route["target"], engine_routing.NO_TARGET)
        self.assertIn("image_to_video", route["reason"])
        self.assertIn("image_generation", route["reason"])

    def test_a_request_bringing_a_frame_needs_a_public_engine_that_can_receive_it(self):
        route = self.choose(
            capability="image_to_video",
            request_params={"model_type": "h3", "image_start": "only-here.png"},
            public_sources=[public_source(capabilities=("image_to_video",), accepts_media=False)],
        )
        self.assertEqual(route["target"], engine_routing.NO_TARGET)
        self.assertIn("no file input", route["reason"])
        accepted = self.choose(
            capability="image_to_video",
            request_params={"model_type": "h3", "image_start": "only-here.png"},
            public_sources=[public_source(capabilities=("image_to_video",), accepts_media=True)],
        )
        self.assertEqual(accepted["target"], engine_routing.PUBLIC_TARGET)
        self.assertEqual(accepted["media_to_transfer"], ["image_start"])

    def test_media_a_public_engine_has_no_input_for_is_refused_for_that_tier(self):
        # A soundtrack has nowhere to go on a public Space, and rendering without it
        # would be a different video than the one that was asked for.
        route = self.choose(
            request_params={"model_type": "h3", "audio_guide": "theme.wav"},
            public_sources=[public_source(accepts_media=True)],
        )
        self.assertEqual(route["target"], engine_routing.NO_TARGET)
        self.assertIn("audio_guide", route["reason"])

    def test_the_first_public_engine_wins_deterministically(self):
        route = self.choose(
            public_sources=[
                public_source("hf-space:b/second", label="Second"),
                public_source("hf-space:a/first", label="First"),
            ]
        )
        self.assertEqual(route["public_provider_id"], "hf-space:b/second")
        self.assertEqual(
            route["public_candidates"], ["hf-space:b/second", "hf-space:a/first"]
        )

    def test_an_offline_public_engine_does_not_hide_an_online_one(self):
        route = self.choose(
            public_sources=[
                public_source("hf-space:off/gone", online=False),
                public_source("hf-space:on/here"),
            ]
        )
        self.assertEqual(route["public_provider_id"], "hf-space:on/here")

    def test_a_request_asking_for_public_compute_names_it_even_with_a_local_gpu(self):
        route = self.choose(local_generation_possible=True)
        self.assertEqual(route["target"], engine_routing.LOCAL_TARGET)
        self.assertTrue(route["public_generation_available"])


class RenderingCapabilitiesTests(unittest.TestCase):
    def test_only_capabilities_that_render_are_considered(self):
        self.assertEqual(
            engine_routing.rendering_capabilities(
                {"capabilities": ["auxiliary_processing", "transcription", "upscaling"]}
            ),
            ["upscaling"],
        )

    def test_a_malformed_capability_list_is_not_a_capability(self):
        self.assertEqual(engine_routing.rendering_capabilities({"capabilities": "text_to_video"}), [])


if __name__ == "__main__":
    unittest.main()

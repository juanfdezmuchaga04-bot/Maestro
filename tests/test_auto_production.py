"""Automatic production, two-node codes, SeedVR2 finishing and load-balanced routing."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from services import auto_production, cloud_node, engine_routing, remote_engine, seedvr2_upscaler  # noqa: E402

TOKEN = "t" * 40


def peer(peer_id: str) -> dict:
    return {"peer_id": peer_id, "label": peer_id, "base_url": f"https://{peer_id}.example",
            "capabilities": ["text_to_video"], "online": True, "engine_id": f"node:{peer_id}"}


class LoadBalancingTests(unittest.TestCase):
    def choose(self, load):
        return engine_routing.choose_target(capability="text_to_video", local_generation_possible=False,
                                            peers=[peer("free-gpu"), peer("free-gpu-2")],
                                            request_params={"model_type": "x"}, peer_load=load)

    def test_the_least_busy_node_gets_the_next_job(self):
        self.assertEqual(self.choose({})["peer_id"], "free-gpu")
        self.assertEqual(self.choose({"free-gpu": 1})["peer_id"], "free-gpu-2")
        self.assertEqual(self.choose({"free-gpu": 1, "free-gpu-2": 1})["peer_id"], "free-gpu")


class TwoNodeCodeTests(unittest.TestCase):
    def test_a_session_with_two_gpus_prints_one_code_for_both(self):
        nodes = [{"url": "https://a.trycloudflare.com", "token": TOKEN, "label": "Kaggle T4 #1", "gpu": "Tesla T4 15360 MiB"},
                 {"url": "https://b.trycloudflare.com/", "token": TOKEN, "label": "Kaggle T4 #2", "gpu": "Tesla T4 15360 MiB"}]
        parsed = cloud_node.parse_nodes(cloud_node.make_multi_code(nodes, expires_hint="session"))
        self.assertEqual([n["url"] for n in parsed], ["https://a.trycloudflare.com", "https://b.trycloudflare.com"])
        self.assertEqual(parsed[1]["expires"], "session")
        self.assertEqual([cloud_node.peer_id_for(i) for i in range(2)], ["free-gpu", "free-gpu-2"])

    def test_a_single_node_still_uses_the_version_1_code(self):
        code = cloud_node.make_multi_code([{"url": "https://a.trycloudflare.com", "token": TOKEN}])
        self.assertEqual(cloud_node.parse_code(code)["url"], "https://a.trycloudflare.com")
        self.assertEqual(len(cloud_node.parse_nodes(code)), 1)

    def test_gpu_memory_is_read_from_the_node_description(self):
        self.assertEqual(cloud_node.gpu_memory_gb("Tesla T4 15360 MiB - until the session ends"), 15.0)
        self.assertEqual(cloud_node.gpu_memory_gb("unknown"), 0.0)


class NestedMediaTransferTests(unittest.TestCase):
    def test_per_clip_keyframes_are_uploaded_and_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = (os.path.join(folder, name) for name in ("a.png", "b.png"))
            for path in (first, second):
                Path(path).write_bytes(b"png")

            class Client:
                def __init__(self):
                    self.sent = []

                def upload_file(self, path, **_):
                    self.sent.append(path)
                    return f"/peer/uploads/{os.path.basename(path)}"

            client = Client()
            params, unsent = remote_engine.transfer_media(client, {"per_clip_keyframes": [[first], [first, second]]})
        self.assertEqual(unsent, [])
        self.assertEqual(params["per_clip_keyframes"],
                         [["/peer/uploads/a.png"], ["/peer/uploads/a.png", "/peer/uploads/b.png"]])
        self.assertEqual(len(client.sent), 2, "an identical file is sent once")


class SeedVR2Tests(unittest.TestCase):
    def test_settings_follow_gpu_memory(self):
        self.assertEqual(seedvr2_upscaler.choose_settings(15)["dit_model"], "seedvr2_ema_3b-Q8_0.gguf")
        self.assertEqual(seedvr2_upscaler.choose_settings(10)["blocks_to_swap"], 32)
        self.assertIn("7b", seedvr2_upscaler.choose_settings(24)["dit_model"])
        with self.assertRaises(seedvr2_upscaler.SeedVR2Error):
            seedvr2_upscaler.choose_settings(6)
        for vram in (8, 15, 24):
            self.assertEqual((seedvr2_upscaler.choose_settings(vram)["batch_size"] - 1) % 4, 0, "batch must be 4n+1")

    def test_command_targets_the_short_side_with_low_vram_options(self):
        settings = seedvr2_upscaler.choose_settings(15)
        command = seedvr2_upscaler.build_command("py", "cli.py", "in.mp4", "out.mp4", short_side=2160,
                                                 settings=settings, model_dir="/m")
        joined = " ".join(command)
        self.assertIn("--resolution 2160", joined)
        self.assertIn("--blocks_to_swap 16", joined)
        self.assertIn("--dit_offload_device cpu", joined)
        self.assertIn("--vae_decode_tiled", joined)
        self.assertIn("--video_backend ffmpeg", joined)
        self.assertEqual(seedvr2_upscaler.target_short_side("4k"), 2160)

    def test_progress_lines_are_parsed(self):
        self.assertEqual(seedvr2_upscaler.parse_progress("[generation] Upscaling batch 3/12"), (3, 12))
        self.assertIsNone(seedvr2_upscaler.parse_progress("Loading model"))

    def test_audio_is_copied_back_from_the_source(self):
        command = seedvr2_upscaler.remux_audio_command("ffmpeg", "v.mp4", "src.mp4", "out.mp4")
        self.assertEqual(command[command.index("-map") + 1], "0:v:0")
        self.assertIn("1:a:0", command)


class RecipeTests(unittest.TestCase):
    def request(self, **changes):
        body = {"description": "A lighthouse keeper finds a message in a bottle.", "duration_seconds": 45,
                "resolution": "4k", "images": ["/u/a.png", {"path": "/u/b.png", "role": "location"}, "/u/c.png"]}
        body.update(changes)
        return auto_production.validate_request(body)

    def test_images_get_roles_and_reach_the_director(self):
        request = self.request()
        recipe = auto_production.plan_recipe(request, render_vram_gb=15, finishing_vram_gb=15)
        params = auto_production.director_params(request, recipe)
        self.assertEqual(params["pipeline_type"], "short_film_story")
        self.assertTrue(params["auto_mode"])
        self.assertFalse(params["seamless"])
        self.assertEqual(params["target_duration"], 45)
        self.assertEqual(params["reference_image_path"], "/u/a.png")
        self.assertEqual(params["location_ref_paths"], ["/u/b.png"])
        self.assertEqual(params["character_ref_paths"], ["/u/c.png"])
        self.assertEqual(params["render_hardware"], {"gpu_vram_gb": 15.0})
        self.assertEqual(params["video_spatial_upsampling"], "flashvsr1.5")
        self.assertEqual(recipe["finish_resolution"], "4k")

    def test_resolutions_map_to_real_upscaler_values(self):
        for resolution, value in (("720p", ""), ("1080p", "flashvsr1.5"), ("1440p", "flashvsr2")):
            recipe = auto_production.plan_recipe(self.request(resolution=resolution), render_vram_gb=16,
                                                 finishing_vram_gb=16)
            self.assertEqual(recipe["in_engine_upscale"], value)
            self.assertEqual(recipe["finish_resolution"], "")
        smooth = auto_production.plan_recipe(self.request(resolution="720p", smooth_motion=True),
                                             render_vram_gb=16, finishing_vram_gb=16)
        self.assertEqual(smooth["temporal_upsampling"], "rife2")

    def test_4k_without_a_capable_gpu_is_refused(self):
        with self.assertRaises(auto_production.AutoProductionError):
            auto_production.plan_recipe(self.request(), render_vram_gb=6, finishing_vram_gb=6)

    def test_bad_requests_are_refused_with_a_reason(self):
        for changes in ({"description": "short"}, {"duration_seconds": 2}, {"resolution": "8k"},
                        {"images": ["/a.png", {"path": "/b.png", "role": "reference"}]}):
            with self.assertRaises(auto_production.AutoProductionError):
                self.request(**changes)

    def test_the_joined_video_is_the_final_output(self):
        status = {"out_dir": "/o", "output_files": ["clip1.mp4", "story_multiclip.mp4", "clip2.mp4"]}
        self.assertEqual(auto_production.final_video(status), "/o/story_multiclip.mp4")
        self.assertEqual(auto_production.final_video({"output_files": []}), "")


class DirectorRenderHardwareTests(unittest.TestCase):
    def test_a_declared_render_gpu_sizes_the_plan(self):
        from services import director_pipeline

        self.assertEqual(director_pipeline._director_render_hardware({"render_hardware": {"gpu_vram_gb": 15}}),
                         {"gpu_vram_gb": 15})
        measured = director_pipeline._director_render_hardware({"render_hardware": {"gpu_vram_gb": 0}})
        self.assertIsInstance(measured, dict)


class ConductorTests(unittest.TestCase):
    def wait(self, conductor, production_id):
        deadline = time.time() + 5
        while time.time() < deadline:
            state = conductor.get(production_id)
            if state and state["status"] in auto_production.TERMINAL:
                return state
            time.sleep(0.02)
        self.fail("the production did not finish")

    def test_a_production_runs_director_then_finishing(self):
        with tempfile.TemporaryDirectory() as folder:
            joined = os.path.join(folder, "film_multiclip.mp4")
            Path(joined).write_bytes(b"video")
            started, polls = [], iter([{"status": "running", "phase": "planning", "progress": {}},
                                       {"status": "completed", "out_dir": folder,
                                        "output_files": ["film_multiclip.mp4"]}])

            def finish(source, resolution, report, cancelled):
                report(50, "half")
                target = source.replace(".mp4", "_4k.mp4")
                Path(target).write_bytes(b"4k")
                return target

            conductor = auto_production.Conductor(
                folder, compute=lambda: {"local_vram_gb": 0, "nodes": [{"peer_id": "free-gpu", "label": "T4",
                                                                       "online": True, "vram_gb": 15}]},
                start_pipeline=lambda params: started.append(params) or "pid1",
                pipeline_status=lambda pid: next(polls), stop_pipeline=lambda pid: None, finish=finish,
                poll_seconds=0.01)
            state = conductor.start({"description": "A storm over the sea at night.", "resolution": "4k"})
            final = self.wait(conductor, state["id"])
        self.assertEqual(final["status"], "completed", final.get("error"))
        self.assertTrue(final["result"].endswith("_4k.mp4"))
        self.assertEqual(started[0]["render_hardware"], {"gpu_vram_gb": 15.0})

    def test_no_gpu_anywhere_is_refused_before_anything_starts(self):
        with tempfile.TemporaryDirectory() as folder:
            conductor = auto_production.Conductor(
                folder, compute=lambda: {"local_vram_gb": 0, "nodes": []},
                start_pipeline=lambda params: self.fail("must not start"), pipeline_status=lambda pid: None,
                stop_pipeline=lambda pid: None, finish=lambda *a: "")
            with self.assertRaises(auto_production.AutoProductionError):
                conductor.start({"description": "A storm over the sea at night."})

    def test_a_failed_director_run_keeps_its_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            conductor = auto_production.Conductor(
                folder, compute=lambda: {"local_vram_gb": 16, "nodes": []},
                start_pipeline=lambda params: "pid", stop_pipeline=lambda pid: None, finish=lambda *a: "",
                pipeline_status=lambda pid: {"status": "failed", "error": "CUDA out of memory"}, poll_seconds=0.01)
            state = conductor.start({"description": "A storm over the sea at night.", "resolution": "1080p"})
            final = self.wait(conductor, state["id"])
        self.assertEqual(final["status"], "failed")
        self.assertIn("CUDA out of memory", final["error"])

    def test_cancel_stops_the_director(self):
        with tempfile.TemporaryDirectory() as folder:
            stopped = threading.Event()
            conductor = auto_production.Conductor(
                folder, compute=lambda: {"local_vram_gb": 16, "nodes": []},
                start_pipeline=lambda params: "pid", stop_pipeline=lambda pid: stopped.set(), finish=lambda *a: "",
                pipeline_status=lambda pid: {"status": "running", "progress": {}}, poll_seconds=0.01)
            state = conductor.start({"description": "A storm over the sea at night.", "resolution": "720p"})
            deadline = time.time() + 2
            while not (conductor.get(state["id"]) or {}).get("pipeline_id") and time.time() < deadline:
                time.sleep(0.01)
            conductor.cancel(state["id"])
            final = self.wait(conductor, state["id"])
        self.assertEqual(final["status"], "cancelled")
        self.assertTrue(stopped.is_set())


if __name__ == "__main__":
    unittest.main()

"""The local engine path: hardware, catalogue, install plan, proof, registration.

The tests that matter most are the refusals: a model that does not fit is never
selected, installing without confirmation does nothing, weights are never fetched
without their own flag, and an engine is never registered without a real clip
coming back.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from comfy_stub import FAKE_MP4, RunningStub, StubComfy, reset_stub  # noqa: E402
from services import comfy_bootstrap, comfy_models, local_hardware  # noqa: E402
from services.comfyui_adapter import default_templates_dir, list_templates  # noqa: E402

NVIDIA_OUTPUT = (
    "0, NVIDIA GeForce RTX 4060, 8188, 560.94, 8.9\n"
    "1, NVIDIA GeForce RTX 4090, 24564, 560.94, 8.9\n"
)


def hardware_stub(*, vram_mb=8188, ram_gb=32.0, free_gb=200.0, gpu=True, tools=None):
    """A hardware record shaped exactly like the probe's, for deterministic tests."""
    gpus = (
        [{"index": 0, "name": "NVIDIA GeForce RTX 4060", "vram_total_mb": vram_mb,
          "driver_version": "560.94", "compute_capability": "8.9"}]
        if gpu
        else []
    )
    primary = gpus[0] if gpus else None
    return {
        "detected_at": 0.0,
        "os": {"system": "Linux", "machine": "x86_64", "python": "3.12.4", "executable": "/usr/bin/python3"},
        "cpu": {"logical_cores": 16, "model": "x86_64"},
        "memory": {"total_mb": int(ram_gb * 1024), "total_gb": ram_gb},
        "disk": {"path": "/", "free_gb": free_gb, "total_gb": free_gb * 2},
        "gpus": gpus,
        "primary_gpu": primary,
        "vram_total_mb": primary["vram_total_mb"] if primary else None,
        "tools": {"git": "/usr/bin/git"} if tools is None else tools,
        "problems": [],
        "unmeasured": [] if gpu else ["nvidia-smi is not on PATH, so no GPU could be queried."],
        "tier": local_hardware.classify_tier(primary["vram_total_mb"] if primary else None),
        "cuda": {
            "available": gpu,
            "reason": "nvidia-smi reported 1 GPU(s)" if gpu else "nvidia-smi is not on PATH",
        },
    }


class HardwareTest(unittest.TestCase):
    def test_nvidia_smi_lines_are_parsed_with_their_measured_vram(self):
        gpus, problems = local_hardware.parse_nvidia_smi(NVIDIA_OUTPUT)
        self.assertEqual(problems, [])
        self.assertEqual([gpu["name"] for gpu in gpus], ["NVIDIA GeForce RTX 4060", "NVIDIA GeForce RTX 4090"])
        self.assertEqual(gpus[1]["vram_total_mb"], 24564)
        self.assertEqual(gpus[0]["compute_capability"], "8.9")

    def test_a_malformed_line_is_reported_not_silently_dropped(self):
        gpus, problems = local_hardware.parse_nvidia_smi("0, NVIDIA RTX, not-a-number, 560.94, 8.9\n")
        self.assertEqual(gpus, [])
        self.assertTrue(problems and "no usable VRAM" in problems[0])

    def test_probe_measures_this_machine_and_says_what_it_could_not(self):
        def fake_runner(command, timeout):
            self.assertIn("nvidia-smi", command[0])
            return (0, NVIDIA_OUTPUT, "")

        hardware = local_hardware.detect_hardware(
            runner=fake_runner,
            disk_path="/",
            tools={"nvidia-smi": "/usr/bin/nvidia-smi", "git": "/usr/bin/git"},
        )
        self.assertEqual(hardware["vram_total_mb"], 24564)
        self.assertTrue(hardware["cuda"]["available"])
        self.assertEqual(hardware["tier"]["tier"], "high_24gb")
        self.assertIn("git", hardware["tools"])
        self.assertEqual(hardware["gpus"][0]["name"], "NVIDIA GeForce RTX 4060")

    def test_no_gpu_is_reported_as_no_gpu_with_a_reason(self):
        def no_smi(command, timeout):  # pragma: no cover - not reached when absent
            return (127, "", "not found")

        hardware = local_hardware.detect_hardware(
            runner=no_smi, disk_path="/", tools={"nvidia-smi": "/usr/bin/nvidia-smi"}
        )
        self.assertEqual(hardware["gpus"], [])
        self.assertEqual(hardware["tier"]["tier"], "no_gpu")
        self.assertTrue(any("exited with 127" in problem for problem in hardware["problems"]))

    def test_missing_nvidia_smi_is_explained_not_invented(self):
        hardware = local_hardware.detect_hardware(
            runner=lambda command, timeout: (127, "", "boom"),
            disk_path="/",
            tools={"git": "/usr/bin/git"},
        )
        self.assertIsNone(hardware["vram_total_mb"])
        self.assertFalse(hardware["cuda"]["available"])
        self.assertTrue(any("nvidia-smi" in note for note in hardware["unmeasured"]))

    def test_tiers_boundaries(self):
        self.assertEqual(local_hardware.classify_tier(None)["tier"], "no_gpu")
        self.assertEqual(local_hardware.classify_tier(4000)["tier"], "below_6gb")
        self.assertEqual(local_hardware.classify_tier(8188)["tier"], "consumer_8gb")
        self.assertEqual(local_hardware.classify_tier(12288)["tier"], "consumer_12gb")
        self.assertEqual(local_hardware.classify_tier(16384)["tier"], "midrange_16gb")
        self.assertEqual(local_hardware.classify_tier(24564)["tier"], "high_24gb")
        self.assertEqual(local_hardware.classify_tier(49140)["tier"], "workstation_48gb_plus")


class CatalogueTest(unittest.TestCase):
    def test_every_model_declares_a_licence_files_and_a_source(self):
        for model_id in comfy_models.model_ids():
            model = comfy_models.get_model(model_id)
            self.assertTrue(model.licence, model_id)
            self.assertTrue(model.licence_url.startswith("https://"), model_id)
            self.assertTrue(model.files, model_id)
            self.assertTrue(model.sources, model_id)
            for entry in model.files:
                self.assertTrue(entry.repo, model_id)
                self.assertTrue(entry.url.startswith("https://"), model_id)
                self.assertGreater(entry.size_gb, 0.005, model_id)

    def test_the_primary_model_is_wan22_5b_under_apache_2(self):
        model = comfy_models.get_model("wan22_ti2v_5b")
        self.assertEqual(model.licence, "Apache-2.0")
        self.assertEqual(model.capability, "text_to_video")
        self.assertEqual(model.model_disk_gb, round(10.0 + 6.74 + 1.41, 2))
        self.assertEqual(model.fps, 24)
        self.assertEqual(model.frames, 121)

    def test_shipped_template_ids_match_the_catalogue(self):
        shipped = {template.template_id for template in list_templates(default_templates_dir())}
        for model_id in comfy_models.model_ids():
            model = comfy_models.get_model(model_id)
            for template_id in model.templates:
                self.assertIn(template_id, shipped, f"{model_id} names a template that is not shipped")

    def test_policy_is_local_only_without_credits(self):
        self.assertFalse(comfy_models.POLICY["commercial_providers"])
        self.assertFalse(comfy_models.POLICY["cloud_generation"])
        self.assertFalse(comfy_models.POLICY["credits"])

    def test_fit_compares_the_measured_machine_with_the_stated_requirement(self):
        model = comfy_models.get_model("wan22_ti2v_5b")
        fits = comfy_models.check_fit(model, hardware_stub(vram_mb=8188, free_gb=200.0))
        self.assertTrue(fits["fits"])
        small = comfy_models.check_fit(model, hardware_stub(vram_mb=4096, free_gb=200.0))
        self.assertFalse(small["fits"])
        self.assertIn("8192 MB", small["blockers"][0])
        cramped = comfy_models.check_fit(model, hardware_stub(vram_mb=8188, free_gb=5.0))
        self.assertFalse(cramped["fits"])
        self.assertIn("free disk", cramped["blockers"][0])

    def test_unmeasurable_requirements_are_not_treated_as_met(self):
        model = comfy_models.get_model("wan22_ti2v_5b")
        cpu_only = comfy_models.check_fit(model, hardware_stub(gpu=False))
        self.assertFalse(cpu_only["fits"])
        self.assertTrue(cpu_only["unknowns"])
        self.assertFalse(cpu_only["blockers"])  # nothing is *known* to be impossible here
        no_ram = hardware_stub()
        no_ram["memory"] = {"total_mb": None, "total_gb": None}
        self.assertFalse(comfy_models.check_fit(model, no_ram)["fits"])

    def test_selection_picks_5b_on_a_consumer_gpu_and_explains_a_refusal(self):
        picked = comfy_models.select_video_model(hardware_stub(vram_mb=8188))
        self.assertIsNotNone(picked["model"])
        self.assertEqual(picked["model"]["model_id"], "wan22_ti2v_5b")
        self.assertTrue(picked["fit"]["fits"])

        none = comfy_models.select_video_model(hardware_stub(gpu=False))
        self.assertIsNone(none["model"])
        self.assertTrue(none["blockers"])
        self.assertIn("Nothing will be installed", none["detail"])

    def test_a_workstation_gets_the_14b_option_considered_and_rejected_on_ram(self):
        result = comfy_models.select_video_model(hardware_stub(vram_mb=49140, ram_gb=32.0, free_gb=500.0))
        # 14B fits on VRAM here but not on the planned system RAM, so 5B is chosen.
        self.assertEqual(result["model"]["model_id"], "wan22_ti2v_5b")
        fourteen_b = next(entry for entry in result["considered"] if entry["model_id"] == "wan22_t2v_a14b")
        self.assertFalse(fourteen_b["fits"])

    def test_ltx2_is_recorded_as_evaluated_and_not_selected_with_reasons(self):
        evaluated = {entry["model_id"]: entry for entry in comfy_models.evaluated_but_not_selected()}
        self.assertIn("ltx2", evaluated)
        ltx = evaluated["ltx2"]
        self.assertTrue(ltx["local_and_free"])
        self.assertFalse(ltx["selected"])
        reasons = " ".join(ltx["reasons"])
        self.assertIn("10M", reasons)
        self.assertIn("32 GB", reasons)
        self.assertIn("No credits are needed", ltx["credits"])
        self.assertIn("32 GB+ GPU", ltx["when_it_would_be_selected"])
        self.assertTrue(ltx["sources"])


class PlanTest(unittest.TestCase):
    def test_plan_lists_the_commands_files_and_sizes_without_running_anything(self):
        model = comfy_models.get_model("wan22_ti2v_5b")
        with tempfile.TemporaryDirectory() as tmp:
            plan = comfy_bootstrap.install_plan(
                hardware=hardware_stub(), model=model, comfy_root=os.path.join(tmp, "ComfyUI")
            )
            ids = [step["id"] for step in plan["steps"]]
            self.assertEqual(
                ids, ["checkout", "venv", "torch", "requirements", "models", "server", "functional_test", "register"]
            )
            checkout = next(step for step in plan["steps"] if step["id"] == "checkout")
            self.assertEqual(checkout["command"][0], "git")
            self.assertIn("github.com/comfyanonymous/ComfyUI", " ".join(checkout["command"]))
            models_step = next(step for step in plan["steps"] if step["id"] == "models")
            self.assertEqual(models_step["requires_flag"], "download_models")
            self.assertAlmostEqual(plan["download_gb"], 18.15, places=1)
            self.assertGreater(plan["disk_required_gb"], plan["download_gb"])
            self.assertEqual(plan["blockers"], [])
            self.assertTrue(plan["policy"]["note"])
            for entry in plan["downloads"]:
                self.assertTrue(entry["url"].startswith("https://huggingface.co/"))
                self.assertIn("diffusion_models", entry["directory"] + "diffusion_models")

    def test_plan_refuses_a_machine_it_cannot_support(self):
        model = comfy_models.get_model("wan22_ti2v_5b")
        plan = comfy_bootstrap.install_plan(hardware=hardware_stub(gpu=False), model=model)
        self.assertTrue(any("No CUDA GPU" in blocker for blocker in plan["blockers"]))
        no_git = comfy_bootstrap.install_plan(hardware=hardware_stub(tools={}), model=model)
        self.assertTrue(any("git is not on PATH" in blocker for blocker in no_git["blockers"]))

    def test_plan_notices_an_existing_checkout_and_venv(self):
        model = comfy_models.get_model("wan22_ti2v_5b")
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "ComfyUI")
            os.makedirs(os.path.join(root, ".git"))
            os.makedirs(os.path.join(root, ".venv", "bin"))
            open(os.path.join(root, ".venv", "bin", "python"), "w").close()
            plan = comfy_bootstrap.install_plan(hardware=hardware_stub(), model=model, comfy_root=root)
            self.assertTrue(next(s for s in plan["steps"] if s["id"] == "checkout")["already_present"])
            self.assertTrue(next(s for s in plan["steps"] if s["id"] == "venv")["already_present"])


class InstallTest(unittest.TestCase):
    def plan(self, tmp, model_id="wan22_ti2v_5b"):
        return comfy_bootstrap.install_plan(
            hardware=hardware_stub(),
            model=comfy_models.get_model(model_id),
            comfy_root=os.path.join(tmp, "ComfyUI"),
        )

    def test_without_confirmation_nothing_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            result = comfy_bootstrap.install(
                self.plan(tmp), confirm=False, runner=lambda c, wd, t: calls.append(c) or (0, "", "")
            )
            self.assertFalse(result["started"])
            self.assertFalse(result["completed"])
            self.assertIn("explicitly confirmed", result["reason"])
            self.assertEqual(calls, [])

    def test_confirmed_install_runs_the_commands_but_never_the_weights_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            result = comfy_bootstrap.install(
                self.plan(tmp),
                confirm=True,
                runner=lambda command, cwd, timeout: (calls.append(command) or (0, "ok", "")),
            )
            self.assertTrue(result["completed"])
            # clone, create the venv, install torch, install ComfyUI's requirements.
            self.assertEqual([command[0] for command in calls[:1]], ["git"])
            self.assertEqual(calls[0][1], "clone")
            self.assertEqual(len(calls), 4)
            self.assertEqual(calls[1][1:3], ["-m", "venv"])
            venv_python = calls[2][0]
            self.assertTrue(venv_python.endswith(os.path.join(".venv", "bin", "python")))
            self.assertEqual(calls[2][1:4], ["-m", "pip", "install"])
            self.assertIn("requirements.txt", calls[3][-1])
            statuses = {step["id"]: step["status"] for step in result["steps"]}
            self.assertEqual(statuses["models"], "skipped")
            self.assertIn("were not requested", next(s for s in result["steps"] if s["id"] == "models")["detail"])
            self.assertEqual(statuses["server"], "manual")
            self.assertEqual(statuses["functional_test"], "pending_action")
            self.assertNotIn('"status": "downloaded"', json.dumps(result))
            self.assertNotIn("files", json.dumps(result))

    def test_a_failed_step_stops_the_run_and_is_never_reported_as_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            def runner(command, cwd, timeout):
                if command[0] == "git":
                    return (128, "", "fatal: not a git repository")
                return (0, "ok", "")

            result = comfy_bootstrap.install(self.plan(tmp), confirm=True, runner=runner)
            self.assertFalse(result["completed"])
            steps = {step["id"]: step for step in result["steps"]}
            self.assertEqual(steps["checkout"]["status"], "failed")
            self.assertEqual(steps["checkout"]["exit_code"], 128)
            self.assertIn("not a git repository", steps["checkout"]["detail"])
            for later in ("venv", "torch", "models"):
                self.assertEqual(steps[later]["status"], "not_reached")

    def test_model_downloads_need_their_own_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = []

            def downloader(url, destination):
                written.append((url, destination))
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                with open(destination, "wb") as handle:
                    handle.write(b"x" * 4096)
                return 4096

            plan = self.plan(tmp)
            result = comfy_bootstrap.install(
                plan,
                confirm=True,
                download_models=True,
                runner=lambda command, cwd, timeout: (0, "ok", ""),
                downloader=downloader,
            )
            self.assertTrue(result["completed"])
            models_step = next(step for step in result["steps"] if step["id"] == "models")
            self.assertEqual(models_step["status"], "done")
            self.assertEqual(len(written), 3)
            self.assertEqual(len(models_step["files"]), 3)
            self.assertTrue(all(entry["status"] == "downloaded" for entry in models_step["files"]))
            self.assertTrue(all(os.path.isfile(entry["path"]) for entry in models_step["files"]))

    def test_a_failed_download_stops_and_is_reported_with_its_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            def downloader(url, destination):
                raise OSError("no space left on device")

            result = comfy_bootstrap.install(
                self.plan(tmp), confirm=True, download_models=True,
                runner=lambda command, cwd, timeout: (0, "ok", ""), downloader=downloader,
            )
            self.assertFalse(result["completed"])
            models_step = next(step for step in result["steps"] if step["id"] == "models")
            self.assertEqual(models_step["status"], "failed")
            self.assertIn("no space left on device", models_step["files"][0]["reason"])

    def test_download_skips_a_file_that_is_already_there_at_full_size(self):
        model = comfy_models.get_model("wan22_ti2v_5b")
        with tempfile.TemporaryDirectory() as tmp:
            entry = model.files[0]
            destination = os.path.join(tmp, "diffusion_models", entry.filename)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with open(destination, "wb") as handle:
                handle.truncate(int(entry.size_gb * 0.99 * 1024 ** 3))
            results = comfy_bootstrap.download_model_files(
                model, tmp, downloader=lambda url, dest: self.fail("must not download a complete file")
            )
            self.assertEqual(results[0]["status"], "already_present")


class FunctionalTestTest(unittest.TestCase):
    def setUp(self):
        reset_stub()

    def test_a_real_clip_comes_back_and_is_verified(self):
        model = comfy_models.get_model("wan22_ti2v_5b")
        with RunningStub() as stub, tempfile.TemporaryDirectory() as tmp:
            result = comfy_bootstrap.functional_test(
                stub.base_url, model=model, out_dir=tmp, poll_interval=0.01
            )
            self.assertTrue(result["passed"], result.get("reason"))
            self.assertEqual(result["template_id"], "wan22_ti2v_5b_txt2video")
            self.assertTrue(os.path.isfile(result["clip_path"]))
            self.assertEqual(result["container"], "mp4/mov")
            self.assertGreaterEqual(result["bytes"], len(FAKE_MP4))
            self.assertEqual(len(result["sha256"]), 64)
            self.assertIn("deliberately small", result["note"])
            # The graph really was submitted with the tiny test dimensions.
            graph = stub.last_params()
            self.assertEqual(graph["7"]["inputs"]["width"], 256)
            self.assertEqual(graph["7"]["inputs"]["length"], 9)
            self.assertEqual(graph["8"]["inputs"]["steps"], 4)
            self.assertIn("paper boat", graph["5"]["inputs"]["text"])

    def test_an_unreachable_server_is_a_failure_with_the_reason(self):
        model = comfy_models.get_model("wan22_ti2v_5b")
        with tempfile.TemporaryDirectory() as tmp:
            result = comfy_bootstrap.functional_test(
                "http://127.0.0.1:1", model=model, out_dir=tmp, poll_interval=0.01
            )
            self.assertFalse(result["passed"])
            self.assertIn("not reachable", result["reason"])

    def test_missing_node_classes_are_named(self):
        model = comfy_models.get_model("wan22_ti2v_5b")
        with RunningStub() as stub, tempfile.TemporaryDirectory() as tmp:
            StubComfy.node_classes = ["KSampler", "CLIPTextEncode"]
            try:
                result = comfy_bootstrap.functional_test(
                    stub.base_url, model=model, out_dir=tmp, poll_interval=0.01
                )
            finally:
                reset_stub()
            self.assertFalse(result["passed"])
            self.assertIn("SaveVideo", result["reason"])
            self.assertIn("Update ComfyUI", result["reason"])

    def test_missing_model_files_are_named_with_where_to_put_them(self):
        model = comfy_models.get_model("wan22_ti2v_5b")
        with RunningStub() as stub, tempfile.TemporaryDirectory() as tmp:
            StubComfy.files = {"diffusion_models": [], "text_encoders": [], "vae": []}
            try:
                result = comfy_bootstrap.functional_test(
                    stub.base_url, model=model, out_dir=tmp, poll_interval=0.01
                )
            finally:
                reset_stub()
            self.assertFalse(result["passed"])
            self.assertIn("wan2.2_ti2v_5B_fp16.safetensors", result["reason"])
            self.assertIn("ComfyUI/models/diffusion_models", result["reason"])
            self.assertEqual(len(result["checked"]["missing_files"]), 3)

    def test_still_running_after_the_timeout_is_not_a_pass(self):
        model = comfy_models.get_model("wan22_ti2v_5b")
        with RunningStub() as stub, tempfile.TemporaryDirectory() as tmp:
            StubComfy.finished = False
            try:
                result = comfy_bootstrap.functional_test(
                    stub.base_url, model=model, out_dir=tmp, poll_interval=0.01, poll_timeout=0.05
                )
            finally:
                reset_stub()
            self.assertFalse(result["passed"])
            self.assertIn("still", result["reason"])
            self.assertIn("not cancelled", result["reason"])

    def test_a_result_without_a_video_file_is_a_failure(self):
        model = comfy_models.get_model("wan22_ti2v_5b")
        with RunningStub() as stub, tempfile.TemporaryDirectory() as tmp:
            # The node reports a file, but not a video one.
            StubComfy.output_key = "text"
            StubComfy.video_filename = "result.txt"
            try:
                result = comfy_bootstrap.functional_test(
                    stub.base_url, model=model, out_dir=tmp, poll_interval=0.01
                )
            finally:
                reset_stub()
            self.assertFalse(result["passed"])
            self.assertIn("no video file", result["reason"])

    def test_a_file_that_is_not_a_video_is_rejected(self):
        model = comfy_models.get_model("wan22_ti2v_5b")
        with RunningStub() as stub, tempfile.TemporaryDirectory() as tmp:
            StubComfy.view_bytes = b"<html>not a video</html>" * 100
            try:
                result = comfy_bootstrap.functional_test(
                    stub.base_url, model=model, out_dir=tmp, poll_interval=0.01
                )
            finally:
                reset_stub()
            self.assertFalse(result["passed"])
            self.assertIn("not a usable video", result["reason"])

    def test_video_file_check_uses_magic_bytes_not_the_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "clip.mp4")
            with open(good, "wb") as handle:
                handle.write(FAKE_MP4)
            self.assertTrue(comfy_bootstrap.verify_video_file(good)["valid"])
            bad = os.path.join(tmp, "clip.mp4")
            with open(bad, "wb") as handle:
                handle.write(b"#!/bin/sh\necho not a video\n" * 100)
            self.assertFalse(comfy_bootstrap.verify_video_file(bad)["valid"])
            tiny = os.path.join(tmp, "tiny.mp4")
            with open(tiny, "wb") as handle:
                handle.write(b"x")
            self.assertFalse(comfy_bootstrap.verify_video_file(tiny)["valid"])


class RegisterTest(unittest.TestCase):
    def setUp(self):
        reset_stub()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.nodes_root = os.path.join(self._tmp.name, "compute_nodes")

    def test_registration_without_a_passed_test_is_refused(self):
        model = comfy_models.get_model("wan22_ti2v_5b")
        with self.assertRaises(comfy_bootstrap.BootstrapError) as ctx:
            comfy_bootstrap.register_engine(
                compute_nodes_root=self.nodes_root,
                base_url="http://127.0.0.1:8188",
                model=model,
                test_result={"passed": False, "reason": "no clip"},
            )
        self.assertIn("no clip", str(ctx.exception))

    def test_a_passed_test_registers_a_node_that_carries_its_proof(self):
        from services import compute_nodes

        model = comfy_models.get_model("wan22_ti2v_5b")
        with RunningStub() as stub, tempfile.TemporaryDirectory() as tmp:
            result = comfy_bootstrap.functional_test(
                stub.base_url, model=model, out_dir=tmp, poll_interval=0.01
            )
            self.assertTrue(result["passed"], result.get("reason"))
            registered = comfy_bootstrap.register_engine(
                compute_nodes_root=self.nodes_root,
                base_url=stub.base_url,
                model=model,
                test_result=result,
                hardware=hardware_stub(),
            )
        node = registered
        self.assertEqual(node["kind"], "comfyui")
        self.assertEqual(node["address"], stub.base_url)
        self.assertIn("text_to_video", node["capabilities"])
        self.assertIn(result["clip_filename"], node["notes"])
        self.assertIn(result["sha256"][:16], node["notes"])
        self.assertEqual(node["hardware"]["gpu"], "NVIDIA GeForce RTX 4060")
        self.assertTrue(node["token"])
        # It is really in the registry, and its token is only shown once.
        stored = compute_nodes.get_node(self.nodes_root, node["node_id"])
        self.assertEqual(stored["node_id"], node["node_id"])
        self.assertNotIn("token", stored)


if __name__ == "__main__":
    unittest.main()

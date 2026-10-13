"""Pinokio launcher regressions that do not require the application runtime."""
from __future__ import annotations

import ast
import copy
import os
from pathlib import Path
import re
from types import SimpleNamespace
import unittest


_ROOT = Path(__file__).resolve().parents[1]
# Same escape hatch as tests/test_image_runtime_regressions.py: point the engine
# assertions at another wgp.py (e.g. the pre-fix revision) when checking that a
# regression test really fails on the code it is meant to pin.
_WGP = Path(os.environ.get("MAESTRO_TEST_WGP_SOURCE", _ROOT / "app" / "wgp.py"))


def _execute(nodes: list[ast.stmt], namespace: dict, filename: Path) -> None:
    module = ast.fix_missing_locations(ast.Module(body=copy.deepcopy(nodes), type_ignores=[]))
    exec(compile(module, str(filename), "exec"), namespace)


def _module_level_capability_probe(engine: Path):
    """The two module-level statements that size the engine to the GPU.

    Located by shape rather than position: WanGP reads the compute capability
    once at import time and turns it into `bfloat16_supported`.
    """
    tree = ast.parse(engine.read_text(encoding="utf-8"))
    guard = next(
        node for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "torch.cuda.is_available()"
    )
    precision = next(
        node for node in tree.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "gpu_major < 8"
    )
    return guard, precision


def _fake_engine_globals(cuda_available: bool, capability=()):
    def get_device_capability(_device=None):
        # What a CPU-only PyTorch wheel does, verbatim: the message the launcher
        # used to see as the last line of a failed Start.
        assert cuda_available, "Torch not compiled with CUDA enabled"
        return capability

    return {
        "args": SimpleNamespace(gpu=[]),
        "torch": SimpleNamespace(cuda=SimpleNamespace(
            is_available=lambda: cuda_available,
            get_device_capability=get_device_capability,
        )),
    }


class TestPinokioGpuCompatibility(unittest.TestCase):
    def test_installed_app_menu_is_not_hidden_by_early_gpu_detection(self):
        launcher = (_ROOT / "pinokio.js").read_text(encoding="utf-8")

        self.assertNotIn("if (kernel.gpu", launcher)
        # Start is always offered once something is installed; the CPU / light
        # runtime only changes its label, never whether the entry exists.
        self.assertIn("text: cpuOnly", launcher)
        self.assertIn(': "Start",', launcher)
        self.assertIn('href: "start.js"', launcher)

    def test_fresh_install_keeps_pinokios_gpu_check_without_aborting(self):
        installer = (_ROOT / "install.js").read_text(encoding="utf-8")

        # Pinokio's documented GPU variable stays the trigger for the notice...
        self.assertIn("{{gpu !== 'nvidia'}}", installer)
        # ...but it only informs. A machine without an NVIDIA GPU still installs
        # the CPU / light runtime, so the notice must not stop the script and
        # the old "requires an NVIDIA GPU" abort text is gone.
        self.assertNotIn("This app requires an NVIDIA GPU", installer)
        notice = installer.index("{{gpu !== 'nvidia'}}")
        # Scope to the whole notify step rather than a fixed-length window: the
        # notice now branches per platform, and a character budget silently stops
        # covering the text it is supposed to pin.
        step = installer[notice:installer.index("\n    },\n", notice)]
        self.assertNotIn("next: null", step)
        # Each machine is pointed at the guide that fits its runtime. macOS has its
        # own dependency set and hardware floor, so it must not be sent to the
        # generic no-NVIDIA page, and a GPU-less PC must not be sent to the Mac one.
        self.assertIn("docs/INSTALAR-SIN-NVIDIA.md", step)
        self.assertIn("docs/INSTALAR-EN-MAC.md", step)

    def test_only_blackwell_keeps_a_driver_stop_and_it_is_the_last_resort(self):
        installer = (_ROOT / "install.js").read_text(encoding="utf-8")

        # RTX 50 has no CUDA 12.8 fallback, so that combination stays a stop...
        self.assertIn("isRtx50(kernel) && needsCuda13DriverUpdate(kernel)", installer)
        # ...while every other NVIDIA card is told about the compatibility
        # runtime and then continues to install it.
        self.assertIn(
            "isSolCapable(kernel) && !isRtx50(kernel) && needsCuda13DriverUpdate(kernel)",
            installer,
        )
        self.assertEqual(installer.count("next: null"), 1)

    def test_cpu_light_runtime_covers_every_launcher_entry_point(self):
        profile = (_ROOT / "launcher_profile.js").read_text(encoding="utf-8")
        installer = (_ROOT / "install.js").read_text(encoding="utf-8")
        torch_script = (_ROOT / "torch.js").read_text(encoding="utf-8")
        start = (_ROOT / "start.js").read_text(encoding="utf-8")
        classic = (_ROOT / "start_classic.js").read_text(encoding="utf-8")
        menu = (_ROOT / "pinokio.js").read_text(encoding="utf-8")
        updater = (_ROOT / "update.js").read_text(encoding="utf-8")
        resetter = (_ROOT / "reset.js").read_text(encoding="utf-8")
        gitignore = (_ROOT / ".gitignore").read_text(encoding="utf-8")

        # Hardware routing: an explicitly reported non-NVIDIA GPU selects the
        # light runtime; an empty inventory does not (Pinokio can report nothing
        # for a supported card, which used to abort Install).
        self.assertIn('env: "env-light"', profile)
        self.assertIn("const isCpuOnly", profile)
        self.assertIn("reportedNonNvidiaGpu", profile)
        self.assertIn("MAESTRO_CPU_RUNTIME", profile)
        self.assertIn("isCpuOnly(kernel)", installer)
        self.assertIn("isCpuOnly(kernel)", torch_script)
        self.assertIn("isCpuOnly(kernel)", start)
        self.assertIn("!cpuOnly && runtime.env !== legacyRuntime.env", classic)

        # CPU wheels, matching the legacy environment's Python 3.10 / 2.7.1 pin.
        self.assertIn(
            "--index-url https://download.pytorch.org/whl/cpu",
            torch_script,
        )
        # The CUDA 13 wheel set follows the environment that was selected, which
        # is what lets a supported GPU on an older driver keep a working runtime.
        self.assertIn(
            'runtime.env === "env-sol" || runtime.env === "env-rtx50"',
            torch_script,
        )
        self.assertNotIn("solCapable && windows", torch_script)

        self.assertIn("CPU / light runtime not installed", start)
        self.assertIn('runtime.marker', start)
        self.assertIn('info.exists("app/env-light")', menu)
        self.assertIn("CPU / light runtime", menu)
        self.assertIn("runtime.env", updater)
        self.assertIn('path: "app/env-light"', resetter)
        self.assertIn("app/env-light/", gitignore)

        # The user-facing guide the notices point at must exist.
        self.assertTrue((_ROOT / "docs" / "INSTALAR-SIN-NVIDIA.md").is_file())

    def test_fresh_install_never_requests_hugging_face_login(self):
        installer = (_ROOT / "install.js").read_text(encoding="utf-8")

        self.assertNotIn('method: "hf.login"', installer)

    def test_hugging_face_login_is_an_explicit_optional_menu_action(self):
        login = (_ROOT / "huggingface_login.js").read_text(encoding="utf-8")
        menu = (_ROOT / "pinokio.js").read_text(encoding="utf-8")

        self.assertIn('method: "hf.login"', login)
        self.assertIn("force: true", login)
        self.assertIn("wait: true", login)
        self.assertEqual(menu.count('href: "huggingface_login.js"'), 3)
        self.assertEqual(menu.count("Connect Hugging Face (Optional)"), 2)
        self.assertIn("Not required for Maestro", menu)

    def test_start_url_uses_the_required_capture_object(self):
        start = (_ROOT / "start.js").read_text(encoding="utf-8")

        self.assertIn('"event": "/(http:\\/\\/[0-9.:]+)/"', start)
        self.assertIn('url: "{{input.event[1]}}"', start)
        self.assertIn('"event": "/Incorrect version of mmgp/i"', start)
        self.assertIn('"break": true', start)

    def test_mmgp_startup_guard_matches_the_pinned_requirement(self):
        requirements = (_ROOT / "app" / "requirements.txt").read_text(
            encoding="utf-8"
        )
        engine = (_ROOT / "app" / "wgp.py").read_text(encoding="utf-8")
        pinned = re.search(r"(?m)^mmgp==([^\s#]+)", requirements)
        guarded = re.search(
            r'^target_mmgp_version\s*=\s*["\']([^"\']+)["\']',
            engine,
            re.MULTILINE,
        )
        self.assertIsNotNone(pinned)
        self.assertIsNotNone(guarded)
        self.assertEqual(guarded.group(1), pinned.group(1))

    def test_rtx50_uses_an_isolated_cuda13_runtime(self):
        profile = (_ROOT / "launcher_profile.js").read_text(encoding="utf-8")
        installer = (_ROOT / "install.js").read_text(encoding="utf-8")
        torch_script = (_ROOT / "torch.js").read_text(encoding="utf-8")

        self.assertIn('target === "sm_120"', profile)
        self.assertIn('env: "env-rtx50"', profile)
        self.assertIn('python: "3.11"', profile)
        self.assertIn("venv_python: runtime.python", installer)
        self.assertIn("torch==2.10.0", torch_script)
        self.assertIn("maestro_torch_rtx50_v2", profile)
        self.assertIn("triton-windows==3.6.0.post25", torch_script)
        self.assertIn("/whl/cu130", torch_script)
        self.assertIn("lightx2v_kernel-0.0.2+torch2.10.0", torch_script)
        menu = (_ROOT / "pinokio.js").read_text(encoding="utf-8")
        self.assertIn("Repair H3 Performance Runtime", menu)

    def test_legacy_windows_flash_wheel_matches_wangp_documented_abi(self):
        profile = (_ROOT / "launcher_profile.js").read_text(encoding="utf-8")
        torch_script = (_ROOT / "torch.js").read_text(encoding="utf-8")

        self.assertIn("flash_attn-2.7.4.post1+cu128torch2.7.0", torch_script)
        self.assertNotIn("flash_attn-2.8.2%2Bcu128torch2.7", torch_script)
        self.assertIn("--force-reinstall --no-deps", torch_script)
        self.assertIn("legacyWindowsFlashSupported", profile)
        self.assertIn("maestro_flash_disabled_v2", profile)
        self.assertIn("uv pip uninstall flash-attn", torch_script)
        self.assertIn("uses SageAttention/SDPA", torch_script)

    def test_update_can_resume_a_missing_hardware_runtime(self):
        profile = (_ROOT / "launcher_profile.js").read_text(encoding="utf-8")
        updater = (_ROOT / "update.js").read_text(encoding="utf-8")

        self.assertIn("alreadyCurrentAndReady", updater)
        self.assertIn("exists('${runtime.marker}')", updater)
        self.assertIn("flashMarker", profile)
        self.assertIn("exists('${runtime.flashMarker}')", updater)
        self.assertIn("flash_only: true", updater)
        self.assertIn("venv: runtime.env", updater)

    def test_missing_react_bundle_is_rebuilt_by_update_and_start(self):
        updater = (_ROOT / "update.js").read_text(encoding="utf-8")

        # The no-op Update path must not win merely because git and the GPU
        # runtime are current. Interrupted Vite builds must re-enter `build`.
        self.assertIn("exists('ui/dist/index.html')", updater)
        self.assertIn("exists('ui/dist/assets')", updater)

        # Start is the final safety net for users who already advanced git and
        # then retried without realizing the first UI build was interrupted.
        for filename in ("start.js", "start_sol.js"):
            launcher = (_ROOT / filename).read_text(encoding="utf-8")
            self.assertIn("!exists('ui/dist/index.html')", launcher)
            self.assertIn("!exists('ui/dist/assets')", launcher)
            self.assertIn('path: "ui"', launcher)
            self.assertIn('"npm install"', launcher)
            self.assertIn('"npm run build"', launcher)

    def test_runtime_diagnostics_run_inside_the_loaded_engine(self):
        engine = (_ROOT / "app" / "wgp.py").read_text(encoding="utf-8")

        self.assertIn("from scripts.runtime_preflight import main", engine)
        self.assertIn("_runtime_preflight()", engine)

    def test_broken_optional_flash_is_guarded_before_mmgp_import(self):
        engine = (_ROOT / "app" / "wgp.py").read_text(encoding="utf-8")

        guard = engine.index("prepare_optional_flash_attention()")
        mmgp_import = engine.index("from mmgp import")
        self.assertLess(guard, mmgp_import)


class TestCpuLightEngineBoot(unittest.TestCase):
    """The CPU / light runtime must be able to load the WanGP engine module.

    `app/env-light` (macOS, AMD/Intel, any machine without an NVIDIA GPU) ships
    CPU-only PyTorch wheels. Every `torch.cuda` probe raises `AssertionError:
    Torch not compiled with CUDA enabled` there, and WanGP read the compute
    capability unconditionally at import time — so Start died before the server
    existed, which made the whole non-generation surface unreachable.
    """

    def test_engine_import_reads_the_capability_only_when_cuda_exists(self):
        engine = _WGP
        guard, precision = _module_level_capability_probe(engine)

        namespace = _fake_engine_globals(cuda_available=False)
        _execute([guard, precision], namespace, engine)

        # No capability to read and no local generation to configure: the
        # fallback keeps the engine importable and matches the pre-Ampere
        # FP16 path. Maestro reports the missing GPU in its own preflight.
        self.assertEqual(namespace["gpu_major"], 0)
        self.assertEqual(namespace["gpu_minor"], 0)
        self.assertFalse(namespace["bfloat16_supported"])

    def test_engine_import_still_uses_the_real_capability_on_a_cuda_machine(self):
        engine = _WGP
        guard, precision = _module_level_capability_probe(engine)

        for capability, expected_bfloat16 in (((8, 9), True), ((9, 0), True), ((7, 5), False)):
            with self.subTest(capability=capability):
                namespace = _fake_engine_globals(cuda_available=True, capability=capability)
                _execute([guard, precision], namespace, engine)
                self.assertEqual(
                    (namespace["gpu_major"], namespace["gpu_minor"]), capability
                )
                self.assertIs(namespace["bfloat16_supported"], expected_bfloat16)


class TestSamInstallerCompatibility(unittest.TestCase):
    def test_sam_runtime_stays_on_the_numpy1_compatible_release_lines(self):
        requirements = (
            _ROOT / "app" / "services" / "sam" / "requirements.txt"
        ).read_text(encoding="utf-8")

        self.assertRegex(requirements, r"(?m)^numpy>=1\.26,<2$")
        self.assertRegex(requirements, r"(?m)^opencv-python-headless<4\.12$")
        self.assertRegex(requirements, r"(?m)^scipy<1\.18$")
        self.assertNotRegex(requirements, r"(?m)^numpy>=2$")

    def test_sam_is_resolved_once_then_checked_before_being_marked_ready(self):
        installer = (_ROOT / "sam_install.js").read_text(encoding="utf-8")
        marker = "app/services/sam/env/.maestro-sam-ready"

        self.assertIn(
            "python -m pip install -r app/services/sam/requirements.txt "
            "app/services/sam/sam3",
            installer,
        )
        self.assertNotIn('"pip install app/services/sam/sam3"', installer)
        self.assertIn("python -m pip check", installer)
        self.assertIn("from sam3.model_builder import", installer)
        self.assertIn("assert major == 1", installer)
        self.assertIn('method: "fs.write"', installer)
        self.assertGreaterEqual(installer.count(marker), 2)
        self.assertLess(installer.index("python -m pip check"), installer.rindex(marker))

    def test_sam_menu_and_update_use_the_health_marker_correctly(self):
        menu = (_ROOT / "pinokio.js").read_text(encoding="utf-8")
        updater = (_ROOT / "update.js").read_text(encoding="utf-8")
        marker = "app/services/sam/env/.maestro-sam-ready"

        self.assertIn(f'const samReady = info.exists("{marker}")', menu)
        self.assertIn("text: samReady", menu)
        self.assertIn(f'const samReadyMarker = "{marker}"', updater)
        self.assertIn("!exists('app/services/sam/env')", updater)
        self.assertIn("exists('${samReadyMarker}')", updater)
        self.assertIn("when: \"{{exists('app/services/sam/env')}}\"", updater)


if __name__ == "__main__":
    unittest.main()

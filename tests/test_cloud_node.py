"""Free NVIDIA notebook node: the shared, testable parts of scripts/maestro_cloud_node.py."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from services import cloud_node  # noqa: E402

SMI = """+-----------------------------------------------------------------------------------------+
| NVIDIA-SMI 570.124.06             Driver Version: 570.124.06     CUDA Version: 12.8     |
"""


class CloudNodeTests(unittest.TestCase):
    def test_gpu_and_driver_parsing(self):
        gpus = cloud_node.parse_gpus("Tesla T4, 15360, 7.5\nTesla T4, 15360, 7.5\n\nbroken line\n")
        self.assertEqual(len(gpus), 2)
        self.assertEqual(gpus[0], {"name": "Tesla T4", "memory_mb": 15360, "compute_capability": 7.5})
        self.assertEqual(cloud_node.driver_cuda(SMI), (12, 8))
        self.assertIsNone(cloud_node.driver_cuda("no gpu"))

    def test_torch_matches_driver_and_gpu(self):
        self.assertIn("cu130", cloud_node.choose_torch((13, 0), 7.5)["index"])
        self.assertIn("cu128", cloud_node.choose_torch((12, 8), 7.5)["index"])
        self.assertIn("cu126", cloud_node.choose_torch((12, 8), 6.0)["index"])  # P100: no CUDA 12.8 build
        self.assertIn("cu126", cloud_node.choose_torch((13, 1), 6.0)["index"])
        with self.assertRaises(cloud_node.CloudNodeError):
            cloud_node.choose_torch((12, 2), 7.5)
        with self.assertRaises(cloud_node.CloudNodeError):
            cloud_node.choose_torch(None, 7.5)

    def test_storage_and_tunnel(self):
        self.assertEqual(cloud_node.pick_storage(["/a", "/b"], {"/a": 10, "/b": 70}), "/b")
        with self.assertRaises(cloud_node.CloudNodeError):
            cloud_node.pick_storage(["/a"], {"/a": 0})
        line = "2026-10-14T10:00:00Z INF |  https://quiet-river-1234.trycloudflare.com  |"
        self.assertEqual(cloud_node.tunnel_url(line), "https://quiet-river-1234.trycloudflare.com")
        self.assertEqual(cloud_node.tunnel_url("https://evil.example.com"), "")

    def test_code_round_trip_and_rejections(self):
        code = cloud_node.make_code("https://a-b.trycloudflare.com/", "t" * 43, label="Kaggle Tesla T4",
                                    gpu="Tesla T4 15360 MiB")
        self.assertTrue(code.startswith("maestro-node:"))
        node = cloud_node.parse_code(code)
        self.assertEqual(node["url"], "https://a-b.trycloudflare.com")
        self.assertEqual(node["token"], "t" * 43)
        for bad in ("", "hello", "maestro-node:@@@", cloud_node.make_code("http://x", "t" * 43, label="x"),
                    cloud_node.make_code("https://x", "short", label="x")):
            with self.subTest(bad=bad[:20]), self.assertRaises(cloud_node.CloudNodeError):
                cloud_node.parse_code(bad)

    def test_colab_free_is_not_offered(self):
        colab = next(p for p in cloud_node.PLATFORMS if p["id"] == "colab")
        self.assertFalse(colab["recommended"])
        self.assertTrue(all(p["source"].startswith("https://") for p in cloud_node.PLATFORMS))

    def test_notebook_runs_the_script(self):
        notebook = json.loads((ROOT / "cloud" / "maestro_free_gpu_node.ipynb").read_text(encoding="utf-8"))
        source = "".join("".join(cell["source"]) for cell in notebook["cells"])
        self.assertIn("scripts/maestro_cloud_node.py --check", source)
        self.assertIn("scripts/maestro_cloud_node.py", source)
        self.assertNotIn("MAESTRO_ACCESS_TOKEN=", source)  # never a fixed token in a shared file

    def test_script_refuses_without_gpu(self):
        done = subprocess.run([sys.executable, str(ROOT / "scripts" / "maestro_cloud_node.py"), "--check"],
                              capture_output=True, text=True, timeout=60)
        if subprocess.run(["which", "nvidia-smi"], capture_output=True).returncode == 0:
            self.skipTest("this machine has an NVIDIA GPU")
        self.assertEqual(done.returncode, 2)
        self.assertIn("Cannot run a node here", done.stdout)


if __name__ == "__main__":
    unittest.main()

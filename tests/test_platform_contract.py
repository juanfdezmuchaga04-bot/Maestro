"""Platform contract: what a Mac installs, and what an iPhone needs to install Maestro.

The launcher's own hardware routing is executed by ``tests/launcher_profiles.cjs``
(real scripts, simulated inventories). These checks cover the parts that only exist
outside those scripts:

* the dependency set a Mac resolves, because a single un-installable requirement
  aborts ``uv pip install -r app/requirements.txt`` and takes the whole install with it,
* the Home Screen icon iOS actually uses, and the head metadata that makes an added
  web app open without browser chrome,
* the documents that promise both, so a renamed file cannot leave a dead link behind.

PyTorch stopped publishing macOS Intel wheels after 2.2.2, which is why the pinned
``torch==2.7.1`` is only satisfiable on Apple Silicon; that is documented, not tested
here, because it is a PyPI fact rather than something this repository controls.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "app" / "requirements.txt"
UI = ROOT / "ui"
PUBLIC = UI / "public"
INDEX = UI / "index.html"

sys.path.insert(0, str(ROOT / "app"))

from services import access_gate as gate_module  # noqa: E402

# Requirements whose marker decides whether a Mac install can complete at all.
_PLATFORM_SENSITIVE = {
    "decord",
    "eva-decord",
    "onnxruntime",
    "onnxruntime-gpu",
    "rembg",
}

_APPLE_TOUCH_ICON = "/maestro-apple-touch-icon.png"
# iOS asks for 180x180 on current iPhones; the artwork is square, so the file must
# be exactly that size rather than a scaled-down logo.
_APPLE_TOUCH_SIZE = 180


def _requirement_lines() -> dict[str, list[tuple[str, str]]]:
    """Map a requirement name to its (raw line, marker) pairs."""
    found: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", stripped)
        if not match:
            continue
        name = match.group(1).lower()
        if name not in _PLATFORM_SENSITIVE:
            continue
        _, _, marker = stripped.partition(";")
        found[name].append((stripped, marker.strip()))
    return found


class TestMacDependencySet(unittest.TestCase):
    def setUp(self) -> None:
        self.lines = _requirement_lines()

    def assert_excluded_on_mac(self, name: str) -> None:
        pairs = self.lines.get(name)
        self.assertTrue(pairs, f"{name} is no longer declared in app/requirements.txt")
        for raw, marker in pairs:
            with self.subTest(line=raw):
                self.assertIn(
                    'sys_platform != "darwin"',
                    marker,
                    f"{name} publishes no macOS build; without this marker the whole "
                    "macOS install fails",
                )

    def assert_installed_on_mac(self, name: str) -> None:
        pairs = self.lines.get(name)
        self.assertTrue(pairs, f"{name} is no longer declared in app/requirements.txt")
        with self.subTest(name=name):
            self.assertTrue(
                any('sys_platform == "darwin"' in marker for _, marker in pairs),
                f"{name} must have a macOS-specific entry",
            )

    def test_packages_without_a_macos_build_are_excluded(self) -> None:
        # onnxruntime-gpu: no macOS wheel for any Python version.
        # decord 0.6.0: no macOS arm64 wheel and no cp310 macOS wheel.
        for name in ("onnxruntime-gpu", "decord"):
            with self.subTest(name=name):
                self.assert_excluded_on_mac(name)

    def test_rembg_gpu_extra_is_not_used_on_macos(self) -> None:
        # The extra itself is fine; it is `onnxruntime-gpu` inside it that is not.
        gpu_lines = [raw for raw, _ in self.lines.get("rembg", []) if raw.startswith("rembg[gpu]")]
        self.assertTrue(gpu_lines, "the rembg [gpu] extra is no longer declared")
        for raw in gpu_lines:
            with self.subTest(line=raw):
                self.assertIn('sys_platform != "darwin"', raw)

    def test_macos_replacements_are_declared(self) -> None:
        for name in ("onnxruntime", "eva-decord", "rembg"):
            with self.subTest(name=name):
                self.assert_installed_on_mac(name)

    def test_eva_decord_provides_the_module_the_app_imports(self) -> None:
        # `decord` 0.6.0 is replaced by EVA's fork on macOS, so it has to install the
        # same top-level module the pipeline imports (`import decord` in wgp.py).
        pairs = self.lines["eva-decord"]
        self.assertTrue(any("0.6" in raw for raw, _ in pairs), "eva-decord pin moved")


class TestAppleTouchIcon(unittest.TestCase):
    def test_icon_is_the_size_and_weight_ios_expects(self) -> None:
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover - depends on the local environment
            self.skipTest("Pillow is not installed")

        icon = PUBLIC / _APPLE_TOUCH_ICON.lstrip("/")
        self.assertTrue(icon.is_file(), f"{icon} is missing; run scripts/generate_pwa_icons.py")
        with Image.open(icon) as image:
            self.assertEqual(image.size, (_APPLE_TOUCH_SIZE, _APPLE_TOUCH_SIZE))
            self.assertEqual(image.format, "PNG")
        # This is fetched by the phone on every install; the logo file it replaced was
        # ~1.9 MB for the same Home Screen slot.
        self.assertLess(icon.stat().st_size, 150_000)

    def test_index_html_points_ios_at_it(self) -> None:
        html = INDEX.read_text(encoding="utf-8")
        self.assertIn(_APPLE_TOUCH_ICON, html)
        self.assertRegex(
            html,
            r'<link[^>]+rel="apple-touch-icon"[^>]+sizes="180x180"[^>]+href="'
            + re.escape(_APPLE_TOUCH_ICON)
            + '"',
        )
        self.assertNotIn(
            'sizes="1254x1254"',
            html,
            "the multi-megabyte logo must not be the iOS Home Screen icon again",
        )


class TestIosHomeScreenContract(unittest.TestCase):
    def setUp(self) -> None:
        self.html = INDEX.read_text(encoding="utf-8")
        self.manifest = json.loads((PUBLIC / "manifest.webmanifest").read_text(encoding="utf-8"))

    def test_head_metadata_opens_the_app_without_browser_chrome(self) -> None:
        self.assertIn('<link rel="manifest" href="/manifest.webmanifest" />', self.html)
        self.assertIn('name="apple-mobile-web-app-capable" content="yes"', self.html)
        self.assertIn('name="apple-mobile-web-app-title" content="Maestro"', self.html)
        self.assertIn('name="apple-mobile-web-app-status-bar-style" content="black"', self.html)
        self.assertIn('name="mobile-web-app-capable" content="yes"', self.html)

    def test_manifest_is_installable_and_its_icons_exist(self) -> None:
        self.assertEqual(self.manifest["display"], "standalone")
        self.assertEqual(self.manifest["start_url"], "/")
        sizes = {icon["sizes"] for icon in self.manifest["icons"]}
        self.assertTrue({"192x192", "512x512"} <= sizes, "Android needs both install sizes")
        self.assertIn("maskable", {icon["purpose"] for icon in self.manifest["icons"]})
        for icon in self.manifest["icons"]:
            with self.subTest(src=icon["src"]):
                self.assertTrue((PUBLIC / icon["src"].lstrip("/")).is_file())

    def test_every_referenced_icon_survives_the_sign_in_gate(self) -> None:
        # Safari fetches the manifest and the Home Screen icon before any session
        # exists; a 401 there makes iOS fall back to a screenshot.
        referenced = set(re.findall(r'<link[^>]+href="(/[^"]+)"', self.html))
        referenced.update(icon["src"] for icon in self.manifest["icons"])
        for path in sorted(referenced):
            with self.subTest(path=path):
                self.assertIn(path, gate_module.PUBLIC_PATHS)


class TestPlatformDocs(unittest.TestCase):
    MAC_DOC = ROOT / "docs" / "INSTALAR-EN-MAC.md"
    IOS_DOC = ROOT / "docs" / "DEPLOY-IOS.md"
    # Everything a user is sent to when installing on a new platform. Fixing or
    # moving one of these files must not leave a dead link in the guides that
    # describe the install.
    GUIDES = (
        "docs/GUIA-FACIL-ES.md",
        "docs/INSTALAR-SIN-NVIDIA.md",
        "docs/INSTALAR-EN-MAC.md",
        "docs/DEPLOY-IOS.md",
        "docs/DEPLOY-ANDROID.md",
    )

    def test_readme_links_both_platform_guides(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for rel in ("docs/INSTALAR-EN-MAC.md", "docs/DEPLOY-IOS.md"):
            with self.subTest(path=rel):
                self.assertIn(rel, readme)
                self.assertTrue((ROOT / rel).is_file())

    def test_relative_links_in_the_platform_guides_resolve(self) -> None:
        for rel in self.GUIDES:
            document = ROOT / rel
            text = document.read_text(encoding="utf-8")
            targets = re.findall(r"\]\(([^)\s]+)\)", text)
            self.assertTrue(targets, f"{document.name} links to nothing")
            for target in targets:
                if re.match(r"^(?:https?:|mailto:|#)", target):
                    continue
                with self.subTest(document=document.name, target=target):
                    self.assertTrue(
                        (document.parent / target.split("#")[0]).exists(),
                        f"{document.name} links to a missing file: {target}",
                    )


if __name__ == "__main__":
    unittest.main()

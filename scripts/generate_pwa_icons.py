"""Generate the installable web-app icons from Maestro's canonical artwork.

The manifest needs fixed-size PNGs (192 and 512, plus a maskable variant), and
Android will only offer "Install app" when it can find both sizes. iOS ignores
those for its Home Screen and asks for an ``apple-touch-icon`` link instead, with
180x180 as the size current iPhones request. Instead of hand-managing copies,
this derives all of them from the single tracked source image so the identity
cannot drift.

Run from the repository root:

    python scripts/generate_pwa_icons.py

It writes into ``ui/public/``:

    maestro-icon-192.png           192x192   purpose "any"
    maestro-icon-512.png           512x512   purpose "any"
    maestro-maskable-512.png       512x512   purpose "maskable" (safe-zone padded)
    maestro-apple-touch-icon.png   180x180   iOS Home Screen (apple-touch-icon)

Requires Pillow (already a Maestro runtime dependency for media handling).
"""

from __future__ import annotations

import os
import sys

try:
    from PIL import Image
except ImportError:  # pragma: no cover - depends on the local environment
    sys.exit("Pillow is required: python -m pip install Pillow")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SOURCE = os.path.join(_REPO_ROOT, "maestro_simplified_icon_alpha.png")
_OUTPUT_DIR = os.path.join(_REPO_ROOT, "ui", "public")

# Android masks a maskable icon to an arbitrary shape and only guarantees the
# central 80% circle, so the mark is scaled to 78% of the canvas and the
# remainder is filled with the artwork's own background colour (it is
# near-black, so the padding is seamless rather than a visible frame).
_MASK_SAFE_FRACTION = 0.78
_BACKGROUND = (0, 0, 0)

# iOS asks for 180x180 on current iPhones (167 for iPad Retina); supplying the
# larger size is what Apple's guidance recommends when only one can be shipped,
# because iOS scales down rather than up. The artwork is square, so a plain
# resize is enough - iOS applies its own rounded mask.
_APPLE_TOUCH_SIZE = 180


def _load_source() -> Image.Image:
    if not os.path.isfile(_SOURCE):
        sys.exit(f"Canonical artwork not found: {_SOURCE}")
    return Image.open(_SOURCE).convert("RGB")


def main() -> int:
    if not os.path.isdir(_OUTPUT_DIR):
        sys.exit(f"Output directory not found: {_OUTPUT_DIR}")
    source = _load_source()

    written: list[str] = []
    for size in (192, 512):
        path = os.path.join(_OUTPUT_DIR, f"maestro-icon-{size}.png")
        source.resize((size, size), Image.LANCZOS).save(path, "PNG", optimize=True)
        written.append(path)

    maskable = Image.new("RGB", (512, 512), _BACKGROUND)
    inner = int(round(512 * _MASK_SAFE_FRACTION))
    mark = source.resize((inner, inner), Image.LANCZOS)
    offset = (512 - inner) // 2
    maskable.paste(mark, (offset, offset))
    maskable_path = os.path.join(_OUTPUT_DIR, "maestro-maskable-512.png")
    maskable.save(maskable_path, "PNG", optimize=True)
    written.append(maskable_path)

    apple_path = os.path.join(_OUTPUT_DIR, "maestro-apple-touch-icon.png")
    source.resize((_APPLE_TOUCH_SIZE, _APPLE_TOUCH_SIZE), Image.LANCZOS).save(
        apple_path, "PNG", optimize=True
    )
    written.append(apple_path)

    for path in written:
        with Image.open(path) as image:
            print(f"{os.path.basename(path)}: {image.size[0]}x{image.size[1]}, "
                  f"{os.path.getsize(path) / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

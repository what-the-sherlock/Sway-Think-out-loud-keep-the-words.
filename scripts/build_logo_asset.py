"""One-off/regenerable build step: turn the source logo artwork into the
clean, transparent, tightly-cropped mask asset the app actually ships and
renders from (src/flow/assets/logo_mask.png).

Not part of the runtime app and not run automatically -- rerun by hand only
if the source artwork changes:

    .venv/Scripts/python.exe scripts/build_logo_asset.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "sway logo.jpg"
OUTPUT = ROOT / "src" / "sway" / "assets" / "logo_mask.png"
#: Output canvas: a square, so every downstream render (tray icon, .ico,
#: overlay avatar) can scale it uniformly without re-deriving an aspect ratio.
CANVAS = 512
#: Fraction of the canvas the mark's longest side fills, leaving even
#: padding on every side -- avoids a mark that touches its own bounding box
#: and looks cramped once composited onto a round/rounded-square badge.
FILL_FRACTION = 0.82


def main() -> None:
    source = Image.open(SOURCE).convert("L")

    # The source is a black mark on a white background (a print-style logo
    # file, not pre-masked art): invert luminance directly into alpha rather
    # than thresholding to a hard binary edge -- the source JPEG's own
    # anti-aliasing (mid-gray edge pixels) survives as partial alpha, so the
    # mark keeps smooth edges instead of getting stair-stepped ones.
    alpha = source.point(lambda p: 255 - p)
    # A hard cutoff for "clearly background" pixels only: JPEG compression
    # leaves a faint non-zero floor across the whole white background, which
    # without this shows up as a barely-visible haze once composited on a
    # dark badge.
    alpha = alpha.point(lambda p: 0 if p < 12 else p)
    mask = Image.new("RGBA", source.size, (255, 255, 255, 0))
    mask.putalpha(alpha)

    bbox = alpha.getbbox()
    if bbox is None:
        raise SystemExit(f"no dark pixels found in {SOURCE}; nothing to crop")
    cropped = mask.crop(bbox)

    scale = (CANVAS * FILL_FRACTION) / max(cropped.size)
    resized = cropped.resize(
        (max(1, round(cropped.width * scale)), max(1, round(cropped.height * scale))),
        Image.LANCZOS,
    )

    canvas = Image.new("RGBA", (CANVAS, CANVAS), (255, 255, 255, 0))
    offset = ((CANVAS - resized.width) // 2, (CANVAS - resized.height) // 2)
    canvas.paste(resized, offset, resized)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(OUTPUT)
    print(f"wrote {OUTPUT} ({canvas.size[0]}x{canvas.size[1]}, bbox from source: {bbox})")


if __name__ == "__main__":
    main()

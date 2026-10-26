"""Per-pixel-alpha overlay helpers (DESIGN.md §8.5). Pure functions only."""

from __future__ import annotations

from PIL import Image

from sway.bar import BarState, render_bar, render_menu, render_tooltip, with_shadow
from sway.platform.layered import premultiplied_bgra


def test_premultiply_scales_colour_by_alpha_and_swaps_to_bgra():
    image = Image.new("RGBA", (2, 1))
    image.putpixel((0, 0), (200, 100, 50, 255))
    image.putpixel((1, 0), (200, 100, 50, 0))
    data = premultiplied_bgra(image)
    assert data[0:4] == bytes([50, 100, 200, 255])
    assert data[4:8] == bytes([0, 0, 0, 0])


def test_premultiply_half_alpha():
    image = Image.new("RGBA", (1, 1), (200, 100, 0, 128))
    b, g, r, a = premultiplied_bgra(image)
    assert a == 128
    assert abs(r - 100) <= 1 and abs(g - 50) <= 1 and b == 0


def test_scale_renders_physical_pixels_and_keeps_logical_regions():
    base, regions_1x = render_bar(BarState.RECORDING, "", elapsed_s=3, level=2, expand=1.0)
    big, regions_15x = render_bar(BarState.RECORDING, "", elapsed_s=3, level=2, expand=1.0, scale=1.5)
    assert big.size == (round(base.width * 1.5), round(base.height * 1.5))
    assert regions_1x == regions_15x


def test_unkeyed_frames_keep_real_alpha():
    image, _ = render_bar(BarState.THINKING, "", expand=1.0, keyed=False)
    # Corners of the capsule are transparent, edges partially so.
    assert image.getpixel((0, 0))[3] == 0
    alphas = {image.getpixel((x, image.height // 2))[3] for x in range(4)}
    assert any(0 < a < 255 for a in alphas) or 0 in alphas


def test_menu_and_tooltip_scale():
    menu, regions = render_menu((), scale=2.0, keyed=False)
    plain, _ = render_menu(())
    assert menu.width == plain.width * 2
    tip = render_tooltip("Microphone", scale=1.25)
    assert tip.height == round(render_tooltip("Microphone").height * 1.25)


def test_shadow_adds_a_constant_margin():
    capsule = Image.new("RGBA", (100, 40), (26, 27, 32, 255))
    framed, margin = with_shadow(capsule, scale=1.0, strength=1.0)
    assert framed.size == (100 + 2 * margin, 40 + 2 * margin)
    # The shadow sits below the capsule, softly.
    below = framed.getpixel((margin + 50, margin + 40 + 2))[3]
    assert 0 < below < 255
    # Strength 0 keeps the margin (no resize as the capsule opens) but no shadow.
    flat, same = with_shadow(capsule, scale=1.0, strength=0.0)
    assert same == margin and flat.getpixel((margin + 50, margin + 40 + 2))[3] == 0

"""The app mark and a small shared dark-UI palette, so the overlay pill, the
tray icon, and every window's title-bar icon all render from the same source
asset and read as one product rather than several ad-hoc widgets.

The mark itself (``assets/logo_mask.png``) is a pre-processed, cropped,
anti-aliased alpha mask built once from the source artwork by
``scripts/build_logo_asset.py`` -- see that script's docstring to regenerate
it if the artwork changes. Shipping the already-processed PNG means no image
thresholding work happens at app startup.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
MARK_PATH = ASSETS_DIR / "logo_mask.png"

#: RGB, 0-255. A small, deliberately restrained dark-UI palette: a neutral
#: surface/ink pair for chrome, plus one accent color per semantic state
#: (kept distinct in both hue and, in the overlay's badge glyph, shape --
#: color alone is never the only signal for a state that matters).
INK = (235, 236, 240)
SURFACE = (26, 27, 32)
SURFACE_RAISED = (38, 40, 47)

ACCENT_IDLE = (108, 111, 125)
ACCENT_RECORDING = (255, 77, 94)
ACCENT_THINKING = (74, 168, 255)
ACCENT_ERROR = (255, 171, 46)


@lru_cache(maxsize=1)
def _raw_mark() -> Image.Image:
    return Image.open(MARK_PATH).convert("RGBA")


@lru_cache(maxsize=64)
def mark_image(size: int, color: tuple[int, int, int, int] = (255, 255, 255, 255)) -> Image.Image:
    """The logo mark, resized to ``size``x``size`` and recolored to
    ``color``. The source asset is a flat white-on-transparent mask, so
    recoloring is just a straight RGB replacement under its own alpha.
    """
    if size < 1:
        raise ValueError("size must be >= 1")
    resized = _raw_mark().resize((size, size), Image.LANCZOS)
    solid = Image.new("RGBA", (size, size), color)
    solid.putalpha(resized.split()[3])
    return solid


#: Mark colour per tray state (D1). Keyed by :class:`~flow.tray.TrayState`'s
#: string values, but typed as plain ``str`` so branding stays importable
#: without pulling in the tray (and its pystray dependency).
#:
#: The whole mark is tinted rather than a status dot being added in a corner:
#: a notification-area icon is drawn at 16px, where a corner dot is about four
#: pixels and effectively invisible. ``disabled`` uses the muted idle grey
#: rather than a fifth accent, so "paused" reads as absence of colour rather
#: than as another status hue competing with the four real ones.
_STATE_COLORS: dict[str, tuple[int, int, int]] = {
    "idle": INK,
    "listening": ACCENT_RECORDING,
    "processing": ACCENT_THINKING,
    "error": ACCENT_ERROR,
    "disabled": ACCENT_IDLE,
}


@lru_cache(maxsize=16)
def app_badge_image(
    size: int, *, corner_ratio: float = 0.32, state: str | None = None
) -> Image.Image:
    """A square app badge: a rounded-corner tile in :data:`SURFACE_RAISED`
    with the mark centered on it. Used everywhere the app needs one static
    icon rather than a live-rendered overlay frame -- the tray icon and
    window title-bar icons.

    ``state`` tints the mark to show engine status in the notification area
    (see :data:`_STATE_COLORS`). ``None``, and any unrecognised value, gives
    the neutral :data:`INK` mark -- an unknown state must still render an
    icon, since the alternative is no tray icon at all.

    Rendered at 4x and downsampled for anti-aliased edges: ``ImageDraw``'s
    primitives are hard-edged at native resolution, which reads as cheap at
    the small sizes (16-64px) these icons are actually shown at.
    """
    if size < 1:
        raise ValueError("size must be >= 1")
    ss = 4
    big = size * ss
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(canvas).rounded_rectangle(
        (0, 0, big - 1, big - 1), radius=round(big * corner_ratio), fill=(*SURFACE_RAISED, 255)
    )
    color = _STATE_COLORS.get(state or "", INK)
    mark = mark_image(round(big * 0.62), color=(*color, 255))
    canvas.alpha_composite(mark, ((big - mark.width) // 2, (big - mark.height) // 2))
    return canvas.resize((size, size), Image.LANCZOS)


__all__ = [
    "ACCENT_ERROR",
    "ACCENT_IDLE",
    "ACCENT_RECORDING",
    "ACCENT_THINKING",
    "INK",
    "MARK_PATH",
    "SURFACE",
    "SURFACE_RAISED",
    "app_badge_image",
    "mark_image",
]

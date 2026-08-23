"""The Sway Bar: the floating surface the user actually lives with.

This replaces the always-on status pill. The change is not cosmetic -- it is
a different idea about what the product is. The pill was a 44px indicator
that sat on screen all day saying "still here". The bar is three tiers:

* **At rest**, a single ~14px dot. The product disappears when it is not
  being used, which is a statement of confidence: it does not need to remind
  you it exists.
* **Engaged**, a capsule carrying the state and the two controls anyone
  actually changes mid-dictation -- language and microphone.
* **Its menu**, opened by right-click or the mic button, holding the rest:
  hide, settings, device, history, and re-paste.

Each tier is one interaction from the last; nothing is buried and nothing is
crowded.

Rendering is PIL compositing into a colour-keyed tkinter window, exactly as
the pill was: every shape is drawn at :data:`_SS`x and downsampled, because
PIL's primitives are hard-edged at native resolution and this is drawn at
14-44px where that looks cheap. A WebView2 bar would have been easier to
style and is disqualified for one reason: a webview window activates when
shown, paste targets the *focused* window, and a bar that steals focus mid
dictation sends the user's words into the bar instead of their document.

Threading is unchanged from the pill and is the part to be careful with. Tk
objects are touched only on the thread running :meth:`SwayBar.run`;
:meth:`set_state`, :meth:`set_level` and :meth:`run_on_ui_thread` are the
thread-safe doors in. Renders are memoised on a key of everything that
varies, so the bar repaints when something changed and not otherwise -- which
matters because this repaint happens during the seconds the latency budget is
being spent.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageTk

from sway.branding import (
    ACCENT_BRAND,
    ACCENT_ERROR,
    ACCENT_IDLE,
    ACCENT_RECORDING,
    INK,
    SURFACE,
    SURFACE_RAISED,
    app_badge_image,
    mark_image,
)

logger = logging.getLogger(__name__)


class BarState(StrEnum):
    """What the bar is currently saying."""

    IDLE = "idle"
    RECORDING = "recording"
    THINKING = "thinking"
    #: Text is being placed at the cursor. Sub-100ms in practice, but it is
    #: the moment the user is watching for, and labelling it means a slow
    #: paste (an elevated window, a busy clipboard) reads as slow rather than
    #: as a hang.
    INJECTING = "injecting"
    #: Briefly confirms what actually landed, then fades. Reports a fact --
    #: words and milliseconds -- rather than a tick.
    DONE = "done"
    ERROR = "error"


#: Actions a click on the bar can produce. The engine binds these; the bar
#: itself knows nothing about what they do.
ACTION_LANGUAGE = "language"
ACTION_MIC = "microphone"
ACTION_MENU = "menu"

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

#: Exact colour keyed out to transparent. Windows' -transparentcolor is a hard
#: match rather than alpha blending, so this is chosen close in tone to
#: SURFACE: anti-aliased edge pixels straddling the key then read as a soft
#: dark edge rather than a coloured fringe against an arbitrary desktop.
_KEY_RGB = (8, 8, 9)

_SS = 4

#: At rest. Small enough to ignore, large enough to hit with a mouse.
_DOT_D = 14
_DOT_BOX = 26

_HEIGHT = 44
_AVATAR = 34
_PAD_LEFT = 6
_PAD_RIGHT = 8
_GAP = 10
_MIN_WIDTH = 132
_BADGE_D = 15
_FONT_SIZE = 13
_LEVEL_W = 18
_BUTTON_D = 28
_BUTTON_GAP = 6

#: How far the bar sits from the screen edge it is docked to.
_MARGIN = 14

_FONT_SEMIBOLD = ("segoeuisb.ttf", "seguisb.ttf", "segoeuib.ttf", "segoeui.ttf")
_FONT_REGULAR = ("segoeui.ttf", "seguisb.ttf")


@dataclass(frozen=True, slots=True)
class HitRegion:
    """A clickable rectangle in bar-local coordinates."""

    x0: int
    y0: int
    x1: int
    y1: int
    action: str

    def contains(self, x: int, y: int) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1


@dataclass(frozen=True, slots=True)
class _Style:
    label: str
    accent: tuple[int, int, int]


_STYLES: dict[BarState, _Style] = {
    BarState.IDLE: _Style("", ACCENT_IDLE),
    BarState.RECORDING: _Style("Listening", ACCENT_RECORDING),
    # Named for the stage that is running, not for a mood. "Thinking" tells
    # the user nothing they can act on; "Transcribing" tells them where the
    # time is going, and we already measure it.
    BarState.THINKING: _Style("Transcribing", ACCENT_BRAND),
    BarState.INJECTING: _Style("Pasting", ACCENT_BRAND),
    BarState.DONE: _Style("Done", ACCENT_IDLE),
    BarState.ERROR: _Style("Error", ACCENT_ERROR),
}

#: States that draw an accent ring. Only recording and error do: an ambient
#: widget on screen all day must not have four colours competing, and these
#: are the two the user needs to notice without looking.
_RINGED = frozenset({BarState.RECORDING, BarState.ERROR})

#: States that expose the control row. During a dictation the buttons would
#: be a target the user is not aiming at, and the bar is busy saying
#: something more useful.
_SHOWS_CONTROLS = frozenset({BarState.IDLE})

ERROR_HOLD_S = 4.0
DONE_HOLD_S = 1.6


def _finish(canvas: Image.Image, width: int, height: int) -> Image.Image:
    """Downsample, then lay the result on the key colour.

    The order matters and is easy to get wrong. Drawing onto a canvas already
    filled with the key colour and *then* resizing blends the key into every
    anti-aliased edge, so the corner pixels come out near-key but not
    exactly-key -- and Windows' -transparentcolor is an exact match, not a
    tolerance. The result is a capsule with visibly square dark corners.

    Compositing after the resize instead means fully-transparent pixels land
    on exactly the key colour and are cut cleanly, while partially
    transparent edge pixels blend toward it and read as a soft dark edge
    against any desktop.
    """
    resized = canvas.resize((width, height), Image.LANCZOS)
    ground = Image.new("RGBA", (width, height), (*_KEY_RGB, 255))
    ground.alpha_composite(resized)
    return ground


@lru_cache(maxsize=4)
def _font(size: int, *, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    fonts_dir = os.environ.get("WINDIR", r"C:\Windows") + r"\Fonts"
    for name in _FONT_SEMIBOLD if bold else _FONT_REGULAR:
        try:
            return ImageFont.truetype(f"{fonts_dir}\\{name}", size * _SS)
        except OSError:
            continue
    logger.debug("no system font found for the bar; falling back to PIL's default")
    return ImageFont.load_default()


def _badge_frame(state: BarState, now_s: float) -> int:
    """Which animation frame to draw at wall-clock ``now_s``.

    A pure function of time, so the bar needs no animation timer of its own --
    it recomputes this per poll tick and repaints only when the value changed.
    """
    ms = now_s * 1000.0
    if state is BarState.RECORDING:
        return int(ms // 500) % 2
    if state in (BarState.THINKING, BarState.INJECTING):
        return int(ms // 90) % 8
    return 0


def _elapsed_label(elapsed_s: int) -> str:
    minutes, seconds = divmod(max(0, elapsed_s), 60)
    return f"{minutes}:{seconds:02d}"


# ---------------------------------------------------------------------------
# Icon glyphs, drawn rather than shipped
# ---------------------------------------------------------------------------


def _draw_globe(draw: ImageDraw.ImageDraw, box, colour, width) -> None:
    """The language control. A circle with a meridian and two parallels --
    the smallest drawing that unambiguously reads as "language" rather than
    "settings"."""
    x0, y0, x1, y1 = box
    draw.ellipse(box, outline=colour, width=width)
    cx = (x0 + x1) / 2
    draw.line([(cx, y0), (cx, y1)], fill=colour, width=width)
    draw.line([(x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2)], fill=colour, width=width)
    inset = (x1 - x0) * 0.28
    draw.ellipse((x0 + inset, y0, x1 - inset, y1), outline=colour, width=width)


def _draw_mic(draw: ImageDraw.ImageDraw, box, colour, width) -> None:
    x0, y0, x1, y1 = box
    w = x1 - x0
    h = y1 - y0
    cap_w = w * 0.42
    cx = (x0 + x1) / 2
    draw.rounded_rectangle(
        (cx - cap_w / 2, y0, cx + cap_w / 2, y0 + h * 0.58),
        radius=cap_w / 2,
        fill=colour,
    )
    draw.arc(
        (x0 + w * 0.12, y0 + h * 0.30, x1 - w * 0.12, y0 + h * 0.86),
        start=0,
        end=180,
        fill=colour,
        width=width,
    )
    draw.line([(cx, y0 + h * 0.80), (cx, y1)], fill=colour, width=width)


def _draw_chevron(draw: ImageDraw.ImageDraw, box, colour, width) -> None:
    x0, y0, x1, y1 = box
    cy = (y0 + y1) / 2
    draw.line(
        [(x0, y0), (x1, cy), (x0, y1)], fill=colour, width=width, joint="curve"
    )


def _draw_clock(draw: ImageDraw.ImageDraw, box, colour, width) -> None:
    x0, y0, x1, y1 = box
    draw.ellipse(box, outline=colour, width=width)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    draw.line([(cx, cy), (cx, y0 + (y1 - y0) * 0.28)], fill=colour, width=width)
    draw.line([(cx, cy), (x0 + (x1 - x0) * 0.72, cy)], fill=colour, width=width)


def _draw_gear(draw: ImageDraw.ImageDraw, box, colour, width) -> None:
    """A gear, not a sun.

    At 17px a real cog outline turns to mush, so this is a thick ring plus
    eight short stubby teeth. The teeth have to be *short and wide* -- long
    thin ones read as rays and the icon becomes a sunburst.
    """
    import math

    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    r = (x1 - x0) / 2
    draw.ellipse(
        (cx - r * 0.50, cy - r * 0.50, cx + r * 0.50, cy + r * 0.50),
        outline=colour, width=width,
    )
    tooth = max(width, round(width * 1.7))
    for index in range(8):
        angle = index * math.pi / 4
        draw.line(
            [
                (cx + math.cos(angle) * r * 0.66, cy + math.sin(angle) * r * 0.66),
                (cx + math.cos(angle) * r * 0.98, cy + math.sin(angle) * r * 0.98),
            ],
            fill=colour, width=tooth,
        )


def _draw_copy(draw: ImageDraw.ImageDraw, box, colour, width) -> None:
    """Two offset sheets -- distinct from the clipboard used for paste, so
    two adjacent menu rows are not carrying the same glyph."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    draw.rounded_rectangle(
        (x0, y0, x1 - w * 0.26, y1 - h * 0.26), radius=w * 0.16,
        outline=colour, width=width,
    )
    draw.rounded_rectangle(
        (x0 + w * 0.26, y0 + h * 0.26, x1, y1), radius=w * 0.16,
        fill=(*colour[:3], 40), outline=colour, width=width,
    )


def _draw_list(draw: ImageDraw.ImageDraw, box, colour, width) -> None:
    x0, y0, x1, y1 = box
    for index in range(3):
        y = y0 + (y1 - y0) * (0.15 + index * 0.35)
        draw.ellipse((x0, y - width, x0 + width * 2, y + width), fill=colour)
        draw.line([(x0 + width * 4, y), (x1, y)], fill=colour, width=width)


def _draw_paste(draw: ImageDraw.ImageDraw, box, colour, width) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    draw.rounded_rectangle(
        (x0 + w * 0.12, y0 + h * 0.12, x1 - w * 0.12, y1),
        radius=w * 0.16,
        outline=colour,
        width=width,
    )
    draw.rounded_rectangle(
        (x0 + w * 0.34, y0, x1 - w * 0.34, y0 + h * 0.22),
        radius=w * 0.08,
        fill=colour,
    )


ICONS: dict[str, Callable[..., None]] = {
    "globe": _draw_globe,
    "mic": _draw_mic,
    "chevron": _draw_chevron,
    "clock": _draw_clock,
    "gear": _draw_gear,
    "list": _draw_list,
    "paste": _draw_paste,
    "copy": _draw_copy,
}


# ---------------------------------------------------------------------------
# The bar itself
# ---------------------------------------------------------------------------


def render_dot(*, accent: tuple[int, int, int] = ACCENT_BRAND) -> tuple[Image.Image, tuple[HitRegion, ...]]:
    """The resting state: one small dot, and nothing else.

    The whole box is a hit region so hovering anywhere near it expands the
    bar -- a 14px target is fine to *see* and unpleasant to *hit*.
    """
    box = _DOT_BOX
    canvas = Image.new("RGBA", (box * _SS, box * _SS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    d = _DOT_D * _SS
    offset = (box * _SS - d) // 2
    # A darker ring under the dot keeps it visible on a light desktop, where
    # a mid-tone indigo alone would wash out.
    draw.ellipse(
        (offset - _SS, offset - _SS, offset + d + _SS, offset + d + _SS),
        fill=(*SURFACE, 200),
    )
    draw.ellipse((offset, offset, offset + d, offset + d), fill=(*accent, 255))
    return _finish(canvas, box, box), (HitRegion(0, 0, box, box, ACTION_MENU),)


def render_bar(
    state: BarState,
    detail: str,
    *,
    badge_frame: int = 0,
    elapsed_s: int | None = None,
    level: int = 0,
    hint: str = "",
    hover: str = "",
) -> tuple[Image.Image, tuple[HitRegion, ...]]:
    """One full bar frame, plus where it can be clicked.

    ``hint`` is coaching text shown instead of the state label -- the "next
    step" idea, which teaches in place at the moment of need rather than in a
    wizard that takes the screen. ``hover`` is the action currently under the
    pointer, so a button can light up.
    """
    style = _STYLES[state]
    text = hint or detail or style.label
    if state is BarState.RECORDING and not detail and elapsed_s is not None:
        text = f"{style.label} · {_elapsed_label(elapsed_s)}"

    show_level = state is BarState.RECORDING
    show_controls = state in _SHOWS_CONTROLS
    font = _font(_FONT_SIZE)
    text_w = round(font.getlength(text) / _SS) if text else 0

    width = _PAD_LEFT + _AVATAR + _GAP + text_w
    if show_level:
        width += _GAP + _LEVEL_W
    if show_controls:
        width += _GAP + _BUTTON_D * 2 + _BUTTON_GAP
    width = max(_MIN_WIDTH, width + _PAD_RIGHT)

    height = _HEIGHT
    bw, bh = width * _SS, height * _SS
    canvas = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    regions: list[HitRegion] = []

    # -- capsule body, a faint top sheen, and a hairline edge
    body = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    body_draw = ImageDraw.Draw(body)
    body_draw.rounded_rectangle((0, 0, bw - 1, bh - 1), radius=bh // 2, fill=(*SURFACE, 255))
    body_draw.rounded_rectangle(
        (2 * _SS, 2 * _SS, bw - 2 * _SS - 1, bh // 2),
        radius=max(0, bh // 2 - 2 * _SS),
        fill=(255, 255, 255, 12),
    )
    body_draw.rounded_rectangle(
        (0, 0, bw - 1, bh - 1), radius=bh // 2, outline=(255, 255, 255, 26), width=_SS
    )
    canvas.alpha_composite(body)

    # -- avatar: the mark on a raised disc, ringed in the state's accent
    avatar_d = _AVATAR * _SS
    avatar_cx = _PAD_LEFT * _SS + avatar_d // 2
    avatar_cy = bh // 2
    avatar_box = (
        avatar_cx - avatar_d // 2,
        avatar_cy - avatar_d // 2,
        avatar_cx + avatar_d // 2,
        avatar_cy + avatar_d // 2,
    )
    avatar = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    avatar_draw = ImageDraw.Draw(avatar)
    avatar_draw.ellipse(avatar_box, fill=(*SURFACE_RAISED, 255))
    if state in _RINGED:
        avatar_draw.ellipse(avatar_box, outline=(*style.accent, 255), width=2 * _SS)
    canvas.alpha_composite(avatar)

    mark = mark_image(round(avatar_d * 0.56), color=(*INK, 255))
    canvas.alpha_composite(mark, (avatar_cx - mark.width // 2, avatar_cy - mark.height // 2))

    _draw_badge(canvas, state, style, badge_frame, avatar_box, bw, bh)

    # -- label
    if text:
        layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        text_x = (_PAD_LEFT + _AVATAR + _GAP) * _SS
        bbox = draw.textbbox((0, 0), text, font=font)
        text_y = (bh - (bbox[3] - bbox[1])) // 2 - bbox[1]
        draw.text((text_x, text_y), text, font=font, fill=(*INK, 255))
        canvas.alpha_composite(layer)

    cursor = width - _PAD_RIGHT

    # -- controls, right to left so the rightmost sits against the padding
    if show_controls:
        for action, glyph in ((ACTION_MIC, "mic"), (ACTION_LANGUAGE, "globe")):
            cursor -= _BUTTON_D
            _draw_button(
                canvas,
                x=cursor,
                y=(height - _BUTTON_D) // 2,
                glyph=glyph,
                active=hover == action,
            )
            regions.append(
                HitRegion(
                    cursor,
                    (height - _BUTTON_D) // 2,
                    cursor + _BUTTON_D,
                    (height + _BUTTON_D) // 2,
                    action,
                )
            )
            cursor -= _BUTTON_GAP

    # -- mic level: three discrete bars.
    #
    # Deliberately not a waveform. The bucket is part of the render key, so
    # this repaints a handful of times per utterance rather than at 30fps --
    # which matters because the repaint is a supersampled composite running
    # during the seconds the latency budget is being spent.
    if show_level:
        cursor -= _LEVEL_W
        layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        bar_w, gap = 4 * _SS, 3 * _SS
        left = cursor * _SS
        for index, ratio in enumerate((0.36, 0.60, 0.84)):
            lit = level > index
            bar_h = round(_AVATAR * _SS * ratio)
            x = left + index * (bar_w + gap)
            y1 = bh // 2 + (_AVATAR * _SS) // 2 - 2 * _SS
            draw.rounded_rectangle(
                (x, y1 - bar_h, x + bar_w, y1),
                radius=bar_w // 2,
                fill=(*style.accent, 255) if lit else (*SURFACE_RAISED, 255),
            )
        canvas.alpha_composite(layer)

    # The whole capsule opens the menu on right-click; the buttons above take
    # precedence for left-clicks because they are checked first.
    regions.append(HitRegion(0, 0, width, height, ACTION_MENU))

    return _finish(canvas, width, height), tuple(regions)


def _draw_badge(canvas, state, style, badge_frame, avatar_box, bw, bh) -> None:
    """The state glyph overlapping the avatar's corner.

    Shape varies as well as colour -- a pulsing dot, a rotating arc, a check,
    a warning triangle -- so state is never carried by colour alone.
    """
    if state is BarState.IDLE:
        return
    bd = _BADGE_D * _SS
    bx = avatar_box[2] - bd + 3 * _SS
    by = avatar_box[3] - bd + 3 * _SS
    badge = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    draw = ImageDraw.Draw(badge)
    # A halo in the capsule's own colour first, so the badge reads as cut
    # into the avatar rather than laid messily over it.
    draw.ellipse((bx - 2 * _SS, by - 2 * _SS, bx + bd + 2 * _SS, by + bd + 2 * _SS),
                 fill=(*SURFACE, 255))

    if state is BarState.RECORDING:
        alpha = 255 if badge_frame == 0 else 130
        draw.ellipse((bx, by, bx + bd, by + bd), fill=(*style.accent, alpha))
    elif state in (BarState.THINKING, BarState.INJECTING):
        start = badge_frame * 45
        draw.arc((bx, by, bx + bd, by + bd), start=start, end=start + 260,
                 fill=(*style.accent, 255), width=3 * _SS)
    elif state is BarState.DONE:
        draw.ellipse((bx, by, bx + bd, by + bd), fill=(*SURFACE_RAISED, 255))
        draw.line(
            [
                (bx + bd * 0.26, by + bd * 0.52),
                (bx + bd * 0.44, by + bd * 0.70),
                (bx + bd * 0.76, by + bd * 0.32),
            ],
            fill=(*INK, 255), width=2 * _SS, joint="curve",
        )
    elif state is BarState.ERROR:
        cx = bx + bd / 2
        draw.polygon([(cx, by), (bx, by + bd), (bx + bd, by + bd)], fill=(*style.accent, 255))
        draw.line([(cx, by + bd * 0.38), (cx, by + bd * 0.66)], fill=(*SURFACE, 255), width=2 * _SS)
        r = bd * 0.05
        draw.ellipse((cx - r, by + bd * 0.76, cx + r, by + bd * 0.76 + 2 * r), fill=(*SURFACE, 255))
    canvas.alpha_composite(badge)


def _draw_button(canvas: Image.Image, *, x: int, y: int, glyph: str, active: bool) -> None:
    """One circular control on the bar."""
    d = _BUTTON_D * _SS
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    box = (x * _SS, y * _SS, x * _SS + d, y * _SS + d)
    draw.ellipse(box, fill=(*SURFACE_RAISED, 255) if not active else (*ACCENT_BRAND, 235))
    inset = d * 0.28
    ICONS[glyph](
        draw,
        (box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset),
        (*INK, 255),
        max(1, round(1.4 * _SS)),
    )
    canvas.alpha_composite(layer)


# ---------------------------------------------------------------------------
# The menu
# ---------------------------------------------------------------------------

#: The menu is light where the bar is dark. That inversion does real work: it
#: makes the panel read as sitting *above* the bar rather than being part of
#: it, without needing a border or a shadow to say so.
_MENU_BG = (252, 251, 249)
_MENU_INK = (22, 21, 15)
_MENU_MUTED = (119, 115, 106)
_MENU_RULE = (230, 227, 221)
_MENU_HOVER = (240, 238, 233)

_MENU_W = 268
_MENU_ROW_H = 44
_MENU_PAD_Y = 8
_MENU_SEP_H = 9
_MENU_RADIUS = 14
_MENU_ICON_X = 18
_MENU_ICON_D = 17
_MENU_LABEL_X = 48


@dataclass(frozen=True, slots=True)
class MenuItem:
    """One row. An empty ``action`` renders a separator."""

    action: str
    label: str = ""
    icon: str = ""
    submenu: bool = False

    @property
    def is_separator(self) -> bool:
        return not self.action


SEPARATOR = MenuItem(action="")


def menu_height(items: tuple[MenuItem, ...]) -> int:
    body = sum(_MENU_SEP_H if item.is_separator else _MENU_ROW_H for item in items)
    return body + _MENU_PAD_Y * 2


def render_menu(
    items: tuple[MenuItem, ...], *, hover: str = ""
) -> tuple[Image.Image, tuple[HitRegion, ...]]:
    """The panel, and where each row is."""
    width, height = _MENU_W, menu_height(items)
    bw, bh = width * _SS, height * _SS
    canvas = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (0, 0, bw - 1, bh - 1), radius=_MENU_RADIUS * _SS, fill=(*_MENU_BG, 255)
    )
    draw.rounded_rectangle(
        (0, 0, bw - 1, bh - 1),
        radius=_MENU_RADIUS * _SS,
        outline=(*_MENU_RULE, 255),
        width=_SS,
    )

    font = _font(14, bold=False)
    regions: list[HitRegion] = []
    y = _MENU_PAD_Y

    for item in items:
        if item.is_separator:
            mid = (y + _MENU_SEP_H / 2) * _SS
            draw.line(
                [(_MENU_ICON_X * _SS, mid), ((width - _MENU_ICON_X) * _SS, mid)],
                fill=(*_MENU_RULE, 255),
                width=_SS,
            )
            y += _MENU_SEP_H
            continue

        if hover == item.action:
            draw.rounded_rectangle(
                (5 * _SS, y * _SS, (width - 5) * _SS, (y + _MENU_ROW_H) * _SS),
                radius=9 * _SS,
                fill=(*_MENU_HOVER, 255),
            )

        cy = (y + _MENU_ROW_H / 2) * _SS
        if item.icon:
            half = _MENU_ICON_D * _SS / 2
            ICONS[item.icon](
                draw,
                (
                    _MENU_ICON_X * _SS,
                    cy - half,
                    (_MENU_ICON_X + _MENU_ICON_D) * _SS,
                    cy + half,
                ),
                (*_MENU_MUTED, 255),
                max(1, round(1.4 * _SS)),
            )

        bbox = draw.textbbox((0, 0), item.label, font=font)
        draw.text(
            (_MENU_LABEL_X * _SS, cy - (bbox[3] - bbox[1]) / 2 - bbox[1]),
            item.label,
            font=font,
            fill=(*_MENU_INK, 255),
        )

        if item.submenu:
            size = 5 * _SS
            x = (width - 22) * _SS
            _draw_chevron(
                draw,
                (x, cy - size, x + size, cy + size),
                (*_MENU_MUTED, 255),
                max(1, round(1.3 * _SS)),
            )

        regions.append(HitRegion(0, y, width, y + _MENU_ROW_H, item.action))
        y += _MENU_ROW_H

    return _finish(canvas, width, height), tuple(regions)

# ---------------------------------------------------------------------------
# Screen placement
# ---------------------------------------------------------------------------


def work_area(fallback: tuple[int, int]) -> tuple[int, int, int, int]:
    """The usable desktop rectangle, excluding the taskbar.

    Without this the bar docks to the bottom of the *screen* and sits behind
    the taskbar, which is exactly where the reference does not put it. Falls
    back to the full screen size when the call is unavailable.
    """
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        # SPI_GETWORKAREA
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        logger.debug("could not read the desktop work area", exc_info=True)
    return 0, 0, fallback[0], fallback[1]


def place(
    position: str, size: tuple[int, int], area: tuple[int, int, int, int]
) -> tuple[int, int]:
    """Top-left corner for ``size`` docked at ``position`` within ``area``.

    Pure, so placement is testable without a screen.
    """
    left, top, right, bottom = area
    width, height = size
    if position.endswith("-right"):
        x = right - width - _MARGIN
    else:
        x = left + (right - left - width) // 2
    y = top + _MARGIN if position.startswith("top-") else bottom - height - _MARGIN
    return x, y


# ---------------------------------------------------------------------------
# The Tk window
# ---------------------------------------------------------------------------

_POLL_MS = 50

#: What the bar's menu offers. Ordered and grouped as the reference's is --
#: dismiss, configure, switch, review, recover -- with copy added beside
#: paste, because a paste the target window rejects still needs a way out.
DEFAULT_MENU: tuple[MenuItem, ...] = (
    MenuItem("hide", "Hide for 1 hour", "clock"),
    MenuItem("settings", "Settings", "gear"),
    SEPARATOR,
    MenuItem("language", "Language", "globe", submenu=True),
    MenuItem("microphone", "Microphone", "mic", submenu=True),
    SEPARATOR,
    MenuItem("history", "Transcript history", "list"),
    MenuItem("paste", "Paste last transcript", "paste"),
    MenuItem("copy", "Copy last transcript", "copy"),
)


class SwayBar:
    """Runs Tk's mainloop on the calling thread until :meth:`stop`.

    Every public method except :meth:`run` is thread-safe: other threads put
    work on a queue and the Tk thread drains it, which is the only safe way
    to drive Tk from a hotkey listener or a dictation worker.
    """

    def __init__(
        self,
        *,
        poll_ms: int = _POLL_MS,
        position: str = "bottom-center",
        on_action: Callable[[str], None] | None = None,
        menu_items: tuple[MenuItem, ...] = DEFAULT_MENU,
    ) -> None:
        self._queue: queue.Queue[tuple[BarState, str]] = queue.Queue()
        self._ui_actions: queue.Queue[Callable[[tk.Tk], None]] = queue.Queue()
        self._poll_ms = poll_ms
        self._position = position
        self._on_action = on_action
        self._menu_items = menu_items

        self._root: tk.Tk | None = None
        self._canvas: tk.Canvas | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._icon_photo: ImageTk.PhotoImage | None = None
        self._image_item: int | None = None

        self._state = BarState.IDLE
        self._detail = ""
        self._hint = ""
        self._hover = ""
        self._pointer_inside = False
        self._level = 0
        self._regions: tuple[HitRegion, ...] = ()
        self._render_key: tuple | None = None
        self._geometry = ""
        self._recording_started_at: float | None = None
        self._error_since: float | None = None
        self._done_since: float | None = None
        self._hidden_until = 0.0
        self._menu: _Menu | None = None

    # -- thread-safe entry points -----------------------------------------

    @property
    def root(self) -> tk.Tk | None:
        return self._root

    def set_state(self, state: BarState, detail: str = "") -> None:
        """Call from any thread; applied on the next poll tick."""
        self._queue.put((state, detail))

    def set_level(self, level: int) -> None:
        """Mic level bucket, 0-3.

        Not queued: a level is a *current* value rather than an event, so the
        newest simply wins and a burst of updates cannot back up behind the
        poll tick.
        """
        self._level = max(0, min(3, int(level)))

    def set_hint(self, hint: str) -> None:
        """Coaching text shown while idle, or an empty string to clear it."""
        self._hint = hint

    def hide_for(self, seconds: float) -> None:
        """Collapse to the dot and stay there.

        Distinct from pausing the agent: the hotkey still works and dictation
        still happens, the bar simply stops talking about it.
        """
        self._hidden_until = time.monotonic() + seconds

    @property
    def hidden(self) -> bool:
        return time.monotonic() < self._hidden_until

    def run_on_ui_thread(self, action: Callable[[tk.Tk], None]) -> None:
        """Run ``action(root)`` on the Tk thread on the next tick."""
        self._ui_actions.put(action)

    # -- lifecycle ---------------------------------------------------------

    def run(self) -> None:
        """Blocks the calling thread until :meth:`stop` or the window dies."""
        key_hex = "#%02x%02x%02x" % _KEY_RGB

        root = tk.Tk()
        self._root = root
        # Distinct from the application window's title. The bar is
        # overrideredirect so this is never shown, but two top-level windows
        # answering to the same name confuses anything that looks one up by
        # title -- including the OS's own window management.
        root.title("Sway Bar")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=key_hex)

        shaped = False
        try:
            root.wm_attributes("-transparentcolor", key_hex)
            shaped = True
        except tk.TclError:
            # Only Windows has colour-keyed transparency. Elsewhere fall back
            # to whole-window translucency: the bar still works, it is just a
            # soft-edged rectangle rather than a capsule.
            try:
                root.attributes("-alpha", 0.94)
            except tk.TclError:
                pass

        try:
            self._icon_photo = ImageTk.PhotoImage(app_badge_image(64))
            root.iconphoto(True, self._icon_photo)
        except tk.TclError:
            pass  # cosmetic only

        canvas = tk.Canvas(root, highlightthickness=0, bg=key_hex if shaped else "#1a1b20")
        canvas.pack()
        self._canvas = canvas

        canvas.bind("<Motion>", self._on_motion)
        canvas.bind("<Enter>", self._on_enter)
        canvas.bind("<Leave>", self._on_leave)
        canvas.bind("<Button-1>", self._on_click)
        canvas.bind("<Button-3>", self._on_right_click)

        # Deferred: the native window has to exist before its style can be
        # changed, and Tk creates it during the first idle cycle.
        root.after(60, self._make_non_activating)

        self._apply_frame(force=True)
        root.after(self._poll_ms, self._poll)
        try:
            root.mainloop()
        finally:
            self._teardown()

    def _teardown(self) -> None:
        if self._menu is not None:
            self._menu.close()
            self._menu = None
        self._root = None
        self._canvas = None
        self._photo = None
        self._icon_photo = None
        self._image_item = None
        self._render_key = None
        self._geometry = ""

    def _make_non_activating(self) -> None:
        """Apply ``WS_EX_NOACTIVATE`` so clicking the bar never takes focus.

        This is not polish. Paste targets the *focused* window, so a bar that
        activates when clicked would move focus off the user's document and
        the next dictation would land somewhere they did not choose.
        Best-effort: where it is unavailable the bar still works.
        """
        root = self._root
        if root is None:
            return
        try:
            import ctypes

            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
            current = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, current | WS_EX_NOACTIVATE)
        except Exception:
            logger.debug("could not mark the bar non-activating", exc_info=True)

    def stop(self) -> None:
        root = self._root
        if root is not None:
            try:
                root.after(0, root.destroy)
            except tk.TclError:
                pass  # already destroyed

    # -- input -------------------------------------------------------------

    def _on_enter(self, _event: tk.Event) -> None:
        self._pointer_inside = True

    def _on_leave(self, _event: tk.Event) -> None:
        self._pointer_inside = False
        self._hover = ""

    def _on_motion(self, event: tk.Event) -> None:
        self._pointer_inside = True
        hover = ""
        for region in self._regions:
            if region.action != ACTION_MENU and region.contains(event.x, event.y):
                hover = region.action
                break
        self._hover = hover

    def _on_click(self, event: tk.Event) -> None:
        for region in self._regions:
            if region.action != ACTION_MENU and region.contains(event.x, event.y):
                self._dispatch(region.action)
                return
        # A left-click on the body, including the resting dot, opens the menu:
        # a 14px dot is too small to carry more than one meaning.
        self._open_menu()

    def _on_right_click(self, _event: tk.Event) -> None:
        self._open_menu()

    def _dispatch(self, action: str) -> None:
        if self._on_action is None:
            return
        try:
            self._on_action(action)
        except Exception:
            logger.exception("bar action %r failed", action)

    # -- the menu ----------------------------------------------------------

    def _open_menu(self) -> None:
        root = self._root
        if root is None:
            return
        if self._menu is not None:
            self._menu.close()
            self._menu = None
            return
        self._menu = _Menu(
            root,
            self._menu_items,
            anchor=(root.winfo_x(), root.winfo_y(), root.winfo_width()),
            position=self._position,
            on_choose=self._on_menu_choice,
        )
        self._menu.open()

    def _on_menu_choice(self, action: str) -> None:
        self._menu = None
        if action:
            self._dispatch(action)

    # -- the frame loop ----------------------------------------------------

    def _poll(self) -> None:
        changed = False
        try:
            while True:
                self._state, self._detail = self._queue.get_nowait()
                changed = True
        except queue.Empty:
            pass

        now = time.monotonic()
        if self._state is BarState.RECORDING:
            if self._recording_started_at is None:
                self._recording_started_at = now
        else:
            self._recording_started_at = None

        # Errors and confirmations are transient notices, not modes: they
        # expire back to idle so the bar never lies about the current state.
        changed |= self._expire(BarState.ERROR, "_error_since", ERROR_HOLD_S, now)
        changed |= self._expire(BarState.DONE, "_done_since", DONE_HOLD_S, now)

        self._apply_frame(force=changed)

        try:
            while True:
                action = self._ui_actions.get_nowait()
                try:
                    action(self._root)
                except Exception:
                    logger.exception("bar UI action failed")
        except queue.Empty:
            pass

        root = self._root
        if root is not None:
            # Rescheduling every tick also lets Ctrl+C at a console interrupt
            # mainloop promptly rather than blocking in Tk's C event loop.
            root.after(self._poll_ms, self._poll)

    def _expire(self, state: BarState, attr: str, hold_s: float, now: float) -> bool:
        if self._state is not state:
            setattr(self, attr, None)
            return False
        since = getattr(self, attr)
        if since is None:
            setattr(self, attr, now)
            return False
        if now - since >= hold_s:
            self._state, self._detail = BarState.IDLE, ""
            setattr(self, attr, None)
            return True
        return False

    def _collapsed(self) -> bool:
        """Whether to draw the resting dot rather than the capsule.

        Idle *and* unattended *and* with nothing to say. Any of a hovering
        pointer, an open menu, a coaching hint, or a non-idle state expands
        it -- which is what makes the dot feel like a product that gets out
        of the way rather than one that is hiding.
        """
        if self._state is not BarState.IDLE:
            return False
        if self._menu is not None:
            return False
        if self.hidden:
            return True
        return not (self._pointer_inside or self._hint)

    def _apply_frame(self, *, force: bool = False) -> None:
        canvas = self._canvas
        if canvas is None:
            return
        now = time.monotonic()
        collapsed = self._collapsed()
        badge_frame = _badge_frame(self._state, now)
        elapsed_s = (
            int(now - self._recording_started_at)
            if self._recording_started_at is not None
            else None
        )
        level = self._level if self._state is BarState.RECORDING else 0

        key = (
            collapsed, self._state, self._detail, self._hint,
            badge_frame, elapsed_s, level, self._hover,
        )
        if not force and key == self._render_key:
            return
        self._render_key = key

        if collapsed:
            image, regions = render_dot()
        else:
            image, regions = render_bar(
                self._state,
                self._detail,
                badge_frame=badge_frame,
                elapsed_s=elapsed_s,
                level=level,
                hint=self._hint if self._state is BarState.IDLE else "",
                hover=self._hover,
            )
        self._regions = regions

        width, height = image.size

        # Geometry first, image second. The other order lets Windows
        # composite one frame with the *new* image inside the *old* window
        # rectangle, which shows up as a visibly doubled, half-transparent
        # bar every time it changes width -- and it changes width on nearly
        # every state change.
        self._reposition(width, height)
        if int(canvas.cget("width")) != width or int(canvas.cget("height")) != height:
            canvas.config(width=width, height=height)

        photo = ImageTk.PhotoImage(image)
        self._photo = photo  # Tk drops images with no live reference
        if self._image_item is None:
            self._image_item = canvas.create_image(0, 0, anchor="nw", image=photo)
        else:
            canvas.itemconfig(self._image_item, image=photo)

    def _reposition(self, width: int, height: int) -> None:
        root = self._root
        if root is None:
            return
        area = work_area((root.winfo_screenwidth(), root.winfo_screenheight()))
        x, y = place(self._position, (width, height), area)
        wanted = f"{width}x{height}+{x}+{y}"
        if wanted == self._geometry:
            return
        self._geometry = wanted
        root.geometry(wanted)


class _Menu:
    """The light panel above the bar. One Toplevel, built and thrown away."""

    def __init__(
        self,
        parent: tk.Tk,
        items: tuple[MenuItem, ...],
        *,
        anchor: tuple[int, int, int],
        position: str,
        on_choose: Callable[[str], None],
    ) -> None:
        self._parent = parent
        self._items = items
        self._anchor = anchor
        self._position = position
        self._on_choose = on_choose
        self._top: tk.Toplevel | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._item_id: int | None = None
        self._canvas: tk.Canvas | None = None
        self._regions: tuple[HitRegion, ...] = ()
        self._hover = ""

    def open(self) -> None:
        key_hex = "#%02x%02x%02x" % _KEY_RGB
        top = tk.Toplevel(self._parent)
        self._top = top
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=key_hex)
        try:
            top.wm_attributes("-transparentcolor", key_hex)
        except tk.TclError:
            pass

        canvas = tk.Canvas(top, highlightthickness=0, bg=key_hex)
        canvas.pack()
        self._canvas = canvas
        canvas.bind("<Motion>", self._on_motion)
        canvas.bind("<Button-1>", self._on_click)
        canvas.bind("<Leave>", lambda _e: self._set_hover(""))

        # Any click elsewhere, or Escape, dismisses. Bound on the parent so a
        # click on the bar itself closes rather than reopening.
        self._parent.bind_all("<Escape>", self._on_escape, add="+")

        self._draw()

        bar_x, bar_y, bar_w = self._anchor
        width, height = _MENU_W, menu_height(self._items)
        x = bar_x + (bar_w - width) // 2
        # Above the bar when the bar is at the bottom, below it when at the
        # top -- the menu always opens toward the middle of the screen.
        y = bar_y - height - 8 if self._position.startswith("bottom-") else bar_y + 52
        top.geometry(f"{width}x{height}+{max(8, x)}+{max(8, y)}")

    def _draw(self) -> None:
        canvas = self._canvas
        if canvas is None:
            return
        image, regions = render_menu(self._items, hover=self._hover)
        self._regions = regions
        photo = ImageTk.PhotoImage(image)
        self._photo = photo
        if self._item_id is None:
            canvas.config(width=image.width, height=image.height)
            self._item_id = canvas.create_image(0, 0, anchor="nw", image=photo)
        else:
            canvas.itemconfig(self._item_id, image=photo)

    def _set_hover(self, action: str) -> None:
        if action == self._hover:
            return
        self._hover = action
        self._draw()

    def _on_motion(self, event: tk.Event) -> None:
        for region in self._regions:
            if region.contains(event.x, event.y):
                self._set_hover(region.action)
                return
        self._set_hover("")

    def _on_click(self, event: tk.Event) -> None:
        for region in self._regions:
            if region.contains(event.x, event.y):
                action = region.action
                self.close()
                self._on_choose(action)
                return
        self.close()
        self._on_choose("")

    def _on_escape(self, _event: tk.Event) -> None:
        self.close()
        self._on_choose("")

    def close(self) -> None:
        try:
            self._parent.unbind_all("<Escape>")
        except tk.TclError:
            pass
        top, self._top = self._top, None
        if top is not None:
            try:
                top.destroy()
            except tk.TclError:
                pass
        self._photo = None
        self._canvas = None
        self._item_id = None


class NullBar:
    """Headless stand-in when the bar is disabled."""

    def __init__(self) -> None:
        self._stop_event = threading.Event()

    @property
    def root(self) -> None:
        return None

    @property
    def hidden(self) -> bool:
        return True

    def set_state(self, state: BarState, detail: str = "") -> None:
        pass

    def set_level(self, level: int) -> None:
        pass

    def set_hint(self, hint: str) -> None:
        pass

    def hide_for(self, seconds: float) -> None:
        pass

    def run_on_ui_thread(self, action: Callable[[tk.Tk], None]) -> None:
        del action
        logger.info("cannot show a window headlessly ([overlay].enabled = false)")

    def run(self) -> None:
        self._stop_event.wait()

    def stop(self) -> None:
        self._stop_event.set()


__all__ = [
    "ACTION_LANGUAGE",
    "ACTION_MENU",
    "ACTION_MIC",
    "DEFAULT_MENU",
    "DONE_HOLD_S",
    "ERROR_HOLD_S",
    "ICONS",
    "SEPARATOR",
    "BarState",
    "HitRegion",
    "MenuItem",
    "NullBar",
    "SwayBar",
    "menu_height",
    "place",
    "render_bar",
    "render_dot",
    "render_menu",
    "work_area",
]

"""The Sway Bar: the floating surface the user actually lives with.

This replaces the always-on status pill. The change is not cosmetic -- it is
a different idea about what the product is. The pill was a 44px indicator
that sat on screen all day saying "still here". The bar is three tiers:

* **At rest**, a single ~14px dot. The product disappears when it is not
  being used, which is a statement of confidence: it does not need to remind
  you it exists.
* **Engaged**, a capsule carrying the state and the two controls anyone
  actually changes mid-dictation -- language and microphone.
* **Its menu**, opened by right-click or a click on the body, holding the
  rest: pause, hide, settings, device, history, and re-paste.

Each tier is one interaction from the last; nothing is buried and nothing is
crowded.

**One renderer spans all three.** :func:`render_bar` takes an ``expand``
value from 0 to 1 and the dot is simply the capsule at 0 -- same rounded
rect, collapsed to a circle, its fill interpolated from the brand indigo to
the capsule's dark surface. That is what makes the transition a morph rather
than a swap, and it means there is exactly one layout to reason about
instead of two that must be kept in visual agreement. :func:`render_dot` is
kept as the ``expand=0`` case for callers and tests that want it by name.

Rendering is PIL compositing into a colour-keyed tkinter window: every shape
is drawn at :data:`_SS`x and downsampled, because PIL's primitives are
hard-edged at native resolution and this is drawn at 14-44px where that looks
cheap. A WebView2 bar would have been easier to style and is disqualified for
one reason: a webview window activates when shown, paste targets the
*focused* window, and a bar that steals focus mid dictation sends the user's
words into the bar instead of their document.

Motion lives in :mod:`sway.motion` and is sampled, never stepped -- see that
module for why. The frame rate here is adaptive: ~60fps only while something
is actually moving or a dictation is live, and 150ms when the bar is a
resting dot nobody is pointing at. CLAUDE.md puts this repaint inside the
latency budget, so the resting cost matters more than the peak.

Threading is unchanged from the pill and is the part to be careful with. Tk
objects are touched only on the thread running :meth:`SwayBar.run`;
:meth:`set_state`, :meth:`set_level`, :meth:`set_hint`, :meth:`open_popover`
and :meth:`run_on_ui_thread` are the thread-safe doors in. Renders are
memoised on a key of everything that varies, so the bar repaints when
something changed and not otherwise.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageTk

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
from sway.motion import (
    BUTTON_FILL_S,
    COLLAPSE_S,
    DOT_HOVER_S,
    DOT_UNHOVER_S,
    LEVEL_S,
    PANEL_CLOSE_S,
    PANEL_OPEN_S,
    PRESS_DOWN_S,
    PRESS_UP_S,
    SHAKE_S,
    TOOLTIP_DELAY_S,
    WIDTH_S,
    Motion,
    Timer,
    ease_in_out_cubic,
    ease_out_back,
    ease_out_cubic,
    lerp,
    lerp_rgb,
    pulse,
    shake_offset,
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
    #: The agent is disarmed: the hotkey does nothing. Previously rendered as
    #: IDLE-with-a-detail, which meant a paused app and an armed one were the
    #: same dot on screen -- the single clearest "what state is it in?"
    #: failure in the product.
    PAUSED = "paused"
    #: Transient teaching. Not a mode: it expires on its own and never
    #: reports engine state, so it can never lie about what the app is doing.
    COACH = "coach"


class HintTone(StrEnum):
    """How loudly a hint is drawn. Coaching is not an error."""

    INFO = "info"
    WARN = "warn"


#: Actions a click on the bar can produce. The engine binds these; the bar
#: itself knows nothing about what they do.
ACTION_LANGUAGE = "language"
ACTION_MIC = "microphone"
ACTION_MENU = "menu"
ACTION_SETTINGS = "settings"
ACTION_PASTE = "paste"
ACTION_COPY = "copy"
ACTION_RESUME = "resume"
ACTION_PAUSE = "pause"
ACTION_HIDE = "hide"
ACTION_UNHIDE = "unhide"
ACTION_HISTORY = "history"

#: Error kind -> the one thing the user can do about it, as
#: ``(button label, action)``. Every action here is something the engine
#: already implements; nothing in this table invents a recovery path, because
#: a button that claims to retry and does not is worse than no button.
#:
#: ``rate_limited`` is deliberately absent. There is genuinely nothing to do
#: but wait, and the capsule already counts the wait down.
RECOVERY: dict[str, tuple[str, str]] = {
    "paste_blocked": ("Copy", ACTION_COPY),
    "clipboard": ("Paste", ACTION_PASTE),
    "audio": ("Fix", ACTION_MIC),
    "auth": ("Fix", ACTION_SETTINGS),
    "no_key": ("Fix", ACTION_SETTINGS),
    "network": ("Copy", ACTION_COPY),
    "timeout": ("Copy", ACTION_COPY),
    "no_speech": ("Mic", ACTION_MIC),
}

#: Recovery actions that are only offered when there is a transcript to act
#: on. Offering "Copy" with nothing to copy is how the old menu produced its
#: own error message.
_NEEDS_TRANSCRIPT = frozenset({ACTION_COPY, ACTION_PASTE})

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

#: Exact colour keyed out to transparent. Windows' -transparentcolor is a hard
#: match rather than alpha blending, so this is chosen close in tone to
#: SURFACE: anti-aliased edge pixels straddling the key then read as a soft
#: dark edge rather than a coloured fringe against an arbitrary desktop.
_KEY_RGB = (8, 8, 9)

_SS = 4

#: At rest the bar is an empty outlined tray: a stroke with nothing inside
#: it, so the desktop shows straight through. It is deliberately the least
#: interesting thing on screen -- a filled dot is a coloured pixel competing
#: for attention all day, and there is nothing at rest worth competing for.
#: Everything the product has to say arrives on hover or on a dictation.
_REST_W = 48
_REST_H = 16
#: Room around the tray for its outer stroke and for the hover swell, so
#: neither is clipped by the window edge.
_REST_PAD = 3
#: How far the tray swells under the pointer, in pixels per side. Small on
#: purpose: this is an acknowledgement, not an invitation.
_REST_GROW = 1.5
#: How opaque the resting tray's interior is. Not a look, a hit target:
#: see-through pixels on a colour-keyed window pass the mouse through,
#: so a truly empty tray cannot be clicked. Low enough to read as empty
#: on a dark desktop, high enough that Windows hit-tests it.
_REST_SCRIM = 0.30

#: The resting window's outer size, and the width the morph starts from.
_DOT_BOX = _REST_W + 2 * _REST_PAD

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

#: The inline recovery pill on an error or a paused capsule.
_PILL_H = 24
_PILL_PAD_X = 11
_PILL_FONT_SIZE = 12

#: A keycap chip inside a hint, e.g. ``[[Right Ctrl]]``.
_KEY_CHIP_H = 19
_KEY_CHIP_PAD_X = 7
_KEY_CHIP_GAP = 4
_KEY_CHIP_FONT_SIZE = 11
_KEY_CHIP_RADIUS = 6

#: How far the bar sits from the screen edge it is docked to.
_MARGIN = 14

_FONT_SEMIBOLD = ("segoeuisb.ttf", "seguisb.ttf", "segoeuib.ttf", "segoeui.ttf")
_FONT_REGULAR = ("segoeui.ttf", "seguisb.ttf")

#: Content stops being drawn below this much expansion. Below it there is not
#: enough capsule for a 34px avatar to sit in without overflowing, and fading
#: text in from zero width reads as a smear rather than as a reveal.
_CONTENT_GATE = 0.30
#: Above this, the layout is close enough to final that controls can be drawn
#: and hit-tested without the pointer chasing a moving target.
_SETTLED = 0.995


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
    BarState.PAUSED: _Style("Paused", ACCENT_IDLE),
    BarState.COACH: _Style("", ACCENT_RECORDING),
}

#: States that draw an accent ring. Only recording and error do: an ambient
#: widget on screen all day must not have four colours competing, and these
#: are the two the user needs to notice without looking.
_RINGED = frozenset({BarState.RECORDING, BarState.ERROR})

#: States that expose the control row. During a dictation the buttons would
#: be a target the user is not aiming at, and the bar is busy saying
#: something more useful.
_SHOWS_CONTROLS = frozenset({BarState.IDLE})

#: States quiet enough to shrink back to a dot. Everything else holds the
#: capsule open because it has something to say.
_COLLAPSIBLE = frozenset({BarState.IDLE, BarState.PAUSED})

ERROR_HOLD_S = 4.0
DONE_HOLD_S = 1.6
#: Coaching is read in a glance and then in the way. Shorter than an error,
#: longer than a confirmation.
COACH_HOLD_S = 2.8

#: How long the pointer must rest on the dot before the capsule opens, and
#: how long it must be gone before it closes again. Both exist because
#: without them a pointer merely *crossing* the bar on its way somewhere else
#: makes it flash open and shut -- the single most obvious flicker in the
#: previous build.
EXPAND_DWELL_S = 0.40
COLLAPSE_DWELL_S = 0.25

#: How long after a panel opens before an outside click can dismiss it. Long
#: enough to cover the open animation and a latched button state, short
#: enough that a deliberate click straight afterwards still lands.
_DISMISS_GRACE_S = 0.25


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


@lru_cache(maxsize=32)
def _load_font(
    names: tuple[str, ...], size: int
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    fonts_dir = os.environ.get("WINDIR", r"C:\Windows") + r"\Fonts"
    for name in names:
        try:
            return ImageFont.truetype(f"{fonts_dir}\\{name}", size * _SS)
        except OSError:
            continue
    logger.debug("no system font found for the bar; falling back to PIL's default")
    return ImageFont.load_default()


def _font(size: int, *, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return _load_font(_FONT_SEMIBOLD if bold else _FONT_REGULAR, size)


#: Script -> the Windows font that actually has those glyphs, tried in order.
#: PIL has no font fallback of its own: a character the chosen face does not
#: cover renders as .notdef, a hollow box. The language popover lists
#: languages by their own name, so without this every non-Latin entry -- the
#: ones a user most needs to recognise -- is a row of tofu.
_SCRIPT_FONTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("\u0900-\u097f", ("Nirmala.ttc", "mangal.ttf")),  # Devanagari
    ("\u3040-\u30ff", ("YuGothR.ttc", "meiryo.ttc", "msgothic.ttc")),  # kana
    ("\u4e00-\u9fff", ("msyh.ttc", "YuGothR.ttc", "simsun.ttc")),  # Han
    ("\uac00-\ud7af", ("malgun.ttf", "gulim.ttc")),  # Hangul
    ("\u0400-\u04ff", ("segoeui.ttf",)),  # Cyrillic: Segoe covers it
    ("\u0600-\u06ff", ("segoeui.ttf", "tahoma.ttf")),  # Arabic
)

_SCRIPT_PATTERNS = tuple(
    (re.compile(f"[{ranges}]"), names) for ranges, names in _SCRIPT_FONTS
)


def _font_for(text: str, size: int, *, bold: bool = False):
    """A face that can actually draw ``text``.

    Falls back to the default UI font, which is right for Latin and is also
    the least-bad answer for a script no table entry covers.
    """
    for pattern, names in _SCRIPT_PATTERNS:
        if pattern.search(text):
            return _load_font(names, size)
    return _font(size, bold=bold)


def _badge_frame(state: BarState, now_s: float) -> int:
    """Which animation frame to draw at wall-clock ``now_s``.

    A pure function of time, so the bar needs no animation timer of its own --
    it recomputes this per poll tick and repaints only when the value changed.
    Kept for the processing arc, whose motion genuinely is stepwise. The
    recording pulse no longer uses it: a two-frame alpha flip reads as a
    blink, and :func:`sway.motion.pulse` gives it a continuous breath over the
    same period.
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


def _alpha(colour: tuple[int, int, int], opacity: float) -> tuple[int, int, int, int]:
    """RGB plus a clamped 0-1 opacity, as PIL's RGBA."""
    return (*colour, max(0, min(255, round(255 * opacity))))


def _safe_radius(box: tuple[float, float, float, float], radius: float) -> float:
    """A corner radius PIL can actually draw in ``box``.

    ``rounded_rectangle`` does not clamp: a radius larger than half the
    shorter side produces overlapping arcs, which show up mid-morph as a
    bright straight line across the capsule where the sheen should have been
    a soft cap. Every radius in this module is derived from an animated
    height, so every one of them has to come through here.
    """
    limit = min(box[2] - box[0], box[3] - box[1]) / 2
    return max(0.0, min(radius, limit))


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def recovery_for(
    state: BarState, kind: str, *, can_paste: bool = True
) -> tuple[str, str] | None:
    """The inline action offered on the capsule, or ``None``.

    Pure, so the whole "does this failure have a way out?" question is
    testable without a screen. Two rules, and both are about not lying:

    * A paused bar offers Resume, because that is the only thing a paused bar
      is for.
    * A recovery that needs a transcript is withheld when there is none, so
      the button can never produce the "nothing to copy yet" error that the
      old menu produced from its own controls.
    """
    if state is BarState.PAUSED:
        return ("Resume", ACTION_RESUME)
    if state not in (BarState.ERROR, BarState.DONE):
        return None
    found = RECOVERY.get(kind)
    if found is None:
        return None
    if found[1] in _NEEDS_TRANSCRIPT and not can_paste:
        return None
    return found


# ---------------------------------------------------------------------------
# Hints with keycaps
# ---------------------------------------------------------------------------
#
# The reference's best idea is that the hotkey is *shown as a key*, in amber,
# at the moment the user needs to press it -- not spelled out in a sentence
# they have to parse. `[[Right Ctrl]]` in any hint or detail renders as a
# keycap chip. Amber is already the token for "this is your hotkey" (see
# branding.py), so the chip spends no new colour.

_RICH_RE = re.compile(r"\[\[(.+?)\]\]")


def rich_segments(text: str) -> tuple[tuple[bool, str], ...]:
    """Split ``text`` into ``(is_keycap, run)`` pairs. Empty runs dropped."""
    if not text:
        return ()
    parts: list[tuple[bool, str]] = []
    cursor = 0
    for match in _RICH_RE.finditer(text):
        if match.start() > cursor:
            parts.append((False, text[cursor : match.start()]))
        parts.append((True, match.group(1)))
        cursor = match.end()
    if cursor < len(text):
        parts.append((False, text[cursor:]))
    return tuple(part for part in parts if part[1])


def _chip_width(label: str) -> int:
    font = _font(_KEY_CHIP_FONT_SIZE)
    return round(font.getlength(label) / _SS) + _KEY_CHIP_PAD_X * 2


def measure_rich(text: str) -> int:
    """Width of a hint in unscaled pixels, keycaps included."""
    if not text:
        return 0
    total = 0.0
    for is_key, run in rich_segments(text):
        if is_key:
            total += _chip_width(run) + _KEY_CHIP_GAP * 2
        else:
            font = _font_for(run, _FONT_SIZE, bold=True)
            total += font.getlength(run) / _SS
    return round(total)


def _draw_rich(
    layer: Image.Image,
    text: str,
    *,
    x: int,
    centre_y: int,
    ink: tuple[int, int, int],
    opacity: float,
) -> None:
    """Draw a hint at ``x``, vertically centred on ``centre_y`` (both in
    unscaled pixels), rendering ``[[...]]`` runs as keycap chips."""
    draw = ImageDraw.Draw(layer)
    chip_font = _font(_KEY_CHIP_FONT_SIZE)
    cursor = float(x)
    for is_key, run in rich_segments(text):
        if is_key:
            cursor += _KEY_CHIP_GAP
            width = _chip_width(run)
            top = centre_y - _KEY_CHIP_H / 2
            box = (
                cursor * _SS,
                top * _SS,
                (cursor + width) * _SS,
                (top + _KEY_CHIP_H) * _SS,
            )
            draw.rounded_rectangle(
                box,
                radius=_KEY_CHIP_RADIUS * _SS,
                fill=_alpha(SURFACE_RAISED, opacity),
                outline=_alpha(ACCENT_RECORDING, opacity * 0.45),
                width=max(1, round(_SS * 0.75)),
            )
            bbox = draw.textbbox((0, 0), run, font=chip_font)
            draw.text(
                (
                    box[0] + _KEY_CHIP_PAD_X * _SS,
                    (box[1] + box[3]) / 2 - (bbox[3] - bbox[1]) / 2 - bbox[1],
                ),
                run,
                font=chip_font,
                fill=_alpha(ACCENT_RECORDING, opacity),
            )
            cursor += width + _KEY_CHIP_GAP
        else:
            # Measured and drawn with the same face, or a language name in a
            # script Segoe does not cover would be laid out for one width and
            # painted at another.
            font = _font_for(run, _FONT_SIZE, bold=True)
            bbox = draw.textbbox((0, 0), run, font=font)
            draw.text(
                (cursor * _SS, centre_y * _SS - (bbox[3] - bbox[1]) / 2 - bbox[1]),
                run,
                font=font,
                fill=_alpha(ink, opacity),
            )
            cursor += font.getlength(run) / _SS


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
    # The teeth are wedges on the rim, not spokes radiating from it. The
    # previous version drew eight lines from 0.66r to 0.98r, which at 17px is
    # a ring with long thin rays -- a sun. Keeping each tooth short (0.78r to
    # 1.0r) and as wide as it is long is what makes it read as machined.
    tooth = max(1, round(width * 2.2))
    for index in range(8):
        angle = index * math.pi / 4
        draw.line(
            [
                (cx + math.cos(angle) * r * 0.78, cy + math.sin(angle) * r * 0.78),
                (cx + math.cos(angle) * r, cy + math.sin(angle) * r),
            ],
            fill=colour, width=tooth,
        )
    # A thick rim over the tooth roots, so the teeth appear to grow out of
    # the body rather than to be stuck onto it.
    draw.ellipse(
        (cx - r * 0.80, cy - r * 0.80, cx + r * 0.80, cy + r * 0.80),
        outline=colour, width=max(1, round(width * 1.6)),
    )
    # The bore. Without it the rim plus teeth reads as a flower.
    draw.ellipse(
        (cx - r * 0.26, cy - r * 0.26, cx + r * 0.26, cy + r * 0.26),
        outline=colour, width=width,
    )


def _draw_copy(draw: ImageDraw.ImageDraw, box, colour, width) -> None:
    """Two offset sheets -- distinct from the clipboard used for paste, so
    two adjacent menu rows are not carrying the same glyph.

    The back sheet is two strokes rather than a full outline, and neither
    sheet is filled. An earlier version tinted the front one with a
    translucent fill, which does not work here: icons are drawn straight onto
    an already-opaque canvas, and ``ImageDraw``'s ``fill`` *replaces* pixels
    instead of blending -- so a 40/255 tint punched the panel out to near
    transparency and rendered as a dark block in the middle of a light menu.
    Drawing the occlusion instead of faking it with alpha avoids the whole
    question.
    """
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    # Back sheet: the top and left edges only, so the front sheet reads as
    # lying on top of it without needing to paint over anything.
    draw.line([(x0 + w * 0.16, y0), (x1 - w * 0.26, y0)], fill=colour, width=width)
    draw.line([(x0, y0 + h * 0.16), (x0, y1 - h * 0.26)], fill=colour, width=width)
    draw.arc(
        (x0, y0, x0 + w * 0.32, y0 + h * 0.32),
        start=180, end=270, fill=colour, width=width,
    )
    draw.rounded_rectangle(
        (x0 + w * 0.26, y0 + h * 0.26, x1, y1),
        radius=w * 0.16,
        outline=colour,
        width=width,
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


def _draw_check(draw: ImageDraw.ImageDraw, box, colour, width) -> None:
    """The selected row in a popover. Carries no box or circle: a tick alone
    reads as "this one", where a tick in a box reads as a checkbox the user
    could clear, which these rows are not."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    draw.line(
        [
            (x0 + w * 0.14, y0 + h * 0.52),
            (x0 + w * 0.40, y0 + h * 0.76),
            (x0 + w * 0.88, y0 + h * 0.24),
        ],
        fill=colour,
        width=width,
        joint="curve",
    )


def _draw_pause(draw: ImageDraw.ImageDraw, box, colour, width) -> None:
    del width
    x0, y0, x1, y1 = box
    w = x1 - x0
    bar_w = w * 0.24
    draw.rounded_rectangle((x0 + w * 0.16, y0, x0 + w * 0.16 + bar_w, y1),
                           radius=bar_w * 0.35, fill=colour)
    draw.rounded_rectangle((x1 - w * 0.16 - bar_w, y0, x1 - w * 0.16, y1),
                           radius=bar_w * 0.35, fill=colour)


def _draw_play(draw: ImageDraw.ImageDraw, box, colour, width) -> None:
    del width
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    draw.polygon(
        [(x0 + w * 0.20, y0), (x1 - w * 0.10, y0 + h / 2), (x0 + w * 0.20, y1)],
        fill=colour,
    )


def _draw_eye(draw: ImageDraw.ImageDraw, box, colour, width) -> None:
    """Un-hide. An eye rather than a crossed-out clock, because the row it
    replaces is about visibility, not about time."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    cy = (y0 + y1) / 2
    draw.arc((x0, cy - h * 0.42, x1, cy + h * 0.42), start=180, end=360,
             fill=colour, width=width)
    draw.arc((x0, cy - h * 0.42, x1, cy + h * 0.42), start=0, end=180,
             fill=colour, width=width)
    r = w * 0.17
    draw.ellipse((x0 + w / 2 - r, cy - r, x0 + w / 2 + r, cy + r),
                 outline=colour, width=width)


ICONS: dict[str, Callable[..., None]] = {
    "globe": _draw_globe,
    "mic": _draw_mic,
    "chevron": _draw_chevron,
    "clock": _draw_clock,
    "gear": _draw_gear,
    "list": _draw_list,
    "paste": _draw_paste,
    "copy": _draw_copy,
    "check": _draw_check,
    "pause": _draw_pause,
    "play": _draw_play,
    "eye": _draw_eye,
}


# ---------------------------------------------------------------------------
# The bar itself
# ---------------------------------------------------------------------------


def natural_width(
    state: BarState,
    detail: str,
    *,
    elapsed_s: int | None = None,
    hint: str = "",
    kind: str = "",
    can_paste: bool = True,
) -> int:
    """The capsule's settled width for this content.

    Split out and pure because it is the *target* of the width tween, and a
    target that shifts while it is being animated toward produces a bar that
    never quite arrives.
    """
    text = _bar_text(state, detail, hint=hint, elapsed_s=elapsed_s)
    width = _PAD_LEFT + _AVATAR + _GAP + measure_rich(text)
    if state is BarState.RECORDING:
        width += _GAP + _LEVEL_W
    if state in _SHOWS_CONTROLS:
        width += _GAP + _BUTTON_D * 2 + _BUTTON_GAP
    found = recovery_for(state, kind, can_paste=can_paste)
    if found is not None:
        width += _GAP + _pill_width(found[0])
    return max(_MIN_WIDTH, width + _PAD_RIGHT)


def _bar_text(
    state: BarState, detail: str, *, hint: str = "", elapsed_s: int | None = None
) -> str:
    style = _STYLES[state]
    text = hint or detail or style.label
    if state is BarState.RECORDING and not detail and elapsed_s is not None:
        text = f"{style.label} · {_elapsed_label(elapsed_s)}"
    return text


def _pill_width(label: str) -> int:
    return round(_font(_PILL_FONT_SIZE).getlength(label) / _SS) + _PILL_PAD_X * 2


def render_dot(
    *,
    accent: tuple[int, int, int] = ACCENT_BRAND,
    grow: float = 0.0,
    ring: float = 0.0,
    opacity: float = 1.0,
    hollow: bool = False,
) -> tuple[Image.Image, tuple[HitRegion, ...]]:
    """The resting state: one small dot, and nothing else.

    The ``expand=0`` case of :func:`render_bar`, kept as its own name because
    that is what callers and tests mean. The whole box is a hit region so
    hovering anywhere near it expands the bar -- a 14px target is fine to
    *see* and unpleasant to *hit*.
    """
    return render_bar(
        BarState.IDLE,
        "",
        expand=0.0,
        dot_accent=accent,
        dot_grow=grow,
        dot_ring=ring,
        opacity=opacity,
        hollow=hollow,
    )


def render_bar(
    state: BarState,
    detail: str,
    *,
    badge_frame: int = 0,
    elapsed_s: int | None = None,
    level: int = 0,
    hint: str = "",
    hover: str = "",
    press: str = "",
    kind: str = "",
    tone: HintTone = HintTone.INFO,
    can_paste: bool = True,
    expand: float = 1.0,
    width: int | None = None,
    hover_t: float = 1.0,
    press_t: float = 0.0,
    pulse_t: float | None = None,
    level_t: tuple[float, float, float] | None = None,
    dot_accent: tuple[int, int, int] | None = None,
    dot_grow: float = 0.0,
    dot_ring: float = 0.0,
    opacity: float = 1.0,
    hollow: bool = False,
) -> tuple[Image.Image, tuple[HitRegion, ...]]:
    """One full bar frame, plus where it can be clicked.

    ``expand`` runs 0 (the resting dot) to 1 (the settled capsule) and every
    value between is a real frame of the morph, so there is one layout rather
    than two that have to be kept looking like each other.

    ``hint`` is coaching text shown instead of the state label -- the "next
    step" idea, which teaches in place at the moment of need rather than in a
    wizard that takes the screen. ``[[Right Ctrl]]`` inside it renders as a
    keycap. ``hover`` and ``press`` are the actions under the pointer and
    under a held mouse button; the ``*_t`` values are the sampled animation
    positions for them, defaulted so a static render still looks correct.
    """
    style = _STYLES[state]
    expand = _clamp01(expand)
    opacity = _clamp01(opacity)
    text = _bar_text(state, detail, hint=hint, elapsed_s=elapsed_s)
    recovery = recovery_for(state, kind, can_paste=can_paste)

    natural = natural_width(
        state, detail, elapsed_s=elapsed_s, hint=hint, kind=kind, can_paste=can_paste
    )
    if width is None:
        width = round(lerp(_REST_W + 2 * _REST_PAD, natural, expand))
    width = max(1, int(width))
    height = max(1, round(lerp(_REST_H + 2 * _REST_PAD, _HEIGHT, expand)))

    bw, bh = width * _SS, height * _SS
    canvas = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    regions: list[HitRegion] = []

    # -- body: the resting tray and the capsule are the same rounded rect.
    #
    # At expand 0 it is an empty 48x16 outline; at 1 it is the full capsule
    # in the dark surface. Only the size and the fill's opacity change, so
    # the two tiers are one object growing rather than two widgets swapping.
    accent = dot_accent if dot_accent is not None else _dot_accent(state, style)
    inset = max(0.0, lerp(_REST_PAD, 0.0, expand) - dot_grow * (1.0 - expand))
    body_box = (
        inset * _SS,
        inset * _SS,
        bw - 1 - inset * _SS,
        bh - 1 - inset * _SS,
    )
    radius = max(1.0, (body_box[3] - body_box[1]) / 2)
    # The surface fades in as the tray grows. Held at zero for the first of
    # the travel so the resting state stays genuinely see-through rather than
    # a very dark rectangle sitting on the desktop.
    fill_t = _clamp01((expand - 0.05) / 0.45)

    body = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    body_draw = ImageDraw.Draw(body)

    def _capsule(pad: float, **kwargs) -> None:
        """The body shape, optionally grown by ``pad``.

        Everything concentric with the body -- halo, hover ring, the fill
        itself -- goes through here. Drawing any of them as an *ellipse*
        instead looks identical at expand 0, where the body is a circle, and
        falls apart everywhere in between: a wide flat ellipse behind a
        rounded rect shows as dark wings at the sides and bare corners at the
        top. That was the one visible defect in the morph.
        """
        box = (
            body_box[0] - pad,
            body_box[1] - pad,
            body_box[2] + pad,
            body_box[3] + pad,
        )
        body_draw.rounded_rectangle(box, radius=_safe_radius(box, radius + pad), **kwargs)

    if fill_t < 0.999:
        # A scrim across the resting tray, and it is load-bearing rather than
        # decorative: Windows hit-tests a colour-keyed window by its
        # *transparency*, so a pixel that keys out does not merely look
        # see-through, it passes the mouse straight through to whatever is
        # behind. An outline with a genuinely empty middle therefore has a
        # hit target the width of its own stroke -- hovering or clicking the
        # centre of the bar lands in the app underneath it, which makes the
        # product's one always-present control almost impossible to use.
        #
        # The scrim is opaque enough to hit-test and dark enough to read as
        # nothing on a dark desktop. On a light one it is a faint dark tray,
        # which is the honest cost of the tray being clickable at all.
        _capsule(0.0, fill=_alpha(SURFACE, _REST_SCRIM * opacity))
    if fill_t > 0.01:
        # The surface *wipes* in from the centre line rather than fading in,
        # and this is forced by the window, not a stylistic choice. Windows'
        # -transparentcolor is an exact match, so a partially transparent
        # pixel cannot be partially transparent on screen -- it is composited
        # onto the key colour and shown as an opaque near-black. A fading
        # fill therefore reads as a solid dark blob growing on a light
        # desktop, which is the opposite of the point. An opaque shape whose
        # *area* grows has no partial alpha anywhere and looks like the tray
        # filling up.
        grow = (1.0 - fill_t) * (body_box[3] - body_box[1]) / 2
        _capsule(-grow, fill=_alpha(SURFACE, opacity))
    if fill_t < 0.999:
        # The resting stroke, in two tones: a dark one just outside a light
        # one. A single hairline can only be legible against backgrounds it
        # contrasts with, and this floats over a desktop we do not control --
        # the pair reads on both a white document and a dark editor without
        # needing a fill behind it.
        stroke = (1.0 - fill_t) * opacity
        # Paused dims the whole tray rather than changing its shape. At this
        # size there is no room for a second silhouette, and the state is
        # spelled out in words the moment the pointer arrives.
        if hollow:
            stroke *= 0.45
        _capsule(
            1.2 * _SS,
            outline=_alpha(SURFACE, stroke * 0.55),
            width=max(1, round(1.4 * _SS)),
        )
        _capsule(
            0.0,
            outline=_alpha(_rest_stroke(state, accent), stroke * (0.85 + 0.15 * dot_ring)),
            width=max(1, round(1.3 * _SS)),
        )
    if expand > 0.55:
        # Sheen and hairline only once there is a capsule to put them on. On
        # a nearly-circular body the sheen's rounded rect degenerates into a
        # bright line across the middle of the dot.
        #
        # Both go on their own layer and are *composited*. Drawing them
        # straight onto the body with ImageDraw would not blend: `fill=`
        # replaces the pixel, so a 12/255 white wash replaced the opaque
        # surface with an almost fully transparent one. The visible result
        # was a capsule whose top half showed the desktop through a faint
        # white haze, with a one-pixel fully transparent slit across it at
        # the sheen's lower edge -- which on Windows keys out to a hole
        # straight through the bar.
        sheen = _clamp01((expand - 0.55) / 0.45) * opacity
        gloss = Image.new("RGBA", (bw, bh), (255, 255, 255, 0))
        sheen_box = (
            body_box[0] + 2 * _SS,
            body_box[1] + 2 * _SS,
            body_box[2] - 2 * _SS,
            (body_box[1] + body_box[3]) / 2,
        )
        # A vertical falloff, not a flat block. A rounded rect of uniform
        # alpha over the top half gives the capsule a hard horizontal edge
        # across its middle -- a band, which is exactly what a highlight is
        # not. Brightest along the top edge, gone by the middle.
        shape = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(shape).rounded_rectangle(
            sheen_box, radius=_safe_radius(sheen_box, radius - 2 * _SS), fill=255
        )
        top, bottom = round(sheen_box[1]), round(sheen_box[3])
        span = max(1, bottom - top)
        ramp = Image.new("L", (1, bh), 0)
        ramp_px = ramp.load()
        for y in range(top, min(bh, bottom + 1)):
            ramp_px[0, y] = round(255 * (1.0 - (y - top) / span) ** 1.6)
        gloss.putalpha(
            ImageChops.multiply(shape, ramp.resize((bw, bh))).point(
                lambda value: round(value * 18 * sheen / 255)
            )
        )
        ImageDraw.Draw(gloss).rounded_rectangle(
            body_box,
            radius=_safe_radius(body_box, radius),
            outline=(255, 255, 255, round(26 * sheen)),
            width=_SS,
        )
        # Clipped to the body, so the hairline cannot spill outside the
        # capsule's own silhouette and leave a bright fringe on the desktop.
        mask = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            body_box, radius=_safe_radius(body_box, radius), fill=255
        )
        body.alpha_composite(Image.composite(gloss, Image.new("RGBA", (bw, bh), (0, 0, 0, 0)), mask))
    canvas.alpha_composite(body)

    # Two gates, not one. The avatar is the leftmost thing and has room
    # early, so it grows with the capsule. Everything else -- the label and
    # the whole right-hand group -- waits until the layout is nearly final
    # and then arrives together. Fading the label in first meant watching it
    # be clipped by controls that had not appeared yet.
    content = _clamp01((expand - _CONTENT_GATE) / (1.0 - _CONTENT_GATE)) * opacity
    detail_t = _clamp01((expand - 0.62) / 0.38) * opacity
    settled = expand >= _SETTLED

    if content > 0.01:
        _draw_avatar(canvas, state, style, height, bw, bh, content, badge_frame,
                     pulse_t=pulse_t)

    cursor = width - _PAD_RIGHT

    # -- the inline recovery action, right-anchored so it is furthest from
    # the avatar and closest to the pointer's resting place.
    if recovery is not None and detail_t > 0.01:
        label, action = recovery
        pill_w = _pill_width(label)
        cursor -= pill_w
        _draw_pill(
            canvas,
            x=cursor,
            y=(height - _PILL_H) // 2,
            width=pill_w,
            label=label,
            accent=style.accent,
            hovered=hover == action,
            press_t=press_t if press == action else 0.0,
            opacity=detail_t,
        )
        if settled:
            regions.append(
                HitRegion(
                    cursor,
                    (height - _PILL_H) // 2,
                    cursor + pill_w,
                    (height + _PILL_H) // 2,
                    action,
                )
            )
        cursor -= _GAP

    # -- controls, right to left so the rightmost sits against the padding
    if state in _SHOWS_CONTROLS and detail_t > 0.01:
        for action, glyph in ((ACTION_MIC, "mic"), (ACTION_LANGUAGE, "globe")):
            cursor -= _BUTTON_D
            active = hover == action
            _draw_button(
                canvas,
                x=cursor,
                y=(height - _BUTTON_D) // 2,
                glyph=glyph,
                fill_t=hover_t if active else 0.0,
                press_t=press_t if press == action else 0.0,
                opacity=detail_t,
            )
            if settled:
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
    # during the seconds the latency budget is being spent. The heights are
    # eased between buckets so the change reads as level rather than as a
    # glitch.
    if state is BarState.RECORDING and detail_t > 0.01:
        cursor -= _LEVEL_W
        _draw_level(canvas, x=cursor, height=height, bw=bw, bh=bh,
                    accent=style.accent, level=level, level_t=level_t,
                    opacity=detail_t)
        cursor -= _GAP

    # -- label, clipped to whatever room the right-hand group has left it
    if text and detail_t > 0.01:
        ink = ACCENT_RECORDING if tone is HintTone.WARN and hint else INK
        text_x = _PAD_LEFT + _AVATAR + _GAP
        clip_right = max(text_x, cursor)
        layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        _draw_rich(layer, text, x=text_x, centre_y=height / 2, ink=ink, opacity=detail_t)
        if clip_right < width:
            blank = Image.new("RGBA", (bw - clip_right * _SS, bh), (0, 0, 0, 0))
            layer.paste(blank, (clip_right * _SS, 0))
        canvas.alpha_composite(layer)

    # The whole capsule opens the menu on click; the controls above take
    # precedence because they are checked first.
    regions.append(HitRegion(0, 0, width, height, ACTION_MENU))

    return _finish(canvas, width, height), tuple(regions)


def _dot_accent(state: BarState, style: _Style) -> tuple[int, int, int]:
    """The accent a collapsed bar carries.

    Idle is the brand. Everything else borrows the state's own accent, so a
    capsule collapsing out of an error or a recording does not flick through
    indigo on the way down.
    """
    if state is BarState.IDLE:
        return ACCENT_BRAND
    return style.accent


def _rest_stroke(state: BarState, accent: tuple[int, int, int]) -> tuple[int, int, int]:
    """The colour of the resting tray's outline.

    Neutral ink when idle or paused: at rest the bar is chrome, and chrome
    that carries the brand colour all day is an advertisement rather than an
    interface. A tray still collapsing out of a recording or an error keeps
    that state's accent, so the last thing the user saw fades out rather than
    switching colour on its way down.
    """
    if state in (BarState.IDLE, BarState.PAUSED):
        return INK
    return accent


def _draw_avatar(
    canvas: Image.Image,
    state: BarState,
    style: _Style,
    height: int,
    bw: int,
    bh: int,
    opacity: float,
    badge_frame: int,
    *,
    pulse_t: float | None,
) -> None:
    """The mark on a raised disc, ringed in the state's accent.

    Scaled with the capsule's height so it grows with the morph rather than
    popping in at full size. Sizes are quantised to even pixels to keep
    :func:`sway.branding.mark_image`'s cache to a handful of entries across a
    transition instead of one per frame.
    """
    scale = height / _HEIGHT
    avatar_d = max(2, 2 * round(_AVATAR * scale / 2)) * _SS
    avatar_cx = round(_PAD_LEFT * scale) * _SS + avatar_d // 2
    avatar_cy = bh // 2
    box = (
        avatar_cx - avatar_d // 2,
        avatar_cy - avatar_d // 2,
        avatar_cx + avatar_d // 2,
        avatar_cy + avatar_d // 2,
    )

    layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    draw.ellipse(box, fill=_alpha(SURFACE_RAISED, opacity))
    if state in _RINGED:
        draw.ellipse(box, outline=_alpha(style.accent, opacity), width=2 * _SS)
    canvas.alpha_composite(layer)

    mark_size = max(1, round(avatar_d * 0.56))
    mark = mark_image(mark_size, color=_alpha(INK, opacity))
    canvas.alpha_composite(
        mark, (avatar_cx - mark.width // 2, avatar_cy - mark.height // 2)
    )

    # Only once the layout is nearly settled: a badge drawn on a half-grown
    # avatar overhangs the capsule edge.
    if opacity > 0.9:
        _draw_badge(canvas, state, style, badge_frame, box, bw, bh, pulse_t=pulse_t)


def _draw_badge(
    canvas, state, style, badge_frame, avatar_box, bw, bh, *, pulse_t=None
) -> None:
    """The state glyph overlapping the avatar's corner.

    Shape varies as well as colour -- a breathing dot, a rotating arc, a
    check, a warning triangle -- so state is never carried by colour alone.
    """
    if state in (BarState.IDLE, BarState.PAUSED, BarState.COACH):
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
        # A continuous breath rather than the old two-frame flip, which at
        # 500ms read as an alarm blink.
        breath = pulse_t if pulse_t is not None else (1.0 if badge_frame == 0 else 0.0)
        alpha = round(lerp(120, 255, _clamp01(breath)))
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


def _draw_button(
    canvas: Image.Image,
    *,
    x: int,
    y: int,
    glyph: str,
    fill_t: float = 0.0,
    press_t: float = 0.0,
    opacity: float = 1.0,
) -> None:
    """One circular control on the bar.

    ``fill_t`` eases the disc from the raised surface to the brand accent on
    hover, and ``press_t`` shrinks it while the mouse button is down. The
    shrink is the whole point: without it, a click on a floating widget that
    never takes focus has no acknowledgement at all until its result arrives.
    """
    d = _BUTTON_D * _SS
    shrink = press_t * 2.0 * _SS
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    box = (
        x * _SS + shrink,
        y * _SS + shrink,
        x * _SS + d - shrink,
        y * _SS + d - shrink,
    )
    fill = lerp_rgb(SURFACE_RAISED, ACCENT_BRAND, _clamp01(fill_t))
    draw.ellipse(box, fill=_alpha(fill, opacity * lerp(1.0, 0.92, press_t)))
    inset = (box[2] - box[0]) * 0.28
    ICONS[glyph](
        draw,
        (box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset),
        _alpha(INK, opacity),
        max(1, round(1.4 * _SS)),
    )
    canvas.alpha_composite(layer)


def _draw_pill(
    canvas: Image.Image,
    *,
    x: int,
    y: int,
    width: int,
    label: str,
    accent: tuple[int, int, int],
    hovered: bool,
    press_t: float,
    opacity: float,
) -> None:
    """The inline recovery action. A labelled pill, not an icon.

    Icons are ambiguous at the exact moment the user is least willing to
    guess. "Copy" and "Fix" are two syllables and unambiguous.
    """
    shrink = press_t * 1.5 * _SS
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    box = (
        x * _SS + shrink,
        y * _SS + shrink,
        (x + width) * _SS - shrink,
        (y + _PILL_H) * _SS - shrink,
    )
    fill = lerp_rgb(SURFACE_RAISED, accent, 0.85 if hovered else 0.0)
    draw.rounded_rectangle(
        box,
        radius=(box[3] - box[1]) / 2,
        fill=_alpha(fill, opacity),
        outline=_alpha(accent, opacity * (0.0 if hovered else 0.55)),
        width=max(1, round(_SS * 0.75)),
    )
    font = _font(_PILL_FONT_SIZE)
    bbox = draw.textbbox((0, 0), label, font=font)
    draw.text(
        (
            (box[0] + box[2]) / 2 - (bbox[2] - bbox[0]) / 2 - bbox[0],
            (box[1] + box[3]) / 2 - (bbox[3] - bbox[1]) / 2 - bbox[1],
        ),
        label,
        font=font,
        fill=_alpha(INK, opacity),
    )
    canvas.alpha_composite(layer)


def _draw_level(
    canvas: Image.Image,
    *,
    x: int,
    height: int,
    bw: int,
    bh: int,
    accent: tuple[int, int, int],
    level: int,
    level_t: tuple[float, float, float] | None,
    opacity: float,
) -> None:
    del height
    # Two layers, because a lit bar is drawn *over* its own unlit track and
    # ImageDraw's fill replaces rather than blends -- painting both onto one
    # layer would cut the track away wherever a bar was partly lit.
    track = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    lamps = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    track_draw = ImageDraw.Draw(track)
    lamp_draw = ImageDraw.Draw(lamps)
    bar_w, gap = 4 * _SS, 3 * _SS
    left = x * _SS
    for index, ratio in enumerate((0.36, 0.60, 0.84)):
        lit = level_t[index] if level_t is not None else (1.0 if level > index else 0.0)
        lit = _clamp01(lit)
        bar_h = round(_AVATAR * _SS * ratio)
        bx = left + index * (bar_w + gap)
        y1 = bh // 2 + (_AVATAR * _SS) // 2 - 2 * _SS
        track_draw.rounded_rectangle(
            (bx, y1 - bar_h, bx + bar_w, y1),
            radius=bar_w // 2,
            fill=_alpha(SURFACE_RAISED, opacity),
        )
        if lit > 0.01:
            # Grows from a stub rather than fading in, so the bar reads as a
            # level rising and not as a light being switched on.
            grown = round(bar_h * lerp(0.28, 1.0, lit))
            lamp_draw.rounded_rectangle(
                (bx, y1 - grown, bx + bar_w, y1),
                radius=bar_w // 2,
                fill=_alpha(accent, opacity),
            )
    canvas.alpha_composite(track)
    canvas.alpha_composite(lamps)


# ---------------------------------------------------------------------------
# Panels: the menu, and the popovers behind the two controls
# ---------------------------------------------------------------------------

#: Panels are light where the bar is dark. That inversion does real work: it
#: makes the panel read as sitting *above* the bar rather than being part of
#: it, without needing a border or a shadow to say so.
_MENU_BG = (252, 251, 249)
_MENU_INK = (22, 21, 15)
_MENU_MUTED = (119, 115, 106)
_MENU_RULE = (230, 227, 221)
_MENU_HOVER = (240, 238, 233)
#: Disabled rows. Light enough to read as unavailable, dark enough to still
#: be legible -- a row the user cannot use still has to explain itself.
_MENU_DISABLED = (176, 172, 164)

_MENU_W = 268
_MENU_ROW_H = 44
_MENU_FOOTER_H = 30
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
    #: A disabled row is drawn muted, does not take hover, and cannot be
    #: chosen. Used instead of letting a row fire and then report that it had
    #: nothing to do -- an error the user was given no way to avoid.
    enabled: bool = True
    #: Marks the current value in a popover. Draws a tick in the icon column.
    checked: bool = False
    #: Right-aligned muted text: a device's status, a countdown, a caveat.
    detail: str = ""
    #: A short, non-interactive caption at the foot of a panel.
    footer: bool = False

    @property
    def is_separator(self) -> bool:
        return not self.action and not self.footer

    @property
    def selectable(self) -> bool:
        return bool(self.action) and self.enabled and not self.footer


SEPARATOR = MenuItem(action="")


def _row_height(item: MenuItem) -> int:
    if item.is_separator:
        return _MENU_SEP_H
    return _MENU_FOOTER_H if item.footer else _MENU_ROW_H


def menu_height(items: tuple[MenuItem, ...]) -> int:
    return sum(_row_height(item) for item in items) + _MENU_PAD_Y * 2


def render_menu(
    items: tuple[MenuItem, ...],
    *,
    hover: str = "",
    focus: str = "",
    width: int = _MENU_W,
) -> tuple[Image.Image, tuple[HitRegion, ...]]:
    """The panel, and where each row is.

    ``focus`` is the keyboard cursor, drawn as a ring rather than as the
    hover fill so that pointing and arrowing are visibly different things --
    otherwise a user who has arrowed down cannot tell whether Enter will act
    on the row they arrowed to or the one the pointer happens to rest on.
    """
    height = menu_height(items)
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
    small = _font(11, bold=False)
    regions: list[HitRegion] = []
    y = _MENU_PAD_Y

    for item in items:
        row_h = _row_height(item)
        if item.is_separator:
            mid = (y + _MENU_SEP_H / 2) * _SS
            draw.line(
                [(_MENU_ICON_X * _SS, mid), ((width - _MENU_ICON_X) * _SS, mid)],
                fill=(*_MENU_RULE, 255),
                width=_SS,
            )
            y += row_h
            continue

        cy = (y + row_h / 2) * _SS

        if item.footer:
            # A caption, not a row: no hover, no hit region, smaller type.
            draw.text(
                (_MENU_ICON_X * _SS, cy - small.size / 2),
                item.label,
                font=small,
                fill=(*_MENU_MUTED, 255),
            )
            y += row_h
            continue

        if item.selectable and hover == item.action:
            draw.rounded_rectangle(
                (5 * _SS, y * _SS, (width - 5) * _SS, (y + row_h) * _SS),
                radius=9 * _SS,
                fill=(*_MENU_HOVER, 255),
            )
        if item.selectable and focus == item.action:
            draw.rounded_rectangle(
                (5 * _SS, y * _SS, (width - 5) * _SS, (y + row_h) * _SS),
                radius=9 * _SS,
                outline=(*ACCENT_BRAND, 255),
                width=max(1, round(1.5 * _SS)),
            )

        ink = _MENU_INK if item.enabled else _MENU_DISABLED
        muted = _MENU_MUTED if item.enabled else _MENU_DISABLED

        glyph = "check" if item.checked else item.icon
        if glyph:
            half = _MENU_ICON_D * _SS / 2
            ICONS[glyph](
                draw,
                (
                    _MENU_ICON_X * _SS,
                    cy - half,
                    (_MENU_ICON_X + _MENU_ICON_D) * _SS,
                    cy + half,
                ),
                (*(ACCENT_BRAND if item.checked else muted), 255),
                max(1, round(1.4 * _SS)),
            )

        row_font = _font_for(item.label, 14)
        bbox = draw.textbbox((0, 0), item.label, font=row_font)
        draw.text(
            (_MENU_LABEL_X * _SS, cy - (bbox[3] - bbox[1]) / 2 - bbox[1]),
            item.label,
            font=row_font,
            fill=(*ink, 255),
        )

        right = width - 22
        if item.submenu:
            size = 5 * _SS
            x = right * _SS
            _draw_chevron(
                draw,
                (x, cy - size, x + size, cy + size),
                (*muted, 255),
                max(1, round(1.3 * _SS)),
            )
            right -= 12
        if item.detail:
            detail_w = small.getlength(item.detail) / _SS
            draw.text(
                (right * _SS - detail_w * _SS, cy - small.size / 2),
                item.detail,
                font=small,
                fill=(*muted, 255),
            )

        if item.selectable:
            regions.append(HitRegion(0, y, width, y + row_h, item.action))
        y += row_h

    return _finish(canvas, width, height), tuple(regions)


_TOOLTIP_PAD_X = 9
_TOOLTIP_H = 26
_TOOLTIP_FONT_SIZE = 11


def render_tooltip(text: str) -> Image.Image:
    """A small dark label. Dark on purpose, matching the bar and not the
    panels: a tooltip belongs to the control it describes."""
    font = _font(_TOOLTIP_FONT_SIZE, bold=False)
    width = round(font.getlength(text) / _SS) + _TOOLTIP_PAD_X * 2
    height = _TOOLTIP_H
    bw, bh = width * _SS, height * _SS
    canvas = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (0, 0, bw - 1, bh - 1), radius=7 * _SS, fill=(*SURFACE_RAISED, 250)
    )
    draw.rounded_rectangle(
        (0, 0, bw - 1, bh - 1), radius=7 * _SS, outline=(255, 255, 255, 28), width=_SS
    )
    bbox = draw.textbbox((0, 0), text, font=font)
    draw.text(
        (_TOOLTIP_PAD_X * _SS, bh / 2 - (bbox[3] - bbox[1]) / 2 - bbox[1]),
        text,
        font=font,
        fill=(*INK, 255),
    )
    return _finish(canvas, width, height)


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


def clamp_to_area(
    x: int, y: int, size: tuple[int, int], area: tuple[int, int, int, int]
) -> tuple[int, int]:
    """Keep a panel fully on the usable desktop.

    A popover anchored under a control near the screen edge would otherwise
    open half off-screen, which is the one failure a menu is not allowed to
    have: the rows the user cannot see are the ones they were reaching for.
    """
    left, top, right, bottom = area
    width, height = size
    x = min(max(x, left + 8), max(left + 8, right - width - 8))
    y = min(max(y, top + 8), max(top + 8, bottom - height - 8))
    return x, y


# ---------------------------------------------------------------------------
# The Tk window
# ---------------------------------------------------------------------------

#: Frame budget. 60fps is entered only while something is moving or a
#: dictation is live; the resting dot ticks six times a second, which is
#: enough to notice a pointer arriving and cheap enough to leave running all
#: day. CLAUDE.md puts this repaint inside the latency budget, so the resting
#: cost is the one that matters.
_POLL_ACTIVE_MS = 16
_POLL_IDLE_MS = 50
_POLL_RESTING_MS = 150
#: Back-compat: the previous fixed rate, still the constructor's default so
#: existing callers and tests keep their behaviour.
_POLL_MS = _POLL_IDLE_MS

#: What the bar's menu offers. Ordered and grouped as the reference's is --
#: arm, dismiss, configure, switch, review, recover -- with copy beside paste,
#: because a paste the target window rejects still needs a way out.
#:
#: Built per open by :func:`build_menu` so pause/resume, hide/unhide and the
#: two recovery rows say what is actually true at that moment.
DEFAULT_MENU: tuple[MenuItem, ...] = (
    MenuItem(ACTION_PAUSE, "Pause dictation", "pause"),
    MenuItem(ACTION_HIDE, "Hide for 1 hour", "clock"),
    MenuItem(ACTION_SETTINGS, "Settings", "gear"),
    SEPARATOR,
    MenuItem(ACTION_LANGUAGE, "Language", "globe", submenu=True),
    MenuItem(ACTION_MIC, "Microphone", "mic", submenu=True),
    SEPARATOR,
    MenuItem(ACTION_HISTORY, "Transcript history", "list"),
    MenuItem(ACTION_PASTE, "Paste last transcript", "paste"),
    MenuItem(ACTION_COPY, "Copy last transcript", "copy"),
)


def _hide_label(seconds: float) -> str:
    """A countdown that rounds *up*.

    Rounding to nearest would report 61 seconds as "under a minute", which is
    the one thing it demonstrably is not.
    """
    minutes = -(-int(seconds) // 60)  # ceil, without importing math
    return f"Unhide ({minutes}m left)" if minutes > 1 else "Unhide (under a minute)"


def build_menu(
    items: tuple[MenuItem, ...] = DEFAULT_MENU,
    *,
    listening: bool = True,
    can_paste: bool = True,
    hidden_for_s: float = 0.0,
) -> tuple[MenuItem, ...]:
    """The menu as it should read *right now*.

    Pure, and the reason the menu stopped lying. Previously every row was
    static: "Pause" with no way to resume, "Hide for 1 hour" with no way to
    un-hide before the hour was up, and two recovery rows that were always
    clickable and produced an error when there was nothing to recover.
    """
    built: list[MenuItem] = []
    for item in items:
        if item.action == ACTION_PAUSE and not listening:
            built.append(MenuItem(ACTION_RESUME, "Resume dictation", "play"))
        elif item.action == ACTION_HIDE and hidden_for_s > 0.0:
            built.append(MenuItem(ACTION_UNHIDE, _hide_label(hidden_for_s), "eye"))
        elif item.action in _NEEDS_TRANSCRIPT and not can_paste:
            built.append(replace(item, enabled=False, detail="nothing yet"))
        else:
            built.append(item)
    return tuple(built)


def _set_window_alpha(window: tk.Misc, value: float) -> bool:
    """Best-effort whole-window opacity.

    Used for panel fades rather than PIL alpha, because these windows are
    colour-keyed: compositing a partially transparent image inside one blends
    it toward the key colour, so a PIL "fade" fades to black rather than to
    clear. Windows supports a colour key and an alpha on the same layered
    window; where it does not, this returns False once and the caller stops
    trying rather than fading in steps that do nothing.
    """
    try:
        window.attributes("-alpha", max(0.0, min(1.0, value)))
        return True
    except tk.TclError:
        return False


class SwayBar:
    """Runs Tk's mainloop on the calling thread until :meth:`stop`.

    Every public method except :meth:`run` is thread-safe: other threads put
    work on a queue and the Tk thread drains it, which is the only safe way
    to drive Tk from a hotkey listener or a dictation worker.
    """

    def __init__(
        self,
        *,
        poll_ms: int | None = None,
        position: str = "bottom-center",
        on_action: Callable[[str], None] | None = None,
        menu_items: tuple[MenuItem, ...] = DEFAULT_MENU,
    ) -> None:
        self._queue: queue.Queue[tuple[BarState, str, str]] = queue.Queue()
        self._ui_actions: queue.Queue[Callable[[tk.Tk], None]] = queue.Queue()
        #: An explicit override pins the frame rate (tests, and anyone who
        #: wants the old fixed cadence); None means adapt.
        self._poll_override = poll_ms
        self._poll_ms = poll_ms or _POLL_IDLE_MS
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
        self._kind = ""
        self._hint = ""
        self._hint_tone = HintTone.INFO
        self._hint_expires_at: float | None = None
        self._hover = ""
        self._press = ""
        self._pointer_inside = False
        self._level = 0
        self._listening = True
        self._can_paste = False
        self._regions: tuple[HitRegion, ...] = ()
        self._render_key: tuple | None = None
        self._geometry = ""
        self._recording_started_at: float | None = None
        self._error_since: float | None = None
        self._done_since: float | None = None
        self._coach_since: float | None = None
        self._state_entered_at = time.monotonic()
        self._hidden_until = 0.0
        self._menu: _Panel | None = None
        self._popover: _Panel | None = None
        self._popover_anchor = ""
        self._panel_opened_at = 0.0
        self._tooltip: _Tooltip | None = None
        self._tooltip_for = ""

        self._motion = Motion()
        self._motion.set("expand", 0.0)
        self._motion.set("width", float(_REST_W + 2 * _REST_PAD))
        self._motion.set("dot", 0.0)
        self._motion.set("hover", 0.0)
        self._motion.set("press", 0.0)
        for index in range(3):
            self._motion.set(f"level{index}", 0.0)
        self._expand_dwell = Timer()
        self._collapse_dwell = Timer()
        self._hover_dwell = Timer()
        self._expanded = False

    # -- thread-safe entry points -----------------------------------------

    @property
    def root(self) -> tk.Tk | None:
        return self._root

    def set_state(self, state: BarState, detail: str = "", *, kind: str = "") -> None:
        """Call from any thread; applied on the next poll tick.

        ``kind`` is the machine-readable failure category (the same one
        :meth:`sway.engine.Engine._record_error` stores) and is what lets the
        capsule offer the right recovery. It is optional so every existing
        caller keeps working unchanged.
        """
        self._queue.put((state, detail, kind))

    def set_level(self, level: int) -> None:
        """Mic level bucket, 0-3.

        Not queued: a level is a *current* value rather than an event, so the
        newest simply wins and a burst of updates cannot back up behind the
        poll tick.
        """
        self._level = max(0, min(3, int(level)))

    def set_hint(
        self,
        hint: str,
        *,
        tone: HintTone = HintTone.INFO,
        ttl_s: float | None = None,
    ) -> None:
        """Coaching text shown while idle, or an empty string to clear it.

        ``ttl_s`` makes it temporary, which is the default posture: guidance
        that outstays its usefulness becomes permanent instructional chrome,
        which is the thing this is meant to replace. ``[[Right Ctrl]]``
        renders as a keycap.
        """
        self._hint = hint
        self._hint_tone = tone
        self._hint_expires_at = (time.monotonic() + ttl_s) if (hint and ttl_s) else None

    def coach(self, text: str, *, ttl_s: float = COACH_HOLD_S) -> None:
        """Teach, right now, in place of whatever the bar was showing.

        Distinct from :meth:`set_hint`, which waits for idle: this is the
        answer to something the user just did wrong, and it is worth
        interrupting a resting dot for.
        """
        self.set_state(BarState.COACH, text)
        self._coach_ttl = max(0.5, ttl_s)

    _coach_ttl: float = COACH_HOLD_S

    def set_listening(self, listening: bool) -> None:
        """Whether the hotkey is armed, for the menu's pause/resume row."""
        self._listening = bool(listening)

    def set_can_paste(self, can_paste: bool) -> None:
        """Whether there is a transcript to re-paste.

        Drives both the menu's two recovery rows and the capsule's inline
        recovery button, so neither can offer an action that has nothing to
        act on.
        """
        self._can_paste = bool(can_paste)

    def hide_for(self, seconds: float) -> None:
        """Collapse to the dot and stay there.

        Distinct from pausing the agent: the hotkey still works and dictation
        still happens, the bar simply stops talking about it.
        """
        self._hidden_until = time.monotonic() + max(0.0, seconds)

    def unhide(self) -> None:
        self._hidden_until = 0.0

    @property
    def hidden(self) -> bool:
        return time.monotonic() < self._hidden_until

    @property
    def hidden_remaining_s(self) -> float:
        return max(0.0, self._hidden_until - time.monotonic())

    def open_popover(
        self, anchor: str, items: tuple[MenuItem, ...], *, width: int = 240
    ) -> None:
        """Show ``items`` under the control named by ``anchor``.

        Thread-safe. The bar deliberately knows nothing about languages or
        audio devices -- the engine owns those and hands over finished rows,
        exactly as it already does for menu actions. That keeps the one
        surface that must never block on the Tk thread free of any lookup
        that could.
        """
        self.run_on_ui_thread(
            lambda _root: self._show_popover(anchor, items, width=width)
        )

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
        canvas.bind("<Button-1>", self._on_press_down)
        canvas.bind("<ButtonRelease-1>", self._on_press_up)
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
        self._close_panels()
        self._hide_tooltip()
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
        self._press = ""
        self._hover_dwell.disarm()
        self._hide_tooltip()

    def _on_motion(self, event: tk.Event) -> None:
        self._pointer_inside = True
        hover = ""
        for region in self._regions:
            if region.action != ACTION_MENU and region.contains(event.x, event.y):
                hover = region.action
                break
        if hover != self._hover:
            self._hover = hover
            # A tooltip is earned by resting, not by passing through.
            self._hide_tooltip()
            if hover:
                self._hover_dwell.arm(TOOLTIP_DELAY_S)
            else:
                self._hover_dwell.disarm()
        # A drag off the control cancels its press, the way every other
        # button on the desktop behaves.
        if self._press and self._press != hover:
            self._press = ""

    def _on_press_down(self, event: tk.Event) -> None:
        self._hide_tooltip()
        for region in self._regions:
            if region.action != ACTION_MENU and region.contains(event.x, event.y):
                self._press = region.action
                return
        self._press = ACTION_MENU

    def _on_press_up(self, event: tk.Event) -> None:
        pressed, self._press = self._press, ""
        if not pressed:
            return
        for region in self._regions:
            if region.action != ACTION_MENU and region.contains(event.x, event.y):
                if region.action == pressed:
                    self._activate(region.action)
                return
        # A left-click on the body, including the resting dot, opens the menu:
        # a 14px dot is too small to carry more than one meaning.
        if pressed == ACTION_MENU:
            self._open_menu()

    def _on_right_click(self, _event: tk.Event) -> None:
        self._open_menu()

    def _activate(self, action: str) -> None:
        """A control on the capsule was clicked.

        The two switchers open their own popover rather than dispatching
        straight through, so the engine can answer with rows instead of
        navigating the user away from what they were doing -- which is what
        both of them used to do.
        """
        if action in (ACTION_LANGUAGE, ACTION_MIC) and self._popover_anchor == action:
            self._close_popover()
            return
        self._dispatch(action)

    def _dispatch(self, action: str) -> None:
        if self._on_action is None:
            return
        try:
            self._on_action(action)
        except Exception:
            logger.exception("bar action %r failed", action)

    # -- panels ------------------------------------------------------------

    def _menu_geometry(self) -> tuple[int, int, int]:
        root = self._root
        if root is None:
            return (0, 0, _DOT_BOX)
        return (root.winfo_x(), root.winfo_y(), root.winfo_width())

    def _open_menu(self) -> None:
        root = self._root
        if root is None:
            return
        self._close_popover()
        if self._menu is not None:
            self._close_menu()
            return
        self._hide_tooltip()
        items = build_menu(
            self._menu_items,
            listening=self._listening,
            can_paste=self._can_paste,
            hidden_for_s=self.hidden_remaining_s,
        )
        self._menu = _Panel(
            root,
            items,
            anchor=self._menu_geometry(),
            position=self._position,
            on_choose=self._on_menu_choice,
        )
        self._menu.open()
        self._panel_opened_at = time.monotonic()

    def _close_menu(self) -> None:
        menu, self._menu = self._menu, None
        if menu is not None:
            menu.close()

    def _show_popover(
        self, anchor: str, items: tuple[MenuItem, ...], *, width: int
    ) -> None:
        """Tk-thread half of :meth:`open_popover`."""
        root = self._root
        if root is None:
            return
        self._close_menu()
        self._close_popover()
        if not items:
            return
        self._hide_tooltip()
        self._popover_anchor = anchor
        offset = self._anchor_offset(anchor)
        self._popover = _Panel(
            root,
            items,
            anchor=self._menu_geometry(),
            position=self._position,
            on_choose=self._on_popover_choice,
            width=width,
            anchor_offset=offset,
        )
        self._popover.open()
        self._panel_opened_at = time.monotonic()

    def _anchor_offset(self, action: str) -> int | None:
        """Where the popover should point, as an x offset into the bar.

        Falls back to ``None`` (centre on the bar) when the control is not
        currently on screen -- which happens if the state changed between the
        click and the engine answering.
        """
        for region in self._regions:
            if region.action == action:
                return (region.x0 + region.x1) // 2
        return None

    def _close_popover(self) -> None:
        popover, self._popover = self._popover, None
        self._popover_anchor = ""
        if popover is not None:
            popover.close()

    def _close_panels(self) -> None:
        self._close_menu()
        self._close_popover()

    def _dismiss_on_outside_click(self) -> None:
        """Close an open panel when the user clicks anywhere else on screen.

        A menu that survives a click elsewhere is a menu the user has to
        dismiss before getting on with their work, which is precisely
        backwards -- clicking on the thing you actually want *is* the
        dismissal everywhere else on the desktop.

        Done by sampling the mouse rather than by grabbing input or
        installing a hook, and the reason is the same one that shapes the
        rest of this module: the bar must never take focus, because paste
        targets the focused window. A Tk global grab would steal input from
        the app the user just clicked into, and a low-level mouse hook needs
        its own message pump. Two ctypes reads on a poll that is already
        running cost nothing and cannot affect anyone else's input.

        ``GetAsyncKeyState``'s low bit means "pressed since the last call",
        so a click is caught even if the button is released between ticks.
        """
        if not self._panel_open:
            return
        # GetAsyncKeyState's "pressed since last call" bit is latched, so the
        # very click that opened the panel can still be waiting to be read on
        # the first tick afterwards. Without a grace period a panel opened by
        # a click that the pointer then moved away from would dismiss itself
        # immediately, which looks like the click simply not working.
        if time.monotonic() - self._panel_opened_at < _DISMISS_GRACE_S:
            return
        point = self._mouse_press_point()
        if point is None:
            return
        x, y = point
        if self._point_in_bar(x, y) or self._point_in_panels(x, y):
            return
        self._close_panels()

    def _mouse_press_point(self) -> tuple[int, int] | None:
        """Where a mouse button was pressed since the last call, or ``None``.

        The one place that touches the OS, kept to a few lines so the
        dismissal decision above is testable without a mouse.

        ``GetAsyncKeyState``'s ``0x0001`` bit means "went down since this was
        last asked", which catches a click that began and ended inside a
        single poll tick; ``0x8000`` means "down right now", which catches a
        press being held. Reading it also clears it, so each press is seen
        once.
        """
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            # VK_LBUTTON 0x01, VK_RBUTTON 0x02.
            pressed = any(
                user32.GetAsyncKeyState(button) & 0x8001 for button in (0x01, 0x02)
            )
            if not pressed:
                return None
            point = wintypes.POINT()
            if not user32.GetCursorPos(ctypes.byref(point)):
                return None
            return point.x, point.y
        except Exception:
            # Not Windows, or the call is unavailable. Escape, choosing a row
            # and clicking the bar all still dismiss.
            logger.debug("cannot watch for an outside click", exc_info=True)
            return None

    def _point_in_bar(self, x: int, y: int) -> bool:
        root = self._root
        if root is None:
            return False
        try:
            left, top = root.winfo_x(), root.winfo_y()
            return (
                left <= x <= left + root.winfo_width()
                and top <= y <= top + root.winfo_height()
            )
        except tk.TclError:
            return False

    def _point_in_panels(self, x: int, y: int) -> bool:
        return any(
            panel is not None and panel.contains(x, y)
            for panel in (self._menu, self._popover)
        )

    def _on_menu_choice(self, action: str) -> None:
        self._menu = None
        if action:
            self._dispatch(action)

    def _on_popover_choice(self, action: str) -> None:
        self._popover = None
        self._popover_anchor = ""
        if action:
            self._dispatch(action)

    @property
    def _panel_open(self) -> bool:
        return self._menu is not None or self._popover is not None

    # -- tooltips ----------------------------------------------------------

    _TOOLTIPS: dict[str, str] = {
        ACTION_LANGUAGE: "Dictation language",
        ACTION_MIC: "Microphone",
    }

    def _tooltip_text(self, action: str) -> str:
        return self._TOOLTIPS.get(action, "")

    def _maybe_show_tooltip(self) -> None:
        # A tooltip must not outlive the control it points at. The state can
        # change under it -- a dictation starts, an error arrives -- and the
        # controls are withdrawn without the pointer having moved, which
        # would otherwise leave a label hanging over nothing.
        if self._tooltip_for and self._anchor_offset(self._tooltip_for) is None:
            self._hide_tooltip()
        if self._panel_open or not self._hover or self._press:
            if self._tooltip is not None and self._press:
                self._hide_tooltip()
            return
        if self._tooltip_for == self._hover or not self._hover_dwell.elapsed():
            return
        text = self._tooltip_text(self._hover)
        if not text:
            self._hover_dwell.disarm()
            return
        root = self._root
        if root is None:
            return
        offset = self._anchor_offset(self._hover)
        if offset is None:
            return
        self._hide_tooltip()
        self._tooltip = _Tooltip(root, text)
        self._tooltip.open(
            bar=self._menu_geometry(),
            bar_height=root.winfo_height(),
            offset_x=offset,
            position=self._position,
        )
        self._tooltip_for = self._hover

    def _hide_tooltip(self) -> None:
        tooltip, self._tooltip = self._tooltip, None
        self._tooltip_for = ""
        if tooltip is not None:
            tooltip.close()

    # -- the frame loop ----------------------------------------------------

    def _poll(self) -> None:
        """One frame, then schedule the next.

        Split from :meth:`_tick` so the work and the scheduling are separable:
        anything that wants to advance the bar by one frame -- a test, a
        driver script -- can call ``_tick`` without starting a second,
        self-multiplying chain of ``after`` callbacks.
        """
        self._tick()
        root = self._root
        if root is not None:
            # Rescheduling every tick also lets Ctrl+C at a console interrupt
            # mainloop promptly rather than blocking in Tk's C event loop.
            root.after(self._next_poll_ms(), self._poll)

    def _tick(self) -> None:
        changed = False
        try:
            while True:
                state, detail, kind = self._queue.get_nowait()
                if state != self._state:
                    self._state_entered_at = time.monotonic()
                    if state is BarState.RECORDING:
                        # A dictation starting closes anything the user left
                        # open: the words are about to go somewhere, and a
                        # panel over the target is the last thing wanted.
                        self._close_panels()
                        self._hide_tooltip()
                self._state, self._detail, self._kind = state, detail, kind
                changed = True
        except queue.Empty:
            pass

        now = time.monotonic()
        if self._state is BarState.RECORDING:
            if self._recording_started_at is None:
                self._recording_started_at = now
        else:
            self._recording_started_at = None

        if self._hint and self._hint_expires_at is not None and now >= self._hint_expires_at:
            self._hint = ""
            self._hint_expires_at = None
            changed = True

        # Errors and confirmations are transient notices, not modes: they
        # expire back to idle so the bar never lies about the current state.
        # An error the pointer is resting on does not expire, so a hand
        # reaching for its recovery button never has it vanish mid-reach.
        hold_error = self._pointer_inside or self._panel_open
        changed |= self._expire(BarState.ERROR, "_error_since", ERROR_HOLD_S, now,
                                frozen=hold_error)
        changed |= self._expire(BarState.DONE, "_done_since", DONE_HOLD_S, now)
        changed |= self._expire(BarState.COACH, "_coach_since", self._coach_ttl, now)

        self._dismiss_on_outside_click()
        self._update_phase(now)
        self._maybe_show_tooltip()
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

    def _next_poll_ms(self) -> int:
        """Adaptive frame budget -- see :data:`_POLL_ACTIVE_MS`."""
        if self._poll_override is not None:
            return self._poll_override
        if not self._motion.settled():
            return _POLL_ACTIVE_MS
        if self._state in (BarState.RECORDING, BarState.THINKING, BarState.INJECTING):
            return _POLL_ACTIVE_MS
        if self._expanded or self._pointer_inside or self._panel_open:
            return _POLL_IDLE_MS
        return _POLL_RESTING_MS

    def _expire(
        self,
        state: BarState,
        attr: str,
        hold_s: float,
        now: float,
        *,
        frozen: bool = False,
    ) -> bool:
        if self._state is not state:
            setattr(self, attr, None)
            return False
        since = getattr(self, attr)
        if since is None:
            setattr(self, attr, now)
            return False
        if frozen:
            # Push the deadline out rather than pausing a stopwatch, so the
            # full hold is available again once the pointer leaves.
            setattr(self, attr, now)
            return False
        if now - since >= hold_s:
            self._state, self._detail, self._kind = BarState.IDLE, "", ""
            self._state_entered_at = now
            setattr(self, attr, None)
            return True
        return False

    def _want_expanded(self, now: float) -> bool:
        """Whether the capsule should be open, dwells included.

        The dwells are the fix for the previous build's worst flicker: enter
        and leave expanded and collapsed instantly, so a pointer crossing the
        bar on its way elsewhere made it flash open and shut.
        """
        del now
        if self._state not in _COLLAPSIBLE:
            self._expand_dwell.disarm()
            self._collapse_dwell.disarm()
            return True
        if self._panel_open:
            self._expand_dwell.disarm()
            self._collapse_dwell.disarm()
            return True
        if self.hidden:
            return False
        if self._hint:
            return True

        if self._pointer_inside:
            self._collapse_dwell.disarm()
            if self._expanded:
                return True
            self._expand_dwell.arm_if_idle(EXPAND_DWELL_S)
            return self._expand_dwell.elapsed()

        self._expand_dwell.disarm()
        if not self._expanded:
            return False
        self._collapse_dwell.arm_if_idle(COLLAPSE_DWELL_S)
        return not self._collapse_dwell.elapsed()

    def _update_phase(self, now: float) -> None:
        """Retarget every tween from the current inputs.

        Called every tick with the same targets most of the time, which
        :meth:`sway.motion.Motion.to` is built for -- a repeated target is a
        no-op, so this cannot restart a tween mid-flight.
        """
        want = self._want_expanded(now)
        if want != self._expanded:
            self._expanded = want
            if want:
                self._collapse_dwell.disarm()
            else:
                self._expand_dwell.disarm()

        self._motion.to(
            "expand",
            1.0 if want else 0.0,
            duration_s=WIDTH_S if want else COLLAPSE_S,
            ease=ease_out_cubic if want else ease_in_out_cubic,
        )
        self._motion.to(
            "dot",
            1.0 if (self._pointer_inside and not want) else 0.0,
            duration_s=DOT_HOVER_S if self._pointer_inside else DOT_UNHOVER_S,
        )
        self._motion.to(
            "hover", 1.0 if self._hover else 0.0, duration_s=BUTTON_FILL_S
        )
        self._motion.to(
            "press",
            1.0 if self._press and self._press != ACTION_MENU else 0.0,
            duration_s=PRESS_DOWN_S if self._press else PRESS_UP_S,
            ease=ease_out_cubic if self._press else ease_out_back,
        )
        level = self._level if self._state is BarState.RECORDING else 0
        for index in range(3):
            self._motion.to(
                f"level{index}", 1.0 if level > index else 0.0, duration_s=LEVEL_S
            )

    def _apply_frame(self, *, force: bool = False) -> None:
        canvas = self._canvas
        if canvas is None:
            return
        now = time.monotonic()
        expand = self._motion.value("expand", 0.0)
        badge_frame = _badge_frame(self._state, now)
        elapsed_s = (
            int(now - self._recording_started_at)
            if self._recording_started_at is not None
            else None
        )
        level = self._level if self._state is BarState.RECORDING else 0
        hint = self._hint if self._state in _COLLAPSIBLE else ""

        natural = natural_width(
            self._state,
            self._detail,
            elapsed_s=elapsed_s,
            hint=hint,
            kind=self._kind,
            can_paste=self._can_paste,
        )
        self._motion.to(
            "width",
            float(natural),
            duration_s=WIDTH_S,
        )
        # Only follow the width tween while the capsule is open; during the
        # morph the width comes from `expand` so the two cannot fight.
        width = (
            round(self._motion.value("width", float(natural)))
            if expand >= _SETTLED
            else None
        )

        dot_t = self._motion.value("dot", 0.0)
        hover_t = self._motion.value("hover", 0.0)
        press_t = self._motion.value("press", 0.0)
        level_t = tuple(self._motion.value(f"level{i}", 0.0) for i in range(3))
        pulse_t = (
            pulse(now, 0.5) if self._state is BarState.RECORDING else None
        )
        opacity = 0.42 if (self.hidden and expand < 0.5) else 1.0
        shake = (
            shake_offset(now - self._state_entered_at)
            if self._state is BarState.ERROR
            else 0.0
        )

        # Quantised, so a settled tween stops producing new keys and the bar
        # genuinely stops repainting rather than merely rendering the same
        # picture at 60fps.
        key = (
            self._state, self._detail, self._kind, hint, self._hint_tone,
            badge_frame, elapsed_s, level, self._hover, self._press,
            self._can_paste, width,
            round(expand, 3), round(dot_t, 2), round(hover_t, 2),
            round(press_t, 2),
            None if pulse_t is None else round(pulse_t, 2),
            tuple(round(value, 2) for value in level_t),
            round(opacity, 2), round(shake, 1),
        )
        if not force and key == self._render_key:
            return
        self._render_key = key

        image, regions = render_bar(
            self._state,
            self._detail,
            badge_frame=badge_frame,
            elapsed_s=elapsed_s,
            level=level,
            hint=hint,
            hover=self._hover,
            press=self._press,
            kind=self._kind,
            tone=self._hint_tone,
            can_paste=self._can_paste,
            expand=expand,
            width=width,
            hover_t=hover_t,
            press_t=press_t,
            pulse_t=pulse_t,
            level_t=level_t,
            dot_grow=dot_t * _REST_GROW,
            dot_ring=dot_t,
            opacity=opacity,
            hollow=self._state is BarState.PAUSED,
        )
        self._regions = regions

        image_w, image_h = image.size

        # Geometry first, image second. The other order lets Windows
        # composite one frame with the *new* image inside the *old* window
        # rectangle, which shows up as a visibly doubled, half-transparent
        # bar every time it changes width -- and it changes width on nearly
        # every state change.
        self._reposition(image_w, image_h, shake=shake)
        if int(canvas.cget("width")) != image_w or int(canvas.cget("height")) != image_h:
            canvas.config(width=image_w, height=image_h)

        photo = ImageTk.PhotoImage(image)
        self._photo = photo  # Tk drops images with no live reference
        if self._image_item is None:
            self._image_item = canvas.create_image(0, 0, anchor="nw", image=photo)
        else:
            canvas.itemconfig(self._image_item, image=photo)

    def _reposition(self, width: int, height: int, *, shake: float = 0.0) -> None:
        root = self._root
        if root is None:
            return
        area = work_area((root.winfo_screenwidth(), root.winfo_screenheight()))
        x, y = place(self._position, (width, height), area)
        x += round(shake)
        wanted = f"{width}x{height}+{x}+{y}"
        if wanted == self._geometry:
            return
        self._geometry = wanted
        root.geometry(wanted)


class _Panel:
    """The light panel near the bar: the menu, and each popover.

    One class for both because they differ only in where they point and how
    wide they are. Built and thrown away per open -- a panel that lives
    between opens has to have its rows invalidated when anything behind them
    changes, and there is no state here worth that.
    """

    #: Set False the first time a fade is refused, so a platform without
    #: layered-window alpha stops paying for animation frames that do nothing.
    _fade_supported = True

    def __init__(
        self,
        parent: tk.Tk,
        items: tuple[MenuItem, ...],
        *,
        anchor: tuple[int, int, int],
        position: str,
        on_choose: Callable[[str], None],
        width: int = _MENU_W,
        anchor_offset: int | None = None,
    ) -> None:
        self._parent = parent
        self._items = items
        self._anchor = anchor
        self._position = position
        self._on_choose = on_choose
        self._width = width
        self._anchor_offset = anchor_offset
        self._top: tk.Toplevel | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._item_id: int | None = None
        self._canvas: tk.Canvas | None = None
        self._regions: tuple[HitRegion, ...] = ()
        self._hover = ""
        self._focus = ""
        self._origin = (0, 0)
        self._opened_at = 0.0
        self._closing = False
        self._fade_job: str | None = None

    # -- lifecycle ---------------------------------------------------------

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

        # Escape and the arrows are bound on the parent because the bar is
        # deliberately never focusable (see SwayBar._make_non_activating) --
        # paste targets the focused window, and a panel that stole focus
        # would send the next dictation into the wrong place. They are
        # unbound again on close.
        self._parent.bind_all("<Escape>", self._on_escape, add="+")
        self._parent.bind_all("<Up>", self._on_up, add="+")
        self._parent.bind_all("<Down>", self._on_down, add="+")
        self._parent.bind_all("<Return>", self._on_return, add="+")

        self._draw()
        self._place()
        self._opened_at = time.monotonic()
        if _Panel._fade_supported:
            if _set_window_alpha(top, 0.0):
                self._animate()
            else:
                _Panel._fade_supported = False

    def _place(self, *, rise: float = 0.0) -> None:
        top = self._top
        if top is None:
            return
        bar_x, bar_y, bar_w = self._anchor
        width, height = self._width, menu_height(self._items)
        if self._anchor_offset is None:
            x = bar_x + (bar_w - width) // 2
        else:
            x = bar_x + self._anchor_offset - width // 2
        # Panels always open toward the middle of the screen, so one docked at
        # the bottom never opens off the bottom edge.
        below = not self._position.startswith("bottom-")
        y = bar_y + 52 if below else bar_y - height - 8
        # The rise: a panel arrives from the direction it is anchored to.
        y += round(rise if below else -rise)
        area = work_area(
            (self._parent.winfo_screenwidth(), self._parent.winfo_screenheight())
        )
        x, y = clamp_to_area(x, y, (width, height), area)
        self._origin = (x, y)
        top.geometry(f"{width}x{height}+{x}+{y}")

    def _animate(self) -> None:
        """Fade and rise on open, fade on close. ~160ms and ~110ms."""
        top = self._top
        if top is None:
            return
        elapsed = time.monotonic() - self._opened_at
        duration = PANEL_CLOSE_S if self._closing else PANEL_OPEN_S
        t = _clamp01(elapsed / duration) if duration > 0 else 1.0
        eased = ease_out_cubic(t)
        alpha = (1.0 - eased) if self._closing else eased
        if not _set_window_alpha(top, alpha):
            _Panel._fade_supported = False
            self._finish_animation()
            return
        if not self._closing:
            self._place(rise=lerp(8.0, 0.0, eased))
        if t >= 1.0:
            self._finish_animation()
            return
        try:
            self._fade_job = top.after(16, self._animate)
        except tk.TclError:
            self._fade_job = None

    def _finish_animation(self) -> None:
        self._fade_job = None
        if self._closing:
            self._destroy()
        else:
            self._place()

    # -- drawing -----------------------------------------------------------

    def _draw(self) -> None:
        canvas = self._canvas
        if canvas is None:
            return
        image, regions = render_menu(
            self._items, hover=self._hover, focus=self._focus, width=self._width
        )
        self._regions = regions
        photo = ImageTk.PhotoImage(image)
        self._photo = photo
        if self._item_id is None:
            canvas.config(width=image.width, height=image.height)
            self._item_id = canvas.create_image(0, 0, anchor="nw", image=photo)
        else:
            canvas.itemconfig(self._item_id, image=photo)

    def contains(self, x: int, y: int) -> bool:
        """Whether a screen point is over this panel.

        Read from the live window rather than from :attr:`_origin` so a panel
        still fading into place is tested where it actually is.
        """
        top = self._top
        if top is None or self._closing:
            return False
        try:
            left, upper = top.winfo_x(), top.winfo_y()
            return (
                left <= x <= left + top.winfo_width()
                and upper <= y <= upper + top.winfo_height()
            )
        except tk.TclError:
            return False

    def _set_hover(self, action: str) -> None:
        if action == self._hover:
            return
        self._hover = action
        self._draw()

    def _set_focus(self, action: str) -> None:
        if action == self._focus:
            return
        self._focus = action
        self._draw()

    # -- input -------------------------------------------------------------

    def _on_motion(self, event: tk.Event) -> None:
        for region in self._regions:
            if region.contains(event.x, event.y):
                self._set_hover(region.action)
                return
        self._set_hover("")

    def _on_click(self, event: tk.Event) -> None:
        for region in self._regions:
            if region.contains(event.x, event.y):
                self._choose(region.action)
                return
        self._choose("")

    def _choose(self, action: str) -> None:
        if self._closing:
            return
        self.close()
        self._on_choose(action)

    def _selectable(self) -> list[str]:
        return [region.action for region in self._regions]

    def _step(self, delta: int) -> None:
        options = self._selectable()
        if not options:
            return
        if self._focus in options:
            index = (options.index(self._focus) + delta) % len(options)
        else:
            index = 0 if delta > 0 else len(options) - 1
        self._set_focus(options[index])

    def _on_up(self, _event: tk.Event) -> str | None:
        if self._top is None:
            return None
        self._step(-1)
        return "break"

    def _on_down(self, _event: tk.Event) -> str | None:
        if self._top is None:
            return None
        self._step(1)
        return "break"

    def _on_return(self, _event: tk.Event) -> str | None:
        if self._top is None:
            return None
        if self._focus:
            self._choose(self._focus)
        return "break"

    def _on_escape(self, _event: tk.Event) -> str | None:
        if self._top is None:
            return None
        self._choose("")
        return "break"

    # -- teardown ----------------------------------------------------------

    def close(self) -> None:
        """Unbind immediately, then fade out.

        The unbind cannot wait for the fade: a second panel opening during it
        would otherwise find the first still holding the arrow keys.
        """
        if self._closing:
            return
        self._closing = True
        self._unbind()
        top = self._top
        if top is None:
            return
        # Cancel the open fade before starting the close one. Leaving it
        # scheduled runs two animation chains at once, and both would
        # eventually call _destroy.
        if self._fade_job is not None:
            try:
                top.after_cancel(self._fade_job)
            except tk.TclError:
                pass
            self._fade_job = None
        if _Panel._fade_supported:
            self._opened_at = time.monotonic()
            self._animate()
        else:
            self._destroy()

    def _unbind(self) -> None:
        for sequence in ("<Escape>", "<Up>", "<Down>", "<Return>"):
            try:
                self._parent.unbind_all(sequence)
            except tk.TclError:
                pass

    def _destroy(self) -> None:
        top, self._top = self._top, None
        if top is not None:
            if self._fade_job is not None:
                try:
                    top.after_cancel(self._fade_job)
                except tk.TclError:
                    pass
            try:
                top.destroy()
            except tk.TclError:
                pass
        self._fade_job = None
        self._photo = None
        self._canvas = None
        self._item_id = None


#: Kept as the old name so nothing that imported it breaks.
_Menu = _Panel


class _Tooltip:
    """A label under a control, shown only after the pointer has rested.

    Non-interactive by construction: no bindings at all, so it can never take
    a click that was meant for the button it describes.
    """

    def __init__(self, parent: tk.Tk, text: str) -> None:
        self._parent = parent
        self._text = text
        self._top: tk.Toplevel | None = None
        self._photo: ImageTk.PhotoImage | None = None

    def open(
        self,
        *,
        bar: tuple[int, int, int],
        bar_height: int,
        offset_x: int,
        position: str,
    ) -> None:
        key_hex = "#%02x%02x%02x" % _KEY_RGB
        try:
            image = render_tooltip(self._text)
            top = tk.Toplevel(self._parent)
            top.overrideredirect(True)
            top.attributes("-topmost", True)
            top.configure(bg=key_hex)
            try:
                top.wm_attributes("-transparentcolor", key_hex)
            except tk.TclError:
                pass
            canvas = tk.Canvas(
                top,
                highlightthickness=0,
                bg=key_hex,
                width=image.width,
                height=image.height,
            )
            canvas.pack()
            photo = ImageTk.PhotoImage(image)
            self._photo = photo
            canvas.create_image(0, 0, anchor="nw", image=photo)

            bar_x, bar_y, _bar_w = bar
            x = bar_x + offset_x - image.width // 2
            below = position.startswith("top-")
            y = bar_y + bar_height + 8 if below else bar_y - image.height - 8
            area = work_area(
                (self._parent.winfo_screenwidth(), self._parent.winfo_screenheight())
            )
            x, y = clamp_to_area(x, y, (image.width, image.height), area)
            top.geometry(f"{image.width}x{image.height}+{x}+{y}")
            self._top = top
        except Exception:
            # A tooltip is the least important thing on screen; it must never
            # be able to take the bar down with it.
            logger.debug("could not show a bar tooltip", exc_info=True)
            self.close()

    def close(self) -> None:
        top, self._top = self._top, None
        if top is not None:
            try:
                top.destroy()
            except tk.TclError:
                pass
        self._photo = None


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

    @property
    def hidden_remaining_s(self) -> float:
        return 0.0

    def set_state(self, state: BarState, detail: str = "", *, kind: str = "") -> None:
        pass

    def set_level(self, level: int) -> None:
        pass

    def set_hint(
        self, hint: str, *, tone: HintTone = HintTone.INFO, ttl_s: float | None = None
    ) -> None:
        pass

    def coach(self, text: str, *, ttl_s: float = COACH_HOLD_S) -> None:
        pass

    def set_listening(self, listening: bool) -> None:
        pass

    def set_can_paste(self, can_paste: bool) -> None:
        pass

    def hide_for(self, seconds: float) -> None:
        pass

    def unhide(self) -> None:
        pass

    def open_popover(
        self, anchor: str, items: tuple[MenuItem, ...], *, width: int = 240
    ) -> None:
        del anchor, items, width

    def run_on_ui_thread(self, action: Callable[[tk.Tk], None]) -> None:
        del action
        logger.info("cannot show a window headlessly ([overlay].enabled = false)")

    def run(self) -> None:
        self._stop_event.wait()

    def stop(self) -> None:
        self._stop_event.set()


__all__ = [
    "ACTION_COPY",
    "ACTION_HIDE",
    "ACTION_HISTORY",
    "ACTION_LANGUAGE",
    "ACTION_MENU",
    "ACTION_MIC",
    "ACTION_PASTE",
    "ACTION_PAUSE",
    "ACTION_RESUME",
    "ACTION_SETTINGS",
    "ACTION_UNHIDE",
    "COACH_HOLD_S",
    "COLLAPSE_DWELL_S",
    "DEFAULT_MENU",
    "DONE_HOLD_S",
    "ERROR_HOLD_S",
    "EXPAND_DWELL_S",
    "ICONS",
    "RECOVERY",
    "SEPARATOR",
    "BarState",
    "HintTone",
    "HitRegion",
    "MenuItem",
    "NullBar",
    "SwayBar",
    "build_menu",
    "clamp_to_area",
    "measure_rich",
    "menu_height",
    "natural_width",
    "place",
    "recovery_for",
    "render_bar",
    "render_dot",
    "render_menu",
    "render_tooltip",
    "rich_segments",
    "work_area",
]

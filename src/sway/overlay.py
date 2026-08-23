"""A small always-on-top status pill: idle / recording / thinking / error.

Built with tkinter (stdlib) plus Pillow (already a dependency for the tray
icon) to render each frame as a single composited image rather than raw Tk
canvas primitives -- see :mod:`flow.branding` for the shared mark/palette.
On Windows, ``-transparentcolor`` keys out everything outside the pill's
rounded-capsule shape, so the window itself has no visible rectangle or
title bar -- just the pill, floating over the desktop.

The Tk mainloop must run on the thread that creates the window, so
:meth:`set_state` is queue-based: any other thread (hotkey listener,
dictation worker) can drive the overlay safely by putting a state onto the
queue rather than touching Tk widgets directly. :meth:`Overlay.run_on_ui_thread`
generalises this so the tray icon (its own thread) can open the searchable
history window (:mod:`flow.history_window`) on the same Tk thread as well,
without either of them touching Tk objects directly.
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

from flow.branding import (
    ACCENT_ERROR,
    ACCENT_IDLE,
    ACCENT_RECORDING,
    ACCENT_THINKING,
    INK,
    SURFACE,
    SURFACE_RAISED,
    app_badge_image,
    mark_image,
)

logger = logging.getLogger(__name__)


class OverlayState(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    THINKING = "thinking"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Rendering: pure PIL, no Tk -- one composited RGB image per frame, keyed on
# whatever varies (state, detail text, animation frame, elapsed seconds), so
# Overlay only re-renders and re-composites when something actually changed.
# ---------------------------------------------------------------------------

#: Exact color the window keys out to transparent (Windows' -transparentcolor
#: is a hard match, not alpha blending). Chosen close in tone to SURFACE so
#: any anti-aliased edge pixels straddling the key read as a soft dark edge
#: rather than a visible color fringe against arbitrary desktop backgrounds.
_KEY_RGB = (8, 8, 9)

#: Supersampling factor: every shape is drawn this many times larger than it
#: will be shown, then downsampled with a high-quality filter. PIL's drawing
#: primitives (ellipse, rounded_rectangle, text) are hard-edged at native
#: resolution, which looks cheap at the small sizes (13-44px) this pill is
#: actually rendered at; downsampling is what makes the edges smooth.
_SS = 4

_HEIGHT = 44
_AVATAR = 34
_PAD_LEFT = 6
_GAP = 10
_PAD_RIGHT = 16
_MIN_ACTIVE_WIDTH = 128
_BADGE_D = 15
_FONT_SIZE = 13

_FONT_CANDIDATES = ("segoeuisb.ttf", "seguisb.ttf", "segoeuib.ttf", "segoeui.ttf")


@dataclass(frozen=True, slots=True)
class _Style:
    label: str
    accent: tuple[int, int, int]


_STYLES: dict[OverlayState, _Style] = {
    OverlayState.IDLE: _Style("", ACCENT_IDLE),
    OverlayState.RECORDING: _Style("Listening", ACCENT_RECORDING),
    OverlayState.THINKING: _Style("Thinking", ACCENT_THINKING),
    OverlayState.ERROR: _Style("Error", ACCENT_ERROR),
}


@lru_cache(maxsize=1)
def _label_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    fonts_dir = os.environ.get("WINDIR", r"C:\Windows") + r"\Fonts"
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(f"{fonts_dir}\\{name}", _FONT_SIZE * _SS)
        except OSError:
            continue
    logger.debug("no system font found for the overlay label; falling back to PIL's default")
    return ImageFont.load_default()


def _badge_frame(state: OverlayState, now_s: float) -> int:
    """Which animation frame to draw for ``state`` at wall-clock ``now_s``.
    Pure function of time, so Overlay never needs its own animation timer --
    it just recomputes this on every poll tick and re-renders only when the
    result actually changed.
    """
    ms = now_s * 1000.0
    if state is OverlayState.RECORDING:
        return int(ms // 500) % 2  # slow on/off pulse
    if state is OverlayState.THINKING:
        return int(ms // 90) % 8  # rotating spinner, 8 steps
    return 0


def _elapsed_label(elapsed_s: int) -> str:
    minutes, seconds = divmod(max(0, elapsed_s), 60)
    return f"{minutes}:{seconds:02d}"


def _render_pill(
    state: OverlayState, detail: str, *, badge_frame: int, elapsed_s: int | None
) -> Image.Image:
    """Composite one full pill frame at display resolution (already
    downsampled from the internal supersampled render)."""
    style = _STYLES[state]
    text = detail or style.label
    if state is OverlayState.RECORDING and not detail and elapsed_s is not None:
        text = f"{style.label} · {_elapsed_label(elapsed_s)}"

    compact = state is OverlayState.IDLE and not text
    if compact:
        width = _HEIGHT
    else:
        font = _label_font()
        text_w = font.getlength(text) / _SS
        width = max(_MIN_ACTIVE_WIDTH, _PAD_LEFT + _AVATAR + _GAP + round(text_w) + _PAD_RIGHT)

    height = _HEIGHT
    bw, bh = width * _SS, height * _SS

    canvas = Image.new("RGBA", (bw, bh), (*_KEY_RGB, 255))

    # -- capsule body, a faint top sheen, and a hairline edge for definition
    body = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    body_draw = ImageDraw.Draw(body)
    body_draw.rounded_rectangle((0, 0, bw - 1, bh - 1), radius=bh // 2, fill=(*SURFACE, 255))
    body_draw.rounded_rectangle(
        (2 * _SS, 2 * _SS, bw - 2 * _SS - 1, bh // 2),
        radius=max(0, bh // 2 - 2 * _SS),
        fill=(255, 255, 255, 12),
    )
    body_draw.rounded_rectangle(
        (0, 0, bw - 1, bh - 1), radius=bh // 2, outline=(255, 255, 255, 24), width=_SS
    )
    canvas.alpha_composite(body)

    # -- avatar: a raised circular badge with the mark, ringed in the
    # state's accent color so the state reads even before the label does
    avatar_d = _AVATAR * _SS
    avatar_cx = bw // 2 if compact else _PAD_LEFT * _SS + avatar_d // 2
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
    avatar_draw.ellipse(avatar_box, outline=(*style.accent, 255), width=2 * _SS)
    canvas.alpha_composite(avatar)

    mark = mark_image(round(avatar_d * 0.56), color=(*INK, 255))
    canvas.alpha_composite(
        mark, (avatar_cx - mark.width // 2, avatar_cy - mark.height // 2)
    )

    # -- status badge overlapping the avatar's corner: distinguishes state
    # by shape as well as color (pulse dot / spinner arc / warning glyph),
    # never by color alone
    if state is not OverlayState.IDLE:
        bd = _BADGE_D * _SS
        bx = avatar_box[2] - bd + 3 * _SS
        by = avatar_box[3] - bd + 3 * _SS
        badge = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        badge_draw = ImageDraw.Draw(badge)
        # halo in the capsule's own color first, so the badge reads as
        # "cut into" the avatar rather than overlapping it messily
        badge_draw.ellipse((bx - 2 * _SS, by - 2 * _SS, bx + bd + 2 * _SS, by + bd + 2 * _SS), fill=(*SURFACE, 255))

        if state is OverlayState.RECORDING:
            alpha = 255 if badge_frame == 0 else 130
            badge_draw.ellipse((bx, by, bx + bd, by + bd), fill=(*style.accent, alpha))
        elif state is OverlayState.THINKING:
            start = badge_frame * 45
            badge_draw.arc((bx, by, bx + bd, by + bd), start=start, end=start + 260, fill=(*style.accent, 255), width=3 * _SS)
        elif state is OverlayState.ERROR:
            cx2 = bx + bd / 2
            badge_draw.polygon(
                [(cx2, by), (bx, by + bd), (bx + bd, by + bd)], fill=(*style.accent, 255)
            )
            badge_draw.line(
                [(cx2, by + bd * 0.38), (cx2, by + bd * 0.66)], fill=(*SURFACE, 255), width=2 * _SS
            )
            r = bd * 0.05
            badge_draw.ellipse((cx2 - r, by + bd * 0.76, cx2 + r, by + bd * 0.76 + 2 * r), fill=(*SURFACE, 255))
        canvas.alpha_composite(badge)

    # -- label
    if not compact:
        font = _label_font()
        text_layer = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        text_draw = ImageDraw.Draw(text_layer)
        text_x = (_PAD_LEFT + _AVATAR + _GAP) * _SS
        bbox = text_draw.textbbox((0, 0), text, font=font)
        text_y = (bh - (bbox[3] - bbox[1])) // 2 - bbox[1]
        text_draw.text((text_x, text_y), text, font=font, fill=(*INK, 255))
        canvas.alpha_composite(text_layer)

    return canvas.resize((width, height), Image.LANCZOS)


# ---------------------------------------------------------------------------
# Overlay: the Tk plumbing around the renderer above
# ---------------------------------------------------------------------------

_POLL_MS = 50

#: How long an error is shown before the pill returns to idle. The error
#: state used to be permanent -- nothing ever cleared it, so a single rate
#: limit left "Error" pinned on screen until the next dictation, long after
#: it stopped being true. Long enough to read, short enough that the pill is
#: an ambient indicator again by the time the user looks back at it.
ERROR_HOLD_S = 4.0


class Overlay:
    """Runs Tk's mainloop on the calling thread until :meth:`stop` is called."""

    def __init__(self, *, poll_ms: int = _POLL_MS) -> None:
        self._queue: queue.Queue[tuple[OverlayState, str]] = queue.Queue()
        self._actions: queue.Queue[Callable[[tk.Tk], None]] = queue.Queue()
        self._poll_ms = poll_ms
        self._root: tk.Tk | None = None
        self._canvas: tk.Canvas | None = None
        self._photo: ImageTk.PhotoImage | None = None
        #: Kept alive for the window's lifetime: PhotoImage.__del__ deletes
        #: the underlying Tcl image as soon as the Python wrapper is
        #: collected, regardless of whether Tk's window manager is still
        #: using it as the title-bar/taskbar icon.
        self._icon_photo: ImageTk.PhotoImage | None = None
        self._image_item: int | None = None
        self._state = OverlayState.IDLE
        self._detail = ""
        self._render_key: tuple | None = None
        self._recording_started_at: float | None = None
        self._error_since: float | None = None

    @property
    def root(self) -> tk.Tk | None:
        """The live Tk root while :meth:`run` is active, else ``None``."""
        return self._root

    def set_state(self, state: OverlayState, detail: str = "") -> None:
        """Thread-safe. Call from any thread; applied on the next poll tick."""
        self._queue.put((state, detail))

    def run_on_ui_thread(self, action: Callable[[tk.Tk], None]) -> None:
        """Thread-safe. Runs ``action(root)`` on the Tk thread on the next
        poll tick -- the only safe way for another thread (the tray icon, a
        dictation worker) to touch Tk widgets, e.g. to open
        :class:`~flow.history_window.HistoryWindow`. A no-op if the overlay
        isn't currently running.
        """
        self._actions.put(action)

    def run(self) -> None:
        """Blocks the calling thread until :meth:`stop` or the window is destroyed."""
        key_hex = "#%02x%02x%02x" % _KEY_RGB

        root = tk.Tk()
        self._root = root
        root.title("Flow")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=key_hex)

        shaped = False
        try:
            root.wm_attributes("-transparentcolor", key_hex)
            shaped = True
        except tk.TclError:
            # Not every Tk build supports color-keyed transparency (only
            # Windows does); fall back to whole-window translucency so the
            # app still runs, just as a visible rounded-ish rectangle.
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

        self._apply_frame(state=OverlayState.IDLE, detail="", force=True)

        def poll() -> None:
            changed = False
            try:
                while True:
                    self._state, self._detail = self._queue.get_nowait()
                    changed = True
            except queue.Empty:
                pass

            now = time.monotonic()
            if self._state is OverlayState.RECORDING:
                if self._recording_started_at is None:
                    self._recording_started_at = now
            else:
                self._recording_started_at = None

            # Errors are transient notices, not a mode: expire them back to
            # idle so the pill never lies about the current state.
            if self._state is OverlayState.ERROR:
                if self._error_since is None:
                    self._error_since = now
                elif now - self._error_since >= ERROR_HOLD_S:
                    self._state, self._detail = OverlayState.IDLE, ""
                    self._error_since = None
                    changed = True
            else:
                self._error_since = None

            self._apply_frame(state=self._state, detail=self._detail, force=changed)

            try:
                while True:
                    action = self._actions.get_nowait()
                    try:
                        action(root)
                    except Exception:
                        logger.exception("overlay UI action failed")
            except queue.Empty:
                pass

            # Rescheduling every tick also lets a Ctrl+C at the console
            # interrupt mainloop() promptly instead of blocking indefinitely
            # in Tk's C event loop.
            root.after(self._poll_ms, poll)

        root.after(self._poll_ms, poll)
        try:
            root.mainloop()
        finally:
            self._root = None
            self._canvas = None
            self._photo = None
            self._icon_photo = None
            self._image_item = None
            self._render_key = None

    def _apply_frame(self, *, state: OverlayState, detail: str, force: bool) -> None:
        assert self._canvas is not None
        now = time.monotonic()
        badge_frame = _badge_frame(state, now)
        elapsed_s = (
            int(now - self._recording_started_at) if self._recording_started_at is not None else None
        )
        key = (state, detail, badge_frame, elapsed_s)
        if not force and key == self._render_key:
            return
        self._render_key = key

        image = _render_pill(state, detail, badge_frame=badge_frame, elapsed_s=elapsed_s)
        photo = ImageTk.PhotoImage(image)
        self._photo = photo  # keep a reference; Tk drops images with none

        canvas = self._canvas
        w, h = image.size
        if self._image_item is None:
            canvas.config(width=w, height=h)
            self._image_item = canvas.create_image(0, 0, anchor="nw", image=photo)
        else:
            current_w = int(canvas.cget("width"))
            if current_w != w:
                canvas.config(width=w, height=h)
            canvas.itemconfig(self._image_item, image=photo)

        root = self._root
        if root is not None:
            current_geometry = root.geometry()
            current_w_geo = int(current_geometry.split("x")[0]) if "x" in current_geometry else -1
            if current_w_geo != w:
                screen_w = root.winfo_screenwidth()
                root.geometry(f"{w}x{h}+{(screen_w - w) // 2}+24")

    def stop(self) -> None:
        root = self._root
        if root is not None:
            try:
                root.after(0, root.destroy)
            except tk.TclError:
                pass  # already destroyed


class NullOverlay:
    """Headless stand-in for :class:`Overlay` when ``[overlay].enabled = false``."""

    def __init__(self) -> None:
        self._stop_event = threading.Event()

    @property
    def root(self) -> None:
        return None

    def set_state(self, state: OverlayState, detail: str = "") -> None:
        pass

    def run_on_ui_thread(self, action: Callable[[tk.Tk], None]) -> None:
        del action
        logger.info("cannot show a window headlessly ([overlay].enabled = false)")

    def run(self) -> None:
        self._stop_event.wait()

    def stop(self) -> None:
        self._stop_event.set()


__all__ = ["NullOverlay", "Overlay", "OverlayState"]

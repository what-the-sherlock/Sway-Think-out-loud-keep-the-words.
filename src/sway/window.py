"""The application window.

This is the difference between "a background process and a localhost page"
and an app. It wraps the dashboard in a real OS window -- taskbar entry, app
icon, no address bar, no tabs, no browser chrome -- using ``pywebview`` over
the WebView2 runtime that ships with Windows 10/11.

**The dependency is soft, deliberately.** ``pywebview`` and WebView2 are both
absent on some machines. Rather than making the whole UI conditional on them,
the engine falls back to the user's browser and everything still works; only
the chrome differs.

**Who owns the main thread.** This is the awkward part, and getting it wrong
is a crash rather than a degradation. ``webview.start()`` refuses outright to
run anywhere but the main thread, and it blocks until the last window closes.
Tk has a softer constraint -- one thread, consistently, which need not be the
main one on Windows.

So the main thread goes to the webview and :class:`~sway.bar.SwayBar` runs on
its own. The loop is started **once, at launch, with the window already
created but hidden**, because ``webview.start()`` needs a window to start
with and cannot be started twice. Opening the app is then just
:meth:`AppWindow.show`, which is instant and can be called from the tray's
thread or an HTTP handler.

Closing the window hides it rather than tearing the loop down, per D1:
quitting is explicit, and a re-open must not have to rebuild WebView2.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Opening size. Matches the reference's proportions while still fitting a
#: 1366x768 laptop screen with room to spare.
DEFAULT_WIDTH = 1180
DEFAULT_HEIGHT = 820
#: DESIGN.md §5.6: below this the layout stops working; enforced here, not
#: in CSS.
MIN_WIDTH = 960
MIN_HEIGHT = 640

#: Where the window's last size, position and maximized state are kept
#: (DESIGN.md §5.6), under the data directory.
WINDOW_STATE_FILENAME = "window.json"

#: Positions further off-screen than this are treated as stale (a monitor
#: that has since been unplugged) and dropped, so the window never reopens
#: somewhere the user cannot see it.
_MAX_COORD = 20_000


def load_window_state(path: Path | None) -> dict[str, Any]:
    """The saved geometry, validated, or ``{}``. Never raises.

    Width and height are clamped to the minimum size; a position is kept
    only when both coordinates are plausible, and a missing or corrupt file
    is simply "no saved state".
    """
    if path is None:
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    state: dict[str, Any] = {}
    width, height = raw.get("width"), raw.get("height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        state["width"] = max(MIN_WIDTH, min(width, _MAX_COORD))
        state["height"] = max(MIN_HEIGHT, min(height, _MAX_COORD))
    x, y = raw.get("x"), raw.get("y")
    if (
        isinstance(x, int) and isinstance(y, int)
        and -_MAX_COORD < x < _MAX_COORD and -200 < y < _MAX_COORD
    ):
        state["x"], state["y"] = x, y
    if raw.get("maximized") is True:
        state["maximized"] = True
    return state


def save_window_state(path: Path | None, state: dict[str, Any]) -> None:
    """Persist geometry. Best-effort: a failed write costs only the restore."""
    if path is None:
        return
    keep = {k: v for k, v in state.items() if k in {"width", "height", "x", "y", "maximized"}}
    try:
        Path(path).write_text(json.dumps(keep), encoding="utf-8")
    except OSError:
        logger.debug("could not save the window state", exc_info=True)


def webview_available() -> bool:
    """Whether a native window can be built on this machine.

    Checks the import only. Whether the WebView2 *runtime* is present is not
    knowable without trying, so :meth:`AppWindow.prepare` also has to handle
    failure.
    """
    try:
        import webview  # noqa: F401
    except Exception:
        logger.debug("pywebview is not available", exc_info=True)
        return False
    return True


class AppWindow:
    """One native window onto the dashboard.

    Lifecycle, in order, and the order matters:

    1. :meth:`prepare` on the main thread, at launch -- builds the window
       hidden.
    2. :meth:`serve_forever` on the main thread -- blocks for the process's
       life.
    3. :meth:`show` from anywhere, any number of times.
    4. :meth:`destroy` at shutdown.
    """

    def __init__(
        self,
        url: str,
        *,
        title: str = "Sway",
        on_close: Callable[[], None] | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._url = url
        self._title = title
        self._on_close = on_close
        self._window: Any = None
        self._lock = threading.RLock()
        self._ready = False
        self._state_path = state_path
        self._geometry: dict[str, Any] = load_window_state(state_path)
        #: The first page load has completed; until then show() is deferred
        #: so the user never sees an empty frame (DESIGN.md §13).
        self._loaded = threading.Event()
        #: Set by destroy(). The closing handler normally cancels a close and
        #: hides instead; during shutdown it must let the close through, or
        #: the webview loop never ends and the process outlives Quit.
        self._destroying = threading.Event()
        self._show_pending = False

    @property
    def ready(self) -> bool:
        """Whether a window exists and :meth:`show` will work."""
        with self._lock:
            return self._ready and self._window is not None

    def prepare(self) -> bool:
        """Build the window, hidden. Main thread, before :meth:`serve_forever`.

        Returns False if no native window is possible, which is the caller's
        signal to keep the main thread for the bar and fall back to a browser.
        """
        try:
            import webview
        except Exception:
            logger.info("pywebview unavailable; the window will open in a browser instead")
            return False

        saved = self._geometry
        try:
            self._window = webview.create_window(
                self._title,
                self._url,
                width=saved.get("width", DEFAULT_WIDTH),
                height=saved.get("height", DEFAULT_HEIGHT),
                x=saved.get("x"),
                y=saved.get("y"),
                min_size=(MIN_WIDTH, MIN_HEIGHT),
                # DESIGN.md §13: the canvas colour, so there is no flash of a
                # different colour before the first paint.
                background_color="#F5F3EF",
                # Nothing here should feel like a document: no "view source",
                # no text-selection cursor on chrome.
                text_select=False,
                # Built now, shown on demand. webview.start() needs a window
                # and can only be called once, so the window has to exist
                # from launch even though the user has not asked for it.
                hidden=True,
            )
        except Exception:
            logger.warning("could not create the application window", exc_info=True)
            self._window = None
            return False

        def _closing() -> bool:
            """Hide instead of closing, so the tray keeps the app alive."""
            save_window_state(self._state_path, self._geometry)
            if self._destroying.is_set():
                # Quitting: allow the close, which ends webview.start().
                return True
            try:
                self._window.hide()
            except Exception:
                logger.debug("could not hide the window on close", exc_info=True)
                return True
            if self._on_close is not None:
                try:
                    self._on_close()
                except Exception:
                    logger.exception("window close handler failed")
            return False

        try:
            self._window.events.closing += _closing
        except Exception:
            logger.debug("could not attach the closing handler", exc_info=True)

        # Geometry is tracked from events rather than read on close: reading
        # the window's size property blocks on the GUI thread.
        def _resized(width: int, height: int) -> None:
            if not self._geometry.get("maximized"):
                self._geometry.update(width=int(width), height=int(height))

        def _moved(x: int, y: int) -> None:
            if not self._geometry.get("maximized"):
                self._geometry.update(x=int(x), y=int(y))

        def _maximized() -> None:
            self._geometry["maximized"] = True

        def _restored() -> None:
            self._geometry.pop("maximized", None)

        def _loaded() -> None:
            if self._loaded.is_set():
                return
            self._loaded.set()
            with self._lock:
                pending, self._show_pending = self._show_pending, False
            if pending:
                self._present()

        for name, handler in (
            ("resized", _resized), ("moved", _moved), ("maximized", _maximized),
            ("restored", _restored), ("loaded", _loaded),
        ):
            try:
                event = getattr(self._window.events, name)
                event += handler
            except Exception:
                logger.debug("could not attach the %s handler", name, exc_info=True)

        self._ready = True
        return True

    def serve_forever(self) -> None:
        """Run the webview loop. Main thread only; blocks until shutdown."""
        try:
            import webview

            webview.start()
        except Exception:
            logger.warning("the window runtime stopped", exc_info=True)
        finally:
            with self._lock:
                self._window = None
                self._ready = False

    def show(self) -> bool:
        """Bring the window up. Safe from any thread. Never raises."""
        with self._lock:
            window = self._window if self._ready else None
            if window is not None and not self._loaded.is_set():
                # Shown the moment the first page has loaded instead, so no
                # blank frame ever appears.
                self._show_pending = True
                return True
        if window is None:
            return False
        return self._present()

    def _present(self) -> bool:
        with self._lock:
            window = self._window if self._ready else None
        if window is None:
            return False
        try:
            window.show()
            if self._geometry.get("maximized"):
                window.maximize()
            else:
                # restore() un-minimises; show() alone leaves a minimised
                # window minimised, which reads as "the tray item did nothing".
                window.restore()
            return True
        except Exception:
            logger.warning("could not show the window", exc_info=True)
            return False

    def navigate(self, url: str) -> None:
        """Point the window at a different URL. Best-effort."""
        with self._lock:
            window = self._window if self._ready else None
        if window is None:
            return
        try:
            window.load_url(url)
        except Exception:
            logger.debug("could not navigate the window", exc_info=True)

    def hide(self) -> None:
        with self._lock:
            window = self._window if self._ready else None
        if window is None:
            return
        try:
            window.hide()
        except Exception:
            logger.debug("could not hide the window", exc_info=True)

    def destroy(self) -> None:
        """Tear the window down. Safe when it was never opened."""
        self._destroying.set()
        with self._lock:
            window, self._window = self._window, None
            self._ready = False
        if window is None:
            return
        try:
            window.destroy()
        except Exception:
            logger.debug("could not destroy the window", exc_info=True)


def open_in_browser(url: str) -> bool:
    """Last resort. Returns whether anything was opened."""
    from sway.platform import get_platform

    try:
        return get_platform().open_url(url)
    except Exception:
        logger.warning("could not open the dashboard in a browser", exc_info=True)
        return False


__all__ = [
    "DEFAULT_HEIGHT",
    "DEFAULT_WIDTH",
    "WINDOW_STATE_FILENAME",
    "AppWindow",
    "load_window_state",
    "save_window_state",
    "open_in_browser",
    "webview_available",
]

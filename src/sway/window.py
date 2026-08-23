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

import logging
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Opening size. Matches the reference's proportions while still fitting a
#: 1366x768 laptop screen with room to spare.
DEFAULT_WIDTH = 1180
DEFAULT_HEIGHT = 820
MIN_WIDTH = 940
MIN_HEIGHT = 600


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
    ) -> None:
        self._url = url
        self._title = title
        self._on_close = on_close
        self._window: Any = None
        self._lock = threading.RLock()
        self._ready = False

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

        try:
            self._window = webview.create_window(
                self._title,
                self._url,
                width=DEFAULT_WIDTH,
                height=DEFAULT_HEIGHT,
                min_size=(MIN_WIDTH, MIN_HEIGHT),
                background_color="#F6F4F0",
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
        if window is None:
            return False
        try:
            window.show()
            # restore() un-minimises; show() alone leaves a minimised window
            # minimised, which reads as "the tray item did nothing".
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
    "AppWindow",
    "open_in_browser",
    "webview_available",
]

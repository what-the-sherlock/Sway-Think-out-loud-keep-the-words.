"""System tray icon and menu (Phase 5): Show History, Quit.

pystray (+ Pillow for the icon image) is the only realistic cross-platform
way to put an icon in the notification area (Windows), menu bar (macOS), or
app indicator (Linux) without hand-rolling ``Shell_NotifyIcon`` in ctypes for
Windows and then a second, unrelated implementation per other OS later --
that would work against CLAUDE.md's ``platform/`` isolation goal (one
interface, OS-specific code behind it) rather than for it.

Best-effort by design: :meth:`TrayIcon.start` never raises. If pystray or its
Pillow dependency can't be imported or the backend can't be started (no
notification area, an unusual desktop environment), it logs a warning and
returns ``False``; :class:`~flow.engine.Engine` just runs without a tray icon.

Not unit tested, like :mod:`flow.overlay`: this is OS/desktop-environment
wiring around a third-party event loop, not transcript-processing logic.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_ICON_SIZE = 64


def _build_icon_image() -> Any:
    # Shared with the overlay pill and every window's title-bar icon (see
    # flow.branding) so the tray icon visibly belongs to the same app rather
    # than being yet another ad-hoc shape.
    from flow.branding import app_badge_image

    return app_badge_image(_ICON_SIZE)


class TrayIcon:
    """Runs pystray's icon loop on its own background thread.

    Windows (the only platform CLAUDE.md targets for real today) supports
    running that loop off the main thread; a future macOS adapter will need
    this coordinated with Tk's main-thread requirement, noted here rather
    than solved speculatively.
    """

    def __init__(self, *, on_show_history: Callable[[], None], on_quit: Callable[[], None]) -> None:
        self._on_show_history = on_show_history
        self._on_quit = on_quit
        self._icon: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Returns ``False`` (after logging why) if no tray icon could be
        started at all; never raises.
        """
        try:
            import pystray

            image = _build_icon_image()
            menu = pystray.Menu(
                pystray.MenuItem("Show History", self._handle_show_history),
                pystray.MenuItem("Quit", self._handle_quit),
            )
            icon = pystray.Icon("flow", image, "Flow", menu)
        except Exception:
            logger.warning("tray icon unavailable (pystray/Pillow not usable)", exc_info=True)
            return False

        self._icon = icon
        self._thread = threading.Thread(target=self._run, name="flow-tray", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        try:
            self._icon.run()
        except Exception:
            logger.exception("tray icon loop failed")

    def _handle_show_history(self, icon: Any, item: Any) -> None:
        del icon, item
        try:
            self._on_show_history()
        except Exception:
            logger.exception("could not open the history window from the tray")

    def _handle_quit(self, icon: Any, item: Any) -> None:
        del item
        try:
            self._on_quit()
        finally:
            icon.stop()

    def stop(self, *, timeout: float = 2.0) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                logger.exception("error stopping the tray icon")
        if self._thread is not None:
            self._thread.join(timeout)


__all__ = ["TrayIcon"]

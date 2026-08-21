"""System tray icon and menu.

pystray (+ Pillow for the icon image) is the only realistic cross-platform
way to put an icon in the notification area (Windows), menu bar (macOS), or
app indicator (Linux) without hand-rolling ``Shell_NotifyIcon`` in ctypes for
Windows and then a second, unrelated implementation per other OS later --
that would work against CLAUDE.md's ``platform/`` isolation goal (one
interface, OS-specific code behind it) rather than for it.

Menu (D1): Open Dashboard, Agent On/Off, View Logs, Show History, Quit.
Settings is deliberately **absent** until D5 builds the pane behind it -- a
menu item that opens nothing reads as a bug, which is worse than a menu that
does not yet offer the feature.

The icon reflects engine state (:class:`TrayState`), so the notification area
answers "is it listening?" without opening anything. State changes arrive
from other threads (the hotkey listener, the dictation worker), so
:meth:`TrayIcon.set_state` only mutates the icon object, which pystray reads
from its own loop.

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
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

_ICON_SIZE = 64


class TrayState(StrEnum):
    """What the icon is currently telling the user."""

    IDLE = "idle"  # armed, waiting for the hotkey
    LISTENING = "listening"  # hotkey held, capturing audio
    PROCESSING = "processing"  # transcribing / cleaning up
    ERROR = "error"  # last dictation failed
    DISABLED = "disabled"  # agent paused; hotkey disarmed


#: Tooltip per state. The tooltip is the only text a tray icon can show
#: without being clicked, so it carries the state in words as well as colour --
#: colour alone is not something every user can read.
_TOOLTIPS: dict[TrayState, str] = {
    TrayState.IDLE: "Flow — ready",
    TrayState.LISTENING: "Flow — listening",
    TrayState.PROCESSING: "Flow — transcribing",
    TrayState.ERROR: "Flow — last dictation failed",
    TrayState.DISABLED: "Flow — paused",
}


def _build_icon_image(state: TrayState) -> Any:
    # Shared with the overlay pill and every window's title-bar icon (see
    # flow.branding) so the tray icon visibly belongs to the same app rather
    # than being yet another ad-hoc shape.
    from flow.branding import app_badge_image

    return app_badge_image(_ICON_SIZE, state=str(state))


class TrayIcon:
    """Runs pystray's icon loop on its own background thread.

    Windows (the only platform CLAUDE.md targets for real today) supports
    running that loop off the main thread; a future macOS adapter will need
    this coordinated with Tk's main-thread requirement, noted here rather
    than solved speculatively.
    """

    def __init__(
        self,
        *,
        on_show_history: Callable[[], None],
        on_quit: Callable[[], None],
        on_open_dashboard: Callable[[], None] | None = None,
        on_view_logs: Callable[[], None] | None = None,
        is_listening: Callable[[], bool] | None = None,
        set_listening: Callable[[bool], None] | None = None,
    ) -> None:
        self._on_show_history = on_show_history
        self._on_open_dashboard = on_open_dashboard
        self._on_view_logs = on_view_logs
        self._on_quit = on_quit
        self._is_listening = is_listening
        self._set_listening = set_listening
        self._icon: Any = None
        self._thread: threading.Thread | None = None
        self._state = TrayState.IDLE

    def start(self) -> bool:
        """Returns ``False`` (after logging why) if no tray icon could be
        started at all; never raises.
        """
        try:
            import pystray

            image = _build_icon_image(self._state)
            items = []
            if self._on_open_dashboard is not None:
                # Default: this is what a double-click on the tray icon runs,
                # and the dashboard is now the app's main window in spirit.
                items.append(
                    pystray.MenuItem(
                        "Open Dashboard", self._handle_open_dashboard, default=True
                    )
                )
            if self._set_listening is not None and self._is_listening is not None:
                # A checkbox rather than two items: "Agent On" and "Agent Off"
                # as separate entries makes the *current* state ambiguous,
                # which is the one thing this menu exists to answer.
                items.append(
                    pystray.MenuItem(
                        "Agent enabled",
                        self._handle_toggle_agent,
                        checked=lambda _item: self._checked(),
                    )
                )
            items.append(pystray.MenuItem("Show History", self._handle_show_history))
            if self._on_view_logs is not None:
                items.append(pystray.MenuItem("View Logs", self._handle_view_logs))
            items += [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", self._handle_quit),
            ]
            menu = pystray.Menu(*items)
            icon = pystray.Icon("flow", image, _TOOLTIPS[self._state], menu)
        except Exception:
            logger.warning("tray icon unavailable (pystray/Pillow not usable)", exc_info=True)
            return False

        self._icon = icon
        self._thread = threading.Thread(target=self._run, name="flow-tray", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        """The pystray loop, on its own thread.

        Wrapped so a backend that dies takes the tray icon down and nothing
        else: the engine holds no reference to this thread's outcome, and
        dictation does not depend on the tray existing.

        The two INFO lines around ``run()`` exist because "is the message loop
        actually running?" was assumed rather than known while debugging an
        icon that rendered but ignored every click. Without them, a loop that
        never starts and a loop that starts but receives nothing look
        identical from the log.
        """
        self._install_notify_probe()
        logger.info("tray: message loop starting")
        try:
            self._icon.run()
        except Exception:
            logger.exception("tray icon loop failed")
        finally:
            logger.info("tray: message loop exited")

    def _install_notify_probe(self) -> None:
        """Log every mouse message Windows delivers to the icon.

        Reaches into pystray's private ``_on_notify`` because there is no
        public hook, and the question it answers cannot be answered any other
        way: an icon that receives no messages at all (an orphan left behind
        by a crashed process, or a failed registration) is indistinguishable
        from one whose messages are being mis-decoded, and the fixes for
        those are completely different.

        Entirely optional. If the attribute is missing -- another backend,
        another pystray version -- this silently does nothing rather than
        breaking the tray it exists to diagnose.
        """
        icon = self._icon
        original = getattr(icon, "_on_notify", None)
        if original is None or not callable(original):
            logger.debug("tray: no _on_notify to probe (backend %s)", type(icon).__name__)
            return

        def _probe(wparam, lparam):
            # WM_LBUTTONUP is 0x0202, WM_RBUTTONUP 0x0205. Newer shell
            # versions pack LOWORD(event)/HIWORD(iconId) into lparam, so the
            # low word is logged separately -- pystray compares the whole
            # value, which silently stops matching if that ever applies here.
            logger.info(
                "tray: _on_notify wparam=%s lparam=%s (low=0x%04X)",
                wparam,
                lparam,
                lparam & 0xFFFF,
            )
            return original(wparam, lparam)

        try:
            icon._on_notify = _probe
            # pystray dispatches through a dict built in __init__, so
            # rebinding the attribute alone is not enough.
            handlers = getattr(icon, "_message_handlers", None)
            if isinstance(handlers, dict):
                for message, handler in list(handlers.items()):
                    if handler == original:
                        handlers[message] = _probe
            logger.debug("tray: notify probe installed")
        except Exception:
            logger.debug("tray: could not install the notify probe", exc_info=True)

    # -- state -------------------------------------------------------------

    def set_state(self, state: TrayState) -> None:
        """Update the icon and tooltip. Safe to call from any thread.

        Swallows every failure: the tray is a status display, and a desktop
        environment that dislikes an icon update must not be able to break a
        dictation that is midway through.
        """
        if state == self._state:
            return
        self._state = state
        icon = self._icon
        if icon is None:
            return
        try:
            icon.icon = _build_icon_image(state)
            icon.title = _TOOLTIPS[state]
        except Exception:
            logger.debug("could not update the tray icon state", exc_info=True)

    def _checked(self) -> bool:
        try:
            return bool(self._is_listening()) if self._is_listening else False
        except Exception:
            logger.debug("could not read the listening state for the tray", exc_info=True)
            return False

    # -- menu handlers -----------------------------------------------------

    def _handle_toggle_agent(self, icon: Any, item: Any) -> None:
        logger.info("tray: %s clicked", "Agent enabled")
        del item
        try:
            if self._set_listening is None or self._is_listening is None:
                return
            self._set_listening(not self._is_listening())
            # pystray only re-reads a checkbox when the menu is rebuilt.
            icon.update_menu()
        except Exception:
            logger.exception("could not toggle the agent from the tray")

    def _handle_show_history(self, icon: Any, item: Any) -> None:
        logger.info("tray: %s clicked", "Show History")
        del icon, item
        try:
            self._on_show_history()
        except Exception:
            logger.exception("could not open the history window from the tray")

    def _handle_open_dashboard(self, icon: Any, item: Any) -> None:
        logger.info("tray: %s clicked", "Open Dashboard")
        del icon, item
        try:
            if self._on_open_dashboard is not None:
                self._on_open_dashboard()
        except Exception:
            logger.exception("could not open the dashboard from the tray")

    def _handle_view_logs(self, icon: Any, item: Any) -> None:
        logger.info("tray: %s clicked", "View Logs")
        del icon, item
        try:
            if self._on_view_logs is not None:
                self._on_view_logs()
        except Exception:
            logger.exception("could not open the log file from the tray")

    def _handle_quit(self, icon: Any, item: Any) -> None:
        logger.info("tray: %s clicked", "Quit")
        del item
        try:
            self._on_quit()
        finally:
            icon.stop()

    def stop(self, *, timeout: float = 2.0) -> None:
        """Remove the icon and stop its loop. Never raises.

        ``visible = False`` first, then ``stop()``. The two are not
        redundant: the visible setter issues ``Shell_NotifyIcon(NIM_DELETE)``
        immediately, while ``stop()`` only posts ``WM_STOP`` and leaves the
        actual removal to the mainloop's own cleanup. If that loop is wedged
        or already dead, the removal never happens and Windows keeps painting
        the icon after the process is gone -- an orphan that looks exactly
        like a working tray icon but ignores every click, which is precisely
        the confusion this app has already caused once.
        """
        icon = self._icon
        if icon is not None:
            try:
                icon.visible = False
            except Exception:
                logger.debug("could not hide the tray icon before stopping", exc_info=True)
            try:
                icon.stop()
            except Exception:
                logger.exception("error stopping the tray icon")
        if self._thread is not None:
            self._thread.join(timeout)


__all__ = ["TrayIcon", "TrayState"]

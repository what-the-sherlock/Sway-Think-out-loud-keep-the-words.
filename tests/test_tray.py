"""D1: the tray icon's wiring.

flow.tray is normally exempt from unit testing -- it is OS wiring around a
third-party event loop, per its own module docstring. This file tests the
narrow part that is not: that :meth:`TrayIcon.start` actually resolves every
attribute it references, and that the menu is assembled from the callbacks
it was given.

That exemption is exactly how a real bug shipped. ``start()`` launches a
thread with ``target=self._run``, and a wholesale rewrite of the module
dropped ``_run`` while leaving the reference. Nothing imported the missing
name, so import-time checks and the whole existing suite stayed green; the
app died at runtime with ``'TrayIcon' object has no attribute '_run'`` the
moment the tray started. A fake pystray costs ~40 lines and closes that hole
without pretending to test the notification area itself.
"""

from __future__ import annotations

import sys
import types

import pytest

from flow.tray import TrayIcon, TrayState


class _FakeIcon:
    """Stands in for ``pystray.Icon``. Records rather than draws."""

    def __init__(self, name, image, title, menu):
        self.name = name
        self.icon = image
        self.title = title
        self.menu = menu
        self.ran = False
        self.stopped = False
        self.menu_updates = 0

    def run(self) -> None:
        self.ran = True

    def stop(self) -> None:
        self.stopped = True

    def update_menu(self) -> None:
        self.menu_updates += 1


class _FakeMenuItem:
    def __init__(self, text, action, checked=None, default=False):
        self.text = text
        self.action = action
        self.checked = checked
        self.default = default


@pytest.fixture
def fake_pystray(monkeypatch):
    """Install a fake ``pystray`` for the duration of one test.

    ``flow.tray`` imports pystray *inside* ``start()`` precisely so a machine
    without a notification area can still import the module, which is what
    makes this injection possible without touching the production code.
    """
    module = types.ModuleType("pystray")
    module.Icon = _FakeIcon
    module.MenuItem = _FakeMenuItem
    module.Menu = lambda *items: list(items)
    module.Menu.SEPARATOR = object()
    monkeypatch.setitem(sys.modules, "pystray", module)
    return module


def _tray(**overrides):
    kwargs = {
        "on_show_history": lambda: None,
        "on_quit": lambda: None,
        "on_open_dashboard": lambda: None,
        "on_view_logs": lambda: None,
        "is_listening": lambda: True,
        "set_listening": lambda _value: None,
    }
    kwargs.update(overrides)
    return TrayIcon(**kwargs)


def test_start_runs_the_icon_loop(fake_pystray) -> None:
    """The regression test for the dropped ``_run``.

    Starting the tray must actually reach ``icon.run()`` on its thread. The
    bug this pins raised AttributeError inside the thread bootstrap, so the
    icon was created but never ran.
    """
    tray = _tray()
    assert tray.start() is True
    tray.stop(timeout=2.0)

    assert tray._icon.ran is True, "the pystray loop was never entered"
    assert tray._icon.stopped is True
    assert tray._thread is not None and not tray._thread.is_alive()


def test_menu_contains_the_d1_items(fake_pystray) -> None:
    tray = _tray()
    assert tray.start() is True
    tray.stop(timeout=2.0)

    labels = [item.text for item in tray._icon.menu if isinstance(item, _FakeMenuItem)]
    assert labels == [
        "Open Dashboard",
        "Agent enabled",
        "Show History",
        "View Logs",
        "Quit",
    ]
    # Settings is deferred to D5, when there is a pane behind it. A menu item
    # that opens nothing reads as a bug.
    assert "Settings" not in labels


def test_optional_items_are_omitted_when_no_callback_is_given(fake_pystray) -> None:
    tray = _tray(on_open_dashboard=None, on_view_logs=None, set_listening=None)
    assert tray.start() is True
    tray.stop(timeout=2.0)

    labels = [item.text for item in tray._icon.menu if isinstance(item, _FakeMenuItem)]
    assert labels == ["Show History", "Quit"]


def test_toggling_the_agent_flips_the_state_and_refreshes_the_menu(fake_pystray) -> None:
    listening = {"value": True}
    tray = _tray(
        is_listening=lambda: listening["value"],
        set_listening=lambda value: listening.__setitem__("value", value),
    )
    tray.start()
    tray.stop(timeout=2.0)

    toggle = next(i for i in tray._icon.menu if getattr(i, "text", "") == "Agent enabled")
    assert toggle.checked(toggle) is True

    toggle.action(tray._icon, toggle)
    assert listening["value"] is False
    assert toggle.checked(toggle) is False
    # pystray only re-reads a checkbox when the menu is rebuilt.
    assert tray._icon.menu_updates == 1


def test_set_state_updates_icon_and_tooltip(fake_pystray) -> None:
    tray = _tray()
    tray.start()
    tray.stop(timeout=2.0)

    before = tray._icon.icon
    tray.set_state(TrayState.LISTENING)
    assert tray._icon.icon is not before
    assert "listening" in tray._icon.title.lower()

    tray.set_state(TrayState.DISABLED)
    assert "paused" in tray._icon.title.lower()


def test_set_state_before_start_does_not_raise() -> None:
    """State can arrive from the engine before the icon exists, or on a
    machine where the tray never started at all."""
    tray = _tray()
    tray.set_state(TrayState.ERROR)  # must be a no-op, not an AttributeError


def test_start_returns_false_when_pystray_is_unavailable(monkeypatch) -> None:
    """A machine with no usable tray must degrade, never raise: the engine
    treats False as 'run without a tray icon'."""
    monkeypatch.setitem(sys.modules, "pystray", None)
    assert _tray().start() is False


def test_a_failing_callback_does_not_escape_the_handler(fake_pystray) -> None:
    """Handlers run on pystray's thread, where an exception would kill the
    tray loop and take the icon away for the rest of the session."""

    def _boom() -> None:
        raise RuntimeError("no history store")

    tray = _tray(on_show_history=_boom)
    tray.start()
    tray.stop(timeout=2.0)

    item = next(i for i in tray._icon.menu if getattr(i, "text", "") == "Show History")
    item.action(tray._icon, item)  # must swallow, not propagate


# -- ghost hygiene and delivery diagnostics ----------------------------------


def test_stop_hides_the_icon_before_stopping_the_loop(fake_pystray) -> None:
    """The orphan-icon guard.

    ``stop()`` alone only posts WM_STOP and leaves removal to the mainloop's
    cleanup; if that loop is wedged or dead, Windows keeps painting the icon
    after the process exits. Setting ``visible = False`` issues NIM_DELETE
    directly, so removal does not depend on a healthy loop. Order matters --
    hiding after stopping would be too late.
    """
    order: list[str] = []

    class _RecordingIcon(_FakeIcon):
        @property
        def visible(self):
            return self._visible

        @visible.setter
        def visible(self, value):
            self._visible = value
            order.append(f"visible={value}")

        def stop(self):
            order.append("stop")
            super().stop()

    fake_pystray.Icon = _RecordingIcon
    tray = _tray()
    tray.start()
    tray.stop(timeout=2.0)

    assert order == ["visible=False", "stop"]


def test_stop_still_stops_when_hiding_fails(fake_pystray) -> None:
    """A backend that refuses `visible` must not prevent the loop stopping."""

    class _UnhidableIcon(_FakeIcon):
        @property
        def visible(self):
            return True

        @visible.setter
        def visible(self, value):
            raise RuntimeError("backend does not support hiding")

    fake_pystray.Icon = _UnhidableIcon
    tray = _tray()
    tray.start()
    tray.stop(timeout=2.0)

    assert tray._icon.stopped is True


def test_notify_probe_is_optional(fake_pystray) -> None:
    """The fake icon has no ``_on_notify``. Installing the probe must be a
    silent no-op rather than breaking the tray it exists to diagnose."""
    tray = _tray()
    assert tray.start() is True
    tray.stop(timeout=2.0)
    assert tray._icon.ran is True

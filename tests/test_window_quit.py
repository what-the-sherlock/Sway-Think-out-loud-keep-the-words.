"""Quit must really close the app window.

The closing handler hides the window and cancels the close (so the X button
keeps the app in the tray). pywebview fires that same event for
``destroy()``; if shutdown's destroy were cancelled too, ``webview.start()``
would never return and the process would outlive Quit.
"""

from __future__ import annotations

import sys
import types

from sway.window import AppWindow


class _Event:
    def __init__(self) -> None:
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _FakeWebviewWindow:
    """Mimics pywebview: any closing handler returning False cancels the close."""

    def __init__(self) -> None:
        self.events = types.SimpleNamespace(
            closing=_Event(), resized=_Event(), moved=_Event(),
            maximized=_Event(), restored=_Event(), loaded=_Event(),
        )
        self.closed = False
        self.hidden = False

    def _try_close(self) -> None:
        results = [handler() for handler in self.events.closing.handlers]
        if False not in results:
            self.closed = True

    def destroy(self) -> None:
        self._try_close()

    def hide(self) -> None:
        self.hidden = True


def _window_with_fake_webview(monkeypatch, tmp_path):
    fake = _FakeWebviewWindow()
    module = types.ModuleType("webview")
    module.create_window = lambda *a, **k: fake
    monkeypatch.setitem(sys.modules, "webview", module)
    window = AppWindow("http://127.0.0.1:1/", state_path=tmp_path / "w.json")
    assert window.prepare()
    return window, fake


def test_user_closing_the_window_only_hides_it(monkeypatch, tmp_path):
    _window, fake = _window_with_fake_webview(monkeypatch, tmp_path)
    fake._try_close()  # the user clicks X
    assert fake.hidden
    assert not fake.closed


def test_destroy_at_shutdown_really_closes_it(monkeypatch, tmp_path):
    window, fake = _window_with_fake_webview(monkeypatch, tmp_path)
    window.destroy()
    assert fake.closed

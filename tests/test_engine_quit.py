"""Quitting from the tray must shut the whole app down, not just the bar.

Regression: the tray's Quit was wired to the bar's stop only. With the app
window available the main thread runs the webview loop, so the process kept
running (hotkey hooks and mic included) after its tray icon disappeared.
"""

from __future__ import annotations

import threading

import sway.engine as engine_mod
from sway.engine import Engine


def _bare_engine() -> Engine:
    eng = Engine.__new__(Engine)
    eng._stop_lock = threading.Lock()
    eng._stopped = False
    eng._stopping = False
    return eng


def test_tray_quit_arms_the_watchdog_and_runs_the_full_shutdown(monkeypatch):
    armed = []
    monkeypatch.setattr(engine_mod, "arm_exit_watchdog", lambda: armed.append(True))
    eng = _bare_engine()
    done = threading.Event()
    eng._shutdown_everything = done.set

    eng._quit_from_tray()

    assert armed == [True]
    assert done.wait(2.0), "the full shutdown never ran"


def test_stop_runs_once_even_when_called_from_two_threads():
    eng = _bare_engine()
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def slow_stop_all() -> None:
        calls.append(1)
        entered.set()
        release.wait(2.0)

    eng._stop_all = slow_stop_all
    first = threading.Thread(target=eng.stop)
    first.start()
    assert entered.wait(2.0)
    second = threading.Thread(target=eng.stop)
    second.start()
    release.set()
    first.join(2.0)
    second.join(2.0)

    assert calls == [1]
    assert eng._stopping

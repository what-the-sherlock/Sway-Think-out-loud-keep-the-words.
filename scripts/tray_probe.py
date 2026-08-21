"""Standalone tray-icon probe: does pystray work on this machine at all?

Flow's tray icon renders but ignores every click. Three fixes aimed at menu
dispatch changed nothing, and dispatch verifies correct against real pystray
in isolation -- so the question is no longer "is Flow's menu code right?" but
"do tray clicks reach this process at all?".

This script answers that with nothing but pystray. No engine, no hotkey, no
audio, no Tk, no dashboard. If its icon responds, the platform is fine and
the fault is in how Flow arranges things around it. If this icon is *also*
inert, the fault is below Flow entirely -- pystray, the shell, or an orphaned
icon left behind by a crashed process -- and no amount of work in flow/tray.py
would ever have fixed it.

Run it while Flow is closed, so there is no doubt which icon is which:

    python scripts/tray_probe.py

It prints a line for every message the icon receives. Right-click it, left-
click it, then choose Quit.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pystray  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402


def _image() -> Image.Image:
    """A plain red square. Deliberately not Flow's mark: the whole point is
    to be able to tell this icon apart from any Flow icon, live or orphaned,
    at a glance in a crowded notification area."""
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((4, 4, 59, 59), fill=(220, 40, 40, 255))
    return image


def main() -> int:
    clicks: list[str] = []
    started = time.monotonic()

    def _stamp() -> str:
        return f"[{time.monotonic() - started:6.1f}s]"

    def on_hello(icon, item):
        print(f"{_stamp()} MENU ITEM CLICKED: {item.text}", flush=True)
        clicks.append(str(item.text))

    def on_quit(icon, item):
        print(f"{_stamp()} MENU ITEM CLICKED: Quit", flush=True)
        clicks.append("Quit")
        icon.stop()

    icon = pystray.Icon(
        "tray_probe",
        _image(),
        "Tray probe (red square)",
        pystray.Menu(
            pystray.MenuItem("Say hello", on_hello, default=True),
            pystray.MenuItem("Quit", on_quit),
        ),
    )

    # Same private hook Flow installs, for the same reason: it is the only
    # way to see whether Windows delivers anything at all.
    original = icon._on_notify

    def _probe(wparam, lparam):
        print(
            f"{_stamp()} message received: lparam={lparam} (low=0x{lparam & 0xFFFF:04X})",
            flush=True,
        )
        return original(wparam, lparam)

    icon._on_notify = _probe
    icon._message_handlers[
        next(k for k, v in icon._message_handlers.items() if v == original)
    ] = _probe

    def _heartbeat() -> None:
        """Proves the process is alive even when nothing is being delivered,
        so silence cannot be mistaken for a hung script."""
        while True:
            time.sleep(10.0)
            print(f"{_stamp()} still running; messages so far: {len(clicks)}", flush=True)

    threading.Thread(target=_heartbeat, daemon=True).start()

    print(__doc__)
    print("=" * 70)
    print("Look for a RED SQUARE in the notification area (check the ^ overflow).")
    print("  1. Right-click it  -> a menu with 'Say hello' and 'Quit' should appear")
    print("  2. Left-click it   -> should print a message line")
    print("  3. Choose Quit     -> this script exits")
    print("Every line below is a message Windows delivered. No lines = none arrived.")
    print("=" * 70, flush=True)

    icon.run()

    print("=" * 70)
    if clicks:
        print(f"RESULT: the tray works. {len(clicks)} item(s) clicked: {clicks}")
        print("-> pystray and the shell are fine; the fault is in how Flow uses them.")
    else:
        print("RESULT: no clicks were ever received.")
        print("-> The fault is below Flow: pystray, the shell, or a stale icon.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

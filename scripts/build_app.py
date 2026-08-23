"""One command to get from source to a Sway you can launch from the Start menu.

    python scripts/build_app.py

Does the four things that otherwise have to be remembered in order:

1. Runs PyInstaller against packaging/flow.spec.
2. Checks the build is distributable -- specifically that no API key has been
   copied next to the executable. Keys live in Windows Credential Manager
   (see :mod:`flow.credentials`); a ``.env`` beside ``Sway.exe`` would ship
   the developer's key inside every zip handed to anyone else. The built app
   asks for a key on first run instead, through onboarding.
3. **Deletes ``build/``.** This matters more than it looks. PyInstaller's
   intermediate work directory contains a ``Sway.exe`` that is only a
   bootloader stub -- double-clicking it fails with "Failed to load Python
   DLL python311.dll", because its ``_internal`` runtime lives in ``dist/``.
   It is right next to the real one in a file browser and looks identical.
   Removing it leaves exactly one Sway.exe on disk, which is the correct one.
4. Creates the Start-menu and Desktop shortcuts.

Pass ``--startup`` to also launch Sway at login, or ``--no-shortcuts`` to skip
step 4.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPEC = PROJECT_ROOT / "packaging" / "sway.spec"
BUILD_DIR = PROJECT_ROOT / "build"
DIST_DIR = PROJECT_ROOT / "dist" / "sway"


def _run(label: str, command: list[str]) -> None:
    print(f"\n=== {label} ===")
    result = subprocess.run(command, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        raise SystemExit(f"error: {label} failed (exit {result.returncode})")


def _refuse_if_running() -> None:
    """Stop before PyInstaller touches dist/ if Sway is running.

    PyInstaller clears ``dist/flow`` before it builds. Windows locks a running
    executable, so a build started while Sway is open deletes most of that
    directory, hits ``PermissionError`` on ``Sway.exe`` itself, and aborts --
    leaving a half-erased install: no ``flow-cli.exe``, a partially stripped
    ``_internal``, and, worst of all, no ``.env``. The running app keeps
    working (it is already mapped into memory), so nothing looks wrong until
    the next launch fails with "Sway needs a Groq API key".

    Detecting it up front costs one file check and turns a corrupted install
    into a one-line message. The test is a rename-in-place: Windows refuses
    to rename a file that is mapped as a running image, while a file merely
    sitting on disk renames fine and is put straight back.
    """
    app = DIST_DIR / "Sway.exe"
    if not app.is_file():
        return
    probe = app.with_suffix(".exe.buildcheck")
    try:
        app.rename(probe)
    except OSError:
        raise SystemExit(
            "error: Sway appears to be running, so dist/flow cannot be rebuilt.\n"
            "\n"
            "Quit it from its tray icon (right-click > Quit), confirm no Sway.exe\n"
            "remains in Task Manager > Details, then run this again.\n"
            "\n"
            "Refusing to start: PyInstaller would clear dist/flow first and fail\n"
            "partway, leaving a half-erased install without its .env."
        ) from None
    probe.rename(app)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--startup", action="store_true", help="launch Sway at login")
    parser.add_argument("--no-shortcuts", action="store_true", help="skip shortcut creation")
    args = parser.parse_args(argv)

    _refuse_if_running()

    _run(
        "Building Sway.exe",
        [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--log-level", "WARN"],
    )

    app = DIST_DIR / "Sway.exe"
    if not app.is_file():
        raise SystemExit(f"error: the build produced no {app}")

    # Deliberately NOT copied: the developer's .env.
    #
    # This used to be step 2, so the built app worked immediately on the build
    # machine. That convenience is a distribution hazard -- `dist/` is what
    # gets zipped and handed over, and the key inside it is a live credential
    # that works for whoever receives it. Keys now go to Windows Credential
    # Manager via onboarding, which is per-user and never lands in a build.
    #
    # A stale .env from an older build is removed rather than left in place:
    # it would still be read by flow.config and would silently outrank what
    # the recipient enters in onboarding.
    stale_env = DIST_DIR / ".env"
    if stale_env.is_file():
        stale_env.unlink()
        print(f"removed  {stale_env} (a build must never carry an API key)")
    print("note: Sway will ask for a Groq API key on first run, and store it")
    print("      in Windows Credential Manager -- not in any file it writes.")

    # See the module docstring: this directory holds a decoy Sway.exe that
    # cannot run, and it is the single most likely thing to be double-clicked
    # by mistake.
    if BUILD_DIR.is_dir():
        shutil.rmtree(BUILD_DIR, ignore_errors=True)
        print(f"removed  {BUILD_DIR} (PyInstaller scratch; held a non-working Sway.exe)")

    if not args.no_shortcuts and sys.platform == "win32":
        command = [sys.executable, str(PROJECT_ROOT / "scripts" / "install_shortcuts.py")]
        if args.startup:
            command.append("--startup")
        _run("Creating shortcuts", command)

    print(f"\nDone. Sway is at {app}")
    print("Launch it from the Start menu, or double-click the Desktop shortcut.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

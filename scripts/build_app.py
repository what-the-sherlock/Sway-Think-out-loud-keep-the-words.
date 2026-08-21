"""One command to get from source to a Flow you can launch from the Start menu.

    python scripts/build_app.py

Does the four things that otherwise have to be remembered in order:

1. Runs PyInstaller against packaging/flow.spec.
2. Copies ``.env`` next to the executable, so the built app has the API key
   instead of greeting you with the setup dialog.
3. **Deletes ``build/``.** This matters more than it looks. PyInstaller's
   intermediate work directory contains a ``Flow.exe`` that is only a
   bootloader stub -- double-clicking it fails with "Failed to load Python
   DLL python311.dll", because its ``_internal`` runtime lives in ``dist/``.
   It is right next to the real one in a file browser and looks identical.
   Removing it leaves exactly one Flow.exe on disk, which is the correct one.
4. Creates the Start-menu and Desktop shortcuts.

Pass ``--startup`` to also launch Flow at login, or ``--no-shortcuts`` to skip
step 4.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SPEC = PROJECT_ROOT / "packaging" / "flow.spec"
BUILD_DIR = PROJECT_ROOT / "build"
DIST_DIR = PROJECT_ROOT / "dist" / "flow"


def _run(label: str, command: list[str]) -> None:
    print(f"\n=== {label} ===")
    result = subprocess.run(command, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        raise SystemExit(f"error: {label} failed (exit {result.returncode})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--startup", action="store_true", help="launch Flow at login")
    parser.add_argument("--no-shortcuts", action="store_true", help="skip shortcut creation")
    args = parser.parse_args(argv)

    _run(
        "Building Flow.exe",
        [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--log-level", "WARN"],
    )

    app = DIST_DIR / "Flow.exe"
    if not app.is_file():
        raise SystemExit(f"error: the build produced no {app}")

    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        shutil.copy2(env_file, DIST_DIR / ".env")
        print(f"copied   {env_file.name} -> {DIST_DIR}")
    else:
        print("note: no .env found; Flow will ask for a Groq API key on first run")

    # See the module docstring: this directory holds a decoy Flow.exe that
    # cannot run, and it is the single most likely thing to be double-clicked
    # by mistake.
    if BUILD_DIR.is_dir():
        shutil.rmtree(BUILD_DIR, ignore_errors=True)
        print(f"removed  {BUILD_DIR} (PyInstaller scratch; held a non-working Flow.exe)")

    if not args.no_shortcuts and sys.platform == "win32":
        command = [sys.executable, str(PROJECT_ROOT / "scripts" / "install_shortcuts.py")]
        if args.startup:
            command.append("--startup")
        _run("Creating shortcuts", command)

    print(f"\nDone. Flow is at {app}")
    print("Launch it from the Start menu, or double-click the Desktop shortcut.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

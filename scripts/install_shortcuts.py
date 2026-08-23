"""Create Start-menu / Desktop shortcuts for the packaged app.

The packaged build lives in ``dist/flow/``, which is not somewhere a user
should have to navigate to. This puts Sway where Windows expects an installed
app to be: the Start menu, so it is reachable from the keyboard, and
optionally the Desktop and the per-user Startup folder.

Run it after ``pyinstaller packaging/flow.spec``::

    python scripts/install_shortcuts.py            # Start menu + Desktop
    python scripts/install_shortcuts.py --startup  # also launch at login
    python scripts/install_shortcuts.py --remove   # take them all away again

Shortcuts are created through the WScript.Shell COM object via PowerShell.
That is the only way to write a real ``.lnk`` without adding pywin32 as a
dependency for a task that runs once, outside the app itself.

Windows-only by design, like the rest of packaging/. It refuses to run
elsewhere rather than silently doing nothing.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST_APP = PROJECT_ROOT / "dist" / "sway" / "Sway.exe"

#: What the shortcut is called. Also how it is found again to remove it.
SHORTCUT_NAME = "Sway.lnk"


def _shell_folders() -> dict[str, Path]:
    """Resolve the user's Start menu, Desktop, and Startup directories.

    Read from the environment rather than hardcoded under the profile: a
    redirected/OneDrive Desktop is common, and writing to a hardcoded path
    would create a shortcut the user never sees.
    """
    appdata = Path(os.environ["APPDATA"])
    programs = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    desktop = Path(
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    return {
        "Start menu": programs,
        "Desktop": desktop,
        "Startup": programs / "Startup",
    }


def _create(target: Path, shortcut: Path, *, working_dir: Path, icon: Path | None) -> None:
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    icon_line = f"$s.IconLocation = '{icon}'" if icon and icon.is_file() else ""
    script = f"""
$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut('{shortcut}')
$s.TargetPath = '{target}'
$s.WorkingDirectory = '{working_dir}'
$s.Description = 'Sway - voice dictation'
{icon_line}
$s.Save()
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", script], check=True, capture_output=True
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--startup", action="store_true", help="also launch Sway when you log in"
    )
    parser.add_argument("--remove", action="store_true", help="remove the shortcuts")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("error: this installer is Windows-only", file=sys.stderr)
        return 2

    folders = _shell_folders()
    if args.remove:
        for label, folder in folders.items():
            shortcut = folder / SHORTCUT_NAME
            if shortcut.is_file():
                shortcut.unlink()
                print(f"removed  {label}: {shortcut}")
        return 0

    if not DIST_APP.is_file():
        print(
            f"error: {DIST_APP} does not exist.\n"
            "Build it first:  pyinstaller packaging/flow.spec --noconfirm",
            file=sys.stderr,
        )
        return 2

    icon = PROJECT_ROOT / "packaging" / "sway.ico"
    wanted = ["Start menu", "Desktop"] + (["Startup"] if args.startup else [])
    for label in wanted:
        shortcut = folders[label] / SHORTCUT_NAME
        _create(DIST_APP, shortcut, working_dir=DIST_APP.parent, icon=icon)
        print(f"created  {label}: {shortcut}")

    env_file = DIST_APP.parent / ".env"
    if not env_file.is_file():
        print(
            f"\nnote: {env_file} does not exist, so Sway will start and ask for a\n"
            "Groq API key. Copy your .env next to Sway.exe to avoid that.",
            file=sys.stderr,
        )

    print("\nFlow is now in your Start menu. Press the Windows key and type 'Sway'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

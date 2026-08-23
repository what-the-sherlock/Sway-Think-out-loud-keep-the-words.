"""POSIX (Linux/macOS) implementation of :class:`~flow.platform.PlatformAdapter`.

Sway targets Windows first (CLAUDE.md); this adapter exists so the codebase
and its tests run unmodified on developer machines and CI runners that are
not Windows. Clipboard access shells out to whatever tool is on PATH
(``xclip`` on Linux, ``pbcopy``/``pbpaste`` on macOS) since there is no
POSIX-standard equivalent of the Win32 clipboard API. Untested against real
hardware; Windows is the supported target for now.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from sway.errors import ClipboardError

_CLIPBOARD_TIMEOUT_S = 2.0


class PosixPlatform:
    """XDG Base Directory layout, with the documented fallbacks."""

    name = "posix"

    @staticmethod
    def _xdg(var: str, default: Path) -> Path:
        raw = os.environ.get(var)
        # The XDG spec says relative paths must be ignored.
        if raw and os.path.isabs(raw):
            return Path(raw)
        return default

    def user_data_dir(self, app_name: str) -> Path:
        base = self._xdg("XDG_DATA_HOME", Path.home() / ".local" / "share")
        return base / app_name.lower()

    def user_config_dir(self, app_name: str) -> Path:
        base = self._xdg("XDG_CONFIG_HOME", Path.home() / ".config")
        return base / app_name.lower()

    # -- clipboard -----------------------------------------------------

    def clipboard_get(self) -> str:
        if sys.platform == "darwin":
            return self._run(["pbpaste"])
        return self._run(["xclip", "-selection", "clipboard", "-o"])

    def clipboard_set(self, text: str) -> None:
        if sys.platform == "darwin":
            self._run(["pbcopy"], input_text=text)
            return
        self._run(["xclip", "-selection", "clipboard"], input_text=text)

    def send_paste(self) -> None:
        from pynput.keyboard import Controller, Key

        controller = Controller()
        controller.press(Key.ctrl)
        try:
            controller.press("v")
            controller.release("v")
        finally:
            controller.release(Key.ctrl)

    def send_copy(self) -> None:
        from pynput.keyboard import Controller, Key

        controller = Controller()
        controller.press(Key.ctrl)
        try:
            controller.press("c")
            controller.release("c")
        finally:
            controller.release(Key.ctrl)

    # -- focused window --------------------------------------------------

    def focused_process_name(self) -> str | None:
        """Not implemented: there is no POSIX-standard way to query the
        focused window's owning process without an extra dependency
        (``xdotool``/``wmctrl`` on Linux; an Accessibility-permissioned call
        on macOS). Profile selection falls back to the default profile on
        this platform until it is implemented.
        """
        return None

    def show_message(self, title: str, text: str, *, error: bool = False) -> None:
        """No portable native dialog exists across Linux desktops without an
        extra dependency (zenity/kdialog/notify-send are all optional), and
        this platform has no windowed build anyway -- so stderr, which a
        POSIX run always has, is the honest fallback.
        """
        del error
        print(f"{title}: {text}", file=sys.stderr)

    def acquire_single_instance_lock(self, name: str) -> bool:
        """Not implemented: fails open, so a POSIX run is never blocked from
        starting. Would need an abstract socket or a lockfile with stale-PID
        handling to do properly.
        """
        del name
        return True

    def paste_likely_blocked(self) -> bool:
        """Not implemented: no privilege-elevation check exists for this
        platform yet. Always fails open, same as :meth:`focused_process_name`.
        """
        return False

    def open_url(self, url: str) -> bool:
        """``webbrowser`` first, then ``xdg-open``/``open`` directly."""
        import webbrowser

        try:
            if webbrowser.open(url):
                return True
        except Exception:
            pass
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        if shutil.which(opener) is None:
            return False
        try:
            subprocess.Popen(
                [opener, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError:
            return False
        return True

    def process_is_running(self, pid: int) -> bool:
        """Signal 0 probes for existence without delivering anything.

        ``PermissionError`` means the process exists but belongs to another
        user, which still counts as running.
        """
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def set_launch_at_login(self, app_name: str, command: str | None) -> bool:
        """Not implemented: there is no single answer here across desktop
        environments (systemd user units, XDG autostart, launchd), and
        guessing wrong would leave an entry the user cannot find to remove.
        Reported as unsupported so the UI can hide the toggle rather than
        offer one that silently does nothing."""
        del app_name, command
        return False

    def launches_at_login(self, app_name: str) -> bool:
        del app_name
        return False

    def _run(self, argv: list[str], *, input_text: str | None = None) -> str:
        exe = argv[0]
        if shutil.which(exe) is None:
            raise ClipboardError(f"clipboard access needs '{exe}' on PATH, but it was not found")
        try:
            result = subprocess.run(
                argv,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=_CLIPBOARD_TIMEOUT_S,
                check=True,
            )
        except subprocess.SubprocessError as exc:
            raise ClipboardError(f"clipboard command {argv!r} failed: {exc}") from exc
        return result.stdout


__all__ = ["PosixPlatform"]

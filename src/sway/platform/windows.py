"""Windows implementation of :class:`~flow.platform.PlatformAdapter`.

Clipboard access goes straight through ``ctypes`` against ``user32``/
``kernel32`` rather than a third-party clipboard package: the Win32 API is
~30 lines of ctypes and avoids one more dependency for something the stdlib
platform we already target can do directly.
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from ctypes import wintypes
from pathlib import Path

from pynput.keyboard import Controller, Key

from sway.errors import ClipboardError

logger = logging.getLogger(__name__)

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_OPEN_RETRIES = 8
_OPEN_RETRY_DELAY_S = 0.02
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_MAX_PATH = 260
_TOKEN_QUERY = 0x0008
_TOKEN_ELEVATION = 20  # TOKEN_INFORMATION_CLASS.TokenElevation
#: SPI_GETCLIENTAREAANIMATION. Settings > Accessibility > Visual effects >
#: Animation effects writes this; it is what Edge maps prefers-reduced-motion
#: onto, so honouring it means the overlay agrees with every web surface on
#: the same machine.
_SPI_GETCLIENTAREAANIMATION = 0x1042

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)


class _TokenElevation(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]

_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.argtypes = []
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.argtypes = []
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = wintypes.LPVOID
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.GetCurrentProcess.argtypes = []
_kernel32.GetCurrentProcess.restype = wintypes.HANDLE
_advapi32.OpenProcessToken.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.HANDLE),
]
_advapi32.OpenProcessToken.restype = wintypes.BOOL
_advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
_advapi32.GetTokenInformation.restype = wintypes.BOOL
_user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
_user32.MessageBoxW.restype = ctypes.c_int
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE

_MB_OK = 0x00000000
_MB_ICONERROR = 0x00000010
_MB_ICONINFORMATION = 0x00000040
_MB_SETFOREGROUND = 0x00010000
_MB_TOPMOST = 0x00040000
_ERROR_ALREADY_EXISTS = 183
#: GetExitCodeProcess returns this while a process is still running.
_STILL_ACTIVE = 259
#: HKCU key Windows runs at every interactive logon.
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

#: Holds the single-instance mutex for the process lifetime. A mutex is
#: released when its last handle closes, so this reference must outlive the
#: call that created it -- dropping it would let a second instance start.
_instance_mutex: int | None = None

#: Shared synthetic-input controller. pynput's Controller is cheap to reuse
#: and reusing it avoids re-touching the input subsystem on every paste.
_kb_controller = Controller()


def _enable_dpi_awareness() -> None:
    """Best-effort, run once at import time (before flow.overlay ever creates
    a Tk window). Without this, Windows treats the process as DPI-unaware and
    upscales its windows by bitmap stretching on any scaled display (125%,
    150%, ... -- the common case, not the exception, on modern displays),
    which blurs the overlay pill's anti-aliased edges and desyncs its logical
    window coordinates from physical screen pixels. Tries the modern,
    per-monitor-aware API first, falling back for older Windows builds that
    lack it; failure here just means the process falls back to the (blurry
    but functional) system-scaled default, so every branch is swallowed.
    """
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, Windows 10 1703+.
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE, Windows 8.1+.
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except (AttributeError, OSError):
        pass
    try:
        _user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        logger.debug("could not enable DPI awareness", exc_info=True)


_enable_dpi_awareness()


def _is_process_elevated(handle: int) -> bool | None:
    """Whether the token owning ``handle`` runs at the elevated integrity
    level. ``None`` if this can't be determined -- e.g. the process handle
    lacks even query-limited rights (a protected/system process).

    Querying *whether* a token is elevated needs no special privilege over
    the target process (only TOKEN_QUERY on a handle to it): this is the
    same technique Task Manager's "Elevated" column uses, and it works from
    an unprivileged process precisely because the elevation flag itself
    isn't sensitive information.
    """
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(handle, _TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        info = _TokenElevation()
        returned = wintypes.DWORD()
        if not _advapi32.GetTokenInformation(
            token, _TOKEN_ELEVATION, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned)
        ):
            return None
        return bool(info.TokenIsElevated)
    finally:
        _kernel32.CloseHandle(token)


class WindowsPlatform:
    """Target platform. Uses %LOCALAPPDATA% for both data and config.

    Config would conventionally live in the roaming %APPDATA%, but Sway's
    config is machine-specific (audio device, hotkey) and belongs next to the
    latency database it points at, so both live under LOCALAPPDATA.
    """

    name = "windows"

    def _local_app_data(self) -> Path:
        raw = os.environ.get("LOCALAPPDATA")
        if raw:
            return Path(raw)
        return Path.home() / "AppData" / "Local"

    def user_data_dir(self, app_name: str) -> Path:
        return self._local_app_data() / app_name

    def user_config_dir(self, app_name: str) -> Path:
        return self._local_app_data() / app_name

    # -- clipboard -----------------------------------------------------

    def clipboard_get(self) -> str:
        self._open_clipboard()
        try:
            handle = _user32.GetClipboardData(_CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = _kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                _kernel32.GlobalUnlock(handle)
        finally:
            _user32.CloseClipboard()

    def clipboard_set(self, text: str) -> None:
        payload = (text + "\0").encode("utf-16-le")
        handle = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(payload))
        if not handle:
            raise ClipboardError("GlobalAlloc failed while setting the clipboard")
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            _kernel32.GlobalFree(handle)
            raise ClipboardError("GlobalLock failed while setting the clipboard")
        try:
            ctypes.memmove(ptr, payload, len(payload))
        finally:
            _kernel32.GlobalUnlock(handle)

        self._open_clipboard()
        try:
            _user32.EmptyClipboard()
            if not _user32.SetClipboardData(_CF_UNICODETEXT, handle):
                _kernel32.GlobalFree(handle)
                raise ClipboardError("SetClipboardData failed")
            # SetClipboardData succeeded: the OS now owns `handle`. Freeing it
            # here would free memory the clipboard is holding onto.
        finally:
            _user32.CloseClipboard()

    def _open_clipboard(self) -> None:
        for _ in range(_OPEN_RETRIES):
            if _user32.OpenClipboard(None):
                return
            time.sleep(_OPEN_RETRY_DELAY_S)
        raise ClipboardError(
            f"could not open the clipboard (Win32 error {ctypes.get_last_error()}); "
            "another application may be holding it"
        )

    # -- input injection -------------------------------------------------

    def send_paste(self) -> None:
        _kb_controller.press(Key.ctrl)
        try:
            _kb_controller.press("v")
            _kb_controller.release("v")
        finally:
            _kb_controller.release(Key.ctrl)

    def send_copy(self) -> None:
        _kb_controller.press(Key.ctrl)
        try:
            _kb_controller.press("c")
            _kb_controller.release("c")
        finally:
            _kb_controller.release(Key.ctrl)

    # -- focused window --------------------------------------------------

    def focused_process_name(self) -> str | None:
        """Best-effort: any failure (no focused window, a protected process
        Sway lacks rights to query, ...) returns None rather than raising, per
        the PlatformAdapter contract -- profile selection just falls back to
        the default profile.
        """
        try:
            hwnd = _user32.GetForegroundWindow()
            if not hwnd:
                return None
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return None
            handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not handle:
                return None
            try:
                buffer = ctypes.create_unicode_buffer(_MAX_PATH)
                size = wintypes.DWORD(_MAX_PATH)
                if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                    return None
                name = os.path.basename(buffer.value).strip().lower()
                return name or None
            finally:
                _kernel32.CloseHandle(handle)
        except OSError:
            logger.debug("could not determine the focused window's process", exc_info=True)
            return None

    def animations_enabled(self) -> bool:
        """Read SPI_GETCLIENTAREAANIMATION -- Windows' reduce-motion switch.

        Fails open, per the PlatformAdapter contract: an unreadable setting
        leaves the orb animating rather than silently freezing it.
        """
        try:
            enabled = wintypes.BOOL()
            ok = _user32.SystemParametersInfoW(
                _SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0
            )
            if not ok:
                return True
            return bool(enabled.value)
        except Exception:
            logger.debug("could not read the animation preference", exc_info=True)
            return True

    def paste_likely_blocked(self) -> bool:
        """True only when the foreground window's process is confidently
        elevated and this process is confidently not -- the one combination
        where Windows' UIPI is guaranteed to drop the synthetic Ctrl+V
        :meth:`send_paste` sends, with no error raised anywhere. Every
        other outcome (same elevation, or either side undeterminable) fails
        open, per the PlatformAdapter contract.
        """
        try:
            hwnd = _user32.GetForegroundWindow()
            if not hwnd:
                return False
            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return False
            handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not handle:
                return False
            try:
                target_elevated = _is_process_elevated(handle)
            finally:
                _kernel32.CloseHandle(handle)
            if target_elevated is not True:
                return False
            self_elevated = _is_process_elevated(_kernel32.GetCurrentProcess())
            return self_elevated is False
        except OSError:
            logger.debug("could not determine foreground window elevation", exc_info=True)
            return False

    # -- windowed-build support (see flow.gui) -----------------------------

    def show_message(self, title: str, text: str, *, error: bool = False) -> None:
        """MessageBoxW. Topmost and foreground-forced: the windowed build has
        no console and often no visible window yet when this is called, so a
        dialog that opened behind other windows would look like a silent
        failure -- exactly what it exists to prevent.
        """
        flags = _MB_OK | _MB_SETFOREGROUND | _MB_TOPMOST
        flags |= _MB_ICONERROR if error else _MB_ICONINFORMATION
        try:
            _user32.MessageBoxW(None, text, title, flags)
        except OSError:
            logger.debug("could not show a message dialog", exc_info=True)

    def acquire_single_instance_lock(self, name: str) -> bool:
        global _instance_mutex
        try:
            # "Local\" scopes the mutex to this login session, so two
            # different users on the same machine can each run their own
            # instance -- a machine-global ("Global\") lock would wrongly
            # stop the second user.
            handle = _kernel32.CreateMutexW(None, True, f"Local\\{name}")
            if not handle:
                return True  # fail open: could not lock, so allow the launch
            if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
                _kernel32.CloseHandle(handle)
                return False
            _instance_mutex = handle
            return True
        except OSError:
            logger.debug("could not acquire the single-instance lock", exc_info=True)
            return True


    # -- D1: opening a URL, instance liveness, launch-at-login -------------

    def open_url(self, url: str) -> bool:
        """Three mechanisms, each logged, because this failed silently once.

        ``webbrowser.open`` is first because it honours the user's configured
        browser. It returns ``False`` (never raises) when it cannot find one,
        and in a frozen windowed build that is a real possibility, so
        ``os.startfile`` -- the Win32 shell association, which is what
        ``WindowsDefault`` wraps anyway but reached directly -- is tried
        next. ``cmd /c start`` is the last resort for the case where the
        shell association exists but ``startfile`` refuses the URL.

        Every attempt logs at DEBUG and the overall failure logs at WARNING,
        so "nothing happened" can never again be indistinguishable from
        "never tried".
        """
        import webbrowser

        try:
            if webbrowser.open(url):
                logger.debug("opened %s via webbrowser", url)
                return True
            logger.debug("webbrowser.open found no browser for %s", url)
        except Exception:
            logger.debug("webbrowser.open raised for %s", url, exc_info=True)

        try:
            os.startfile(url)  # noqa: S606 - a loopback URL we constructed
            logger.debug("opened %s via os.startfile", url)
            return True
        except OSError:
            logger.debug("os.startfile could not open %s", url, exc_info=True)

        try:
            import subprocess

            # DETACHED_PROCESS: this runs from the tray thread of a windowed
            # build with no console, and inheriting a null console handle is
            # what makes the naive version fail. `start` needs the empty
            # first argument, which it treats as the window title.
            subprocess.Popen(  # noqa: S602 - fixed argv, no shell interpolation of url
                ["cmd", "/c", "start", "", url],
                creationflags=0x00000008 | 0x08000000,  # DETACHED | NO_WINDOW
                close_fds=True,
            )
            logger.debug("opened %s via cmd start", url)
            return True
        except Exception:
            logger.warning("could not open %s in a browser by any method", url, exc_info=True)
        return False

    def process_is_running(self, pid: int) -> bool:
        """Whether ``pid`` is live, via ``OpenProcess``.

        Fails closed. ``OpenProcess`` succeeding is not quite sufficient on
        its own -- a handle can still be opened to a process that has exited
        but whose object is kept alive by an outstanding handle -- so the
        exit code is checked too: ``STILL_ACTIVE`` means genuinely running.
        """
        if pid <= 0:
            return False
        handle = None
        try:
            handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        except OSError:
            logger.debug("could not test whether pid %d is running", pid, exc_info=True)
            return False
        finally:
            if handle:
                _kernel32.CloseHandle(handle)

    def set_launch_at_login(self, app_name: str, command: str | None) -> bool:
        """Write or delete this app's ``HKCU\\...\\Run`` value.

        HKCU rather than HKLM on purpose: HKLM needs administrator rights,
        which CLAUDE.md's "no admin" rule rules out, and a dictation tool is
        a per-user thing anyway.
        """
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                if command is None:
                    try:
                        winreg.DeleteValue(key, app_name)
                    except FileNotFoundError:
                        pass  # already absent; the requested state is the state
                else:
                    winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, command)
            return True
        except OSError:
            logger.warning("could not update launch-at-login for %s", app_name, exc_info=True)
            return False

    def launches_at_login(self, app_name: str) -> bool:
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_QUERY_VALUE
            ) as key:
                value, _kind = winreg.QueryValueEx(key, app_name)
        except OSError:
            return False
        return bool(value)


__all__ = ["WindowsPlatform"]

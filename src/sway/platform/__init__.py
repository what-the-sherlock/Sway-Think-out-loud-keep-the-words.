"""OS-specific behaviour, isolated behind a single interface.

CLAUDE.md requires every platform-dependent call to sit behind this package so
macOS/Linux support is an added module rather than a scattered rewrite. Phase 1
added clipboard access and paste injection; Phase 3 adds focused-window
detection; Phase 5 adds a copy shortcut, for reading the current text
selection ahead of a voice command (see flow.selection).

Note: this package shadows the stdlib ``platform`` module *only* inside the
``flow`` namespace. Python 3 uses absolute imports, so ``import platform``
elsewhere still resolves to the stdlib.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class PlatformAdapter(Protocol):
    """Everything Sway needs from the host operating system."""

    #: Short, stable identifier used in logs and diagnostics.
    name: str

    def user_data_dir(self, app_name: str) -> Path:
        """Per-user directory for databases, logs, and other mutable state."""

    def user_config_dir(self, app_name: str) -> Path:
        """Per-user directory for hand-edited configuration files."""

    def clipboard_get(self) -> str:
        """Return the current clipboard text, or "" if it holds none/non-text."""

    def clipboard_set(self, text: str) -> None:
        """Replace the clipboard contents with ``text``."""

    def send_paste(self) -> None:
        """Send the OS paste shortcut (Ctrl+V) to the focused application."""

    def send_copy(self) -> None:
        """Send the OS copy shortcut (Ctrl+C) to the focused application."""

    def focused_process_name(self) -> str | None:
        """Executable basename (e.g. ``"slack.exe"``, lower-cased) that owns the
        current foreground window, or ``None`` if it could not be determined
        (no window focused, an inaccessible process, an unsupported platform).
        Best-effort: implementations must never raise for this."""

    def show_message(self, title: str, text: str, *, error: bool = False) -> None:
        """Show a modal, native message dialog. The only way a windowed
        build (no console -- see :mod:`flow.gui`) can report a fatal startup
        failure to the user. Best-effort: implementations must never raise,
        since this is itself the error path."""

    def acquire_single_instance_lock(self, name: str) -> bool:
        """Try to claim a system-wide lock identified by ``name``, held for
        the rest of the process's lifetime. ``False`` means another instance
        already holds it and this one should exit -- two live instances would
        mean two open microphones and two hotkey listeners, so every
        dictation would be captured and pasted twice. Best-effort: returns
        ``True`` (fails open, allowing the launch) if locking is unsupported
        or errors, since refusing to start is the worse failure."""

    def open_url(self, url: str) -> bool:
        """Open ``url`` in the user's default browser. ``True`` if something
        was launched.

        Not just ``webbrowser.open``: that returns ``False`` rather than
        raising when it cannot find a browser, so a caller who ignores the
        result gets a silent no-op -- which is exactly how "Open Dashboard"
        appeared to do nothing at all. Implementations must try more than one
        mechanism and must never raise."""

    def process_is_running(self, pid: int) -> bool:
        """Whether ``pid`` names a live process.

        Used to decide whether ``instance.json`` still describes a running
        instance (:mod:`flow.instance`). Fails **closed** -- returns ``False``
        when it cannot tell -- which is the opposite of most adapter methods
        here and deliberate: a wrong ``True`` means sending this instance's
        auth token to whatever process now holds that recycled port, while a
        wrong ``False`` costs only the focus nicety."""

    def set_launch_at_login(self, app_name: str, command: str | None) -> bool:
        """Register or clear a login-time launch for ``app_name``.

        ``command`` is the command line to run, or ``None`` to remove the
        entry. Returns whether the change was applied. Best-effort: never
        raises, since a settings toggle must not be able to crash the app."""

    def launches_at_login(self, app_name: str) -> bool:
        """Whether a login-time launch is currently registered. ``False`` when
        unsupported or unreadable."""

    def synthetic_input_likely_blocked(self) -> bool:
        """True only when the caller can be confident a synthetic keystroke
        (:meth:`send_paste` or :meth:`send_copy`) would silently do nothing
        -- e.g. the foreground window belongs to a process running at a
        higher privilege level than this one, which Windows' UIPI blocks
        *any* synthetic input into, not just paste. Fails open (``False``)
        whenever that can't be determined: a false positive here means
        dictation or a transform silently stops working, which is worse
        than an occasional paste or selection-read that fails anyway."""

    def animations_enabled(self) -> bool:
        """Whether the OS wants interface animation at all.

        The desktop equivalent of the web's ``prefers-reduced-motion``: the
        overlay's orb reads this and holds a single frame when it is False.
        Fails *open* (``True``) whenever the setting cannot be read -- an
        unreadable preference must not silently freeze the one part of the
        bar that tells the user it is still working."""

    def modifier_keys_down(self) -> frozenset[str] | None:
        """The sided modifier key names (``"ctrl_l"``, ``"ctrl_r"``,
        ``"alt_l"``, ``"alt_r"``, ``"shift_l"``, ``"shift_r"``, ``"cmd"``)
        the OS currently reports as physically held, queried independently
        of :class:`sway.hotkey.BindingListener`'s own hook state.

        Best-effort and read-only: ``None`` means "cannot be determined on
        this platform", never an error. Used only to heal
        :class:`~sway.hotkey.BindingListener`'s hook state after it drifts
        from reality (a swallowed release, a missed event) -- see
        ``BindingListener._maybe_resync_locked``. Not a substitute for the
        hook itself: polling this on every keystroke would be wasteful, so
        callers rate-limit it."""


@lru_cache(maxsize=1)
def get_platform() -> PlatformAdapter:
    """Return the adapter for the current OS (cached; adapters are stateless)."""
    if sys.platform == "win32":
        from sway.platform.windows import WindowsPlatform

        return WindowsPlatform()

    from sway.platform.posix import PosixPlatform

    return PosixPlatform()


__all__ = ["PlatformAdapter", "get_platform"]

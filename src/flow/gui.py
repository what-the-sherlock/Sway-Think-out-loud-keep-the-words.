"""Entry point for the windowed build (``Flow.exe``), the one a non-technical
user double-clicks.

It differs from :mod:`flow.cli` in exactly two ways, both consequences of
there being no console attached:

* **No arguments means ``run``.** The console build prints help when invoked
  bare, which is right for a terminal; for a double-clicked icon it would
  flash a window and exit having apparently done nothing.
* **Failures are shown, not printed.** ``sys.stdout``/``sys.stderr`` are
  ``None`` in a PyInstaller ``console=False`` build, so every diagnostic the
  CLI writes there is discarded. Anything fatal is surfaced through
  :meth:`~flow.platform.PlatformAdapter.show_message` instead, with the
  no-API-key case -- overwhelmingly the most likely first-run failure for
  someone who was handed a copy -- given its own actionable message.

Console-attached usage is unaffected: ``flow-cli.exe`` (and ``python -m
flow``) still go through :func:`flow.cli.main` unchanged.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Sequence

from flow import APP_NAME
from flow.errors import FlowError
from flow.platform import get_platform

logger = logging.getLogger(__name__)

#: Identifies this app's single-instance lock. Version-independent on
#: purpose: two *different* builds running at once would double-paste just
#: as surely as two copies of one build.
_INSTANCE_LOCK_NAME = "FlowDictation.SingleInstance"

_NO_API_KEY_MESSAGE = (
    "Flow needs a Groq API key before it can transcribe anything.\n\n"
    "1. Get a free key at https://console.groq.com/keys\n"
    "2. In the same folder as Flow.exe, create a new text file named .env\n"
    "   (in Notepad: File > Save As, then set 'Save as type' to 'All Files'\n"
    "   and name it exactly .env)\n"
    "3. Put one line in it: GROQ_API_KEY=your-key-here\n\n"
    "Then start Flow again."
)

_ALREADY_RUNNING_MESSAGE = (
    "Flow is already running.\n\n"
    "Look for its icon in the notification area (the '^' arrow next to the "
    "clock). Running a second copy would type everything you dictate twice."
)


def main_windowed(argv: Sequence[str] | None = None) -> int:
    """``Flow.exe``'s entry point. Never raises: every failure path ends in a
    dialog and a non-zero return code.
    """
    # Imported here, not at module scope: this module is the very first thing
    # the frozen app runs, and a syntax/import error anywhere in the CLI's
    # dependency tree would otherwise crash before the dialog machinery below
    # exists to report it.
    from flow.cli import main as cli_main

    platform = get_platform()
    args = list(argv) if argv is not None else []

    try:
        if not platform.acquire_single_instance_lock(_INSTANCE_LOCK_NAME):
            platform.show_message(APP_NAME, _ALREADY_RUNNING_MESSAGE)
            return 0

        # Only the bare, double-clicked case defaults to `run`; an explicit
        # argument list is passed through untouched so a shortcut with
        # arguments still behaves like the CLI.
        if not args:
            args = ["run"]

        if args == ["run"] and not _has_api_key():
            platform.show_message(f"{APP_NAME} — setup needed", _NO_API_KEY_MESSAGE, error=True)
            return 2

        code = cli_main(args)
    except FlowError as exc:
        # Expected, user-facing failures (bad config, unopenable database):
        # the message is already written for a human, so show it as-is.
        platform.show_message(f"{APP_NAME} — error", str(exc), error=True)
        return 1
    except Exception:
        detail = traceback.format_exc(limit=3)
        platform.show_message(
            f"{APP_NAME} — unexpected error",
            f"Flow hit an unexpected problem and stopped.\n\n{detail}\n{_log_hint()}",
            error=True,
        )
        return 1

    if code != 0:
        # cli_main handled it and wrote why to a stderr that does not exist
        # here, so the log file is the only place that detail survives.
        platform.show_message(
            f"{APP_NAME} — error",
            f"Flow could not start (exit code {code}).\n\n{_log_hint()}",
            error=True,
        )
    return code


def _has_api_key() -> bool:
    """Whether a key is configured, tolerating a config that fails to load --
    a broken config is a different failure, reported by the normal path with
    its own specific message rather than mislabelled as a missing key.
    """
    try:
        from flow.config import load_config

        return bool(load_config().groq_api_key)
    except Exception:
        return True


def _log_hint() -> str:
    try:
        from flow.config import load_config

        return f"Details are in:\n{load_config().log_file_path}"
    except Exception:
        return "Details may be in Flow's log file, under %LOCALAPPDATA%\\Flow."


__all__ = ["main_windowed"]

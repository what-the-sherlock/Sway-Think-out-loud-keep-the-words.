"""Logging configuration.

Console output goes to stderr so stdout stays clean for the latency tables —
``flow report > timings.txt`` should capture the table, not the log lines.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from flow.config import Config

_CONSOLE_FORMAT = "%(levelname)-7s %(name)s: %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s]: %(message)s"

#: Root of the application's logger tree. Configuring this instead of the real
#: root logger keeps third-party libraries from inheriting our handlers.
LOGGER_NAME = "flow"


def configure_logging(config: Config, *, force_level: str | None = None) -> logging.Logger:
    """Install console and (optionally) rotating-file handlers. Idempotent."""
    logger = logging.getLogger(LOGGER_NAME)
    level = (force_level or config.logging.level).upper()
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    # sys.stderr is None in a windowed (console=False) PyInstaller build --
    # see flow.gui. A StreamHandler wrapping None raises on every emit and
    # swallows it in handleError, which is pure overhead on a hot path, so
    # skip the console handler entirely when there is no console to write to.
    # File logging below is what carries diagnostics in that build.
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
        logger.addHandler(console)

    if config.logging.file_enabled:
        handler = _file_handler(config)
        if handler is not None:
            logger.addHandler(handler)

    return logger


def _file_handler(config: Config) -> logging.Handler | None:
    path: Path = config.log_file_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path,
            maxBytes=config.logging.file_max_bytes,
            backupCount=config.logging.file_backup_count,
            encoding="utf-8",
            delay=True,
        )
    except OSError as exc:
        # An unwritable log directory must not stop the app from dictating.
        print(f"warning: file logging disabled ({exc})", file=sys.stderr)
        return None
    handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    return handler


__all__ = ["LOGGER_NAME", "configure_logging"]

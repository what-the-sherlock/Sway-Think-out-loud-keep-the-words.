"""Sway: local voice dictation with LLM cleanup."""

from __future__ import annotations

#: Product name. This is not only a label: it also names the data directory
#: (``%LOCALAPPDATA%\Sway``) and the launch-at-login registry value, so
#: changing it is a migration event, not a copy edit. See :mod:`sway.migrate`,
#: which carries an existing install across the Sway -> Sway rename.
APP_NAME = "Sway"
__version__ = "0.1.0"

__all__ = ["APP_NAME", "__version__"]

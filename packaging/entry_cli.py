"""PyInstaller entry script for the console build (``flow-cli.exe``).

Mirrors ``python -m flow`` exactly. Trivial for the same reason as
entry_gui.py -- see packaging/flow.spec.
"""

from __future__ import annotations

from flow.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

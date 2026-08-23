"""PyInstaller entry script for the windowed build (``Sway.exe``).

Deliberately trivial: PyInstaller executes this file as ``__main__``, so
keeping it to an import and a call means the real logic lives in
:mod:`flow.gui`, where it is importable and testable like the rest of the
package. See packaging/flow.spec.
"""

from __future__ import annotations

from sway.gui import main_windowed

if __name__ == "__main__":
    raise SystemExit(main_windowed())

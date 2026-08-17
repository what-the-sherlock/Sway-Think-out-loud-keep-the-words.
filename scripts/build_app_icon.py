"""Build the packaged .exe's icon (packaging/flow.ico) from the app badge
in flow.branding. Rerun by hand only if the mark or palette changes:

    .venv/Scripts/python.exe scripts/build_app_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flow.branding import app_badge_image  # noqa: E402

OUTPUT = ROOT / "packaging" / "flow.ico"
_SIZES = [16, 24, 32, 48, 64, 128, 256]


def main() -> None:
    # A single source image sized to the largest requested resolution;
    # Pillow downsamples it per-entry when writing the multi-resolution ICO.
    largest = app_badge_image(max(_SIZES)).convert("RGBA")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    largest.save(OUTPUT, format="ICO", sizes=[(s, s) for s in _SIZES])
    print(f"wrote {OUTPUT} ({', '.join(f'{s}x{s}' for s in _SIZES)})")


if __name__ == "__main__":
    main()

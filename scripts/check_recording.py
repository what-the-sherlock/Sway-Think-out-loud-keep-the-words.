"""Is Sway recording dictations to latency.db right now?

Diagnostic for when scripts/baseline.py sits at the same clip count while you
speak. It answers three questions the harness cannot distinguish between:
whether the app is writing runs at all, whether it is writing them where this
script looks, and whether new runs carry the D0 stages the baseline needs.

Run it with Sway running, dictate once, and watch.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sway.config import load_config  # noqa: E402
from sway.latency.store import LatencyStore  # noqa: E402


def main() -> int:
    config = load_config()
    db = Path(config.latency_db_path)
    print(f"latency enabled : {config.latency.enabled}")
    print(f"database        : {db}")
    print(f"exists          : {db.exists()}")
    if not db.exists():
        print("\n-> Sway has never written a run here. Is `flow run` actually running,")
        print("   and did it start from the same config as this script?")
        return 1
    print(f"last modified   : {time.strftime('%H:%M:%S', time.localtime(db.stat().st_mtime))}")

    with LatencyStore.open(db) as store:
        total = store.count_runs()
        recent = store.fetch_runs(limit=3)
    print(f"total runs      : {total}")
    if recent:
        newest = recent[0]
        age = time.time() - newest.started_at
        stages = ", ".join(str(t.stage) for t in newest.stages) or "(none)"
        print(f"newest run      : {age:.0f}s ago, ok={newest.ok}")
        print(f"  stages        : {stages}")
        print(f"  has hotkey_hold: {'hotkey_hold' in stages}")
        if newest.error:
            print(f"  error         : {newest.error}")

    print("\nNow hold the hotkey, speak for ~3 seconds, and release. Watching 30s...")
    baseline_count = total
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        with LatencyStore.open(db) as store:
            now = store.count_runs()
            if now > baseline_count:
                run = store.fetch_runs(limit=1)[0]
                stages = ", ".join(str(t.stage) for t in run.stages)
                print(f"\n  RECORDED: ok={run.ok} total={run.total_ms:.0f}ms")
                print(f"  stages: {stages}")
                if "hotkey_hold" not in stages:
                    print("\n  -> No hotkey_hold stage. This Sway is running PRE-D0 code.")
                    print("     Restart it from this branch so the new stages are recorded.")
                    return 1
                print("\n  -> Recording works. baseline.py should advance too.")
                return 0
        time.sleep(0.25)

    print("\n  NOTHING RECORDED in 30s.")
    print("  -> The hotkey is not reaching Sway, or the hold was under 200ms,")
    print(f"     or Sway is writing somewhere other than {db}.")
    print("     Check the Sway window for log output while you hold the key.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

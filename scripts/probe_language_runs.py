"""What the language layer actually decided, for the last few dictations.

The multilingual changes make four decisions per dictation that are invisible
from the pasted text alone: which Whisper model was used, which language the
transcript was resolved to, whether an output language was in force, and what
the cleanup model itself said the language was. All four are annotated onto
the latency run; this prints them.

    python scripts/probe_language_runs.py          # last 8 dictations
    python scripts/probe_language_runs.py 20

Read-only. Safe to run while Sway is running.

Columns:
  ms        total run time
  stt       turbo | v3 (the accurate model, used for Hindi/Marathi)
  lang      the language the pipeline resolved the transcript to
  ->out     the requested output language, or "-" for "same as spoken"
  says      what the cleanup model reported via its #lang: header
  outcome   why cleanup was skipped, which guardrail tripped, or "cleaned"
"""

from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    db = pathlib.Path(os.environ["LOCALAPPDATA"], "Sway", "latency.db")
    if not db.exists():
        print(f"no latency database at {db}")
        return 1

    # Read-only: this is a diagnostic, and Sway may be writing to the file.
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = list(
        con.execute(
            "select total_ms, metadata from runs order by started_at desc limit ?",
            (limit,),
        )
    )
    if not rows:
        print("no dictations recorded yet")
        return 0

    header = (
        f"{'ms':>6}  {'stt':6} {'lang':5} {'->out':6} {'says':6} "
        f"{'outcome':22} chars"
    )
    print(header)
    print("-" * len(header))
    for row in reversed(rows):  # oldest first, so it reads like a session
        meta = json.loads(row["metadata"])
        model = meta.get("stt_model", "")
        stt = "v3" if model.endswith("large-v3") else ("turbo" if model else "-")
        outcome = (
            meta.get("cleanup_skip_reason")
            or meta.get("cleanup_guardrail_reason")
            or ("cleaned" if meta.get("cleaned_characters") else "-")
        )
        if meta.get("stt_retried"):
            stt += "*"  # turbo ran first, then re-transcribed on the full model
        print(
            f"{row['total_ms']:6.0f}  {stt:6} "
            f"{meta.get('resolved_language') or '-':5} "
            f"{meta.get('target_language') or '-':6} "
            f"{meta.get('detected_language') or '-':6} "
            f"{outcome:22} {meta.get('characters', '')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

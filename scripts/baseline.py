"""D0 baseline capture: 20 guided dictations, reported per clip-length class.

Why a script rather than ``flow report``: the D0 baseline has to be
comparable against the D3 after-table, and that needs three things ``flow
report`` does not do on its own.

* **A fixed sample.** It captures runs recorded *after this script started*,
  so an existing latency.db does not dilute the numbers.
* **Clip-length classes.** A 3-second and a 30-second dictation have very
  different transcribe times, and averaging them into one table hides
  exactly the thing D3 needs to see. Runs are bucketed by their measured
  ``hotkey_hold``, not by which prompt was on screen when you spoke, so a
  mistimed hold lands in the bucket it actually belongs to.
* **A saved artifact.** The table is written to a file so D3 can diff
  against it rather than against a screenshot.

Run it with the app already running (``flow run`` or Sway.exe) in another
window -- this script only reads the database, it never touches the mic. It
prompts you through the clips; dictate into any text field you like.

    python scripts/baseline.py                  # capture, then report
    python scripts/baseline.py --report-only    # re-report the last capture

The 20 clips are 7 short, 7 medium, 6 long, matching the D0 brief.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sway.config import load_config  # noqa: E402
from sway.latency.models import RunRecord  # noqa: E402
from sway.latency.report import format_table, summarize  # noqa: E402
from sway.latency.store import LatencyStore  # noqa: E402


@dataclass(frozen=True, slots=True)
class ClipClass:
    """One clip-length bucket: what to say, and which holds belong to it."""

    name: str
    target_s: float
    count: int
    #: Inclusive lower / exclusive upper bound on hold seconds. Deliberately
    #: wide and gapless so every run lands somewhere -- a run that fell
    #: outside every bucket would silently vanish from the baseline.
    low_s: float
    high_s: float
    prompt: str
    #: One topic per clip. Deliberately topics to speak about rather than
    #: lines to read: read-aloud speech has no fillers, no false starts, and
    #: no self-corrections, so it trips the cleanup skip heuristic far more
    #: often than real dictation does. A baseline captured by reading would
    #: understate how often cleanup runs, which is the stage D3 most needs to
    #: measure. Topics are ordinary work talk for the same reason.
    topics: tuple[str, ...] = ()


CLASSES: tuple[ClipClass, ...] = (
    ClipClass(
        "short",
        3.0,
        7,
        0.0,
        6.0,
        "One sentence each. Say it the way you would type it into a chat window.",
        (
            "Tell someone you are running late to a meeting.",
            "Ask a colleague whether they have looked at the pull request yet.",
            "Say what you had for lunch.",
            "Reply yes to an invitation, and add one detail.",
            "Ask someone to send you the link they mentioned.",
            "Say that a build is failing and you are looking into it.",
            "Tell someone you will pick this up again tomorrow morning.",
        ),
    ),
    ClipClass(
        "medium",
        10.0,
        7,
        6.0,
        18.0,
        "Two or three sentences each. Talk, do not read -- pauses and 'um' are wanted.",
        (
            "Explain what you are working on in this repo right now.",
            "Describe the last bug you fixed and what caused it.",
            "Explain to someone why this app pastes instead of typing.",
            "Say what you want to have finished by the end of the week.",
            "Describe how you would explain this project to a non-technical friend.",
            "Talk about a tool you use every day and one thing you dislike about it.",
            "Explain what you would do first if the app stopped working tomorrow.",
        ),
    ),
    ClipClass(
        "long",
        30.0,
        6,
        18.0,
        float("inf"),
        "A full paragraph each. Let yourself ramble and correct yourself; that is realistic.",
        (
            "Describe a technical problem you have not solved yet, and how you might approach it.",
            "Walk through how this dictation app works, from key press to text appearing.",
            "Explain a decision you made recently and the alternatives you rejected.",
            "Describe your ideal setup for the work you do, and why you do not have it.",
            "Talk through what you would want built next, after this project is finished.",
            "Explain something you know well to an imaginary beginner.",
        ),
    ),
)

BASELINE_PATH = Path(__file__).resolve().parent.parent / "docs" / "d0-baseline.txt"
MARKER_PATH = Path(__file__).resolve().parent.parent / "docs" / ".d0-baseline-window.json"


def classify(run: RunRecord) -> str:
    """Bucket a run by its measured hold, falling back to the total duration.

    Pre-D0 runs have no ``hotkey_hold`` stage. They cannot be classified and
    are reported separately rather than being guessed into a bucket -- see
    the D0 rule that a missing measurement is never invented.
    """
    hold_ms = next(
        (timing.duration_ms for timing in run.stages if str(timing.stage) == "hotkey_hold"),
        None,
    )
    if hold_ms is None:
        return "unclassified"
    hold_s = hold_ms / 1000.0
    for clip in CLASSES:
        if clip.low_s <= hold_s < clip.high_s:
            return clip.name
    return "unclassified"  # pragma: no cover - the buckets are gapless


def _runs_since(since: float) -> list[RunRecord]:
    """Dictation runs recorded since ``since``, newest first.

    Opens a short-lived connection each call so it sees the running app's
    committed writes (the DB is in WAL mode and the app's writer thread
    batches, so a long-held reader would miss them).

    **Only ``source=hotkey`` runs count.** The command hotkey writes runs to
    the same table, and on this machine it fires constantly by accident --
    the default command key is Right Shift, which people press while typing
    and talking, producing a stream of ``skipped=empty_selection`` runs with
    no ``hotkey_hold`` stage. Counting those as baseline clips would advance
    the capture without a dictation having happened and put unclassifiable
    rows into the report.
    """
    config = load_config()
    db_path = Path(config.latency_db_path)
    if not db_path.exists():
        return []
    with LatencyStore.open(db_path) as store:
        runs = store.fetch_runs(since=since)
    return [run for run in runs if run.metadata.get("source", "hotkey") == "hotkey"]


def capture() -> float:
    """Walk the user through the 20 clips. Returns the capture start time.

    Confirms each clip against the database before moving on, so a hotkey
    that is not reaching the app is obvious on clip 1 rather than after
    twenty of them.
    """
    started = time.time()
    total = sum(clip.count for clip in CLASSES)
    print(f"\nD0 baseline: {total} dictations.")
    print("Sway must already be running in another window. Nothing here touches the mic.")
    print("Put your cursor in a text field -- Notepad is fine. NOT a terminal or code")
    print("editor: those match the VERBATIM profile and skip cleanup entirely, which")
    print("would leave the baseline with no data on the stage D3 most needs.")
    print("\nEach prompt is a topic to TALK about, not a line to read. Read-aloud speech")
    print("has no fillers or false starts and trips the skip heuristic far more than")
    print("real dictation, so a baseline captured by reading would not match your use.\n")

    seen = len(_runs_since(started))
    index = 0
    for clip in CLASSES:
        print(f"--- {clip.name}: {clip.count} clips of about {clip.target_s:g}s each ---")
        print(f"    {clip.prompt}\n")
        for offset in range(clip.count):
            index += 1
            topic = clip.topics[offset] if offset < len(clip.topics) else clip.prompt
            print(f"  [{index}/{total}] {topic}")
            input("        Press Enter, then hold the hotkey and speak... ")
            seen = _await_run(started, seen, index, total)
    print("\nCapture window closed.\n")
    MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    MARKER_PATH.write_text(json.dumps({"started": started}), encoding="utf-8")
    return started


def _await_run(
    since: float, seen: int, index: int, total: int, *, timeout_s: float = 25.0
) -> int:
    """Wait for one new run to land, and report what it looked like.

    Returns the new run count. On timeout it says so and moves on rather than
    blocking the capture: a dictation that never reached the app is a real
    data point about the app, and stalling here would just hide it.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        runs = _runs_since(since)
        if len(runs) > seen:
            newest = runs[0]  # fetch_runs is newest-first
            hold = next(
                (t.duration_ms for t in newest.stages if str(t.stage) == "hotkey_hold"), None
            )
            where = classify(newest)
            skipped = newest.metadata.get("skipped")
            if hold is None:
                detail = "no hotkey_hold stage -- this Sway is running pre-D0 code"
            elif skipped:
                # A run landed, but nothing was transcribed: silence, or a
                # hold too short to count. It is recorded and it advances the
                # count, but saying "ok" would be a lie.
                detail = f"hold {hold / 1000.0:.1f}s -> NOTHING CAPTURED ({skipped})"
            else:
                detail = f"hold {hold / 1000.0:.1f}s -> {where}"
            status = "ok" if newest.ok else f"FAILED ({newest.error})"
            print(f"        captured {len(runs)}/{total} | {detail} | {status}")
            return len(runs)
        time.sleep(0.25)
    print(
        f"        nothing recorded after {timeout_s:.0f}s -- the hotkey may not have"
        " reached Sway. Continuing."
    )
    return seen


#: Below this many runs the percentiles are not worth writing down, and a
#: file that looks like a baseline but is not one is worse than no file.
MIN_RUNS = 10


def report(since: float) -> str:
    config = load_config()
    db_path = config.latency_db_path
    if not Path(db_path).exists():
        raise SystemExit(f"no latency database at {db_path}; run Sway at least once first")

    with LatencyStore.open(db_path) as store:
        runs = store.fetch_runs(since=since)
    if not runs:
        raise SystemExit(
            "no runs recorded in the capture window.\n"
            "Was Sway actually running, and is [latency] enabled in your config?"
        )

    # Refuse to produce a baseline out of runs that predate the capture.
    # This is the guard that was missing: `--since 1` happily re-reported the
    # entire historical database as though it were a fresh capture, epoch
    # timestamp and all, and every row landed in `unclassified` because none
    # of them had a hotkey_hold stage. A file that reads as a baseline but
    # contains no new dictations is the worst possible artifact for D3 to
    # diff against, so it is now an error rather than a quiet oddity.
    classified = [run for run in runs if classify(run) != "unclassified"]
    if len(classified) < MIN_RUNS:
        raise SystemExit(
            f"refusing to write a baseline: {len(runs)} run(s) in the window, but only "
            f"{len(classified)} carry a hotkey_hold stage (need at least {MIN_RUNS}).\n"
            "\n"
            "Runs without hotkey_hold predate D0 and cannot be classified by clip length.\n"
            "If you passed --since, it is probably reaching back before this build.\n"
            "Run `python scripts/baseline.py` with Sway running to capture a real one."
        )

    by_class: dict[str, list[RunRecord]] = {}
    for run in runs:
        by_class.setdefault(classify(run), []).append(run)

    sections = [
        "D0 BASELINE",
        f"captured {time.strftime('%Y-%m-%d %H:%M', time.localtime(since))}",
        f"database  {db_path}",
        f"runs      {len(runs)}",
        "",
        "Per-stage percentiles in milliseconds. A stage marked * was not measured in",
        "every run -- see flow.latency.report.StageStats.partial. Failed runs are",
        "excluded from the statistics but counted in the run total.",
        "",
    ]

    order = [clip.name for clip in CLASSES] + ["unclassified"]
    for name in order:
        group = by_class.get(name)
        if not group:
            continue
        sections.append("=" * 72)
        sections.append(format_table(summarize(group), title=f"{name} clips"))
        sections.append("")

    sections.append("=" * 72)
    sections.append(format_table(summarize(runs), title="all clips"))
    return "\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="skip the guided capture and re-report the previous capture window",
    )
    parser.add_argument(
        "--since",
        type=float,
        default=None,
        help="unix timestamp to report from, overriding the saved capture window",
    )
    args = parser.parse_args()

    if args.since is not None:
        since = args.since
    elif args.report_only:
        if not MARKER_PATH.exists():
            raise SystemExit("no saved capture window; run without --report-only first")
        since = float(json.loads(MARKER_PATH.read_text(encoding="utf-8"))["started"])
    else:
        since = capture()

    text = report(since)
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"\nWritten to {BASELINE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

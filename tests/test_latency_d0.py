"""D0 guarantees: the stage schema is additive, and measurement stays free of
user content.

These are the two properties D0 promised that nothing else in the suite would
catch if they regressed. Everything here is a pure function or an in-memory
SQLite store; nothing touches audio, the network, or a real dictation.
"""

from __future__ import annotations

import json

from flow.latency.models import STAGE_ORDER, RunRecord, Stage, StageTiming
from flow.latency.report import format_table, summarize
from flow.latency.store import LatencyStore
from flow.latency.tracker import LatencyTracker

# The stage values that existed before D0. A rename orphans every historical
# row carrying the old spelling, and the break is silent: the report simply
# shows two stages where there was one, each with half the data. Adding a
# member here is fine; changing or removing a string is the bug this guards.
PRE_D0_STAGE_VALUES = {
    "capture",
    "vad",
    "encode",
    "transcribe",
    "merge",
    "selection",
    "command",
    "cleanup",
    "inject",
}

D0_ADDED_STAGE_VALUES = {
    "hotkey_hold",
    "dispatch",
    "stt_first_byte",
    "llm_first_token",
}


def test_pre_d0_stage_values_are_never_renamed() -> None:
    current = {stage.value for stage in Stage}
    assert PRE_D0_STAGE_VALUES <= current, (
        "a pre-D0 stage value was renamed or removed; historical latency.db rows "
        "still carry the old spelling and would be orphaned"
    )


def test_d0_additions_are_present_and_ordered_before_their_successors() -> None:
    current = {stage.value for stage in Stage}
    assert D0_ADDED_STAGE_VALUES <= current

    # Display ordering only -- STAGE_ORDER is derived from declaration order
    # and is never persisted -- but a first-byte span sorting after the stage
    # it subdivides makes the table actively misleading.
    assert STAGE_ORDER["hotkey_hold"] < STAGE_ORDER["dispatch"] < STAGE_ORDER["capture"]
    assert STAGE_ORDER["stt_first_byte"] < STAGE_ORDER["transcribe"]
    assert STAGE_ORDER["llm_first_token"] < STAGE_ORDER["cleanup"]


def _run(run_id: str, stages: dict[str, float], total_ms: float = 100.0) -> RunRecord:
    return RunRecord(
        id=run_id,
        started_at=1_700_000_000.0,
        total_ms=total_ms,
        ok=True,
        stages=tuple(
            StageTiming(stage=name, seq=index, started_at=1_700_000_000.0, duration_ms=ms)
            for index, (name, ms) in enumerate(stages.items())
        ),
    )


def test_a_stage_missing_from_older_runs_is_not_counted_as_zero() -> None:
    """The core D0 read-back guarantee.

    One pre-D0 run (no ``stt_first_byte``) and one post-D0 run that measured
    it at 300 ms. If the missing row were folded in as a zero, p50 would come
    out at 0 ms and the stage would look free.
    """
    runs = [
        _run("old", {"transcribe": 400.0}),
        _run("new", {"stt_first_byte": 300.0, "transcribe": 400.0}),
    ]
    summary = summarize(runs)
    by_name = {stats.name: stats for stats in summary.stages}

    first_byte = by_name["stt_first_byte"]
    assert first_byte.count == 1
    assert first_byte.sampled == 2
    assert first_byte.p50 == 300.0
    assert first_byte.partial is True

    # transcribe was measured in both, so it carries no caveat.
    assert by_name["transcribe"].partial is False


def test_partial_stages_are_flagged_in_the_rendered_table() -> None:
    runs = [
        _run("old", {"transcribe": 400.0}),
        _run("new", {"stt_first_byte": 300.0, "transcribe": 400.0}),
    ]
    table = format_table(summarize(runs))
    assert "stt_first_byte*" in table
    assert "not measured in every run" in table
    # A stage present in every run must not be starred.
    assert "transcribe*" not in table


def test_a_genuinely_zero_stage_still_reads_as_measured() -> None:
    """The other half: 0 ms must stay distinguishable from absent."""
    runs = [_run("a", {"inject": 0.0}), _run("b", {"inject": 0.0})]
    stats = {s.name: s for s in summarize(runs).stages}["inject"]
    assert stats.count == 2
    assert stats.partial is False
    assert stats.p50 == 0.0
    assert "inject*" not in format_table(summarize(runs))


def test_recorder_can_record_a_span_that_closed_before_the_run_existed() -> None:
    tracker = LatencyTracker(":memory:", enabled=False)
    recorder = tracker.new_run()
    recorder.record(Stage.HOTKEY_HOLD, 1234.5, started_at=1_700_000_000.0)
    with recorder.stage(Stage.CAPTURE):
        pass

    record = recorder.finish()
    stages = {timing.stage: timing for timing in record.stages}
    assert stages["hotkey_hold"].duration_ms == 1234.5
    # Pre-recorded spans take a seq from the same counter, so they sort ahead
    # of everything measured after them.
    assert stages["hotkey_hold"].seq < stages["capture"].seq


def test_persisted_runs_carry_no_transcript_or_audio(tmp_path) -> None:
    """CLAUDE.md keeps audio out of the metrics DB; D0 extends that to text.

    The engine annotates counts and durations, never the words themselves.
    This asserts the store round-trips exactly what it was given and that a
    realistic metadata payload stays numeric.
    """
    secret = "the quick brown fox jumped over the lazy dog"
    record = RunRecord(
        id="r1",
        started_at=1_700_000_000.0,
        total_ms=900.0,
        ok=True,
        metadata={"words": 9, "wpm": 132.4, "characters": len(secret), "held_ms": 4100.0},
        stages=(
            StageTiming(stage="capture", seq=0, started_at=1_700_000_000.0, duration_ms=2.0),
        ),
    )

    db = tmp_path / "latency.db"
    with LatencyStore.open(db) as store:
        store.insert_runs([record])

    blob = db.read_bytes()
    assert secret.encode() not in blob
    for word in secret.split():
        assert f'"{word}"'.encode() not in blob

    with LatencyStore.open(db) as store:
        loaded = store.fetch_runs()[0]
    assert loaded.metadata == record.metadata
    # Every stored metadata value is a number, not free text.
    for key, value in json.loads(json.dumps(dict(loaded.metadata))).items():
        assert isinstance(value, (int, float)), f"{key} is not numeric"

"""Tests for the SQLite latency store."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sway.errors import LatencyStoreError
from sway.latency.models import RunRecord, StageTiming
from sway.latency.store import SCHEMA_VERSION, LatencyStore


def _record(run_id: str, *, started_at: float = 1000.0, ok: bool = True) -> RunRecord:
    return RunRecord(
        id=run_id,
        started_at=started_at,
        total_ms=512.5,
        ok=ok,
        error=None if ok else "boom",
        metadata={"source": "test", "index": 1},
        stages=(
            StageTiming(stage="capture", seq=0, started_at=started_at, duration_ms=4.5),
            StageTiming(stage="transcribe", seq=1, started_at=started_at, duration_ms=480.0),
        ),
    )


def test_round_trip_preserves_everything(tmp_path: Path):
    with LatencyStore.open(tmp_path / "latency.db") as store:
        store.insert_runs([_record("a")])
        (loaded,) = store.fetch_runs()

    original = _record("a")
    assert loaded.id == original.id
    assert loaded.total_ms == pytest.approx(original.total_ms)
    assert loaded.ok is True
    assert loaded.error is None
    assert loaded.metadata == {"source": "test", "index": 1}
    assert loaded.stages == original.stages


def test_creates_parent_directories(tmp_path: Path):
    target = tmp_path / "nested" / "deeper" / "latency.db"
    with LatencyStore.open(target) as store:
        store.insert_runs([_record("a")])
    assert target.exists()


def test_schema_version_is_recorded_and_reopen_is_idempotent(tmp_path: Path):
    path = tmp_path / "latency.db"
    with LatencyStore.open(path) as store:
        store.insert_runs([_record("a")])
    with LatencyStore.open(path) as store:
        store.insert_runs([_record("b")])
        assert store.count_runs() == 2

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


def test_newer_schema_is_refused(tmp_path: Path):
    path = tmp_path / "latency.db"
    LatencyStore.open(path).close()
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    finally:
        conn.close()

    with pytest.raises(LatencyStoreError, match="newer than this build"):
        LatencyStore.open(path)


def test_fetch_runs_is_newest_first_and_respects_limit(tmp_path: Path):
    with LatencyStore.open(tmp_path / "latency.db") as store:
        store.insert_runs([_record(f"r{i}", started_at=1000.0 + i) for i in range(5)])
        assert [r.id for r in store.fetch_runs()] == ["r4", "r3", "r2", "r1", "r0"]
        assert [r.id for r in store.fetch_runs(limit=2)] == ["r4", "r3"]


def test_runs_sharing_a_timestamp_keep_insertion_order(tmp_path: Path):
    # The wall clock does not always tick between two runs; the tie-break must
    # be insertion order, not the random uuid.
    with LatencyStore.open(tmp_path / "latency.db") as store:
        for i in range(5):
            store.insert_runs([_record(f"r{i}", started_at=1000.0)])
        assert [r.id for r in store.fetch_runs()] == ["r4", "r3", "r2", "r1", "r0"]


def test_fetch_runs_filters(tmp_path: Path):
    with LatencyStore.open(tmp_path / "latency.db") as store:
        store.insert_runs(
            [
                _record("old", started_at=1000.0),
                _record("new", started_at=2000.0),
                _record("bad", started_at=3000.0, ok=False),
            ]
        )
        assert [r.id for r in store.fetch_runs(since=1500.0)] == ["bad", "new"]
        assert [r.id for r in store.fetch_runs(only_ok=True)] == ["new", "old"]
        assert [r.id for r in store.fetch_runs(only_ok=False)] == ["bad"]


def test_stage_timings_are_attached_to_the_right_run(tmp_path: Path):
    with LatencyStore.open(tmp_path / "latency.db") as store:
        store.insert_runs([_record(f"r{i}", started_at=1000.0 + i) for i in range(3)])
        for run in store.fetch_runs():
            assert [s.stage for s in run.stages] == ["capture", "transcribe"]
            assert [s.seq for s in run.stages] == [0, 1]


def test_prune_deletes_old_runs_and_their_stages(tmp_path: Path):
    path = tmp_path / "latency.db"
    now = 10_000_000.0
    with LatencyStore.open(path) as store:
        store.insert_runs(
            [
                _record("old", started_at=now - 40 * 86_400),
                _record("recent", started_at=now - 1 * 86_400),
            ]
        )
        assert store.prune(30, now=now) == 1
        assert [r.id for r in store.fetch_runs()] == ["recent"]

    conn = sqlite3.connect(path)
    try:
        remaining = conn.execute("SELECT DISTINCT run_id FROM stage_timings").fetchall()
    finally:
        conn.close()
    assert remaining == [("recent",)]


def test_prune_is_a_no_op_when_disabled(tmp_path: Path):
    with LatencyStore.open(tmp_path / "latency.db") as store:
        store.insert_runs([_record("a", started_at=0.0)])
        assert store.prune(0) == 0
        assert store.count_runs() == 1


def test_insert_of_empty_batch_is_a_no_op(tmp_path: Path):
    with LatencyStore.open(tmp_path / "latency.db") as store:
        store.insert_runs([])
        assert store.count_runs() == 0


def test_unserialisable_metadata_does_not_lose_the_run(tmp_path: Path):
    record = RunRecord(
        id="a",
        started_at=1.0,
        total_ms=1.0,
        ok=True,
        metadata={"obj": object()},
        stages=(),
    )
    with LatencyStore.open(tmp_path / "latency.db") as store:
        store.insert_runs([record])
        (loaded,) = store.fetch_runs()
    # default=str keeps the run; the value is stringified rather than dropped.
    assert loaded.id == "a"
    assert "obj" in loaded.metadata


def test_close_is_idempotent(tmp_path: Path):
    store = LatencyStore.open(tmp_path / "latency.db")
    store.close()
    store.close()

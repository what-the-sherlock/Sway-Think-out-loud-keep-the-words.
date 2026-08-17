"""Tests for the latency tracker and its background writer."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from flow.latency.models import Stage
from flow.latency.store import LatencyStore
from flow.latency.tracker import LatencyTracker


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "latency.db"


def _read(path: Path):
    with LatencyStore.open(path) as store:
        return store.fetch_runs()


def test_run_records_stages_in_order(db_path: Path):
    with LatencyTracker(db_path) as tracker:
        with tracker.run({"source": "test"}) as run:
            with run.stage(Stage.CAPTURE):
                pass
            with run.stage(Stage.TRANSCRIBE):
                pass
            run.annotate(characters=42)
        assert tracker.flush()

    (record,) = _read(db_path)
    assert [s.stage for s in record.stages] == ["capture", "transcribe"]
    assert [s.seq for s in record.stages] == [0, 1]
    assert all(s.ok for s in record.stages)
    assert record.ok is True
    assert record.error is None
    assert record.metadata == {"source": "test", "characters": 42}
    assert record.total_ms >= sum(s.duration_ms for s in record.stages)


def test_stage_accepts_plain_strings(db_path: Path):
    with LatencyTracker(db_path) as tracker:
        with tracker.run() as run:
            with run.stage("future_phase_stage"):
                pass
        assert tracker.flush()

    (record,) = _read(db_path)
    assert record.stages[0].stage == "future_phase_stage"


def test_exception_marks_stage_and_run_failed_and_propagates(db_path: Path):
    with LatencyTracker(db_path) as tracker:
        with pytest.raises(RuntimeError, match="groq exploded"):
            with tracker.run() as run:
                with run.stage(Stage.TRANSCRIBE):
                    raise RuntimeError("groq exploded")
        assert tracker.flush()

    (record,) = _read(db_path)
    assert record.ok is False
    assert record.error == "RuntimeError: groq exploded"
    assert record.stages[0].ok is False
    # The failed stage is still timed — time-to-failure is useful data.
    assert record.stages[0].duration_ms >= 0.0


def test_first_failure_cause_wins(db_path: Path):
    with LatencyTracker(db_path) as tracker:
        run = tracker.new_run()
        run.mark_failed("first")
        run.mark_failed("second")
        tracker.submit(run.finish())
        assert tracker.flush()

    (record,) = _read(db_path)
    assert record.error == "first"
    assert record.ok is False


def test_manual_begin_end_supports_overlapping_spans(db_path: Path):
    with LatencyTracker(db_path) as tracker:
        run = tracker.new_run()
        first = run.begin("chunk-0")
        second = run.begin("chunk-1")
        run.end(second)
        run.end(first)
        tracker.submit(run.finish())
        assert tracker.flush()

    (record,) = _read(db_path)
    # Ordered by start (seq), not by completion.
    assert [s.stage for s in record.stages] == ["chunk-0", "chunk-1"]


def test_concurrent_stages_get_unique_sequence_numbers(db_path: Path):
    threads_count, per_thread = 8, 25
    with LatencyTracker(db_path) as tracker:
        run = tracker.new_run()
        barrier = threading.Barrier(threads_count)

        def worker() -> None:
            barrier.wait()
            for _ in range(per_thread):
                with run.stage(Stage.ENCODE):
                    pass

        threads = [threading.Thread(target=worker) for _ in range(threads_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        tracker.submit(run.finish())
        assert tracker.flush()

    (record,) = _read(db_path)
    seqs = [s.seq for s in record.stages]
    assert len(seqs) == threads_count * per_thread
    assert sorted(seqs) == list(range(len(seqs)))


def test_many_runs_are_all_persisted(db_path: Path):
    count = 250
    with LatencyTracker(db_path, batch_size=16) as tracker:
        for index in range(count):
            with tracker.run({"index": index}) as run:
                with run.stage(Stage.CAPTURE):
                    pass
        assert tracker.flush(timeout=10.0)

    with LatencyStore.open(db_path) as store:
        assert store.count_runs() == count
    assert tracker.dropped_records == 0


def test_close_flushes_pending_records(db_path: Path):
    tracker = LatencyTracker(db_path)
    with tracker.run() as run:
        with run.stage(Stage.INJECT):
            pass
    tracker.close()
    assert len(_read(db_path)) == 1


def test_close_is_idempotent_and_stops_accepting(db_path: Path):
    tracker = LatencyTracker(db_path)
    with tracker.run() as run:
        with run.stage(Stage.CAPTURE):
            pass
    tracker.close()
    tracker.close()

    later = tracker.new_run()
    assert tracker.submit(later.finish()) is False
    assert len(_read(db_path)) == 1


def test_disabled_tracker_writes_nothing_but_still_works(db_path: Path):
    with LatencyTracker(db_path, enabled=False) as tracker:
        assert tracker.enabled is False
        with tracker.run() as run:
            with run.stage(Stage.CAPTURE):
                pass
        assert tracker.flush() is True
    assert not db_path.exists()


def test_full_queue_drops_instead_of_blocking(db_path: Path):
    # A tracker with no writer thread: the queue fills and never drains, which
    # is exactly the stalled-disk case the drop policy exists for.
    tracker = LatencyTracker(db_path, enabled=False)
    tracker._enabled = True  # keep the queue live without starting a writer
    try:
        for _ in range(tracker._queue.maxsize + 25):
            tracker.submit(tracker.new_run().finish())
        assert tracker.dropped_records == 25
    finally:
        tracker._enabled = False
        tracker.close()


def test_bad_construction_arguments_are_rejected(tmp_path: Path):
    with pytest.raises(ValueError):
        LatencyTracker(tmp_path / "a.db", queue_size=0)
    with pytest.raises(ValueError):
        LatencyTracker(tmp_path / "a.db", batch_size=0)


def test_unwritable_database_degrades_without_raising(tmp_path: Path):
    # A directory where the file should be: opening it must fail, and the app
    # must keep running with measurement quietly disabled.
    blocked = tmp_path / "latency.db"
    blocked.mkdir()
    with LatencyTracker(blocked) as tracker:
        with tracker.run() as run:
            with run.stage(Stage.CAPTURE):
                pass
        assert tracker.flush(timeout=5.0) is True

"""Tests for the Phase 0 stub pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from flow.latency.models import Stage
from flow.latency.store import LatencyStore
from flow.latency.tracker import LatencyTracker
from flow.pipeline.stub import STUB_STAGES, StubPipeline, StubStageSpec


@pytest.fixture
def tracker(tmp_path: Path):
    with LatencyTracker(tmp_path / "latency.db") as instance:
        yield instance


def _noop_sleep(_seconds: float) -> None:
    """Keep tests fast: the pipeline's timing behaviour is the tracker's job."""


def test_run_records_every_stage(tracker: LatencyTracker, tmp_path: Path):
    pipeline = StubPipeline(tracker, seed=1, sleep=_noop_sleep)
    assert pipeline.run(3) == (3, 0)
    assert tracker.flush()

    with LatencyStore.open(tmp_path / "latency.db") as store:
        runs = store.fetch_runs()

    assert len(runs) == 3
    expected = [spec.stage.value for spec in STUB_STAGES]
    for run in runs:
        assert [s.stage for s in run.stages] == expected
        assert run.ok is True
        assert run.metadata["source"] == "stub"
        assert "characters" in run.metadata


def test_failures_are_recorded_and_swallowed(tracker: LatencyTracker, tmp_path: Path):
    pipeline = StubPipeline(tracker, seed=7, fail_rate=1.0, sleep=_noop_sleep)
    assert pipeline.run(4) == (0, 4)
    assert tracker.flush()

    with LatencyStore.open(tmp_path / "latency.db") as store:
        runs = store.fetch_runs()

    assert len(runs) == 4
    for run in runs:
        assert run.ok is False
        assert run.error is not None and "simulated failure" in run.error
        # The run stops at the failing stage, so it records fewer stages.
        assert 1 <= len(run.stages) <= len(STUB_STAGES)
        assert run.stages[-1].ok is False


def test_same_seed_produces_the_same_durations(tmp_path: Path):
    recorded: list[list[float]] = []
    for name in ("a", "b"):
        with LatencyTracker(tmp_path / f"{name}.db") as tracker:
            StubPipeline(tracker, seed=99, sleep=_noop_sleep).run(3)
            tracker.flush()
        with LatencyStore.open(tmp_path / f"{name}.db") as store:
            recorded.append([run.metadata["characters"] for run in store.fetch_runs()])
    assert recorded[0] == recorded[1]


def test_durations_stay_within_the_declared_ranges(tracker: LatencyTracker):
    slept: list[float] = []
    pipeline = StubPipeline(tracker, seed=3, sleep=slept.append)
    pipeline.run(5)

    low = min(spec.min_ms for spec in STUB_STAGES) / 1000.0
    high = max(spec.max_ms for spec in STUB_STAGES) / 1000.0
    assert len(slept) == 5 * len(STUB_STAGES)
    assert all(low <= value <= high for value in slept)


def test_stub_budget_stays_under_the_target():
    """The fake stages must model a pipeline that meets the <1000ms budget."""
    assert sum(spec.max_ms for spec in STUB_STAGES) < 1000.0


def test_invalid_arguments_are_rejected(tracker: LatencyTracker):
    with pytest.raises(ValueError):
        StubPipeline(tracker, stages=())
    with pytest.raises(ValueError):
        StubPipeline(tracker, fail_rate=1.5)
    with pytest.raises(ValueError):
        StubPipeline(tracker, sleep=_noop_sleep).run(-1)
    with pytest.raises(ValueError):
        StubStageSpec(Stage.CAPTURE, 10.0, 5.0)

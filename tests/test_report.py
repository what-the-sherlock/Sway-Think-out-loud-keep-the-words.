"""Tests for the pure summarising and formatting functions."""

from __future__ import annotations

import pytest

from sway.latency.models import RunRecord, Stage, StageTiming
from sway.latency.report import format_table, percentile, summarize


def _run(run_id: str, *, ok: bool = True, total_ms: float = 100.0, **stages: float) -> RunRecord:
    timings = tuple(
        StageTiming(stage=name, seq=index, started_at=0.0, duration_ms=value)
        for index, (name, value) in enumerate(stages.items())
    )
    return RunRecord(id=run_id, started_at=0.0, total_ms=total_ms, ok=ok, stages=timings)


# -- percentile ------------------------------------------------------------


def test_percentile_returns_observed_values():
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0) == 10.0
    assert percentile(values, 50) == 30.0
    assert percentile(values, 90) == 50.0
    assert percentile(values, 100) == 50.0


def test_percentile_sorts_its_input():
    assert percentile([50.0, 10.0, 30.0], 50) == 30.0


def test_percentile_on_single_value():
    assert percentile([7.0], 99) == 7.0


def test_percentile_rejects_empty_and_out_of_range():
    with pytest.raises(ValueError):
        percentile([], 50)
    with pytest.raises(ValueError):
        percentile([1.0], 101)


# -- summarize -------------------------------------------------------------


def test_summarize_groups_by_stage():
    runs = [
        _run("a", total_ms=100.0, capture=1.0, transcribe=50.0),
        _run("b", total_ms=200.0, capture=3.0, transcribe=90.0),
    ]
    summary = summarize(runs)
    assert summary.run_count == 2
    assert summary.failed_count == 0
    assert [s.name for s in summary.stages] == ["capture", "transcribe"]

    capture = summary.stages[0]
    assert capture.count == 2
    assert capture.mean == pytest.approx(2.0)
    assert capture.maximum == 3.0
    assert summary.total is not None
    assert summary.total.p50 == 100.0
    assert summary.total.maximum == 200.0


def test_summarize_excludes_failed_runs_but_counts_them():
    runs = [
        _run("ok", total_ms=100.0, capture=1.0),
        _run("bad", ok=False, total_ms=9999.0, capture=8888.0),
    ]
    summary = summarize(runs)
    assert summary.run_count == 2
    assert summary.failed_count == 1
    assert summary.sampled_count == 1
    assert summary.stages[0].count == 1
    assert summary.stages[0].maximum == 1.0
    assert summary.total is not None
    assert summary.total.maximum == 100.0


def test_summarize_orders_stages_by_pipeline_then_name():
    run = _run(
        "a",
        inject=1.0,
        capture=1.0,
        zebra=1.0,
        transcribe=1.0,
        alpha=1.0,
    )
    names = [s.name for s in summarize([run]).stages]
    known = [Stage.CAPTURE.value, Stage.TRANSCRIBE.value, Stage.INJECT.value]
    assert names[: len(known)] == known
    assert names[len(known) :] == ["alpha", "zebra"]


def test_summarize_of_nothing():
    summary = summarize([])
    assert summary.run_count == 0
    assert summary.stages == ()
    assert summary.total is None


def test_summarize_of_only_failures_has_no_total():
    summary = summarize([_run("bad", ok=False, capture=1.0)])
    assert summary.total is None
    assert summary.stages == ()


# -- format_table ----------------------------------------------------------


def test_format_table_is_ascii_and_aligned():
    runs = [_run(str(i), total_ms=100.0 + i, capture=1.0 + i, transcribe=500.0 + i) for i in range(5)]
    table = format_table(summarize(runs))

    assert table.isascii(), "table must survive a legacy Windows console codepage"
    lines = table.splitlines()
    assert "5 run(s), 0 failed" in lines[0]

    header_index = next(i for i, line in enumerate(lines) if line.split()[:2] == ["Stage", "Count"])
    header = lines[header_index]
    for column in ("Count", "p50", "p90", "p99", "mean", "max"):
        assert column in header

    body = [line for line in lines[header_index:] if not set(line) <= {"-"}]
    assert any(line.startswith("capture") for line in body)
    assert any(line.startswith("TOTAL") for line in body)


def test_format_table_reports_failures_in_the_title():
    runs = [_run("ok", capture=1.0), _run("bad", ok=False, capture=1.0)]
    assert "2 run(s), 1 failed" in format_table(summarize(runs))


def test_format_table_handles_no_data():
    table = format_table(summarize([]))
    assert "0 run(s)" in table
    assert "no successful runs recorded" in table


def test_format_table_uses_the_given_title():
    assert format_table(summarize([]), title="Custom").startswith("Custom - ")

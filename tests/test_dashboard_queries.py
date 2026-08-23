"""Tests for the dashboard's aggregation layer.

The theme running through these: an *absent* measurement must stay absent all
the way to the API. Most of the bugs this layer can have are ones where a
missing value quietly becomes 0 and the dashboard then reports a confident,
wrong number -- which CLAUDE.md's Phase 6 rules exist to prevent.
"""

from __future__ import annotations

import time
import uuid
from datetime import date, timedelta

import pytest

from sway.analytics import session_metrics
from sway.dashboard.queries import DashboardQueries, percentile
from sway.history import KIND_DICTATION, HistoryStore
from sway.latency.models import RunRecord, StageTiming
from sway.latency.store import LatencyStore

DAY = 86_400.0


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "history.db", tmp_path / "latency.db"


def _add_session(
    history_db,
    *,
    text="one two three four five six seven eight nine ten",
    speech_ms=5_000.0,
    when=None,
    app="code.exe",
    run_id=None,
    with_metrics=True,
):
    with HistoryStore.open(history_db) as store:
        metrics = (
            session_metrics(text, speech_ms=speech_ms, audio_ms=speech_ms + 1_000, typing_wpm=45.0)
            if with_metrics
            else None
        )
        return store.add(
            kind=KIND_DICTATION,
            text=text,
            raw_text=text,
            focused_process=app,
            created_at=when if when is not None else time.time(),
            run_id=run_id,
            metrics=metrics,
            status="pasted",
        )


def _add_run(latency_db, *, total_ms=700.0, ok=True, felt=None, skipped=None, error=None, when=None):
    run_id = uuid.uuid4().hex
    metadata = {"source": "hotkey"}
    if felt is not None:
        metadata["release_to_paste_ms"] = felt
    if skipped:
        metadata["skipped"] = skipped
    with LatencyStore.open(latency_db) as store:
        store.insert_runs(
            [
                RunRecord(
                    id=run_id,
                    started_at=when if when is not None else time.time(),
                    total_ms=total_ms,
                    ok=ok,
                    error=error,
                    metadata=metadata,
                    stages=(
                        StageTiming("transcribe", 0, time.time(), 400.0),
                        StageTiming("cleanup", 1, time.time(), 200.0),
                    ),
                )
            ]
        )
    return run_id


class TestPercentile:
    def test_empty_sample_is_none_not_zero(self) -> None:
        assert percentile([], 0.5) is None

    def test_single_value(self) -> None:
        assert percentile([42.0], 0.95) == 42.0

    def test_interpolates(self) -> None:
        assert percentile([0.0, 10.0], 0.5) == pytest.approx(5.0)

    def test_p50_of_a_known_sample(self) -> None:
        assert percentile([1, 2, 3, 4, 5], 0.5) == pytest.approx(3.0)


class TestEmptyInstall:
    """A user who has never dictated must still get a working dashboard."""

    def test_missing_databases_do_not_raise(self, paths) -> None:
        with DashboardQueries.open(*paths) as q:
            assert q.summary().sessions == 0
            assert q.sessions() == []
            assert q.app_usage() == []
            assert q.performance()["total"] == 0
            assert q.session_count() == 0

    def test_empty_summary_reports_none_not_zero_for_averages(self, paths) -> None:
        with DashboardQueries.open(*paths) as q:
            summary = q.summary()
            assert summary.avg_wpm is None
            assert summary.median_latency_ms is None

    def test_empty_streaks(self, paths) -> None:
        with DashboardQueries.open(*paths) as q:
            assert q.streaks().current == 0

    def test_health_is_unknown_without_data(self, paths) -> None:
        with DashboardQueries.open(*paths) as q:
            assert q.health().status == "unknown"


class TestSummary:
    def test_counts_words_and_sessions(self, paths) -> None:
        history, latency = paths
        _add_session(history)
        _add_session(history)
        with DashboardQueries.open(history, latency) as q:
            summary = q.summary()
            assert summary.sessions == 2
            assert summary.words == 20

    def test_avg_wpm_ignores_unmeasured_sessions(self, paths) -> None:
        history, latency = paths
        # 10 words in 5s => 120 WPM.
        _add_session(history, speech_ms=5_000)
        # No metrics at all: must not be averaged in as a zero.
        _add_session(history, with_metrics=False)
        with DashboardQueries.open(history, latency) as q:
            summary = q.summary()
            assert summary.avg_wpm == pytest.approx(120.0)
            assert summary.measured_sessions == 1
            assert summary.sessions == 2

    def test_avg_wpm_is_none_when_nothing_was_measured(self, paths) -> None:
        history, latency = paths
        _add_session(history, with_metrics=False)
        with DashboardQueries.open(history, latency) as q:
            assert q.summary().avg_wpm is None

    def test_time_saved_accumulates(self, paths) -> None:
        history, latency = paths
        _add_session(history, speech_ms=5_000)  # 10 words: 13.3s typing - 5s
        with DashboardQueries.open(history, latency) as q:
            assert q.summary().time_saved_s == pytest.approx(10 / 45 * 60 - 5.0)

    def test_window_excludes_older_sessions(self, paths) -> None:
        history, latency = paths
        _add_session(history, when=time.time() - 40 * DAY)
        _add_session(history)
        with DashboardQueries.open(history, latency) as q:
            assert q.summary(since=time.time() - 7 * DAY).sessions == 1
            assert q.summary().sessions == 2


class TestLatency:
    def test_prefers_the_felt_latency_annotation(self, paths) -> None:
        history, latency = paths
        _add_run(latency, total_ms=700.0, felt=712.0)
        with DashboardQueries.open(history, latency) as q:
            assert q.median_latency() == pytest.approx(712.0)

    def test_falls_back_to_run_total_without_the_annotation(self, paths) -> None:
        history, latency = paths
        _add_run(latency, total_ms=650.0)
        with DashboardQueries.open(history, latency) as q:
            assert q.median_latency() == pytest.approx(650.0)

    def test_skipped_runs_are_excluded_from_the_distribution(self, paths) -> None:
        """A silent hotkey press finishes in ~1ms having transcribed nothing.
        Counting it would drag the reported median below anything a real
        dictation ever achieves."""
        history, latency = paths
        _add_run(latency, total_ms=1.3, felt=0.0, skipped="no_speech")
        _add_run(latency, total_ms=600.0, felt=600.0)
        _add_run(latency, total_ms=800.0, felt=800.0)
        with DashboardQueries.open(history, latency) as q:
            assert q.median_latency() == pytest.approx(700.0)
            perf = q.performance()
            assert perf["total"] == 2
            assert perf["skipped"] == 1
            assert perf["latency"]["min"] == pytest.approx(600.0)


class TestPerformance:
    def test_counts_failures_and_error_rate(self, paths) -> None:
        history, latency = paths
        _add_run(latency, ok=True, felt=500.0)
        _add_run(latency, ok=False, error="GroqRateLimitError")
        with DashboardQueries.open(history, latency) as q:
            perf = q.performance()
            assert (perf["total"], perf["ok"], perf["failed"]) == (2, 1, 1)
            assert perf["error_rate"] == pytest.approx(0.5)
            assert perf["errors"] == [{"error": "GroqRateLimitError", "count": 1}]

    def test_stage_shares_sum_to_one(self, paths) -> None:
        history, latency = paths
        _add_run(latency, felt=600.0)
        with DashboardQueries.open(history, latency) as q:
            stages = q.stage_breakdown()
            assert {s.stage for s in stages} == {"transcribe", "cleanup"}
            assert sum(s.share for s in stages) == pytest.approx(1.0)

    def test_health_flags_a_high_failure_rate(self, paths) -> None:
        history, latency = paths
        for _ in range(8):
            _add_run(latency, ok=False, error="GroqError")
        _add_run(latency, ok=True, felt=500.0)
        with DashboardQueries.open(history, latency) as q:
            assert q.health().status == "problem"

    def test_health_is_excellent_when_fast_and_reliable(self, paths) -> None:
        history, latency = paths
        for _ in range(6):
            _add_run(latency, felt=600.0)
        with DashboardQueries.open(history, latency) as q:
            health = q.health()
            assert health.status == "excellent"
            # The indicator must always explain itself.
            assert health.detail


class TestStreaks:
    def test_consecutive_days(self, paths) -> None:
        history, latency = paths
        today = date.today()
        for offset in (0, 1, 2):
            day = today - timedelta(days=offset)
            _add_session(history, when=time.mktime(day.timetuple()) + 3600)
        with DashboardQueries.open(history, latency) as q:
            streaks = q.streaks(today=today)
            assert streaks.current == 3
            assert streaks.longest == 3
            assert streaks.active_days == 3

    def test_today_being_idle_does_not_break_the_streak(self, paths) -> None:
        """The day is not over yet."""
        history, latency = paths
        today = date.today()
        for offset in (1, 2):
            day = today - timedelta(days=offset)
            _add_session(history, when=time.mktime(day.timetuple()) + 3600)
        with DashboardQueries.open(history, latency) as q:
            assert q.streaks(today=today).current == 2

    def test_a_gap_breaks_the_streak(self, paths) -> None:
        history, latency = paths
        today = date.today()
        for offset in (0, 3, 4):
            day = today - timedelta(days=offset)
            _add_session(history, when=time.mktime(day.timetuple()) + 3600)
        with DashboardQueries.open(history, latency) as q:
            streaks = q.streaks(today=today)
            assert streaks.current == 1
            assert streaks.longest == 2


class TestDailyBuckets:
    def test_includes_days_with_no_activity(self, paths) -> None:
        history, latency = paths
        _add_session(history)
        with DashboardQueries.open(history, latency) as q:
            buckets = q.daily_buckets(7)
            assert len(buckets) == 7
            assert sum(b.sessions for b in buckets) == 1
            assert [b.day for b in buckets] == sorted(b.day for b in buckets)

    def test_idle_day_reports_no_wpm_rather_than_zero(self, paths) -> None:
        history, latency = paths
        with DashboardQueries.open(history, latency) as q:
            assert all(bucket.avg_wpm is None for bucket in q.daily_buckets(3))


class TestAppUsage:
    def test_groups_and_shares(self, paths) -> None:
        history, latency = paths
        _add_session(history, app="code.exe")
        _add_session(history, app="code.exe")
        _add_session(history, app="chrome.exe")
        with DashboardQueries.open(history, latency) as q:
            usage = q.app_usage()
            assert usage[0]["app"] == "code.exe"
            assert usage[0]["share"] == pytest.approx(2 / 3)
            assert sum(item["share"] for item in usage) == pytest.approx(1.0)

    def test_missing_app_is_labelled_unknown_not_dropped(self, paths) -> None:
        history, latency = paths
        _add_session(history, app=None)
        with DashboardQueries.open(history, latency) as q:
            assert q.app_usage()[0]["app"] == "unknown"


class TestSessionDetail:
    def test_joins_the_run_and_its_stages(self, paths) -> None:
        history, latency = paths
        run_id = _add_run(latency, felt=712.0)
        entry = _add_session(history, run_id=run_id)
        with DashboardQueries.open(history, latency) as q:
            detail = q.session_detail(entry.id)
            assert detail["run"]["release_to_paste_ms"] == pytest.approx(712.0)
            assert [s["stage"] for s in detail["stages"]] == ["transcribe", "cleanup"]

    def test_unlinked_entry_reports_no_stages_rather_than_failing(self, paths) -> None:
        """Pre-v2 rows have no run_id; the UI says so instead of drawing an
        empty waterfall that reads as zero latency."""
        history, latency = paths
        entry = _add_session(history, run_id=None)
        with DashboardQueries.open(history, latency) as q:
            detail = q.session_detail(entry.id)
            assert detail["stages"] == []
            assert detail["run"] is None

    def test_unknown_id_is_none(self, paths) -> None:
        history, latency = paths
        with DashboardQueries.open(history, latency) as q:
            assert q.session_detail("nope") is None


class TestSessionSearch:
    def test_matches_substring(self, paths) -> None:
        history, latency = paths
        _add_session(history, text="deploy the vercel project")
        _add_session(history, text="something entirely different")
        with DashboardQueries.open(history, latency) as q:
            assert len(q.sessions(query="vercel")) == 1

    def test_like_wildcards_are_matched_literally(self, paths) -> None:
        history, latency = paths
        _add_session(history, text="plain text")
        with DashboardQueries.open(history, latency) as q:
            # Were % not escaped, this would match every row.
            assert q.sessions(query="%") == []


class TestSessionListLatency:
    """The Activity table's processing-time column comes from a LEFT JOIN
    onto the run, so a session with no linked run must still list."""

    def test_linked_session_carries_its_felt_latency(self, paths) -> None:
        history, latency = paths
        run_id = _add_run(latency, felt=712.0)
        _add_session(history, run_id=run_id)
        with DashboardQueries.open(history, latency) as q:
            row = q.sessions()[0]
            assert row["latency_ms"] == pytest.approx(712.0)
            assert row["run_ok"] is True

    def test_unlinked_session_still_appears_with_no_latency(self, paths) -> None:
        history, latency = paths
        _add_session(history, run_id=None)
        with DashboardQueries.open(history, latency) as q:
            rows = q.sessions()
            assert len(rows) == 1
            assert rows[0]["latency_ms"] is None

    def test_falls_back_to_run_total_when_unannotated(self, paths) -> None:
        history, latency = paths
        run_id = _add_run(latency, total_ms=655.0)
        _add_session(history, run_id=run_id)
        with DashboardQueries.open(history, latency) as q:
            assert q.sessions()[0]["latency_ms"] == pytest.approx(655.0)

    def test_search_still_works_with_the_join(self, paths) -> None:
        history, latency = paths
        run_id = _add_run(latency, felt=500.0)
        _add_session(history, text="deploy the vercel project", run_id=run_id)
        _add_session(history, text="unrelated chatter")
        with DashboardQueries.open(history, latency) as q:
            found = q.sessions(query="vercel")
            assert len(found) == 1
            assert found[0]["latency_ms"] == pytest.approx(500.0)

    def test_kind_filter_still_works_with_the_join(self, paths) -> None:
        history, latency = paths
        _add_session(history)
        with DashboardQueries.open(history, latency) as q:
            assert len(q.sessions(kind="dictation")) == 1
            assert q.sessions(kind="command") == []

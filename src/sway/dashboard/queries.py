"""Read-only aggregation across the engine's databases.

The dashboard reads ``history.db`` (what was said) and ``latency.db`` (how
long each stage took) together, joined on ``history_entries.run_id``, which
schema v2 added for exactly this purpose. Both are ATTACHed to one connection
so the join happens in SQLite rather than by pulling both tables into Python
and matching them by hand.

Two rules run through every function here, both from CLAUDE.md's Phase 6:

* **Never invent a number.** An average over zero rows is ``None``, not 0.0;
  a WPM that was never measured stays ``None`` all the way to the UI, which
  renders it as "not available". Returning 0 would be a lie that silently
  drags every average down, and it is indistinguishable from a real zero.
* **Say when a number is an estimate.** Time saved compares against an
  assumed typing speed, so anything derived from it is labelled as such.

Day bucketing uses the *local* calendar day, not UTC: a user dictating at
11pm expects it to count toward today, and a streak that breaks at 5pm
because UTC rolled over would be simply wrong.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Self

from sway.analytics import DEFAULT_TYPING_WPM, time_saved_seconds
from sway.errors import SwayError

logger = logging.getLogger(__name__)

#: Stages reported in the pipeline breakdown, in pipeline order. Mirrors
#: flow.latency.models.Stage but named here so the dashboard's ordering does
#: not silently change when a stage is added to that enum for another purpose.
PIPELINE_STAGES = (
    "capture",
    "vad",
    "encode",
    "transcribe",
    "merge",
    "selection",
    "command",
    "cleanup",
    "inject",
)

#: Above this, a dictation stops feeling instant. CLAUDE.md targets <1000ms
#: p50; this is the p95 line where the experience is visibly degraded rather
#: than merely off-target.
SLOW_P95_MS = 2_000.0

#: Columns schema v2 introduced. Every query here assumes them, so their
#: absence means the database has not been migrated yet.
_V2_COLUMNS = frozenset(
    {"run_id", "word_count", "character_count", "audio_ms", "speech_ms", "wpm",
     "time_saved_s", "language", "status"}
)


class DashboardQueryError(SwayError):
    """A dashboard read failed. Never fatal: the engine keeps dictating."""


def _day_of(timestamp: float) -> date:
    return datetime.fromtimestamp(timestamp).date()


def _start_of_local_day(day: date) -> float:
    return datetime(day.year, day.month, day.day).timestamp()


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Linear-interpolated percentile, or ``None`` for an empty sample.

    Implemented here rather than taken from numpy because the dashboard must
    return ``None`` for an empty sample instead of numpy's ``nan``, and
    because it keeps this module importable without the audio stack.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _mean(values: Sequence[float]) -> float | None:
    """Average, or ``None`` when there is nothing to average."""
    return sum(values) / len(values) if values else None


@dataclass(frozen=True, slots=True)
class Summary:
    """The headline numbers for one window of time."""

    words: int
    sessions: int
    #: ``None`` when no session in the window had a measurable speaking rate.
    avg_wpm: float | None
    #: An estimate: assumes the user would otherwise have typed at
    #: ``typing_wpm``. Always labelled as such in the UI.
    time_saved_s: float
    #: Hotkey release to text on screen, median. ``None`` without runs.
    median_latency_ms: float | None
    #: How many sessions actually had a measurable WPM, so the UI can say how
    #: much of the window ``avg_wpm`` really covers.
    measured_sessions: int


@dataclass(frozen=True, slots=True)
class DayBucket:
    day: date
    words: int
    sessions: int
    avg_wpm: float | None


@dataclass(frozen=True, slots=True)
class Streaks:
    current: int
    longest: int
    active_days: int
    #: Calendar days from first use to today, for "used on 12 of 30 days".
    span_days: int


@dataclass(frozen=True, slots=True)
class StageBreakdown:
    stage: str
    median_ms: float
    p95_ms: float | None
    share: float


@dataclass(frozen=True, slots=True)
class Health:
    """The traffic light on the Performance page.

    Derived only from measured values, and always carrying the sentence that
    explains it -- an indicator the user cannot act on is decoration, which
    CLAUDE.md's Phase 6 rules exclude.
    """

    status: str  # "excellent" | "degraded" | "problem" | "unknown"
    headline: str
    detail: str


class DashboardQueries:
    """One read-only view over the engine's data. Use as a context manager.

    Owns its own connection rather than borrowing the engine's stores: those
    are thread-confined by contract, and the dashboard is served from an HTTP
    thread. Only SELECTs are ever issued here.
    """

    def __init__(self, connection: sqlite3.Connection, *, typing_wpm: float) -> None:
        self._conn = connection
        self._typing_wpm = typing_wpm
        self._closed = False

    @classmethod
    def open(
        cls,
        history_db: Path | str,
        latency_db: Path | str,
        *,
        typing_wpm: float = DEFAULT_TYPING_WPM,
    ) -> Self:
        """Open both databases on one connection.

        Missing files are not an error: a user who has never dictated has no
        databases yet, and the dashboard must open and show its empty states
        rather than refuse to start. SQLite creates an empty file in that
        case, and the resulting absence of tables is handled by
        :meth:`_has_table` on every query.
        """
        try:
            conn = sqlite3.connect(str(history_db), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("ATTACH DATABASE ? AS lat", (str(latency_db),))
        except sqlite3.Error as exc:
            raise DashboardQueryError(f"could not open the dashboard databases: {exc}") from exc
        return cls(conn, typing_wpm=typing_wpm)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- schema tolerance --------------------------------------------------

    def _has_table(self, name: str, schema: str = "main") -> bool:
        """Whether a table exists. On a first run neither database is
        populated, and every query below degrades to an empty result rather
        than raising.
        """
        row = self._conn.execute(
            f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        return row is not None

    def _columns(self, table: str, schema: str = "main") -> set[str]:
        try:
            rows = self._conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
        except sqlite3.Error:
            return set()
        return {row[1] for row in rows}

    @property
    def has_history(self) -> bool:
        """Whether there is a history table this build can actually read.

        A pre-v2 database has the table but none of the metric columns. The
        server migrates on startup so this should not normally happen, but a
        database opened read-only, or one belonging to a newer build, must
        degrade to empty states rather than 500 every page -- the dashboard
        failing loudly would look like the *engine* being broken, which it
        would not be.
        """
        if not self._has_table("history_entries"):
            return False
        if not _V2_COLUMNS <= self._columns("history_entries"):
            logger.warning(
                "history database predates this build (missing %s); "
                "reporting empty until it is migrated",
                ", ".join(sorted(_V2_COLUMNS - self._columns("history_entries"))),
            )
            return False
        return True

    @property
    def has_runs(self) -> bool:
        return self._has_table("runs", "lat")

    def _select(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        try:
            return self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise DashboardQueryError(f"dashboard query failed: {exc}") from exc

    # -- summaries ---------------------------------------------------------

    def summary(self, *, since: float | None = None, until: float | None = None) -> Summary:
        """Headline numbers over a time window (defaults to all time)."""
        if not self.has_history:
            return Summary(0, 0, None, 0.0, None, 0)

        where, params = _time_window("created_at", since, until)
        rows = self._select(
            f"SELECT word_count, wpm, speech_ms, time_saved_s FROM history_entries{where}",
            params,
        )
        words = sum(int(row["word_count"] or 0) for row in rows)
        measured_wpm = [float(row["wpm"]) for row in rows if row["wpm"] is not None]

        # Pre-v2 rows recorded no speech duration, so their saving cannot be
        # recomputed. They contribute their words to the total but nothing to
        # the estimate, which is the honest split rather than a guess.
        saved = 0.0
        for row in rows:
            if row["time_saved_s"] is not None:
                saved += float(row["time_saved_s"])
            elif row["speech_ms"] is not None and row["word_count"]:
                saved += time_saved_seconds(
                    int(row["word_count"]), float(row["speech_ms"]), typing_wpm=self._typing_wpm
                )

        return Summary(
            words=words,
            sessions=len(rows),
            avg_wpm=_mean(measured_wpm),
            time_saved_s=saved,
            median_latency_ms=self.median_latency(since=since, until=until),
            measured_sessions=len(measured_wpm),
        )

    def median_latency(
        self, *, since: float | None = None, until: float | None = None
    ) -> float | None:
        """Median hotkey-release-to-paste time -- the number the user feels."""
        return percentile(self._latency_samples(since=since, until=until), 0.5)

    def _latency_samples(
        self, *, since: float | None = None, until: float | None = None
    ) -> list[float]:
        """Per-run felt latency for successful runs that actually dictated.

        Prefers the ``release_to_paste_ms`` annotation, which starts at the
        hotkey release; falls back to the run's own total, which starts
        fractionally later when the worker thread was scheduled.

        Runs annotated ``skipped`` (an empty capture, or a hold that turned
        out to be all silence) are excluded. They succeed in about a
        millisecond having transcribed nothing, so counting them would pull
        the reported median below what any real dictation ever achieves --
        the distribution would describe the pauses, not the product.
        """
        if not self.has_runs:
            return []
        where, params = _time_window("started_at", since, until)
        clause = f"{where} AND ok = 1" if where else " WHERE ok = 1"
        rows = self._select(f"SELECT total_ms, metadata FROM lat.runs{clause}", params)
        samples: list[float] = []
        for row in rows:
            if _was_skipped(row["metadata"]):
                continue
            value = _metadata_number(row["metadata"], "release_to_paste_ms")
            samples.append(value if value is not None else float(row["total_ms"]))
        return samples

    # -- activity over time ------------------------------------------------

    def daily_buckets(self, days: int, *, until_day: date | None = None) -> list[DayBucket]:
        """One bucket per calendar day, oldest first, including days with no
        activity -- a chart that omits empty days silently redraws a broken
        streak as a continuous one.
        """
        end = until_day or date.today()
        start = end - timedelta(days=max(1, days) - 1)
        per_day: dict[date, tuple[list[float], int, int]] = {
            start + timedelta(days=offset): ([], 0, 0)
            for offset in range((end - start).days + 1)
        }

        if self.has_history:
            rows = self._select(
                "SELECT created_at, word_count, wpm FROM history_entries WHERE created_at >= ?",
                (_start_of_local_day(start),),
            )
            for row in rows:
                day = _day_of(float(row["created_at"]))
                if day not in per_day:
                    continue
                wpms, words, sessions = per_day[day]
                if row["wpm"] is not None:
                    wpms.append(float(row["wpm"]))
                per_day[day] = (wpms, words + int(row["word_count"] or 0), sessions + 1)

        return [
            DayBucket(
                day=day,
                words=per_day[day][1],
                sessions=per_day[day][2],
                avg_wpm=_mean(per_day[day][0]),
            )
            for day in sorted(per_day)
        ]

    def streaks(self, *, today: date | None = None) -> Streaks:
        """Consecutive-day usage.

        A day counts if the user dictated at all. Today not yet having a
        session does not break the streak -- the day is not over yet.
        Yesterday having none does.
        """
        if not self.has_history:
            return Streaks(0, 0, 0, 0)
        rows = self._select("SELECT created_at FROM history_entries")
        active = {_day_of(float(row["created_at"])) for row in rows}
        if not active:
            return Streaks(0, 0, 0, 0)

        now = today or date.today()
        ordered = sorted(active)

        longest = run = 1
        for previous, current in zip(ordered, ordered[1:]):
            run = run + 1 if (current - previous).days == 1 else 1
            longest = max(longest, run)

        cursor = now if now in active else now - timedelta(days=1)
        current_streak = 0
        while cursor in active:
            current_streak += 1
            cursor -= timedelta(days=1)

        return Streaks(
            current=current_streak,
            longest=longest,
            active_days=len(active),
            span_days=(now - ordered[0]).days + 1,
        )

    # -- app usage ---------------------------------------------------------

    def app_usage(self, *, since: float | None = None, limit: int = 12) -> list[dict[str, Any]]:
        """Which applications received dictated text, most-used first.

        Real data: the focused process name is captured on hotkey press (see
        flow.platform). Entries recorded without one are grouped as unknown
        rather than dropped, so the shares still sum to the whole.
        """
        if not self.has_history:
            return []
        where, params = _time_window("created_at", since, None)
        rows = self._select(
            "SELECT COALESCE(NULLIF(focused_process, ''), 'unknown') AS app,"
            " COUNT(*) AS sessions, COALESCE(SUM(word_count), 0) AS words"
            f" FROM history_entries{where}"
            " GROUP BY app ORDER BY sessions DESC, words DESC",
            params,
        )
        total = sum(int(row["sessions"]) for row in rows) or 1
        return [
            {
                "app": row["app"],
                "sessions": int(row["sessions"]),
                "words": int(row["words"]),
                "share": int(row["sessions"]) / total,
            }
            for row in rows[:limit]
        ]

    # -- sessions ----------------------------------------------------------

    def sessions(
        self, *, query: str = "", limit: int = 100, offset: int = 0, kind: str | None = None
    ) -> list[dict[str, Any]]:
        """The Activity list: one row per dictation or command, newest first."""
        if not self.has_history:
            return []
        # Columns are qualified from the start: this query LEFT JOINs
        # lat.runs, and an unqualified `text`/`kind` would be ambiguous the
        # moment either table grows a column of that name.
        where: list[str] = []
        params: list[Any] = []
        if kind:
            where.append("h.kind = ?")
            params.append(kind)
        query = query.strip()
        if query:
            where.append(
                "(h.text LIKE ? ESCAPE '\\' OR h.raw_text LIKE ? ESCAPE '\\')"
            )
            pattern = f"%{_escape_like(query)}%"
            params += [pattern, pattern]
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params += [limit, offset]

        # LEFT JOIN, not an inner one: entries written before schema v2 have
        # no run_id and must still appear in the list, just without a timing.
        # The join is only possible once latency.db actually has its tables.
        joined = self.has_runs
        select = (
            "SELECT h.*, r.total_ms AS run_total_ms, r.metadata AS run_metadata,"
            " r.ok AS run_ok"
            " FROM history_entries h LEFT JOIN lat.runs r ON r.id = h.run_id"
            if joined
            else "SELECT h.* FROM history_entries h"
        )
        rows = self._select(
            f"{select}{clause} ORDER BY h.created_at DESC, h.rowid DESC LIMIT ? OFFSET ?",
            params,
        )
        return [_session_row(row, with_run=joined) for row in rows]

    def session_count(self) -> int:
        if not self.has_history:
            return 0
        return int(self._select("SELECT COUNT(*) AS n FROM history_entries")[0]["n"])

    def session_detail(self, entry_id: str) -> dict[str, Any] | None:
        """One session with its full stage waterfall, when the run is linked.

        Entries written before schema v2 carry no ``run_id``; their ``stages``
        come back empty and the UI says the breakdown was not recorded, rather
        than drawing an empty waterfall that reads as zero latency.
        """
        if not self.has_history:
            return None
        rows = self._select("SELECT * FROM history_entries WHERE id = ?", (entry_id,))
        if not rows:
            return None
        row = rows[0]
        detail = _session_row(row)
        detail["text"] = row["text"]
        detail["raw_text"] = row["raw_text"]
        detail["instruction"] = row["instruction"]
        detail["stages"] = []
        detail["run"] = None

        run_id = row["run_id"]
        if run_id and self.has_runs:
            run_rows = self._select(
                "SELECT id, started_at, total_ms, ok, error, metadata FROM lat.runs WHERE id = ?",
                (run_id,),
            )
            if run_rows:
                run = run_rows[0]
                detail["run"] = {
                    "id": run["id"],
                    "total_ms": float(run["total_ms"]),
                    "ok": bool(run["ok"]),
                    "error": run["error"],
                    "release_to_paste_ms": _metadata_number(run["metadata"], "release_to_paste_ms"),
                    "metadata": _metadata_dict(run["metadata"]),
                }
                stage_rows = self._select(
                    "SELECT stage, seq, started_at, duration_ms, ok FROM lat.stage_timings"
                    " WHERE run_id = ? ORDER BY seq",
                    (run_id,),
                )
                detail["stages"] = [
                    {
                        "stage": s["stage"],
                        "seq": int(s["seq"]),
                        "started_at": float(s["started_at"]),
                        "duration_ms": float(s["duration_ms"]),
                        "ok": bool(s["ok"]),
                    }
                    for s in stage_rows
                ]
        return detail

    # -- performance -------------------------------------------------------

    def performance(self, *, since: float | None = None) -> dict[str, Any]:
        """Request counts, latency distribution, and the per-stage breakdown."""
        if not self.has_runs:
            return {
                "total": 0,
                "ok": 0,
                "failed": 0,
                "error_rate": None,
                "skipped": 0,
                "latency": _empty_latency(),
                "stages": [],
                "errors": [],
            }
        where, params = _time_window("started_at", since, None)
        all_rows = self._select(
            f"SELECT id, total_ms, ok, error, metadata FROM lat.runs{where}", params
        )
        # A skipped run is a hotkey press that produced no speech. It is not a
        # failure and not a request; counting it in either would misreport
        # both the error rate and the latency distribution.
        skipped = [row for row in all_rows if _was_skipped(row["metadata"])]
        rows = [row for row in all_rows if not _was_skipped(row["metadata"])]
        ok_rows = [row for row in rows if row["ok"]]
        samples: list[float] = []
        for row in ok_rows:
            value = _metadata_number(row["metadata"], "release_to_paste_ms")
            samples.append(value if value is not None else float(row["total_ms"]))

        errors: dict[str, int] = {}
        for row in rows:
            if not row["ok"] and row["error"]:
                errors[row["error"]] = errors.get(row["error"], 0) + 1

        return {
            "total": len(rows),
            "ok": len(ok_rows),
            "failed": len(rows) - len(ok_rows),
            "skipped": len(skipped),
            "error_rate": (len(rows) - len(ok_rows)) / len(rows) if rows else None,
            "latency": {
                "mean": _mean(samples),
                "p50": percentile(samples, 0.50),
                "p95": percentile(samples, 0.95),
                "p99": percentile(samples, 0.99),
                "min": min(samples) if samples else None,
                "max": max(samples) if samples else None,
                "count": len(samples),
            },
            "stages": [
                {
                    "stage": s.stage,
                    "median_ms": s.median_ms,
                    "p95_ms": s.p95_ms,
                    "share": s.share,
                }
                for s in self.stage_breakdown(since=since)
            ],
            "errors": sorted(
                ({"error": name, "count": count} for name, count in errors.items()),
                key=lambda item: item["count"],
                reverse=True,
            ),
        }

    def understanding(self, *, since: float | None = None) -> dict[str, Any]:
        """How often Sway acted on intent rather than transcribing literally.

        Every figure here is counted from what the engine annotated on real
        runs -- corrections detected, transforms fired, snippets substituted,
        utterances resolved with no model call at all. Nothing is derived from
        an assumption, so nothing here needs an "estimate" label; a database
        with no runs in it reports zero rather than a plausible-looking number.
        """
        if not self.has_runs:
            return _empty_understanding()

        where, params = _time_window("started_at", since, None)
        rows = [
            row
            for row in self._select(f"SELECT metadata FROM lat.runs{where}", params)
            if not _was_skipped(row["metadata"])
        ]
        # Only dictations. A command or a transform run has no transcript to
        # correct, and counting them would dilute the rate.
        dictations = [
            _metadata_dict(row["metadata"])
            for row in rows
            if _metadata_dict(row["metadata"]).get("source") == "hotkey"
        ]
        if not dictations:
            return _empty_understanding()

        corrections = [m for m in dictations if m.get("correction_detected")]
        transforms = sum(int(m.get("transforms_fired") or 0) for m in dictations)
        snippets = sum(int(m.get("triggers_fired") or 0) for m in dictations)
        no_model = [m for m in dictations if m.get("cleanup_skipped")]

        kinds: dict[str, int] = {}
        for meta in corrections:
            for kind in str(meta.get("correction_kinds") or "").split(","):
                if kind:
                    kinds[kind] = kinds.get(kind, 0) + 1

        return {
            "dictations": len(dictations),
            "corrections": len(corrections),
            "correction_rate": len(corrections) / len(dictations),
            "transforms": transforms,
            "snippets": snippets,
            "no_model_call": len(no_model),
            "no_model_call_rate": len(no_model) / len(dictations),
            "correction_kinds": sorted(
                ({"kind": name, "count": count} for name, count in kinds.items()),
                key=lambda item: item["count"],
                reverse=True,
            ),
        }

    def stage_breakdown(self, *, since: float | None = None) -> list[StageBreakdown]:
        """Median and p95 per pipeline stage, plus each stage's share of the
        total. This is what answers "why did that dictation feel slow?".
        """
        if not self.has_runs:
            return []
        where, params = _time_window("r.started_at", since, None)
        clause = f"{where} AND r.ok = 1" if where else " WHERE r.ok = 1"
        rows = self._select(
            "SELECT s.stage AS stage, s.duration_ms AS duration_ms, r.metadata AS metadata"
            " FROM lat.stage_timings s JOIN lat.runs r ON r.id = s.run_id"
            f"{clause}",
            params,
        )
        by_stage: dict[str, list[float]] = {}
        for row in rows:
            if _was_skipped(row["metadata"]):
                continue
            by_stage.setdefault(row["stage"], []).append(float(row["duration_ms"]))

        medians = {stage: (percentile(values, 0.5) or 0.0) for stage, values in by_stage.items()}
        total = sum(medians.values()) or 1.0
        ordered = sorted(
            by_stage,
            key=lambda stage: (
                PIPELINE_STAGES.index(stage) if stage in PIPELINE_STAGES else len(PIPELINE_STAGES),
                stage,
            ),
        )
        return [
            StageBreakdown(
                stage=stage,
                median_ms=medians[stage],
                p95_ms=percentile(by_stage[stage], 0.95),
                share=medians[stage] / total,
            )
            for stage in ordered
        ]

    def health(self, *, now: float | None = None) -> Health:
        """The Performance page's traffic light, with the sentence explaining it.

        Compares the last 24h against the week before it, so "degraded" means
        degraded *relative to this user's own normal* rather than against a
        threshold invented for a machine we know nothing about.
        """
        now = now or time.time()
        day_ago, week_ago = now - 86_400, now - 8 * 86_400

        recent = self.performance(since=day_ago)
        if recent["total"] == 0:
            return Health(
                "unknown",
                "Not enough recent activity",
                "Sway hasn't run a dictation in the last 24 hours, so there's nothing to assess.",
            )

        error_rate = recent["error_rate"] or 0.0
        p95 = recent["latency"]["p95"]
        baseline_p95 = percentile(self._latency_samples(since=week_ago, until=day_ago), 0.95)

        trend = ""
        if p95 is not None and baseline_p95:
            change = (p95 - baseline_p95) / baseline_p95
            direction = "increased" if change >= 0 else "decreased"
            trend = f" P95 latency {direction} {abs(change) * 100:.0f}% versus the previous week."

        if error_rate > 0.15:
            return Health(
                "problem",
                "Problems detected",
                f"{error_rate * 100:.0f}% of the last {recent['total']} dictations failed.{trend}",
            )
        if error_rate > 0.05 or (p95 is not None and p95 > SLOW_P95_MS):
            reason = (
                f"{error_rate * 100:.0f}% of recent dictations failed."
                if error_rate > 0.05
                else f"P95 latency is {p95:.0f}ms, above the {SLOW_P95_MS / 1000:.0f}s comfort threshold."
            )
            return Health("degraded", "Degraded", f"{reason}{trend}")
        return Health(
            "excellent",
            "Excellent",
            f"Performance is normal across the last {recent['total']} dictations.{trend}",
        )

    # -- vocabulary --------------------------------------------------------

    def frequent_corrections(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """The raw material for the "add all to dictionary" suggestion."""
        if not self._has_table("corrections"):
            return []
        rows = self._select(
            "SELECT original, corrected, COUNT(*) AS n FROM corrections"
            " GROUP BY original, corrected ORDER BY n DESC, original ASC LIMIT ?",
            (limit,),
        )
        return [
            {"original": r["original"], "corrected": r["corrected"], "count": int(r["n"])}
            for r in rows
        ]


def _empty_latency() -> dict[str, Any]:
    return {
        "mean": None,
        "p50": None,
        "p95": None,
        "p99": None,
        "min": None,
        "max": None,
        "count": 0,
    }


def _session_row(row: sqlite3.Row, *, with_run: bool = False) -> dict[str, Any]:
    """Shape one history row for the API. Nulls stay null all the way out."""
    text = row["text"] or ""
    latency: float | None = None
    ok: bool | None = None
    if with_run and row["run_id"]:
        latency = _metadata_number(row["run_metadata"], "release_to_paste_ms")
        if latency is None and row["run_total_ms"] is not None:
            latency = float(row["run_total_ms"])
        ok = bool(row["run_ok"]) if row["run_ok"] is not None else None
    return {
        "latency_ms": latency,
        "run_ok": ok,
        "id": row["id"],
        "created_at": float(row["created_at"]),
        "kind": row["kind"],
        "preview": text[:160],
        "app": row["focused_process"] or None,
        "run_id": row["run_id"],
        "word_count": row["word_count"],
        "character_count": row["character_count"],
        "audio_ms": row["audio_ms"],
        "speech_ms": row["speech_ms"],
        "wpm": row["wpm"],
        "time_saved_s": row["time_saved_s"],
        "language": row["language"],
        "status": row["status"],
    }


def _time_window(column: str, since: float | None, until: float | None) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if since is not None:
        clauses.append(f"{column} >= ?")
        params.append(since)
    if until is not None:
        clauses.append(f"{column} < ?")
        params.append(until)
    return ((" WHERE " + " AND ".join(clauses)) if clauses else "", params)


def _empty_understanding() -> dict[str, Any]:
    return {
        "dictations": 0,
        "corrections": 0,
        "correction_rate": None,
        "transforms": 0,
        "snippets": 0,
        "no_model_call": 0,
        "no_model_call_rate": None,
        "correction_kinds": [],
    }


def _metadata_dict(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _metadata_number(raw: Any, key: str) -> float | None:
    value = _metadata_dict(raw).get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _was_skipped(raw: Any) -> bool:
    """Whether a run bailed out before transcribing anything (empty capture,
    or a hold containing no detected speech)."""
    return bool(_metadata_dict(raw).get("skipped"))


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def start_of_today() -> float:
    return _start_of_local_day(date.today())


__all__ = [
    "PIPELINE_STAGES",
    "SLOW_P95_MS",
    "DashboardQueries",
    "DashboardQueryError",
    "DayBucket",
    "Health",
    "StageBreakdown",
    "Streaks",
    "Summary",
    "percentile",
    "start_of_today",
]

"""Summarising and formatting latency records.

Everything here is a pure function over :class:`~flow.latency.models.RunRecord`
values — no I/O, no clocks — so the reporting logic is directly testable.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from flow.latency.models import STAGE_ORDER, TOTAL_ROW, RunRecord

#: Percentiles shown in the table, in column order.
PERCENTILES: tuple[float, ...] = (50.0, 90.0, 99.0)


@dataclass(frozen=True, slots=True)
class StageStats:
    """Aggregated durations for one stage (or the synthetic TOTAL row)."""

    name: str
    count: int
    percentiles: tuple[float, ...]
    mean: float
    maximum: float
    #: Runs this stage *could* have appeared in (the sampled, successful runs).
    #: ``count < sampled`` means the stage is absent from some runs, which is
    #: never the same thing as it having taken 0 ms -- see :meth:`partial`.
    sampled: int = 0

    @property
    def p50(self) -> float:
        return self.percentiles[0]

    @property
    def partial(self) -> bool:
        """True when this stage was not measured in every sampled run.

        Two very different things produce this, and the table cannot tell them
        apart on its own, so it flags rather than guesses:

        * the stage did not exist yet when the older runs were recorded (every
          stage added in D0 does this to pre-D0 rows), or
        * the stage genuinely did not run (cleanup skipped by the heuristic,
          merge on a non-streamed run, selection on a plain dictation).

        Either way the percentiles describe only the ``count`` runs that have
        the stage, which is the honest reading. What must never happen is a
        missing stage being folded in as a zero, which would drag every
        percentile toward zero and quietly flatter the numbers.
        """
        return 0 < self.count < self.sampled


@dataclass(frozen=True, slots=True)
class Summary:
    """Everything the table needs, already aggregated."""

    run_count: int
    failed_count: int
    stages: tuple[StageStats, ...]
    total: StageStats | None

    @property
    def sampled_count(self) -> int:
        """Runs the statistics were computed over (successful runs only)."""
        return self.run_count - self.failed_count


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile of ``values`` (need not be sorted).

    Nearest-rank rather than interpolation: it always returns an observed
    measurement, which is what you want when reasoning about a latency budget.
    """
    if not values:
        raise ValueError("percentile() requires at least one value")
    if not 0 <= q <= 100:
        raise ValueError(f"percentile q must be in [0, 100], got {q}")
    ordered = sorted(values)
    if q == 0:
        return ordered[0]
    rank = math.ceil(q / 100.0 * len(ordered))
    return ordered[min(rank, len(ordered)) - 1]


def _stats(name: str, values: Sequence[float], sampled: int = 0) -> StageStats:
    return StageStats(
        name=name,
        count=len(values),
        percentiles=tuple(percentile(values, q) for q in PERCENTILES),
        mean=sum(values) / len(values),
        maximum=max(values),
        sampled=sampled,
    )


def _sort_key(name: str) -> tuple[int, str]:
    """Canonical stages in pipeline order; unknown stages after them, by name."""
    return (STAGE_ORDER.get(name, len(STAGE_ORDER)), name)


def summarize(runs: Sequence[RunRecord]) -> Summary:
    """Aggregate runs into per-stage statistics.

    Failed runs are counted but excluded from the statistics: a run that died
    at ``transcribe`` has no meaningful end-to-end latency, and mixing partial
    runs in would quietly flatter the percentiles.
    """
    failed = sum(1 for run in runs if not run.ok)
    successful = [run for run in runs if run.ok]

    by_stage: dict[str, list[float]] = {}
    for run in successful:
        for timing in run.stages:
            by_stage.setdefault(str(timing.stage), []).append(timing.duration_ms)

    sampled = len(successful)
    stages = tuple(
        _stats(name, by_stage[name], sampled)
        for name in sorted(by_stage, key=_sort_key)
        if by_stage[name]
    )
    totals = [run.total_ms for run in successful]
    return Summary(
        run_count=len(runs),
        failed_count=failed,
        stages=stages,
        total=_stats(TOTAL_ROW, totals, sampled) if totals else None,
    )


def format_table(summary: Summary, *, title: str = "Stage latency") -> str:
    """Render ``summary`` as a plain-ASCII table.

    ASCII rather than box-drawing characters so the output survives a legacy
    Windows console codepage without turning into mojibake.
    """
    headers = ["Stage", "Count", *(f"p{q:g}" for q in PERCENTILES), "mean", "max"]
    rows: list[list[str]] = []

    for stats in summary.stages:
        rows.append(_row(stats))
    if summary.total is not None:
        rows.append(_row(summary.total))

    lines = [f"{title} - {summary.run_count} run(s), {summary.failed_count} failed"]
    if not rows:
        lines.append("  (no successful runs recorded)")
        return "\n".join(lines)

    lines.append("All values in milliseconds; failed runs excluded.")
    lines.append("")

    widths = [
        max(len(headers[col]), *(len(row[col]) for row in rows)) for col in range(len(headers))
    ]
    lines.append(_join(headers, widths))
    lines.append("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for index, row in enumerate(rows):
        # Separate the synthetic TOTAL row from the real stages.
        if summary.total is not None and index == len(rows) - 1 and len(rows) > 1:
            lines.append("-" * (sum(widths) + 2 * (len(widths) - 1)))
        lines.append(_join(row, widths))

    # Spell out what a '*' means rather than leaving the reader to infer that a
    # stage with a low Count was fast. It was not measured -- see
    # StageStats.partial for the two reasons that happens.
    partial = [stats for stats in summary.stages if stats.partial]
    if partial:
        lines.append("")
        lines.append(
            f"* not measured in every run ({summary.sampled_count} sampled); "
            "percentiles cover only the runs that have the stage, and a"
        )
        lines.append(
            "  missing stage is never counted as 0 ms. Either the stage post-dates"
            " those runs, or it did not run."
        )
    return "\n".join(lines)


def _row(stats: StageStats) -> list[str]:
    return [
        stats.name + ("*" if stats.partial else ""),
        str(stats.count),
        *(_ms(value) for value in stats.percentiles),
        _ms(stats.mean),
        _ms(stats.maximum),
    ]


def _ms(value: float) -> str:
    return f"{value:.1f}"


def _join(cells: Sequence[str], widths: Sequence[int]) -> str:
    # First column left-aligned (names), numeric columns right-aligned.
    parts = [cells[0].ljust(widths[0])]
    parts.extend(cell.rjust(width) for cell, width in zip(cells[1:], widths[1:], strict=True))
    return "  ".join(parts).rstrip()


__all__ = [
    "PERCENTILES",
    "StageStats",
    "Summary",
    "format_table",
    "percentile",
    "summarize",
]

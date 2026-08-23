"""Stage-latency measurement.

Built before the feature it measures, per CLAUDE.md Phase 0: every stage
boundary in the dictation pipeline emits a timing event, those events are
durably stored in SQLite, and a formatted table proves whether the
sub-1000ms budget is being met.
"""

from __future__ import annotations

from sway.latency.models import RunRecord, Stage, StageTiming
from sway.latency.report import Summary, StageStats, format_table, percentile, summarize
from sway.latency.store import LatencyStore
from sway.latency.tracker import LatencyTracker, RunRecorder

__all__ = [
    "LatencyStore",
    "LatencyTracker",
    "RunRecord",
    "RunRecorder",
    "Stage",
    "StageStats",
    "StageTiming",
    "Summary",
    "format_table",
    "percentile",
    "summarize",
]

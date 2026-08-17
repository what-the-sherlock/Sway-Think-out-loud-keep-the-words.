"""Value types for latency records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Stage(StrEnum):
    """Canonical pipeline stages, in the order they run.

    The tracker also accepts arbitrary stage names so later phases can add
    stages without editing this enum; members exist to give the common stages
    a single spelling and a stable report ordering.
    """

    CAPTURE = "capture"  # drain the ring buffer into a contiguous frame
    VAD = "vad"  # trim leading/trailing silence
    ENCODE = "encode"  # FLAC, 16 kHz mono
    TRANSCRIBE = "transcribe"  # Groq whisper-large-v3-turbo (Phase 4: just the tail, once chunked)
    MERGE = "merge"  # stitch overlapping chunk transcripts into one (Phase 4)
    SELECTION = "selection"  # Ctrl+C round trip to read the text selection (Phase 5 commands)
    COMMAND = "command"  # Groq llama-3.1-8b-instant applies the spoken instruction (Phase 5)
    CLEANUP = "cleanup"  # Groq llama-3.1-8b-instant (Phase 2)
    INJECT = "inject"  # clipboard save / set / Ctrl+V / restore


#: Report ordering for known stages. Unknown stages sort after these, by name.
STAGE_ORDER: dict[str, int] = {stage.value: index for index, stage in enumerate(Stage)}

#: Synthetic row name for wall-to-wall run duration. Not a real stage, so it can
#: never collide with one.
TOTAL_ROW = "TOTAL"


@dataclass(frozen=True, slots=True)
class StageTiming:
    """One completed stage span within a run."""

    stage: str
    #: Start order within the run; stable even when spans overlap (Phase 4).
    seq: int
    #: Unix epoch seconds when the span opened. Wall clock, for correlation only.
    started_at: float
    #: Measured with a monotonic clock, so it is never distorted by NTP steps.
    duration_ms: float
    ok: bool = True


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One end-to-end dictation attempt: hotkey release to text on screen."""

    id: str
    started_at: float
    total_ms: float
    ok: bool
    error: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    stages: tuple[StageTiming, ...] = ()


__all__ = ["STAGE_ORDER", "TOTAL_ROW", "RunRecord", "Stage", "StageTiming"]

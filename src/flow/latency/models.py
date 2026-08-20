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

    **Schema note (D0).** The persisted form of a stage is its *string value*
    (``stage_timings.stage``), never its position in this enum. Members may
    therefore be inserted in pipeline order without touching stored data;
    only the report's column ordering changes. What must never happen is
    renaming an existing value, which would orphan every historical row that
    carries the old spelling. The D0 additions below (``hotkey_hold``,
    ``dispatch``, ``stt_first_byte``, ``llm_first_token``) are purely
    additive: runs recorded before D0 simply have no row for them, which the
    report renders as a lower ``Count``, not as a 0 ms measurement. See
    :func:`flow.latency.report.summarize`.
    """

    # -- before the pipeline proper: the part of the wait that D0 made visible
    HOTKEY_HOLD = "hotkey_hold"  # key press -> key release (how long the user spoke)
    DISPATCH = "dispatch"  # release -> the worker thread actually starts running

    CAPTURE = "capture"  # drain the ring buffer into a contiguous frame
    VAD = "vad"  # trim leading/trailing silence
    ENCODE = "encode"  # FLAC, 16 kHz mono
    # Request handed to httpx -> response *headers* back. Bounds the upload,
    # the queue wait, and the model's own compute together; see the module
    # docstring of flow.groq_client for why those three cannot be separated
    # through httpx's public API.
    STT_FIRST_BYTE = "stt_first_byte"
    TRANSCRIBE = "transcribe"  # Groq whisper-large-v3-turbo (Phase 4: just the tail, once chunked)
    MERGE = "merge"  # stitch overlapping chunk transcripts into one (Phase 4)
    SELECTION = "selection"  # Ctrl+C round trip to read the text selection (Phase 5 commands)
    COMMAND = "command"  # Groq applies the spoken instruction (Phase 5)
    LLM_FIRST_TOKEN = "llm_first_token"  # cleanup request sent -> first content delta
    CLEANUP = "cleanup"  # Groq cleanup model, whole streamed response (Phase 2)
    INJECT = "inject"  # inject_start -> inject_complete: clipboard save / set / Ctrl+V
    #    (the 150 ms clipboard *restore* runs on a timer after this span closes,
    #     so it has never counted against the user-visible latency)


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

"""The dictation pipeline.

Phase 0 ships only :mod:`flow.pipeline.stub`, a fake pipeline whose sole job is
to exercise the measurement harness end to end. Phase 1 replaces the fake
stages with the real ones behind the same tracker API.
"""

from __future__ import annotations

from sway.pipeline.stub import STUB_STAGES, StubPipeline, StubStageSpec

__all__ = ["STUB_STAGES", "StubPipeline", "StubStageSpec"]

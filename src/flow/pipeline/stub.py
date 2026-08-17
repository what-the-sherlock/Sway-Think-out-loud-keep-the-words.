"""A fake dictation pipeline that exercises the measurement harness.

This is Phase 0's proof that the tracker works: it runs the real
:class:`~flow.latency.tracker.LatencyTracker` through the same stage sequence
the real pipeline uses, sleeping for plausible durations instead of touching a
microphone or the network. Later phases swap the sleeps for real work; the
tracker call sites do not change.

Durations are drawn from a seeded RNG so a run is reproducible, and the
defaults sum to roughly 400-700 ms — comfortably inside the 1000 ms budget, so
a regression in the harness itself is visible in the table.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from flow.errors import FlowError
from flow.latency.models import Stage
from flow.latency.tracker import LatencyTracker

logger = logging.getLogger(__name__)


class StubFailure(FlowError):
    """Simulated stage failure, used to exercise the error-recording path."""


@dataclass(frozen=True, slots=True)
class StubStageSpec:
    """A fake stage and the duration range it pretends to take."""

    stage: Stage
    min_ms: float
    max_ms: float

    def __post_init__(self) -> None:
        if self.min_ms < 0 or self.max_ms < self.min_ms:
            raise ValueError(f"invalid duration range for {self.stage}: {self.min_ms}-{self.max_ms}")


#: Rough expectations, used only to make the demo table realistic.
STUB_STAGES: tuple[StubStageSpec, ...] = (
    StubStageSpec(Stage.CAPTURE, 2.0, 6.0),
    StubStageSpec(Stage.VAD, 8.0, 20.0),
    StubStageSpec(Stage.ENCODE, 25.0, 45.0),
    StubStageSpec(Stage.TRANSCRIBE, 350.0, 600.0),
    StubStageSpec(Stage.CLEANUP, 120.0, 280.0),
    StubStageSpec(Stage.INJECT, 12.0, 25.0),
)


class StubPipeline:
    """Runs fake dictations against a real tracker."""

    def __init__(
        self,
        tracker: LatencyTracker,
        *,
        stages: Sequence[StubStageSpec] = STUB_STAGES,
        seed: int | None = None,
        fail_rate: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not stages:
            raise ValueError("stub pipeline needs at least one stage")
        if not 0.0 <= fail_rate <= 1.0:
            raise ValueError(f"fail_rate must be in [0, 1], got {fail_rate}")
        self._tracker = tracker
        self._stages = tuple(stages)
        self._fail_rate = fail_rate
        self._sleep = sleep
        self._random = random.Random(seed)

    def run_once(self, index: int = 0) -> bool:
        """Execute one fake dictation. Returns True if it succeeded.

        Simulated failures are recorded and swallowed, exactly as the real app
        must behave: a failed dictation shows a failure state on the overlay
        and the app stays alive with the mic cold.
        """
        failing_stage = self._pick_failing_stage()
        try:
            with self._tracker.run({"source": "stub", "index": index}) as run:
                for spec in self._stages:
                    with run.stage(spec.stage):
                        self._sleep(self._duration_s(spec))
                        if spec.stage is failing_stage:
                            raise StubFailure(f"simulated failure in {spec.stage}")
                run.annotate(characters=self._random.randint(20, 180))
        except StubFailure as exc:
            logger.debug("stub run %d failed: %s", index, exc)
            return False
        return True

    def run(self, count: int) -> tuple[int, int]:
        """Execute ``count`` fake dictations. Returns ``(succeeded, failed)``."""
        if count < 0:
            raise ValueError("count must be >= 0")
        succeeded = 0
        for index in range(count):
            if self.run_once(index):
                succeeded += 1
        return succeeded, count - succeeded

    def _duration_s(self, spec: StubStageSpec) -> float:
        return self._random.uniform(spec.min_ms, spec.max_ms) / 1000.0

    def _pick_failing_stage(self) -> Stage | None:
        # Draw unconditionally so the RNG stream — and therefore the whole run —
        # stays reproducible for a given seed regardless of fail_rate.
        roll = self._random.random()
        victim = self._random.choice(self._stages).stage
        return victim if roll < self._fail_rate else None


__all__ = ["STUB_STAGES", "StubFailure", "StubPipeline", "StubStageSpec"]

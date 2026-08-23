"""The stage-latency tracker.

Design constraints, straight from CLAUDE.md:

* Recording a timing must never block the caller. The audio callback thread is
  forbidden from doing I/O, and the network thread must not stall on a disk
  write, so :class:`LatencyTracker` only appends to a bounded in-memory queue.
  A dedicated daemon thread owns the SQLite connection and does every write.
* The queue is bounded and drops on overflow. Telemetry that can stall the
  dictation path is worse than telemetry with a hole in it; drops are counted
  and logged.
* A failure inside the tracker is logged and swallowed. Measurement never takes
  the app down.
"""

from __future__ import annotations

import atexit
import logging
import queue
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from sway.errors import SwayError
from sway.latency.models import RunRecord, Stage, StageTiming
from sway.latency.store import LatencyStore

logger = logging.getLogger(__name__)

#: Dedicated logger for per-stage timing events, so timings can be turned on
#: (or routed elsewhere) without enabling debug logging for the whole app.
stage_logger = logging.getLogger("sway.latency.stages")

_NS_PER_MS = 1_000_000.0


@dataclass(frozen=True, slots=True)
class _Span:
    """An open stage measurement. Returned by ``begin``, consumed by ``end``."""

    stage: str
    seq: int
    started_at: float
    started_ns: int


class _Stop:
    """Queue sentinel telling the writer thread to finish and exit."""


@dataclass(frozen=True, slots=True)
class _Flush:
    """Queue marker; the writer sets ``event`` once everything before it landed."""

    event: threading.Event


_STOP = _Stop()


class RunRecorder:
    """Records the stages of a single dictation run.

    Thread-safe: Phase 4 overlaps chunk uploads on the network thread with
    capture on the main thread, so spans can be opened concurrently.
    """

    __slots__ = (
        "_error",
        "_lock",
        "_metadata",
        "_next_seq",
        "_stages",
        "id",
        "started_at",
        "started_ns",
    )

    def __init__(self, run_id: str, started_at: float, started_ns: int) -> None:
        self.id = run_id
        self.started_at = started_at
        self.started_ns = started_ns
        self._lock = threading.Lock()
        self._stages: list[StageTiming] = []
        self._metadata: dict[str, Any] = {}
        self._next_seq = 0
        self._error: str | None = None

    # -- measurement -------------------------------------------------------

    def begin(self, stage: Stage | str) -> _Span:
        """Open a span. Prefer :meth:`stage`; use this when begin/end are split."""
        name = str(stage)
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
        return _Span(stage=name, seq=seq, started_at=time.time(), started_ns=time.perf_counter_ns())

    def end(self, span: _Span, *, ok: bool = True) -> float:
        """Close a span and record it. Returns its duration in milliseconds."""
        duration_ms = (time.perf_counter_ns() - span.started_ns) / _NS_PER_MS
        timing = StageTiming(
            stage=span.stage,
            seq=span.seq,
            started_at=span.started_at,
            duration_ms=duration_ms,
            ok=ok,
        )
        with self._lock:
            self._stages.append(timing)
        stage_logger.debug(
            "run=%s stage=%s seq=%d duration_ms=%.2f ok=%s",
            self.id,
            span.stage,
            span.seq,
            duration_ms,
            ok,
        )
        return duration_ms

    def record(
        self,
        stage: Stage | str,
        duration_ms: float,
        *,
        started_at: float | None = None,
        ok: bool = True,
    ) -> None:
        """Record an already-completed span measured elsewhere.

        Exists for the spans that finish *before* there is a recorder to hold
        them: the hotkey hold ends at key release, and the release-to-worker
        dispatch gap ends at the moment the run itself is created. Both are
        timed by the caller against the same monotonic clock and handed over
        here, rather than being reconstructed from wall-clock differences.

        ``seq`` is allocated from the same counter as :meth:`begin`, so these
        sort into the run ahead of everything measured afterwards.
        """
        name = str(stage)
        with self._lock:
            seq = self._next_seq
            self._next_seq += 1
            self._stages.append(
                StageTiming(
                    stage=name,
                    seq=seq,
                    started_at=time.time() if started_at is None else started_at,
                    duration_ms=duration_ms,
                    ok=ok,
                )
            )
        stage_logger.debug(
            "run=%s stage=%s seq=%d duration_ms=%.2f ok=%s (pre-recorded)",
            self.id,
            name,
            seq,
            duration_ms,
            ok,
        )

    @contextmanager
    def stage(self, stage: Stage | str) -> Iterator[_Span]:
        """Time a block of work. Exceptions mark the span failed and propagate."""
        span = self.begin(stage)
        try:
            yield span
        except BaseException:
            self.end(span, ok=False)
            raise
        self.end(span, ok=True)

    # -- annotation --------------------------------------------------------

    def annotate(self, **values: Any) -> None:
        """Attach diagnostic metadata (audio seconds, transcript length, ...)."""
        with self._lock:
            self._metadata.update(values)

    def mark_failed(self, error: BaseException | str) -> None:
        """Record why the run failed. The first cause recorded wins."""
        with self._lock:
            if self._error is None:
                self._error = _describe(error)

    # -- finalisation ------------------------------------------------------

    def finish(self, *, ok: bool | None = None, end_ns: int | None = None) -> RunRecord:
        """Freeze this recorder into an immutable record."""
        total_ns = (time.perf_counter_ns() if end_ns is None else end_ns) - self.started_ns
        with self._lock:
            resolved_ok = (self._error is None) if ok is None else ok
            return RunRecord(
                id=self.id,
                started_at=self.started_at,
                total_ms=total_ns / _NS_PER_MS,
                ok=resolved_ok,
                error=self._error,
                metadata=dict(self._metadata),
                stages=tuple(sorted(self._stages, key=lambda t: t.seq)),
            )


class LatencyTracker:
    """Public entry point for recording and persisting stage latencies."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        enabled: bool = True,
        queue_size: int = 1024,
        batch_size: int = 64,
        retention_days: int = 30,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be >= 1")
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")

        self._db_path = Path(db_path)
        self._enabled = enabled
        self._batch_size = batch_size
        self._retention_days = retention_days
        self._queue: queue.Queue[RunRecord | _Flush | _Stop] = queue.Queue(maxsize=queue_size)
        self._dropped = 0
        self._close_lock = threading.Lock()
        self._closed = False
        self._thread: threading.Thread | None = None

        if enabled:
            # Daemon so a forgotten close() can never hang shutdown; the atexit
            # hook does the graceful flush in the normal case.
            self._thread = threading.Thread(
                target=self._writer_loop, name="flow-latency-writer", daemon=True
            )
            self._thread.start()
            atexit.register(self.close)

    @classmethod
    def from_config(cls, config: Any) -> Self:
        """Build a tracker from a :class:`~flow.config.Config`."""
        return cls(
            config.latency_db_path,
            enabled=config.latency.enabled,
            queue_size=config.latency.queue_size,
            batch_size=config.latency.batch_size,
            retention_days=config.latency.retention_days,
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def dropped_records(self) -> int:
        """Records discarded because the queue was full."""
        return self._dropped

    # -- recording ---------------------------------------------------------

    def new_run(self, metadata: Mapping[str, Any] | None = None) -> RunRecorder:
        """Start a run without a ``with`` block; caller must call :meth:`submit`."""
        recorder = RunRecorder(uuid.uuid4().hex, time.time(), time.perf_counter_ns())
        if metadata:
            recorder.annotate(**dict(metadata))
        return recorder

    @contextmanager
    def run(self, metadata: Mapping[str, Any] | None = None) -> Iterator[RunRecorder]:
        """Measure one end-to-end dictation run.

        The record is submitted whether the body succeeds or raises, so failures
        show up in the data instead of vanishing.
        """
        recorder = self.new_run(metadata)
        try:
            yield recorder
        except BaseException as exc:
            recorder.mark_failed(exc)
            self.submit(recorder.finish(ok=False))
            raise
        self.submit(recorder.finish())

    def submit(self, record: RunRecord) -> bool:
        """Hand a finished record to the writer. Never blocks. Returns False if dropped."""
        if not self._enabled or self._closed:
            return False
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._dropped += 1
            # Log the first drop, then every 100th, so a stalled disk cannot
            # turn into a log flood that makes things worse.
            if self._dropped == 1 or self._dropped % 100 == 0:
                logger.warning(
                    "latency queue full; dropped %d record(s) so far", self._dropped
                )
            return False
        return True

    # -- lifecycle ---------------------------------------------------------

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until everything queued so far has been written."""
        if not self._enabled or self._closed:
            return True
        marker = _Flush(threading.Event())
        try:
            self._queue.put(marker, timeout=timeout)
        except queue.Full:
            logger.warning("latency flush timed out enqueuing marker")
            return False
        if not marker.event.wait(timeout):
            logger.warning("latency flush timed out after %.1fs", timeout)
            return False
        return True

    def close(self, timeout: float = 5.0) -> None:
        """Stop the writer thread after draining the queue. Idempotent."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            self._thread = None

        if thread is None:
            return
        try:
            atexit.unregister(self.close)
        except Exception:  # pragma: no cover - defensive; unregister is total
            pass
        # Blocking put: the writer always drains, so this cannot deadlock, and
        # losing the stop signal would leak the thread.
        try:
            self._queue.put(_STOP, timeout=timeout)
        except queue.Full:
            logger.warning("latency writer did not accept the stop signal")
            return
        thread.join(timeout)
        if thread.is_alive():
            logger.warning("latency writer thread did not stop within %.1fs", timeout)
        elif self._dropped:
            logger.warning("latency tracker dropped %d record(s) this session", self._dropped)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- writer thread -----------------------------------------------------

    def _writer_loop(self) -> None:
        store: LatencyStore | None = None
        try:
            store = LatencyStore.open(self._db_path)
            removed = store.prune(self._retention_days)
            if removed:
                logger.info("pruned %d latency run(s) older than %d day(s)", removed, self._retention_days)
        except SwayError as exc:
            # Degraded mode: keep draining so producers never block, but write
            # nothing. The app keeps dictating; only measurement is lost.
            logger.error("latency tracking disabled: %s", exc)
            store = None
        except Exception:  # pragma: no cover - unexpected; must not kill the app
            logger.exception("latency tracking disabled: unexpected error opening store")
            store = None

        try:
            self._drain(store)
        finally:
            if store is not None:
                store.close()

    def _drain(self, store: LatencyStore | None) -> None:
        while True:
            item = self._queue.get()
            batch: list[RunRecord] = []
            pending_flushes: list[_Flush] = []
            stopping = False

            for candidate in self._collect(item):
                if isinstance(candidate, RunRecord):
                    batch.append(candidate)
                elif isinstance(candidate, _Flush):
                    pending_flushes.append(candidate)
                else:
                    stopping = True

            if batch and store is not None:
                try:
                    store.insert_runs(batch)
                except SwayError as exc:
                    logger.error("could not persist %d latency record(s): %s", len(batch), exc)
                except Exception:  # pragma: no cover - never kill the writer
                    logger.exception("unexpected error persisting latency records")

            for pending in pending_flushes:
                pending.event.set()

            if stopping:
                return

    def _collect(self, first: RunRecord | _Flush | _Stop) -> list[RunRecord | _Flush | _Stop]:
        """Take ``first`` plus whatever else is already queued, up to batch_size."""
        items: list[RunRecord | _Flush | _Stop] = [first]
        while len(items) < self._batch_size:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return items


def _describe(error: BaseException | str) -> str:
    if isinstance(error, str):
        return error[:500]
    return f"{type(error).__name__}: {error}"[:500]


__all__ = ["LatencyTracker", "RunRecorder"]

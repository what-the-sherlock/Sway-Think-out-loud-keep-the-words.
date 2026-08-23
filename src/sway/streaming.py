"""Phase 4: overlapping chunked transcription while the hotkey is still held.

CLAUDE.md's spec: ship 5-second overlapping audio chunks while the user is
still speaking, so key release only has to transcribe the short tail; handle
chunk-boundary deduplication carefully; degrade gracefully to Phase 1 (one
whole-clip request) on network failure.

Two pure, hardware-free pieces do the real thinking and carry the unit tests:

* :class:`ChunkClock` — frame-count arithmetic for the overlapping chunk
  schedule. No audio, threads, or I/O.
* :func:`stitch_transcripts` — merges the ordered per-chunk transcripts (plus
  the final tail) into one deduplicated transcript by finding the longest
  matching word run at each boundary.

:class:`ChunkStreamer` is the impure orchestration around them: a background
thread that, while the hotkey is held, periodically cuts a chunk out of the
ring buffer and fires off a transcription request without blocking on the
result. Per CLAUDE.md this wiring is not unit tested (same as
:mod:`flow.audio.capture` and :mod:`flow.engine`); only the pure pieces are.
"""

from __future__ import annotations

import logging
import re
import string
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field

from sway.audio.ring_buffer import RingBuffer
from sway.audio.vad import VadTrimmer
from sway.groq_client import TranscriptionResult

logger = logging.getLogger(__name__)

#: Generous ceiling on spoken words per second of audio, used to bound how
#: many trailing/leading words `stitch_transcripts` compares at a chunk
#: boundary. Kept well above typical speaking rates (~2.5 words/s) so genuine
#: boundary duplicates are always within the search window.
_WORDS_PER_SECOND_CEILING = 5.0
_MIN_OVERLAP_WORDS = 3

_WORD_SPLIT_RE = re.compile(r"\s+")


def max_overlap_words(overlap_seconds: float) -> int:
    """How many trailing/leading words :func:`stitch_transcripts` should compare."""
    return max(_MIN_OVERLAP_WORDS, round(overlap_seconds * _WORDS_PER_SECOND_CEILING))


# ---------------------------------------------------------------------------
# ChunkClock — pure frame-count math for the overlapping chunk schedule
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ChunkClock:
    """Tracks when the next overlapping chunk is due, purely in frame counts.

    Chunk 1 covers ``[start_frame, start_frame + chunk_length_frames)`` and is
    due once that many frames exist. Each later chunk covers the previous
    chunk's right edge minus the overlap, through however many frames now
    exist, so consecutive chunks always share ``overlap_frames`` of audio —
    the overlap :func:`stitch_transcripts` later dedups away.
    """

    start_frame: int
    chunk_length_frames: int
    overlap_frames: int
    _last_boundary_frame: int = field(init=False)
    _chunks_emitted: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.chunk_length_frames <= 0:
            raise ValueError("chunk_length_frames must be > 0")
        if self.overlap_frames < 0:
            raise ValueError("overlap_frames must be >= 0")
        if self.overlap_frames >= self.chunk_length_frames:
            raise ValueError("overlap_frames must be less than chunk_length_frames")
        self._last_boundary_frame = self.start_frame

    @property
    def step_frames(self) -> int:
        return self.chunk_length_frames - self.overlap_frames

    @property
    def chunks_emitted(self) -> int:
        return self._chunks_emitted

    def next_due_frame(self) -> int:
        """The frame count at which the next chunk becomes due."""
        if self._chunks_emitted == 0:
            return self.start_frame + self.chunk_length_frames
        return self._last_boundary_frame + self.step_frames

    def is_due(self, now_frame: int) -> bool:
        return now_frame >= self.next_due_frame()

    def read_range(self, now_frame: int) -> tuple[int, int]:
        """The ``[start, end)`` frame range to read for the chunk due now."""
        return self._range_from(now_frame)

    def advance(self, end_frame: int) -> None:
        """Record that a chunk covering up to ``end_frame`` was just emitted."""
        self._last_boundary_frame = end_frame
        self._chunks_emitted += 1

    def tail_range(self, now_frame: int) -> tuple[int, int]:
        """The ``[start, end)`` range for the final tail read at key release.

        Same overlap rule as any other chunk, so the tail dedups against the
        last emitted chunk exactly like two chunks dedup against each other.
        If no chunk was ever emitted, this is the whole clip since
        ``start_frame`` — i.e. plain Phase 1 behavior.
        """
        return self._range_from(now_frame)

    def _range_from(self, now_frame: int) -> tuple[int, int]:
        if self._chunks_emitted == 0:
            start = self.start_frame
        else:
            start = max(self.start_frame, self._last_boundary_frame - self.overlap_frames)
        return start, max(start, now_frame)


# ---------------------------------------------------------------------------
# stitch_transcripts — pure chunk-boundary deduplication
# ---------------------------------------------------------------------------


def _normalize_word(word: str) -> str:
    return word.strip(string.punctuation).lower()


def _find_overlap(prev_words: list[str], next_words: list[str], limit: int) -> int:
    """Largest ``k <= limit`` where the last k of ``prev_words`` equal (case/
    punctuation-insensitively) the first k of ``next_words``. 0 if none match.
    """
    bound = min(len(prev_words), len(next_words), limit)
    if bound <= 0:
        return 0
    prev_norm = [_normalize_word(w) for w in prev_words[-bound:]]
    next_norm = [_normalize_word(w) for w in next_words[:bound]]
    for k in range(bound, 0, -1):
        if prev_norm[bound - k :] == next_norm[:k]:
            return k
    return 0


def stitch_transcripts(pieces: Sequence[str], *, max_overlap_words: int = 8) -> str:
    """Merge ordered, overlapping-audio transcript pieces into one transcript.

    Each pair of adjacent pieces is assumed to come from audio windows that
    overlap by roughly the same short span, so their transcribed text tends
    to repeat a few words verbatim at the boundary. For each new piece, the
    longest exact (normalized) match between the tail of the merged text so
    far and the head of the new piece is found and dropped from the new
    piece before appending — first occurrence wins. Empty/blank pieces are
    skipped. A piece with no detected overlap is simply concatenated, so
    nothing is ever silently lost.
    """
    words: list[str] = []
    for piece in pieces:
        piece = piece.strip() if piece else ""
        if not piece:
            continue
        next_words = _WORD_SPLIT_RE.split(piece)
        if not words:
            words = next_words
            continue
        overlap = _find_overlap(words, next_words, max_overlap_words)
        words.extend(next_words[overlap:])
    return " ".join(words)


# ---------------------------------------------------------------------------
# ChunkStreamer — the impure background worker (untested per CLAUDE.md)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChunkSessionResult:
    """What the background chunk worker produced, handed back at key release."""

    chunk_texts: list[str]
    #: Total windows the clock considered due, whether or not they had speech.
    chunk_count: int
    #: True if any chunk's transcription failed; callers should fall back to
    #: a single whole-clip request rather than trust the partial results.
    degraded: bool
    error: str | None
    clock: ChunkClock


class ChunkStreamer:
    """Background worker: while running, periodically transcribes overlapping
    windows of the ring buffer without blocking on the network result, so
    most of the utterance is already transcribed by the time the hotkey is
    released.
    """

    def __init__(
        self,
        *,
        ring: RingBuffer,
        vad: VadTrimmer,
        encode: Callable[[bytes], bytes],
        submit_transcribe: Callable[[bytes], Future[TranscriptionResult]],
        start_frame: int,
        sample_rate: int,
        chunk_seconds: float,
        overlap_seconds: float,
        poll_interval_s: float = 0.2,
    ) -> None:
        self._ring = ring
        self._vad = vad
        self._encode = encode
        self._submit_transcribe = submit_transcribe
        self._clock = ChunkClock(
            start_frame=start_frame,
            chunk_length_frames=max(1, round(chunk_seconds * sample_rate)),
            overlap_frames=max(0, round(overlap_seconds * sample_rate)),
        )
        self._poll_interval_s = poll_interval_s
        self._stop_event = threading.Event()
        self._failed_event = threading.Event()
        self._error: str | None = None
        self._futures: list[Future[TranscriptionResult]] = []
        self._thread = threading.Thread(target=self._run, name="flow-chunk-streamer", daemon=True)

    @property
    def clock(self) -> ChunkClock:
        return self._clock

    def start(self) -> None:
        self._thread.start()

    def stop(self, *, timeout: float = 20.0) -> ChunkSessionResult:
        """Stop scheduling new chunks and collect whatever was in flight.

        Bounds the *total* wait across every outstanding future to
        ``timeout`` (a shared deadline, not per-future) so a release can
        never hang waiting on chunk work that started long before it.
        """
        self._stop_event.set()
        self._thread.join(min(timeout, 2.0))
        if self._thread.is_alive():
            logger.warning("chunk streamer worker did not stop promptly")

        texts: list[str] = []
        degraded = self._failed_event.is_set()
        error = self._error
        if not degraded:
            deadline = time.monotonic() + timeout
            for future in self._futures:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    result = future.result(timeout=remaining)
                except Exception as exc:  # noqa: BLE001 - any failure degrades, never propagates
                    degraded = True
                    error = str(exc)
                    logger.warning("streaming chunk transcription failed: %s", exc)
                    break
                if result.text:
                    texts.append(result.text)

        return ChunkSessionResult(
            chunk_texts=texts,
            chunk_count=self._clock.chunks_emitted,
            degraded=degraded,
            error=error,
            clock=self._clock,
        )

    def _run(self) -> None:
        while not self._stop_event.wait(self._poll_interval_s):
            if self._failed_event.is_set():
                return
            now = self._ring.mark()
            if not self._clock.is_due(now):
                continue
            self._emit_chunk(now)

    def _emit_chunk(self, now: int) -> None:
        start, end = self._clock.read_range(now)
        self._clock.advance(end)
        pcm = self._ring.read_since(start)
        if not pcm:
            return
        try:
            trimmed = self._vad.trim(pcm)
        except Exception:
            logger.exception("chunk VAD failed; skipping this chunk")
            return
        if not trimmed:
            return
        try:
            flac_bytes = self._encode(trimmed)
        except Exception:
            logger.exception("chunk FLAC encode failed; skipping this chunk")
            return
        try:
            future = self._submit_transcribe(flac_bytes)
        except Exception as exc:
            logger.warning("could not submit streaming chunk transcription: %s", exc)
            self._error = str(exc)
            self._failed_event.set()
            return
        future.add_done_callback(self._on_chunk_done)
        self._futures.append(future)

    def _on_chunk_done(self, future: Future[TranscriptionResult]) -> None:
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            logger.warning("streaming chunk transcription failed, will fall back: %s", exc)
            self._error = str(exc)
            self._failed_event.set()


__all__ = [
    "ChunkClock",
    "ChunkSessionResult",
    "ChunkStreamer",
    "max_overlap_words",
    "stitch_transcripts",
]

"""A fixed-capacity circular buffer for raw PCM audio.

Sized once at startup and never reallocated: CLAUDE.md requires the audio
callback thread to never block, allocate, or do I/O, and a lock held only for
a `memcpy` is fast enough not to introduce audible jitter into a ~10-30ms
PortAudio callback period. Pure and hardware-free, so it is fully unit
tested; :mod:`flow.audio.capture` is the thin, untested wrapper that feeds it
from a real input stream.
"""

from __future__ import annotations

import threading

#: Anything memoryview() accepts: bytes, bytearray, a cffi buffer from the
#: audio callback, etc. `collections.abc.Buffer` is 3.12+; this stays 3.11-safe.
_BufferLike = bytes | bytearray | memoryview


class RingBuffer:
    """Single-writer (audio callback), multi-reader circular PCM buffer."""

    def __init__(self, capacity_frames: int, *, sample_width: int = 2, channels: int = 1) -> None:
        if capacity_frames < 1:
            raise ValueError("capacity_frames must be >= 1")
        if sample_width < 1:
            raise ValueError("sample_width must be >= 1")
        if channels < 1:
            raise ValueError("channels must be >= 1")
        self._frame_size = sample_width * channels
        self._capacity_frames = capacity_frames
        self._buf = bytearray(capacity_frames * self._frame_size)
        self._lock = threading.Lock()
        self._total_frames = 0

    @property
    def capacity_frames(self) -> int:
        return self._capacity_frames

    @property
    def frame_size(self) -> int:
        return self._frame_size

    def write(self, data: _BufferLike) -> None:
        """Append PCM bytes. Called from the audio callback thread.

        A trailing partial sample (an odd byte count) is dropped rather than
        raising: a malformed final block must never crash the audio thread.
        """
        view = memoryview(data)
        if view.format != "B":
            view = view.cast("B")
        nbytes = len(view)
        remainder = nbytes % self._frame_size
        if remainder:
            nbytes -= remainder
            view = view[:nbytes]
        if nbytes == 0:
            return

        frames = nbytes // self._frame_size
        cap_bytes = len(self._buf)
        with self._lock:
            if nbytes > cap_bytes:
                # A single write bigger than the whole ring: only its tail
                # would survive anyway, so write just that tail. Without this,
                # a write over 2x capacity would wrap more than once and hit
                # a slice-length mismatch below — never happens with a real
                # callback block against a multi-second ring, but a write()
                # must not be able to raise regardless of its input size.
                skip_bytes = nbytes - cap_bytes
                view = view[skip_bytes:]
                pos = ((self._total_frames + skip_bytes // self._frame_size) * self._frame_size) % cap_bytes
                nbytes = cap_bytes
            else:
                pos = (self._total_frames * self._frame_size) % cap_bytes
            end = pos + nbytes
            if end <= cap_bytes:
                self._buf[pos:end] = view
            else:
                first = cap_bytes - pos
                self._buf[pos:cap_bytes] = view[:first]
                self._buf[0 : end - cap_bytes] = view[first:]
            self._total_frames += frames

    def mark(self) -> int:
        """A monotonically increasing frame position, usable with :meth:`read_since`."""
        with self._lock:
            return self._total_frames

    def read_since(self, start_frame: int, end_frame: int | None = None) -> bytes:
        """Return the frames in ``[start_frame, end_frame)``, oldest first.

        ``end_frame`` defaults to everything written so far. Passing it
        matters for the streaming chunker, which computes a chunk's right edge
        and then has to be able to *read* that edge: without the bound, every
        chunk silently ran on to the present moment, so consecutive chunks
        overlapped by more audio than the schedule intended and the stitcher's
        dedup window -- sized from the nominal overlap -- could not span the
        real one. The duplicated speech was then appended verbatim.

        If the writer has wrapped past ``start_frame`` (the caller held the
        hotkey longer than the ring's capacity), the oldest frames are
        silently dropped and only the most recent ``capacity_frames`` are
        returned.
        """
        cap_bytes = len(self._buf)
        with self._lock:
            end_frame = self._total_frames if end_frame is None else min(end_frame, self._total_frames)
            available = end_frame - start_frame
            if available <= 0:
                return b""
            if available > self._capacity_frames:
                start_frame = end_frame - self._capacity_frames
                available = self._capacity_frames
            start_pos = (start_frame * self._frame_size) % cap_bytes
            nbytes = available * self._frame_size
            end_pos = start_pos + nbytes
            if end_pos <= cap_bytes:
                return bytes(self._buf[start_pos:end_pos])
            first = cap_bytes - start_pos
            return bytes(self._buf[start_pos:cap_bytes]) + bytes(self._buf[0 : end_pos - cap_bytes])


__all__ = ["RingBuffer"]

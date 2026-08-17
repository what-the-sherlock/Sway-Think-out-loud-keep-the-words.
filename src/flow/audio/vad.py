"""Trims leading/trailing silence from a recorded utterance using WebRTC VAD.

A pure transformation over PCM bytes: given the same frames and the same
per-frame speech classification, the trimmed result is deterministic. That
makes it independently testable by injecting a fake VAD that flags specific
frames as voiced, without needing real speech audio or the native
``webrtcvad`` extension in the test itself (though the tests do exercise the
real one too).
"""

from __future__ import annotations

from typing import Protocol

import webrtcvad

#: webrtcvad only accepts these frame durations...
VALID_FRAME_MS = (10, 20, 30)
#: ...and these sample rates.
VALID_SAMPLE_RATES = (8000, 16000, 32000, 48000)

_BYTES_PER_SAMPLE = 2  # int16 mono, per CLAUDE.md


class VadProtocol(Protocol):
    def is_speech(self, frame: bytes, sample_rate: int) -> bool: ...


class VadTrimmer:
    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        mode: int = 2,
        padding_ms: int = 200,
        vad: VadProtocol | None = None,
    ) -> None:
        if sample_rate not in VALID_SAMPLE_RATES:
            raise ValueError(f"sample_rate must be one of {VALID_SAMPLE_RATES}")
        if frame_ms not in VALID_FRAME_MS:
            raise ValueError(f"frame_ms must be one of {VALID_FRAME_MS}")
        if not 0 <= mode <= 3:
            raise ValueError("mode must be between 0 and 3")
        if padding_ms < 0:
            raise ValueError("padding_ms must be >= 0")
        self._sample_rate = sample_rate
        self._frame_bytes = (sample_rate * frame_ms // 1000) * _BYTES_PER_SAMPLE
        self._padding_frames = padding_ms // frame_ms
        self._vad = vad if vad is not None else webrtcvad.Vad(mode)

    def trim(self, pcm: bytes) -> bytes:
        """Return ``pcm`` with leading/trailing silence removed.

        Returns ``b""`` if no frame in the clip is classified as speech, so
        callers can skip the encode/upload stages entirely for a false-start
        press (saves both latency and Groq free-tier quota).
        """
        frames = self._split(pcm)
        if not frames:
            return b""
        voiced = [self._vad.is_speech(f, self._sample_rate) for f in frames]
        if not any(voiced):
            return b""
        first_voiced = voiced.index(True)
        last_voiced = len(voiced) - 1 - voiced[::-1].index(True)
        start = max(0, first_voiced - self._padding_frames)
        end = min(len(frames) - 1, last_voiced + self._padding_frames)
        return b"".join(frames[start : end + 1])

    def _split(self, pcm: bytes) -> list[bytes]:
        usable = len(pcm) - (len(pcm) % self._frame_bytes)
        return [pcm[i : i + self._frame_bytes] for i in range(0, usable, self._frame_bytes)]


__all__ = ["VALID_FRAME_MS", "VALID_SAMPLE_RATES", "VadProtocol", "VadTrimmer"]

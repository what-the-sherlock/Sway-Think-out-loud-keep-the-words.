"""Tests for silence trimming.

Uses a fake VAD (deterministic, no real speech needed) for the trimming logic
itself, plus a handful of sanity checks against the real webrtcvad binding on
synthetic silence.
"""

from __future__ import annotations

import pytest

from sway.audio.vad import VadTrimmer


class FakeVad:
    """Flags exactly the given frame indices as speech."""

    def __init__(self, voiced_indices: set[int]) -> None:
        self._voiced = voiced_indices
        self.calls: list[bytes] = []

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        self.calls.append(frame)
        return len(self.calls) - 1 in self._voiced


def _frame(frame_bytes: int, marker: int) -> bytes:
    """A distinguishable frame: `marker` repeated to fill the frame."""
    return bytes([marker % 256]) * frame_bytes


def _clip(frame_bytes: int, count: int) -> bytes:
    return b"".join(_frame(frame_bytes, i) for i in range(count))


@pytest.fixture
def frame_bytes() -> int:
    # sample_rate=16000, frame_ms=20 -> 320 samples * 2 bytes = 640 bytes/frame
    return 640


def test_trims_leading_and_trailing_silence(frame_bytes: int):
    fake = FakeVad(voiced_indices={3, 4, 5})
    trimmer = VadTrimmer(sample_rate=16000, frame_ms=20, padding_ms=0, vad=fake)
    clip = _clip(frame_bytes, 10)
    result = trimmer.trim(clip)
    assert result == b"".join(_frame(frame_bytes, i) for i in (3, 4, 5))


def test_padding_extends_on_both_sides(frame_bytes: int):
    fake = FakeVad(voiced_indices={5})
    # padding_ms=40 with frame_ms=20 -> 2 frames of padding each side
    trimmer = VadTrimmer(sample_rate=16000, frame_ms=20, padding_ms=40, vad=fake)
    clip = _clip(frame_bytes, 10)
    result = trimmer.trim(clip)
    assert result == b"".join(_frame(frame_bytes, i) for i in (3, 4, 5, 6, 7))


def test_padding_is_clamped_to_clip_bounds(frame_bytes: int):
    fake = FakeVad(voiced_indices={0})
    trimmer = VadTrimmer(sample_rate=16000, frame_ms=20, padding_ms=100, vad=fake)
    clip = _clip(frame_bytes, 3)
    result = trimmer.trim(clip)
    assert result == clip  # padding would run off both ends; clamped to the whole clip


def test_no_speech_returns_empty(frame_bytes: int):
    fake = FakeVad(voiced_indices=set())
    trimmer = VadTrimmer(sample_rate=16000, frame_ms=20, vad=fake)
    assert trimmer.trim(_clip(frame_bytes, 10)) == b""


def test_all_speech_returns_the_whole_clip(frame_bytes: int):
    fake = FakeVad(voiced_indices=set(range(10)))
    trimmer = VadTrimmer(sample_rate=16000, frame_ms=20, padding_ms=0, vad=fake)
    clip = _clip(frame_bytes, 10)
    assert trimmer.trim(clip) == clip


def test_empty_input_returns_empty(frame_bytes: int):
    fake = FakeVad(voiced_indices={0})
    trimmer = VadTrimmer(sample_rate=16000, frame_ms=20, vad=fake)
    assert trimmer.trim(b"") == b""


def test_partial_trailing_frame_is_ignored(frame_bytes: int):
    fake = FakeVad(voiced_indices={0})
    trimmer = VadTrimmer(sample_rate=16000, frame_ms=20, vad=fake)
    clip = _clip(frame_bytes, 2) + b"\x00" * (frame_bytes // 2)  # half a frame extra
    trimmer.trim(clip)
    assert len(fake.calls) == 2  # the partial frame was never classified


def test_single_voiced_frame(frame_bytes: int):
    fake = FakeVad(voiced_indices={4})
    trimmer = VadTrimmer(sample_rate=16000, frame_ms=20, padding_ms=0, vad=fake)
    clip = _clip(frame_bytes, 10)
    assert trimmer.trim(clip) == _frame(frame_bytes, 4)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sample_rate": 44100},
        {"frame_ms": 25},
        {"mode": 4},
        {"mode": -1},
        {"padding_ms": -1},
    ],
)
def test_invalid_construction_is_rejected(kwargs: dict):
    with pytest.raises(ValueError):
        VadTrimmer(**kwargs)


# -- against the real webrtcvad binding (no fake) --------------------------


def test_real_vad_classifies_digital_silence_as_non_speech():
    trimmer = VadTrimmer(sample_rate=16000, frame_ms=20)
    silence = b"\x00" * (320 * 2 * 25)  # 25 frames of true silence
    assert trimmer.trim(silence) == b""

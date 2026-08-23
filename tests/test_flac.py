"""Tests for FLAC encoding, verified by round-tripping through soundfile."""

from __future__ import annotations

import io
import struct

import numpy as np
import pytest
import soundfile as sf

from sway.audio.flac import encode_flac


def _pcm(*values: int) -> bytes:
    return struct.pack(f"<{len(values)}h", *values)


def _sine_pcm(seconds: float, sample_rate: int, freq: float = 440.0) -> bytes:
    n = int(seconds * sample_rate)
    t = np.arange(n) / sample_rate
    samples = (np.sin(2 * np.pi * freq * t) * 12000).astype("<i2")
    return samples.tobytes()


def test_round_trip_is_lossless_and_exact():
    pcm = _sine_pcm(0.5, 16000)
    flac_bytes = encode_flac(pcm, sample_rate=16000)

    decoded, sample_rate = sf.read(io.BytesIO(flac_bytes), dtype="int16")
    assert sample_rate == 16000
    assert decoded.tobytes() == pcm


def test_output_is_smaller_than_raw_pcm_for_real_audio():
    pcm = _sine_pcm(2.0, 16000)
    flac_bytes = encode_flac(pcm, sample_rate=16000)
    assert len(flac_bytes) < len(pcm)


def test_output_starts_with_the_flac_magic_bytes():
    pcm = _sine_pcm(0.1, 16000)
    flac_bytes = encode_flac(pcm, sample_rate=16000)
    assert flac_bytes[:4] == b"fLaC"


def test_stereo_round_trip():
    left = _sine_pcm(0.2, 16000, freq=440.0)
    right = _sine_pcm(0.2, 16000, freq=880.0)
    left_arr = np.frombuffer(left, dtype="<i2")
    right_arr = np.frombuffer(right, dtype="<i2")
    interleaved = np.empty(len(left_arr) * 2, dtype="<i2")
    interleaved[0::2] = left_arr
    interleaved[1::2] = right_arr
    pcm = interleaved.tobytes()

    flac_bytes = encode_flac(pcm, sample_rate=16000, channels=2)
    decoded, sample_rate = sf.read(io.BytesIO(flac_bytes), dtype="int16")
    assert sample_rate == 16000
    assert decoded.shape == (len(left_arr), 2)
    assert decoded.tobytes() == pcm


def test_empty_audio_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        encode_flac(b"", sample_rate=16000)


def test_misaligned_pcm_length_is_rejected():
    with pytest.raises(ValueError, match="whole number"):
        encode_flac(_pcm(1, 2, 3) + b"\x00", sample_rate=16000)  # 7 bytes


def test_invalid_channels_is_rejected():
    with pytest.raises(ValueError):
        encode_flac(_pcm(1, 2), sample_rate=16000, channels=0)

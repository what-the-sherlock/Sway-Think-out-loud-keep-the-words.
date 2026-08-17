"""Encodes 16-bit PCM to FLAC in memory, for upload to Groq.

Pure function: PCM bytes in, FLAC bytes out, with no I/O beyond an in-memory
buffer. FLAC (not WAV) per CLAUDE.md's Phase 1 spec — it shrinks the upload
several-fold over raw PCM for a lossless, no-quality-cost win on the network
stage of the latency budget.
"""

from __future__ import annotations

import io

import numpy as np
import soundfile as sf

_BYTES_PER_SAMPLE = 2  # int16 mono, per CLAUDE.md


def encode_flac(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Encode raw little-endian int16 PCM as FLAC and return the file bytes."""
    if not pcm:
        raise ValueError("cannot encode empty audio")
    if channels < 1:
        raise ValueError("channels must be >= 1")
    frame_size = _BYTES_PER_SAMPLE * channels
    if len(pcm) % frame_size != 0:
        raise ValueError("pcm length is not a whole number of int16 frames")

    samples = np.frombuffer(pcm, dtype="<i2")
    if channels > 1:
        samples = samples.reshape(-1, channels)

    buffer = io.BytesIO()
    sf.write(buffer, samples, sample_rate, format="FLAC", subtype="PCM_16")
    return buffer.getvalue()


__all__ = ["encode_flac"]

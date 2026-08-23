"""Owns the single, persistent PortAudio input stream.

CLAUDE.md: the audio input stream opens ONCE at app startup and stays open
forever, writing into a ring buffer; never open the mic on hotkey press. The
callback here does the minimum possible — a bounds-checked memcpy into the
ring buffer — with no I/O and no logging on the steady-state path. Per
CLAUDE.md this module is not unit tested; :mod:`flow.audio.ring_buffer` (the
part that can be exercised without real hardware) is.
"""

from __future__ import annotations

import logging

import sounddevice as sd

from flow.audio.ring_buffer import RingBuffer
from flow.config import AudioConfig
from flow.errors import AudioError

logger = logging.getLogger(__name__)


def list_input_devices() -> list[str]:
    """Human-readable input device descriptions, for ``flow devices`` and config."""
    try:
        devices = sd.query_devices()
    except sd.PortAudioError as exc:
        raise AudioError(f"could not query audio devices: {exc}") from exc
    try:
        default_input = sd.default.device[0]
    except (TypeError, IndexError):
        default_input = -1
    return [
        f"[{i}] {d['name']} ({d['max_input_channels']} in)"
        + (" (default)" if i == default_input else "")
        for i, d in enumerate(devices)
        if d["max_input_channels"] > 0
    ]


class AudioCapture:
    """Opens one input stream at :meth:`start` and holds it until :meth:`stop`."""

    def __init__(self, ring: RingBuffer, config: AudioConfig) -> None:
        self._ring = ring
        self._config = config
        self._stream: sd.RawInputStream | None = None
        #: Count of PortAudio-reported overflows. Checked/logged outside the
        #: callback so the callback itself never does I/O.
        self.overflow_count = 0

    @property
    def is_running(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            stream = sd.RawInputStream(
                samplerate=self._config.sample_rate,
                channels=self._config.channels,
                dtype="int16",
                blocksize=self._config.blocksize_frames,
                device=self._config.device,
                callback=self._callback,
            )
            stream.start()
        except sd.PortAudioError as exc:
            raise AudioError(f"could not open audio input device: {exc}") from exc
        self._stream = stream
        logger.info(
            "audio input open: %dHz, %d channel(s), device=%r",
            self._config.sample_rate,
            self._config.channels,
            self._config.device,
        )

    def stop(self) -> None:
        if self._stream is None:
            return
        stream, self._stream = self._stream, None
        stream.stop()
        stream.close()
        if self.overflow_count:
            logger.warning("audio input overflowed %d time(s) this session", self.overflow_count)

    def _callback(self, indata, frames: int, time_info, status: sd.CallbackFlags) -> None:
        if status:
            self.overflow_count += 1
        self._ring.write(indata)


__all__ = ["AudioCapture", "list_input_devices"]

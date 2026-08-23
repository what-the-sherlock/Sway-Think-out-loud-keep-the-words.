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

import numpy as np
import sounddevice as sd

from sway.audio.ring_buffer import RingBuffer
from sway.config import AudioConfig
from sway.errors import AudioError

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


def input_devices() -> list[dict[str, object]]:
    """Structured input devices, for the Settings picker.

    Separate from :func:`list_input_devices`, which formats one line per
    device for the CLI. The UI needs the fields apart -- it renders the name,
    marks the default, and writes the name back into ``[audio].device`` -- and
    parsing them back out of a display string would be fragile.

    Never raises: a machine with no working audio backend returns an empty
    list, which the UI renders as an explicit "0 microphones found" rather
    than as an error page.
    """
    try:
        devices = sd.query_devices()
    except Exception:
        logger.warning("could not query audio devices", exc_info=True)
        return []

    try:
        default_input = sd.default.device[0]
    except (TypeError, IndexError):
        default_input = -1

    result: list[dict[str, object]] = []
    for index, device in enumerate(devices):
        if device["max_input_channels"] <= 0:
            continue
        result.append(
            {
                "index": index,
                "name": device["name"],
                "channels": int(device["max_input_channels"]),
                "default": index == default_input,
            }
        )
    return result


class AudioCapture:
    """Opens one input stream at :meth:`start` and holds it until :meth:`stop`."""

    def __init__(self, ring: RingBuffer, config: AudioConfig) -> None:
        self._ring = ring
        self._config = config
        self._stream: sd.RawInputStream | None = None
        #: Latest 0-3 mic level bucket. Written by the callback thread, read
        #: by the UI; a plain int assignment is atomic under the GIL.
        self.level = 0
        self._measure_level = False
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
        if self._measure_level:
            self._update_level(indata)

    # -- level metering ----------------------------------------------------

    def set_level_metering(self, enabled: bool) -> None:
        """Turn peak metering on or off.

        Gated rather than always-on because CLAUDE.md's rule is that the audio
        callback does nothing but write to the ring buffer. Metering is only
        switched on while something is actually displaying it -- a dictation
        in progress, or the onboarding microphone check -- so the steady state
        is one boolean test per block.
        """
        self._measure_level = enabled
        if not enabled:
            self.level = 0

    def _update_level(self, indata) -> None:
        """Quantise this block's peak into a 0-3 bucket.

        Two ``ndarray`` reductions over a zero-copy view: no allocation, no
        I/O, no locking. A bucket rather than a float because the overlay only
        repaints when the bucket changes, which is what keeps a live meter
        from turning the pill into a 30fps render loop.
        """
        try:
            samples = np.frombuffer(indata, dtype=np.int16)
            if samples.size == 0:
                return
            peak = max(int(samples.max()), -int(samples.min()))
        except Exception:
            # Metering is cosmetic; it must never be able to break capture.
            self._measure_level = False
            return

        if peak < _LEVEL_SILENT:
            self.level = 0
        elif peak < _LEVEL_SPEECH:
            self.level = 1
        elif peak < _LEVEL_LOUD:
            self.level = 2
        else:
            self.level = 3


#: Peak thresholds over int16 full scale (32767).
#: ~1.5% of full scale -- below this is room tone, not speech.
_LEVEL_SILENT = 500
#: ~9% -- comfortable speech at a normal distance.
_LEVEL_SPEECH = 3000
#: ~46% -- loud, and approaching the range where clipping starts to matter.
_LEVEL_LOUD = 15000


__all__ = ["AudioCapture", "input_devices", "list_input_devices"]

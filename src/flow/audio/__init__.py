"""Audio capture, silence trimming, and FLAC encoding.

Deliberately minimal re-exports: importing :mod:`flow.audio` must not force a
transitive import of ``sounddevice`` (via :mod:`flow.audio.capture`) just to
reach the pure, hardware-free pieces in :mod:`flow.audio.ring_buffer` and
:mod:`flow.audio.vad`. Import submodules directly.
"""

from __future__ import annotations

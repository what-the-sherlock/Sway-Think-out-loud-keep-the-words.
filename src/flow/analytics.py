"""Pure functions turning raw dictation records into the product-level numbers
the dashboard reports: word counts, speaking rate, and estimated time saved.

Everything here is deliberately free of I/O and of any store or config object
so it can be unit-tested directly, per CLAUDE.md's rule about testing the
transcript-processing pure functions rather than the audio path.

The one judgement call worth naming: :func:`words_per_minute` divides by
*speech* duration (post-VAD) rather than by how long the hotkey was held.
Hold time includes the silence before the user starts talking and the pause
before they release, which on a short dictation is most of the clip. Dividing
by hold time would report a speaking rate roughly half the real one, and the
whole time-saved estimate is built on top of this number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: A "word" for counting purposes: a run of letters/digits that may contain
#: internal apostrophes or hyphens, so "don't" and "well-known" each count
#: once rather than twice. Whisper output is already normalised prose, so a
#: regex is enough; no tokeniser dependency is justified for this.
_WORD_RE = re.compile(r"[^\W_]+(?:['\u2019-][^\W_]+)*", re.UNICODE)

#: Below this much detected speech, a WPM figure is noise rather than a
#: measurement -- a single word clipped tight can imply 600 WPM. Sessions
#: under it report no WPM at all instead of a fabricated one.
MIN_SPEECH_MS_FOR_WPM = 1_000.0

#: Sanity ceiling. Human speech tops out near 300 WPM even for auctioneers;
#: anything above this means the speech-duration measurement was wrong (a VAD
#: mistrim, say), so we decline to report rather than publish a fake record.
MAX_PLAUSIBLE_WPM = 400.0

#: Default assumed typing speed for the time-saved estimate. 45 WPM is a
#: commonly cited average for a proficient non-touch-typist. It is exposed as
#: a parameter (and, in config, as a user setting) precisely because it is an
#: assumption -- the dashboard must label anything derived from it an estimate.
DEFAULT_TYPING_WPM = 45.0


def count_words(text: str) -> int:
    """Number of words in ``text``. Empty/whitespace-only text counts zero."""
    return len(_WORD_RE.findall(text))


def count_characters(text: str) -> int:
    return len(text)


def pcm_duration_ms(byte_count: int, sample_rate: int, *, frame_size: int = 2) -> float:
    """Wall-clock duration of ``byte_count`` bytes of raw PCM.

    ``frame_size`` is bytes per frame (sample width x channels): 2 for the
    16-bit mono capture CLAUDE.md fixes the pipeline to.
    """
    if sample_rate <= 0 or frame_size <= 0 or byte_count <= 0:
        return 0.0
    return (byte_count / frame_size) / sample_rate * 1000.0


def words_per_minute(word_count: int, speech_ms: float) -> float | None:
    """Speaking rate, or ``None`` when the sample is too small or implausible
    to report honestly. Callers must render ``None`` as "not available"
    rather than as zero -- a zero here would drag every average down.
    """
    if word_count <= 0 or speech_ms < MIN_SPEECH_MS_FOR_WPM:
        return None
    wpm = word_count / (speech_ms / 60_000.0)
    if wpm > MAX_PLAUSIBLE_WPM:
        return None
    return wpm


def typing_seconds(word_count: int, typing_wpm: float = DEFAULT_TYPING_WPM) -> float:
    """How long ``word_count`` words would have taken to type, in seconds."""
    if word_count <= 0 or typing_wpm <= 0:
        return 0.0
    return word_count / typing_wpm * 60.0


def time_saved_seconds(
    word_count: int,
    speech_ms: float,
    *,
    typing_wpm: float = DEFAULT_TYPING_WPM,
) -> float:
    """Estimated seconds of typing avoided: what typing these words would have
    cost, minus the time actually spent speaking them.

    Clamped at zero. A dictation slower than typing (a long pause mid-sentence)
    saved no time, but it did not *cost* time either -- the user was thinking,
    not typing -- so a negative contribution would misrepresent the total.
    """
    saved = typing_seconds(word_count, typing_wpm) - speech_ms / 1000.0
    return max(0.0, saved)


@dataclass(frozen=True, slots=True)
class SessionMetrics:
    """The derived numbers for one dictation, computed once at record time and
    stored, rather than recomputed on every dashboard read: they depend on
    audio the app no longer keeps, so they cannot be recovered later.
    """

    word_count: int
    character_count: int
    #: Post-VAD speech duration. The basis for :attr:`wpm`.
    speech_ms: float
    #: Full captured clip including silence, for reference in the detail view.
    audio_ms: float
    #: ``None`` when the clip was too short to measure honestly.
    wpm: float | None
    time_saved_s: float


def session_metrics(
    text: str,
    *,
    speech_ms: float,
    audio_ms: float,
    typing_wpm: float = DEFAULT_TYPING_WPM,
) -> SessionMetrics:
    """Bundle every derived per-session number in one pass."""
    words = count_words(text)
    return SessionMetrics(
        word_count=words,
        character_count=count_characters(text),
        speech_ms=speech_ms,
        audio_ms=audio_ms,
        wpm=words_per_minute(words, speech_ms),
        time_saved_s=time_saved_seconds(words, speech_ms, typing_wpm=typing_wpm),
    )


def format_duration(seconds: float) -> str:
    """Human duration for the UI: "1h 42m", "8m", "12s"."""
    total = int(max(0.0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


__all__ = [
    "DEFAULT_TYPING_WPM",
    "MAX_PLAUSIBLE_WPM",
    "MIN_SPEECH_MS_FOR_WPM",
    "SessionMetrics",
    "count_characters",
    "count_words",
    "format_duration",
    "pcm_duration_ms",
    "session_metrics",
    "time_saved_seconds",
    "typing_seconds",
    "words_per_minute",
]

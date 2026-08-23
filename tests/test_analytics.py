"""Tests for the derived product metrics. Pure functions only, per CLAUDE.md."""

from __future__ import annotations

import pytest

from sway.analytics import (
    MAX_PLAUSIBLE_WPM,
    count_words,
    format_duration,
    pcm_duration_ms,
    session_metrics,
    time_saved_seconds,
    typing_seconds,
    words_per_minute,
)


class TestCountWords:
    def test_empty_and_whitespace_count_zero(self) -> None:
        assert count_words("") == 0
        assert count_words("   \n\t ") == 0

    def test_plain_sentence(self) -> None:
        assert count_words("the quick brown fox") == 4

    def test_contractions_and_hyphens_count_once(self) -> None:
        # "don't" is one word, not two; likewise "well-known". Splitting them
        # would inflate WPM by ~10% on conversational speech.
        assert count_words("don't") == 1
        assert count_words("a well-known problem") == 3

    def test_punctuation_is_not_a_word(self) -> None:
        assert count_words("Hello, world! -- yes.") == 3

    def test_digits_count(self) -> None:
        assert count_words("ship 3 builds") == 3


class TestPcmDurationMs:
    def test_one_second_of_16k_mono_16bit(self) -> None:
        assert pcm_duration_ms(32_000, 16_000, frame_size=2) == pytest.approx(1000.0)

    def test_degenerate_inputs_are_zero_not_an_error(self) -> None:
        assert pcm_duration_ms(0, 16_000) == 0.0
        assert pcm_duration_ms(32_000, 0) == 0.0


class TestWordsPerMinute:
    def test_straightforward_rate(self) -> None:
        # 30 words in 10 seconds is 180 WPM.
        assert words_per_minute(30, 10_000) == pytest.approx(180.0)

    def test_too_short_a_clip_reports_nothing(self) -> None:
        # A single word clipped tight would otherwise imply a huge rate.
        assert words_per_minute(1, 200) is None

    def test_no_words_reports_nothing(self) -> None:
        assert words_per_minute(0, 5_000) is None

    def test_implausible_rate_is_refused_rather_than_published(self) -> None:
        assert words_per_minute(10_000, 2_000) is None

    def test_boundary_just_under_the_ceiling_is_reported(self) -> None:
        wpm = words_per_minute(6, 1_000)  # 360 WPM
        assert wpm is not None and wpm < MAX_PLAUSIBLE_WPM


class TestTimeSaved:
    def test_typing_seconds_matches_the_stated_baseline(self) -> None:
        assert typing_seconds(45, 45.0) == pytest.approx(60.0)

    def test_saving_is_typing_time_minus_speaking_time(self) -> None:
        # 45 words would take 60s to type at 45 WPM; spoken in 15s.
        assert time_saved_seconds(45, 15_000, typing_wpm=45.0) == pytest.approx(45.0)

    def test_slower_than_typing_never_reports_negative_savings(self) -> None:
        assert time_saved_seconds(2, 60_000, typing_wpm=45.0) == 0.0

    def test_no_words_saves_nothing(self) -> None:
        assert time_saved_seconds(0, 5_000) == 0.0


class TestSessionMetrics:
    def test_bundles_every_derived_number(self) -> None:
        metrics = session_metrics(
            "one two three four five six seven eight nine ten",
            speech_ms=5_000,
            audio_ms=6_500,
            typing_wpm=45.0,
        )
        assert metrics.word_count == 10
        assert metrics.character_count == len("one two three four five six seven eight nine ten")
        assert metrics.wpm == pytest.approx(120.0)
        assert metrics.audio_ms == 6_500
        assert metrics.time_saved_s == pytest.approx(10 / 45 * 60 - 5.0)

    def test_unmeasurable_clip_yields_no_wpm_rather_than_zero(self) -> None:
        # Zero would silently drag every reported average down; None lets the
        # UI say "not available", which is the truth.
        metrics = session_metrics("hello", speech_ms=0.0, audio_ms=0.0)
        assert metrics.wpm is None
        assert metrics.word_count == 1


class TestFormatDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "0s"), (12, "12s"), (90, "1m"), (600, "10m"), (6120, "1h 42m")],
    )
    def test_formats(self, seconds: float, expected: str) -> None:
        assert format_duration(seconds) == expected

    def test_negative_is_clamped(self) -> None:
        assert format_duration(-5) == "0s"

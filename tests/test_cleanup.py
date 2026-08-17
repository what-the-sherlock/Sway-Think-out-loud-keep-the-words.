"""Tests for the cleanup skip heuristic and skip-rate stats.

Pure functions only, per CLAUDE.md: no network involved.
"""

from __future__ import annotations

import logging

from flow.cleanup import (
    CLEANUP_SYSTEM_PROMPT,
    FORMAL_CLEANUP_SYSTEM_PROMPT,
    CleanupStats,
    has_filler_markers,
    is_cleanup_output_safe,
    should_skip_cleanup,
    system_prompt_for_style,
    word_count,
)
from flow.profiles import CleanupStyle


# -- word_count --------------------------------------------------------------


def test_word_count_counts_whitespace_separated_tokens():
    assert word_count("hello world") == 2
    assert word_count("") == 0
    assert word_count("   ") == 0
    assert word_count("one") == 1


# -- has_filler_markers -------------------------------------------------------


def test_detects_classic_filler_words():
    assert has_filler_markers("um so I think we should go")
    assert has_filler_markers("uh yeah that works")
    assert has_filler_markers("it was, you know, fine")
    assert has_filler_markers("I mean it could work")


def test_ignores_common_words_that_are_not_reliable_filler_signals():
    # "like", "actually", "so" are frequently used sincerely; flagging them
    # would make the skip heuristic almost never fire.
    assert not has_filler_markers("I like pizza")
    assert not has_filler_markers("actually we should leave now")
    assert not has_filler_markers("so what time is it")


def test_detects_immediate_word_repetition_as_self_correction():
    assert has_filler_markers("I I want to go home")
    assert has_filler_markers("the the meeting is at noon")


def test_detects_double_dash_as_self_correction():
    assert has_filler_markers("send it to Bob -- actually send it to Alice")


def test_clean_short_sentence_has_no_filler_markers():
    assert not has_filler_markers("Meet me at noon tomorrow")


# -- has_filler_markers, multilingual -----------------------------------------


def test_undetected_language_falls_back_to_english_fillers():
    assert has_filler_markers("um yeah that works", language=None)


def test_detects_spanish_filler_words_when_language_is_spanish():
    assert has_filler_markers("eh bueno vamos a ver", language="spanish")


def test_english_filler_words_are_not_matched_against_spanish_transcripts():
    # "este" is a Spanish filler; it must not be checked against the
    # English-only pattern when language="spanish" is not given.
    assert not has_filler_markers("this is a normal sentence", language="spanish")


def test_detects_french_filler_words():
    assert has_filler_markers("euh je pense que oui", language="french")


def test_unknown_language_skips_the_word_list_but_keeps_structural_checks():
    # A language we have no filler list for (e.g. Japanese): the word-list
    # check is skipped entirely, but repeated-word self-correction still
    # counts as disfluency regardless of language.
    assert not has_filler_markers("clean sentence with no repeats", language="japanese")
    assert has_filler_markers("home home is where I want to go", language="japanese")


def test_should_skip_cleanup_is_language_aware():
    # Same short transcript: no Spanish filler present, so it's clean and
    # short enough to skip; with the (wrong) English pattern it would also
    # have skipped, so this alone doesn't prove much -- paired with the
    # filler-present case below it does.
    assert should_skip_cleanup("vamos a la tienda ahora", language="spanish") is True
    assert should_skip_cleanup("eh vamos a la tienda", language="spanish") is False


# -- should_skip_cleanup ------------------------------------------------------


def test_skips_short_clean_transcripts():
    assert should_skip_cleanup("Meet me at noon tomorrow") is True


def test_does_not_skip_short_transcripts_with_filler():
    assert should_skip_cleanup("um meet me at noon") is False


def test_does_not_skip_long_transcripts_even_when_clean():
    text = "Please remember to send the quarterly report to finance before Friday afternoon"
    assert word_count(text) >= 8
    assert should_skip_cleanup(text) is False


def test_skip_threshold_is_configurable():
    text = "one two three four five"
    assert should_skip_cleanup(text, min_words=10) is True
    assert should_skip_cleanup(text, min_words=3) is False


def test_word_count_boundary_is_exclusive_of_min_words():
    # Exactly min_words should NOT be skipped ("under min_words", not "at or under").
    text = "one two three four five six seven eight"
    assert word_count(text) == 8
    assert should_skip_cleanup(text, min_words=8) is False


# -- CleanupStats --------------------------------------------------------------


def test_stats_tracks_processed_and_skipped_counts():
    stats = CleanupStats()
    stats.record(skipped=True)
    stats.record(skipped=False)
    stats.record(skipped=True)
    assert stats.processed == 3
    assert stats.skipped == 2
    assert stats.skip_rate == 2 / 3


def test_stats_skip_rate_is_zero_before_any_record():
    assert CleanupStats().skip_rate == 0.0


def test_stats_rejects_non_positive_log_every():
    import pytest

    with pytest.raises(ValueError):
        CleanupStats(log_every=0)


def test_stats_logs_summary_at_info_every_log_every_records(caplog: "logging.LogCaptureFixture"):
    stats = CleanupStats(log_every=2)
    with caplog.at_level(logging.INFO, logger="flow.cleanup"):
        stats.record(skipped=True)
        assert not any(r.levelno == logging.INFO for r in caplog.records)
        stats.record(skipped=False)
        assert any(r.levelno == logging.INFO and "skip rate" in r.message for r in caplog.records)


# -- system_prompt_for_style (Phase 3 profiles) --------------------------------


def test_casual_style_uses_the_default_prompt():
    assert system_prompt_for_style(CleanupStyle.CASUAL) == CLEANUP_SYSTEM_PROMPT


def test_formal_style_uses_the_formal_prompt():
    assert system_prompt_for_style(CleanupStyle.FORMAL) == FORMAL_CLEANUP_SYSTEM_PROMPT


def test_formal_and_casual_prompts_both_forbid_answering_the_content():
    for prompt in (CLEANUP_SYSTEM_PROMPT, FORMAL_CLEANUP_SYSTEM_PROMPT):
        assert "never answer" in prompt.lower()


def test_verbatim_style_falls_back_to_the_default_prompt():
    # VERBATIM never reaches the LLM (callers skip the call entirely), but the
    # lookup itself should not raise for it.
    assert system_prompt_for_style(CleanupStyle.VERBATIM) == CLEANUP_SYSTEM_PROMPT


# -- is_cleanup_output_safe ----------------------------------------------------


def test_ordinary_tidy_up_is_safe():
    original = "um so I think we should uh go to the the store today"
    cleaned = "I think we should go to the store today."
    assert is_cleanup_output_safe(original, cleaned)


def test_formal_expansion_within_reason_is_safe():
    original = "cant make it dont wait for me"
    cleaned = "I cannot make it. Do not wait for me."
    assert is_cleanup_output_safe(original, cleaned)


def test_empty_cleanup_output_is_never_safe():
    assert not is_cleanup_output_safe("meet me at noon", "")
    assert not is_cleanup_output_safe("meet me at noon", "   ")


def test_answering_instead_of_rewriting_is_unsafe():
    original = "what time is it"
    cleaned = "I'm sorry, I don't have access to real-time information."
    assert not is_cleanup_output_safe(original, cleaned)


def test_a_long_transcript_collapsed_to_almost_nothing_is_unsafe():
    original = " ".join(["word"] * 40)
    cleaned = "word."
    assert not is_cleanup_output_safe(original, cleaned)


def test_a_long_transcript_massively_expanded_is_unsafe():
    original = " ".join(["word"] * 20)
    cleaned = " ".join(["word"] * 60)
    assert not is_cleanup_output_safe(original, cleaned)


def test_short_transcript_allows_a_little_growth():
    # "yes" -> "Yes." is fine; within the absolute word budget for short input.
    assert is_cleanup_output_safe("yeah ok", "Yes, okay.")


def test_short_transcript_rejects_a_full_unrelated_reply():
    assert not is_cleanup_output_safe("yes", "Yes, that sounds like a great plan to me, let's do it")

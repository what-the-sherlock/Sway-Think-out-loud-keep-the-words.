"""Tests for the cleanup safety layer: output normalization, the guardrails
that decide whether a rewrite may be pasted at all, and the disfluency
signals that decide whether to spend a cleanup call in the first place.

Pure functions only, per CLAUDE.md: no network involved. Several of these
encode failures observed end-to-end against the live Groq API rather than
hypothetical ones -- see the comments on the individual tests.
"""

from __future__ import annotations

import pytest

from sway.cleanup import (
    CLEANUP_SYSTEM_PROMPT,
    has_filler_markers,
    is_cleanup_output_safe,
    normalize_cleanup_output,
    should_skip_cleanup,
    system_prompt_for_style,
    unsafe_reason,
)
from sway.profiles import CleanupStyle

# Hindi fixtures, spelled out so the file stays readable in any editor.
HI_TOMORROW_TEN = "कल सुबह दस बजे ठीक रहेगा"
HI_TOMORROW_TEN_CLEAN = "कल सुबह दस बजे ठीक रहेगा।"


# -- self-correction detection ----------------------------------------------
# The failure this catches was observed end-to-end: "Let's meet Friday, sorry,
# Thursday at 4." is seven words with no "um", so the word-count heuristic
# skipped cleanup and shipped the retracted day to the user's cursor.


@pytest.mark.parametrize(
    "text",
    [
        "Let's meet Friday, sorry, Thursday at 4.",
        "Send it to Anna - no wait, to Priya.",
        "The total is 40, I meant 45.",
        "Book the 9am - actually no, the 10am.",
        "Ship it Tuesday. Scratch that, Wednesday.",
    ],
)
def test_self_corrections_are_detected_as_disfluency(text: str):
    assert has_filler_markers(text)
    assert not should_skip_cleanup(text)


@pytest.mark.parametrize(
    "text",
    [
        "I'm sorry about the delay.",  # a sincere apology, not a correction
        "I actually finished it yesterday.",  # "actually" as emphasis
        "Rather than that, use the other one.",
    ],
)
def test_sincere_phrasing_is_not_a_self_correction(text: str):
    assert not has_filler_markers(text)


# -- output normalization ----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Here is the cleaned-up text: Let's meet at three.", "Let's meet at three."),
        ("Sure, here's the cleaned up version: Ship it today.", "Ship it today."),
        ("Cleaned-up text: Ship it today.", "Ship it today."),
        ('"Ship it today."', "Ship it today."),
        ("“Ship it today.”", "Ship it today."),
        ("```\nShip it today.\n```", "Ship it today."),
        ("```text\nShip it today.\n```", "Ship it today."),
        ("Ship it today.", "Ship it today."),
    ],
)
def test_normalize_strips_model_wrappers(raw: str, expected: str):
    assert normalize_cleanup_output(raw, "ship it today") == expected


def test_normalize_keeps_quotes_the_speaker_actually_dictated():
    original = '"the whole thing is quoted"'
    cleaned = '"The whole thing is quoted."'
    assert normalize_cleanup_output(cleaned, original) == cleaned


# -- guardrails: data the cleanup pass must never alter -----------------------


@pytest.mark.parametrize(
    "original,cleaned",
    [
        # a number silently changed
        ("send the 450 pound invoice today please", "Send the 460 pound invoice today, please."),
        # an email address "tidied"
        (
            "email rahul.sharma@acme.com about the delay tomorrow",
            "Email rahul@acme.com about the delay tomorrow.",
        ),
        # a URL truncated
        (
            "the docs are at https://example.com/a/b/c go and read them",
            "The docs are at https://example.com and you should read them.",
        ),
        # a file path paraphrased away
        (
            "open the config at /etc/nginx/nginx.conf and restart it",
            "Open the config at the nginx directory and restart it.",
        ),
    ],
)
def test_altered_user_data_is_rejected(original: str, cleaned: str):
    assert not is_cleanup_output_safe(original, cleaned)
    assert unsafe_reason(original, cleaned).startswith("lost_entity")


def test_preserved_user_data_passes():
    original = "um email the invoice to rahul.sharma@acme.com its 450 pounds due on the 12th"
    cleaned = "Email the invoice to rahul.sharma@acme.com. It's 450 pounds, due on the 12th."
    assert is_cleanup_output_safe(original, cleaned)


def test_currency_symbol_added_around_a_preserved_number_is_fine():
    original = "the invoice is 450 pounds and it is due next week sometime"
    assert is_cleanup_output_safe(original, "The invoice is £450 and it is due next week.")


# -- guardrails: translation -------------------------------------------------


def test_translating_a_transcript_away_is_rejected():
    cleaned = "Ten o'clock tomorrow morning works."
    assert not is_cleanup_output_safe(HI_TOMORROW_TEN, cleaned)
    assert unsafe_reason(HI_TOMORROW_TEN, cleaned) == "language_changed"


def test_cleaning_within_the_same_script_is_allowed():
    original = "कल कल सुबह दस बजे ठीक रहेगा"
    assert is_cleanup_output_safe(original, HI_TOMORROW_TEN_CLEAN)


def test_mixed_language_speech_keeps_both_scripts():
    original = f"can you send this to Rahul {HI_TOMORROW_TEN}"
    good = f"Can you send this to Rahul? {HI_TOMORROW_TEN_CLEAN}"
    dropped_hindi = "Can you send this to Rahul tomorrow morning at ten?"
    assert is_cleanup_output_safe(original, good)
    assert not is_cleanup_output_safe(original, dropped_hindi)


# -- guardrails: markdown and assistant voice --------------------------------


@pytest.mark.parametrize(
    "cleaned",
    [
        "- Buy milk\n- Buy eggs\n- Buy bread",
        "# Shopping list\n\nBuy milk, eggs and bread today",
        "Buy **milk**, eggs and bread at the store today",
        "1. Buy milk\n2. Buy eggs\n3. Buy bread",
        "```\nbuy milk eggs and bread\n```",
    ],
)
def test_invented_markdown_is_rejected(cleaned: str):
    original = "buy milk eggs and bread at the store today please"
    assert not is_cleanup_output_safe(original, cleaned)
    assert unsafe_reason(original, cleaned) == "markdown"


def test_markdown_the_speaker_dictated_is_allowed():
    original = "- buy milk\n- buy eggs\n- buy bread and some cheese"
    cleaned = "- Buy milk\n- Buy eggs\n- Buy bread and some cheese."
    assert is_cleanup_output_safe(original, cleaned)


@pytest.mark.parametrize(
    "cleaned",
    [
        "As an AI, I can't help with scheduling meetings for you today.",
        "The transcript says the user wants to meet on Thursday afternoon.",
        "Sure, here is the text you asked me to clean up for you today.",
    ],
)
def test_assistant_voice_is_rejected(cleaned: str):
    original = "lets meet on thursday afternoon if that works for everyone"
    assert not is_cleanup_output_safe(original, cleaned)
    assert unsafe_reason(original, cleaned) == "assistant_voice"


def test_ordinary_first_person_speech_is_not_assistant_voice():
    # The regression that motivated narrowing the pattern: a legitimate
    # formal expansion of "cant make it" must survive.
    assert is_cleanup_output_safe("cant make it dont wait for me", "I cannot make it. Do not wait.")


def test_answering_a_dictated_question_is_rejected():
    original = "what is the capital of france anyway do you know"
    cleaned = "The capital of France is Paris, one of the largest cities."
    assert not is_cleanup_output_safe(original, cleaned)
    assert unsafe_reason(original, cleaned) == "answered_question"


def test_a_question_that_stays_a_question_is_fine():
    original = "um so what is the plan for thursday do you know yet"
    assert is_cleanup_output_safe(original, "So what is the plan for Thursday, do you know yet?")


def test_removing_a_spurious_question_mark_is_not_treated_as_answering():
    # Observed live: Whisper punctuated this long statement as a question,
    # and the correct rewrite dropped the "?". Rejecting that would have
    # thrown away a good cleanup over Whisper's own mistake. The rewrite is
    # built almost entirely from the speaker's own words, so it cannot be an
    # answer to them.
    original = (
        "Okay so, um, I was thinking about the roadmap for next quarter, and I think we "
        "should, uh, we should really focus on the onboarding flow first, because, you "
        "know, the drop off there is like 40%, which is, I mean, that's clearly the "
        "biggest problem we have right now, and everything else can honestly wait until "
        "we fix it?"
    )
    cleaned = (
        "Okay, I was thinking about the roadmap for next quarter, and I think we should "
        "really focus on the onboarding flow first, because the drop off there is like "
        "40%, which is clearly the biggest problem we have right now, and everything "
        "else can honestly wait until we fix it."
    )
    assert is_cleanup_output_safe(original, cleaned)
    assert unsafe_reason(original, cleaned) == ""


def test_unsafe_reason_is_empty_for_a_good_rewrite():
    assert unsafe_reason("um we should go to the store", "We should go to the store.") == ""


# -- language-aware system prompt --------------------------------------------


def test_system_prompt_pins_the_detected_language():
    prompt = system_prompt_for_style(CleanupStyle.CASUAL, language="hindi")
    assert "reply in hindi" in prompt.lower()


def test_system_prompt_without_a_language_stays_agnostic():
    prompt = system_prompt_for_style(CleanupStyle.CASUAL, language=None)
    assert prompt == CLEANUP_SYSTEM_PROMPT
    assert "primary language is" not in prompt


def test_every_style_prompt_forbids_translating_and_markdown():
    for style in (CleanupStyle.CASUAL, CleanupStyle.FORMAL):
        prompt = system_prompt_for_style(style)
        assert "Never translate" in prompt
        assert "no markdown" in prompt


# ---------------------------------------------------------------------------
# The correction exemption
#
# The entity guard treats every number in a transcript as protected. That is
# right for a tidy-up and wrong for a retraction: "a meeting at 7am, no no,
# it's at 8am" *must* lose the 7 to be correct, and without an exemption the
# right answer is indistinguishable from the model dropping a figure. These
# tests pin both halves -- that the exemption works, and that it stays narrow.
# ---------------------------------------------------------------------------


def test_a_retracted_number_may_disappear():
    raw = "I have a meeting at 7am, no no, it's at 8am."
    cleaned = "I have a meeting at 8am."
    assert not is_cleanup_output_safe(raw, cleaned)
    assert is_cleanup_output_safe(
        raw, cleaned, retracted="7am, no no", correction_detected=True
    )


def test_a_retracted_time_may_disappear():
    raw = "Call me at 10:30 - make that 11."
    cleaned = "Call me at 11."
    assert unsafe_reason(raw, cleaned) == "lost_entity:10:30"
    assert is_cleanup_output_safe(
        raw, cleaned, retracted="10:30 - make that", correction_detected=True
    )


def test_the_exemption_does_not_cover_untouched_entities():
    """A correction licenses losing what the speaker took back, and nothing
    else. The room number was never retracted and must survive."""
    raw = "Meeting at 7am, no no, at 8am, in room 412."
    assert is_cleanup_output_safe(
        raw, "Meeting at 8am in room 412.", retracted="7am, no no", correction_detected=True
    )
    assert not is_cleanup_output_safe(
        raw, "Meeting at 8am.", retracted="7am, no no", correction_detected=True
    )


def test_the_exemption_is_off_by_default():
    """Every existing caller passes neither argument, so the defaults must
    reproduce the old behaviour exactly."""
    raw = "The invoice is 4471 and the total is 320."
    cleaned = "The invoice is 4471."
    assert not is_cleanup_output_safe(raw, cleaned)
    assert unsafe_reason(raw, cleaned) == "lost_entity:320"


def test_a_correction_lowers_the_shrink_floor():
    """A long, stumbling retraction correctly collapses to a short sentence.
    Twenty words becoming five is a ratio the ordinary floor rejects outright,
    and rejecting it means pasting the whole stumble at the user's cursor."""
    raw = (
        "The budget for this whole project is five thousand pounds, no wait, "
        "sorry, scratch that, make it fifty thousand pounds"
    )
    cleaned = "The budget is fifty thousand."
    assert not is_cleanup_output_safe(raw, cleaned)
    assert unsafe_reason(raw, cleaned) == "shrank"
    assert is_cleanup_output_safe(raw, cleaned, correction_detected=True)


def test_the_lowered_floor_still_rejects_a_summary():
    """Relaxing the floor must not open the door to the model summarizing."""
    raw = (
        "So the plan is that we ship the beta on Tuesday, gather feedback for a "
        "fortnight, and then decide whether the general release slips to the "
        "following month or stays where it is on the roadmap."
    )
    assert not is_cleanup_output_safe(raw, "We ship Tuesday.", correction_detected=True)


def test_unsafe_reason_agrees_with_the_check_under_a_correction():
    """A rejection with no stated cause would be unloggable, so the two
    functions have to stay in step under every argument combination."""
    cases = [
        ("Meeting at 7am, no no, at 8am, in room 412.", "Meeting at 8am.", "7am, no no"),
        ("I have a meeting at 7am, no no, it's at 8am.", "I have a meeting at 8am.", "7am, no no"),
        ("Send it to a@b.com, sorry, c@d.com.", "Send it to c@d.com.", "a@b.com, sorry"),
    ]
    for raw, cleaned, retracted in cases:
        safe = is_cleanup_output_safe(
            raw, cleaned, retracted=retracted, correction_detected=True
        )
        reason = unsafe_reason(raw, cleaned, retracted=retracted, correction_detected=True)
        assert safe == (reason == "")


def test_the_correction_directive_is_only_added_when_asked():
    plain = system_prompt_for_style(CleanupStyle.CASUAL)
    with_correction = system_prompt_for_style(CleanupStyle.CASUAL, correction=True)
    assert "corrected themselves somewhere" not in plain
    assert "corrected themselves somewhere" in with_correction
    # The stable half must stay byte-identical and stay first, or Groq's
    # prompt caching has nothing to reuse across requests.
    assert with_correction.startswith(plain)


def test_the_correction_directive_teaches_the_negative_cases():
    """The examples are the specification. Without the two pass-through cases
    the model over-applies the instruction and "I don't want to go, actually"
    loses its negation."""
    prompt = system_prompt_for_style(CleanupStyle.CASUAL, correction=True)
    assert prompt.count("I don't want to go, actually.") == 2
    assert prompt.count("We shipped Monday and, actually, Tuesday as well.") == 2

"""Trigger matching.

The negative cases carry the most weight here. D8 names "silently eating
words" as the failure mode that matters: a trigger firing inside ordinary
speech deletes what the user said, and they may not notice until later. So
every "must not fire" case below is as important as the ones that must.
"""

from __future__ import annotations

from datetime import date

import pytest

from sway.settings import Trigger
from sway.triggers import apply_triggers, find_conflicts, resolve_payload

TODAY = date(2026, 8, 22)


def macro(id_: str, phrase: str, payload: str, *, enabled: bool = True) -> Trigger:
    return Trigger(id=id_, phrase=phrase, payload=payload, type="macro", enabled=enabled)


EMAIL = macro("email", "my email", "you@example.com")
EMAIL_LONG = macro("email_long", "my email address", "Flat 2, 14 Example Street")
SIGN_OFF = macro("sign", "sign off", "Best regards,\nAshwini")
DATE = Trigger(id="date", phrase="today's date", payload="", type="date")


# -- the happy path --------------------------------------------------------


def test_substitutes_a_simple_phrase():
    result = apply_triggers("send it to my email", [EMAIL])
    assert result.text == "send it to you@example.com"
    assert result.fired


def test_preserves_surrounding_punctuation():
    """The sentence's full stop belongs to the sentence, not the trigger."""
    result = apply_triggers("send it to my email.", [EMAIL])
    assert result.text == "send it to you@example.com."


def test_matches_case_and_punctuation_insensitively():
    result = apply_triggers("Send it to My Email, please", [EMAIL])
    assert result.text == "Send it to you@example.com, please"


def test_multiple_distinct_triggers_all_fire():
    result = apply_triggers("my email then sign off", [EMAIL, SIGN_OFF])
    assert result.text == "you@example.com then Best regards,\nAshwini"
    assert len(result.hits) == 2


def test_same_trigger_fires_more_than_once():
    result = apply_triggers("my email and my email", [EMAIL])
    assert result.text == "you@example.com and you@example.com"
    assert len(result.hits) == 2


def test_longest_phrase_wins_regardless_of_order():
    """"my email address" must never be pre-empted by "my email"."""
    for order in ([EMAIL, EMAIL_LONG], [EMAIL_LONG, EMAIL]):
        result = apply_triggers("use my email address please", order)
        assert result.text == "use Flat 2, 14 Example Street please"


def test_raw_text_is_always_preserved():
    """D8.5's undo requirement: a false positive must be recoverable."""
    original = "send it to my email"
    result = apply_triggers(original, [EMAIL])
    assert result.raw_text == original
    assert result.text != original


# -- the negative cases ----------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    [
        "beautify my emails today",  # substring inside a longer word
        "my emailing habits",
        "check the company email",  # missing the leading word
        "email my colleague",  # right words, wrong order
        "my e-mail",  # not the configured spelling
    ],
)
def test_does_not_fire_on_ordinary_speech(sentence):
    result = apply_triggers(sentence, [EMAIL])
    assert not result.fired
    assert result.text == sentence


def test_disabled_triggers_never_fire():
    result = apply_triggers("my email", [macro("x", "my email", "nope", enabled=False)])
    assert not result.fired


def test_no_triggers_is_a_no_op():
    assert apply_triggers("anything at all", []).text == "anything at all"


def test_empty_transcript_is_a_no_op():
    assert apply_triggers("", [EMAIL]).text == ""
    assert apply_triggers("   ", [EMAIL]).text == "   "


def test_trigger_with_a_blank_phrase_is_ignored():
    assert not apply_triggers("hello", [macro("blank", "   ", "x")]).fired


def test_overlapping_triggers_cannot_eat_each_others_words():
    """Once a span is claimed, nothing else may take part of it."""
    a = macro("a", "one two", "A")
    b = macro("b", "two three", "B")
    result = apply_triggers("one two three", [a, b])
    # Exactly one fires; the other's words are already consumed.
    assert len(result.hits) == 1
    assert result.text in {"A three", "one B"}


# -- the date type ---------------------------------------------------------


def test_date_trigger_renders_the_current_date():
    result = apply_triggers("it is today's date", [DATE], today=TODAY)
    assert result.text == "it is 22 August 2026"


def test_date_payload_ignores_any_stored_text():
    """A date must never freeze the day it was created."""
    stored = Trigger(id="d", phrase="x", payload="1 January 1970", type="date")
    assert resolve_payload(stored, today=TODAY) == "22 August 2026"


def test_macro_payload_is_returned_verbatim():
    assert resolve_payload(EMAIL) == "you@example.com"


# -- seams and tidying -----------------------------------------------------


def test_empty_payload_does_not_leave_double_spaces():
    result = apply_triggers("say nothing here", [macro("n", "nothing", "")])
    assert "  " not in result.text
    assert result.text == "say here"


def test_payload_containing_punctuation_survives():
    result = apply_triggers("my email", [macro("e", "my email", "a.b@c.co.uk")])
    assert result.text == "a.b@c.co.uk"


# -- conflicts -------------------------------------------------------------


def test_identical_phrases_conflict_symmetrically():
    a = macro("a", "my email", "1")
    b = macro("b", "My Email.", "2")
    conflicts = find_conflicts([a, b])
    assert conflicts["a"] == ["b"]
    assert conflicts["b"] == ["a"]


def test_prefix_containment_is_reported():
    conflicts = find_conflicts([EMAIL, EMAIL_LONG])
    assert conflicts["email"] == ["email_long"]
    assert conflicts["email_long"] == ["email"]


def test_unrelated_phrases_do_not_conflict():
    assert find_conflicts([EMAIL, SIGN_OFF]) == {}


def test_conflicts_on_an_empty_list():
    assert find_conflicts([]) == {}


# -- robustness ------------------------------------------------------------


def test_unicode_transcript_is_handled():
    trigger = macro("u", "मेरा ईमेल", "you@example.com")
    result = apply_triggers("कृपया मेरा ईमेल भेजें", [trigger])
    assert result.text == "कृपया you@example.com भेजें"


def test_curly_quotes_do_not_prevent_a_match():
    """Whisper punctuates with typographic quotes, not ASCII ones."""
    result = apply_triggers("he said “my email” loudly", [EMAIL])
    assert result.fired


def test_a_very_long_transcript_is_linear_enough():
    """Guards against an accidental quadratic scan on real dictations."""
    text = " ".join(["word"] * 5000) + " my email"
    result = apply_triggers(text, [EMAIL, EMAIL_LONG, SIGN_OFF, DATE])
    assert result.fired
    assert result.text.endswith("you@example.com")


# ---------------------------------------------------------------------------
# Spoken lead-ins ("use my intro email")
#
# A snippet has to be invocable by voice without the user saying its phrase
# bare in the middle of a sentence. The lead-in is consumed along with the
# phrase, or the pasted text starts with a stray "use my".
# ---------------------------------------------------------------------------


def _snippet(phrase: str = "intro email", payload: str = "Hey, chat later?") -> Trigger:
    return Trigger(id="s1", phrase=phrase, payload=payload)


@pytest.mark.parametrize("lead", ["use my", "insert my", "paste my", "Use my", "USE MY"])
def test_a_lead_in_fires_the_snippet_and_is_consumed(lead: str) -> None:
    result = apply_triggers(f"{lead} intro email.", [_snippet()])
    assert result.fired
    assert result.text == "Hey, chat later?."
    assert "my" not in result.text


def test_the_bare_phrase_still_fires_on_its_own() -> None:
    """Lead-ins are additive. The behaviour that shipped must not change."""
    result = apply_triggers("intro email", [_snippet()])
    assert result.fired
    assert result.text == "Hey, chat later?"


def test_a_lead_in_mid_sentence_keeps_the_words_around_it() -> None:
    result = apply_triggers("Please insert my intro email and then stop.", [_snippet()])
    assert result.text == "Please Hey, chat later? and then stop."


def test_lead_ins_can_be_turned_off() -> None:
    result = apply_triggers("use my intro email", [_snippet()], prefixes=())
    assert result.fired
    # The phrase still matched; only the lead-in survives.
    assert result.text == "use my Hey, chat later?"


def test_a_lead_in_does_not_invent_a_match() -> None:
    """"use my" next to words that are not a trigger must do nothing at all."""
    result = apply_triggers("I will use my own email client.", [_snippet("my email", "a@b.com")])
    assert not result.fired
    assert result.text == "I will use my own email client."


def test_the_longest_form_wins_over_the_bare_phrase() -> None:
    """Both "email" and "use my email" match the same speech. The longer span
    has to win, or the lead-in is left stranded in the output."""
    result = apply_triggers("use my email", [_snippet("email", "a@b.com")])
    assert result.text == "a@b.com"


def test_a_lead_in_also_matches_the_display_name() -> None:
    """A snippet called "Signature" triggered by "sign off" should still
    respond to "use my signature" -- that is what the user calls it."""
    snippet = Trigger(id="s1", name="Signature", phrase="sign off", payload="Best regards")
    assert apply_triggers("use my signature", [snippet]).text == "Best regards"
    assert apply_triggers("sign off", [snippet]).text == "Best regards"


def test_the_display_name_never_matches_bare() -> None:
    """Names are casual labels; phrases are chosen to be unique. Matching a
    name on its own would fire "Address" on any sentence mentioning one."""
    snippet = Trigger(id="s1", name="Address", phrase="my postal address", payload="1 High St")
    result = apply_triggers("Send it to the address on file.", [snippet])
    assert not result.fired
    assert result.text == "Send it to the address on file."

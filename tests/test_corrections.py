"""Tests for :mod:`sway.corrections`.

Weighted deliberately toward the negative cases. A missed correction leaves the
user with the literal transcript -- visible, annoying, fixable by hand. A false
positive silently deletes words they meant to keep, and they may never notice.
So every marker word gets at least one test proving it does *not* fire in
ordinary speech, and the detector is only allowed to be clever where the
evidence is strong.

Pure functions over strings, so this covers the module completely without
touching audio, the network, or a model.
"""

from __future__ import annotations

import pytest

from sway.corrections import (
    KIND_ENTITY,
    KIND_EXPLICIT,
    KIND_RESTATEMENT,
    CorrectionScan,
    scan,
)


class TestTheBriefsExamples:
    """The specification, verbatim. Each of these must resolve to the second
    value the speaker gave, not the first."""

    @pytest.mark.parametrize(
        "text",
        [
            "I have a meeting at 7am, no no, it's at 8am.",
            "Send this to John - actually, send it to Sarah.",
            "I'll be there Tuesday, sorry, Wednesday.",
            "The budget is five thousand - wait, make that fifty thousand.",
            "I think we should launch in June... actually July would be better.",
            "Create a meeting for Monday at 3pm. No, Tuesday at 3pm.",
            "We're using Stripe - sorry, I mean Paddle.",
        ],
    )
    def test_detects_the_correction(self, text: str) -> None:
        assert scan(text).detected

    @pytest.mark.parametrize(
        ("text", "withdrawn"),
        [
            ("Let's meet Friday - sorry, Saturday.", "Friday"),
            ("Call me at 10:30 - make that 11.", "10:30"),
            ("Let's do Paris, no, London.", "Paris"),
            ("I have a meeting at 7am, no no, it's at 8am.", "7am"),
        ],
    )
    def test_retracted_span_covers_the_superseded_entity(self, text: str, withdrawn: str) -> None:
        """The retracted span is what lets the guardrail accept a rewrite that
        drops the first value. If it does not contain that value, the correct
        output is rejected and the raw transcript is pasted instead."""
        assert withdrawn in scan(text).retracted_text()


class TestOrdinarySpeech:
    """Every marker word, used sincerely. None of these may fire."""

    @pytest.mark.parametrize(
        "text",
        [
            # The brief names this one explicitly: it must not become "I want to go".
            "I don't want to go, actually.",
            "No, that's exactly right.",
            "I'm sorry about that, it was a mistake.",
            "Sorry I'm late, the meeting is at 3.",
            "Wait for the build to finish before you merge.",
            "I actually finished it yesterday.",
            "Rather than that, use the other one.",
            "The build is green and the tests pass.",
            "Please review the PR when you get a chance.",
        ],
    )
    def test_does_not_fire(self, text: str) -> None:
        assert not scan(text).detected

    def test_emphatic_repetition_is_not_a_correction(self) -> None:
        """The shared cleanup rules already carve this out: repetition doing
        real work is kept. Detection must agree, or the model would be told to
        resolve a correction that was never made."""
        assert not scan("No, no, that's important.").detected

    def test_adding_is_not_replacing(self) -> None:
        """Two weekdays either side of a marker look exactly like a correction.
        The coordinating conjunction is what says the speaker is adding."""
        assert not scan("We shipped Monday and, actually, Tuesday as well.").detected
        assert not scan("Ship it Tuesday, and also Wednesday.").detected

    def test_a_trailing_marker_retracts_nothing(self) -> None:
        """Nothing follows, so nothing replaced anything -- a sentence-final
        hedge, not a retraction."""
        assert not scan("We should ship it on Tuesday, actually.").detected


class TestEvidence:
    """An ambiguous marker needs corroboration; an explicit one does not."""

    def test_explicit_marker_fires_without_a_comparable_pair(self) -> None:
        result = scan("Scratch that, let's start over.")
        assert result.detected
        assert result.markers[0].kind == KIND_EXPLICIT

    def test_ambiguous_marker_needs_a_comparable_pair(self) -> None:
        """"sorry" between two weekdays is a correction; "sorry" between a
        weekday and nothing comparable is an apology."""
        assert scan("I'll be there Tuesday, sorry, Wednesday.").detected
        assert not scan("I'll be there Tuesday, sorry, the traffic was bad.").detected

    def test_entity_classes_must_match(self) -> None:
        """A weekday is not replaced by a time. Pairing across classes would
        make almost any sentence with two nouns look like a correction."""
        assert not scan("Let's meet Tuesday, sorry, at 3pm.").detected

    def test_an_unchanged_value_is_carried_over_not_corrected(self) -> None:
        """"Monday at 3pm. No, Tuesday at 3pm" repeats the time deliberately.
        The weekday is what changed, and the weekday is what must be reported."""
        result = scan("Create a meeting for Monday at 3pm. No, Tuesday at 3pm.")
        assert result.detected
        assert result.markers[0].entity_class == "weekday"

    def test_a_repeated_noun_marks_a_restatement(self) -> None:
        """"the old API - sorry, the new API" swaps a modifier, so there is no
        entity pair to find; the repeated noun is the evidence."""
        result = scan("Use the old API - sorry, the new API.")
        assert result.detected
        assert result.markers[0].kind == KIND_RESTATEMENT

    def test_reports_the_entity_class_it_matched(self) -> None:
        result = scan("I have a meeting at 7am, no no, it's at 8am.")
        assert result.markers[0].kind == KIND_ENTITY
        assert result.markers[0].entity_class == "time"


class TestSentenceInitialCapitals:
    """Whisper capitalises the first word of every sentence, so capitalisation
    alone cannot mean "proper noun" -- otherwise "Create..." pairs with any
    other capitalised word nearby."""

    def test_sentence_openers_are_not_proper_nouns(self) -> None:
        assert not scan("Deploy the service. Actually, check the logs.").detected

    def test_mid_sentence_names_still_pair(self) -> None:
        assert scan("Send this to John - actually, send it to Sarah.").detected


class TestModes:
    def test_off_disables_detection_entirely(self) -> None:
        assert not scan("I have a meeting at 7am, no no, it's at 8am.", mode="off").detected

    def test_balanced_accepts_a_bare_marker(self) -> None:
        """Conservative needs a comparable pair; balanced settles for a marker
        with content on both sides."""
        text = "Let's ship the thing, no, hold off until the review lands."
        assert not scan(text, mode="conservative").detected
        assert scan(text, mode="balanced").detected

    def test_balanced_still_refuses_a_trailing_hedge(self) -> None:
        """Loosening the evidence bar must not reach the case the brief calls
        out by name."""
        assert not scan("I don't want to go, actually.", mode="balanced").detected

    def test_an_unknown_mode_behaves_conservatively(self) -> None:
        """It can only get here from a hand-edited settings file, and refusing
        to start over a bad string would be worse than ignoring it."""
        assert scan("Let's meet Friday - sorry, Saturday.", mode="nonsense").detected


class TestEmptyAndDegenerate:
    @pytest.mark.parametrize("text", ["", "   ", "\n"])
    def test_blank_input_is_not_a_correction(self, text: str) -> None:
        result = scan(text)
        assert not result.detected
        assert result.retracted_text() == ""

    def test_a_clean_transcript_reports_nothing(self) -> None:
        result = scan("The deploy finished and everything looks healthy.")
        assert result.markers == ()
        assert result.describe() == ""
        assert result.kinds == ()

    def test_default_scan_is_falsy_and_safe_to_use(self) -> None:
        """The engine substitutes a bare CorrectionScan when detection is
        skipped, so the empty value has to behave like "found nothing"."""
        empty = CorrectionScan(text="anything")
        assert not empty.detected
        assert empty.retracted_text() == ""
        assert empty.describe() == ""


class TestMultipleCorrections:
    def test_each_correction_is_reported(self) -> None:
        result = scan("Meet me Tuesday, sorry, Wednesday, at 7am, no no, 8am.")
        assert len({m.marker for m in result.markers}) >= 2
        retracted = result.retracted_text()
        assert "Tuesday" in retracted
        assert "7am" in retracted

    def test_markers_come_back_in_transcript_order(self) -> None:
        result = scan("Meet me Tuesday, sorry, Wednesday, at 7am, no no, 8am.")
        starts = [m.start for m in result.markers]
        assert starts == sorted(starts)

    def test_an_ambiguous_marker_inside_an_explicit_one_is_not_double_counted(self) -> None:
        """"no wait" contains "no"; reporting both would inflate the marker
        count and retract the same span twice."""
        result = scan("Send it Friday, no wait, Saturday.")
        assert len(result.markers) == 1


class TestNumericOnlyCorrections:
    """Regression: the "nothing follows the marker" guard was first written as
    a search for a *word*, which is alphabetic-only. That silently stopped
    "meeting at 7, no, 8" -- where everything after the marker is a digit --
    from ever being detected."""

    @pytest.mark.parametrize(
        "text",
        [
            "Meeting at 7, no, 8.",
            "Room 214, sorry, 215.",
            "It's 15 - make that 50.",
        ],
    )
    def test_a_correction_whose_replacement_is_only_digits(self, text: str) -> None:
        assert scan(text).detected

    def test_trailing_punctuation_is_still_not_content(self) -> None:
        assert not scan("We should ship it on Tuesday, actually.").detected


# -- "make it": the correction phrasing that was reported as broken ----------
# "Book a meeting for Monday at 3 PM" then "No, no, make it Tuesday at 6 AM"
# must resolve to Tuesday at 6 AM, without the speaker restating the request.
#
# "make it" sits in the *ambiguous* tier, not the explicit one, and these
# tests are mostly about what it must NOT do: a false positive here silently
# deletes the clause the speaker meant to keep.


class TestMakeIt:
    def test_a_spoken_correction_with_make_it_is_detected(self) -> None:
        scan_result = scan("Book a meeting for Monday at 3 PM. No, no, make it Tuesday at 6 AM.")
        assert scan_result.detected
        assert "make it" in [m.marker for m in scan_result.markers]

    def test_the_retracted_span_covers_the_replaced_value(self) -> None:
        """What the guardrail needs: the old time is exempt from the
        lost-entity check, so a rewrite that correctly drops it is not thrown
        away for having dropped a number."""
        scan_result = scan("Lunch at 12, make it 1.")
        assert scan_result.detected
        assert "12" in scan_result.retracted_text()

    @pytest.mark.parametrize(
        "text",
        [
            # The reported false-positive risk. "and then" makes this two
            # statements, not a retraction of one.
            "I want to book a meeting for Monday 6 am and then I'll make it Tuesday 3 pm",
            # Ordinary instructions that happen to contain the words.
            "Make it three copies.",
            "Print the report, make it snappy.",
            "Make it clear that we cannot ship on Friday.",
        ],
    )
    def test_ordinary_speech_containing_make_it_is_left_alone(self, text: str) -> None:
        result = scan(text)
        assert not result.detected, [m.marker for m in result.markers]
        assert result.text == text

    def test_a_bare_make_it_with_no_comparable_pair_does_not_fire(self) -> None:
        """The ambiguous tier's whole point: without a day-for-day or
        time-for-time replacement there is nothing to say a retraction
        happened."""
        assert not scan("Send the deck over, make it good.").detected

    def test_the_last_correction_in_a_sequence_wins(self) -> None:
        """Several corrections in one utterance: every marker is recorded, in
        spoken order, so the model resolves to the final value rather than an
        arbitrary one."""
        result = scan("Monday at 3 PM, no, make it Tuesday, actually make it Wednesday.")
        assert result.detected
        starts = [m.start for m in result.markers]
        assert starts == sorted(starts)

    def test_changing_only_the_time_keeps_the_day(self) -> None:
        """A one-parameter correction must not retract the whole clause -- the
        day was never withdrawn, so it stays protected by the entity guard."""
        result = scan("Tuesday at 3 PM, make it 6 AM.")
        assert result.detected
        assert "Tuesday" not in result.retracted_text()

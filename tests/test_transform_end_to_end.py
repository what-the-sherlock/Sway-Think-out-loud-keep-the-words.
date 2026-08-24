"""The reported failure, end to end across the pure pipeline.

The user enabled "Turn into bullet points" and dictated:

    "I want to order groceries like Kurkure, namkeen, potatoes, sugar,
     make that bullet points."

and got no bullets. Each stage was individually correct and the feature was
still broken, which is exactly the shape of bug that unit tests on the stages
do not catch. This file walks one transcript through every pure stage in the
order the engine runs them -- match, excise, prompt, guard -- and asserts the
bullets survive to the end.

Only the model call is stood in for. Everything else is the real code path:
sway.transforms.as_matchable -> sway.triggers.apply_triggers ->
sway.transforms.build_directive_block / expectation_for ->
sway.cleanup.unsafe_reason.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from sway.cleanup import unsafe_reason
from sway.transforms import (
    DEFAULT_TRANSFORMS,
    as_matchable,
    build_directive_block,
    by_id,
    expectation_for,
)
from sway.triggers import apply_triggers

REPORTED = (
    "I want to order groceries like Kurkure, namkeen, potatoes, sugar, "
    "make that bullet points."
)


def _enabled(*ids: str):
    return tuple(replace(t, enabled=t.id in ids) for t in DEFAULT_TRANSFORMS)


def _pipeline(transcript: str, enabled_ids: tuple[str, ...]):
    """The engine's pure stages, in order. Returns (text, fired, expectation)."""
    library = _enabled(*enabled_ids)
    result = apply_triggers(transcript, as_matchable(library))
    by_trigger = {t.id: t for t in library}
    fired = [by_trigger[h.trigger_id] for h in result.hits if h.trigger_id in by_trigger]
    return result.text, fired, expectation_for(fired)


def test_the_reported_sentence_fires_the_transform():
    _text, fired, _expectation = _pipeline(REPORTED, ("bullets",))
    assert [t.id for t in fired] == ["bullets"]


def test_the_phrase_is_excised_and_the_groceries_survive():
    text, _fired, _expectation = _pipeline(REPORTED, ("bullets",))
    assert "bullet points" not in text
    for item in ("Kurkure", "namkeen", "potatoes", "sugar"):
        assert item in text


def test_the_instruction_reaches_the_prompt():
    _text, fired, _expectation = _pipeline(REPORTED, ("bullets",))
    directive = build_directive_block(fired)
    assert "bulleted list" in directive
    assert "- " in directive


def test_the_bulleted_answer_is_not_thrown_away():
    """The stage that was actually broken. Every earlier stage already worked;
    the model's correct answer was rejected here and the raw transcript pasted
    in its place."""
    text, fired, expectation = _pipeline(REPORTED, ("bullets",))
    model_output = "- Kurkure\n- Namkeen\n- Potatoes\n- Sugar"
    assert unsafe_reason(text, model_output, expectation=expectation) == ""


def test_the_same_answer_is_rejected_when_no_transform_fired():
    """The guardrail must still do its job for ordinary dictation."""
    text, fired, expectation = _pipeline(REPORTED, ())
    assert fired == []
    model_output = "- Kurkure\n- Namkeen\n- Potatoes\n- Sugar"
    assert unsafe_reason(text, model_output, expectation=expectation) == "markdown"


@pytest.mark.parametrize(
    "transform_id, transcript, model_output",
    [
        (
            "summarize",
            "so the deploy went out at nine and the migration finished around ten "
            "and then we found the caching bug and rolled the whole thing back at "
            "eleven and Priya is picking it up again tomorrow morning to work out "
            "whether it was the invalidation logic or the deploy script, summarize that",
            "Deploy at nine, migration at ten, rollback at eleven. Priya investigates tomorrow.",
        ),
        (
            "email",
            "tell Ali the deck is ready, make that an email",
            "Hi Ali,\n\nJust a quick note to let you know that the deck is ready "
            "whenever you would like to review it. Do let me know if anything needs "
            "changing.\n\nBest regards",
        ),
        (
            "concise",
            "what I am basically trying to say here is that we are not going to be "
            "able to ship the thing on Friday like we said we would, make that concise",
            "We cannot ship on Friday.",
        ),
    ],
)
def test_every_shape_changing_transform_survives_the_guardrails(
    transform_id: str, transcript: str, model_output: str
):
    """Bullets was the reported one; these are the others the same guardrail
    set defeated, caught here rather than one bug report at a time."""
    text, fired, expectation = _pipeline(transcript, (transform_id,))
    assert [t.id for t in fired] == [transform_id]
    assert unsafe_reason(text, model_output, expectation=expectation) == ""


def test_a_transform_never_licenses_dropping_what_was_said():
    """The guard that has to hold under every instruction."""
    text, _fired, expectation = _pipeline(
        "order 6 packets of namkeen and 2 kilos of sugar, make that bullet points",
        ("bullets",),
    )
    lossy = "- Namkeen\n- Sugar"
    assert unsafe_reason(text, lossy, expectation=expectation).startswith("lost_entity:")


def test_a_disabled_transform_changes_nothing():
    """The toggle in the UI is the gate. With it off, the phrase stays in the
    text and the guardrails stay at full strength."""
    text, fired, expectation = _pipeline(REPORTED, ())
    assert fired == []
    assert "make that bullet points" in text

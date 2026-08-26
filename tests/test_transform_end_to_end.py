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

import pytest

from sway.cleanup import unsafe_reason
from sway.transforms import (
    DEFAULT_TRANSFORMS,
    Transform,
    as_matchable,
    build_directive_block,
    expectation_for,
)
from sway.triggers import apply_triggers

REPORTED = (
    "I want to order groceries like Kurkure, namkeen, potatoes, sugar, "
    "make that bullet points."
)

#: "Turn into bullet points" was the transform actually reported broken. It
#: no longer ships as a default (see transforms.DEFAULT_TRANSFORMS and
#: _RETIRED_DEFAULT_EXPECTATIONS), but the regression this file pins down is
#: about the pipeline, not about which transforms happen to ship today -- so
#: it is reconstructed here exactly as it shipped, rather than rewritten to
#: use a current default and silently stop testing the reported shape of bug.
_BULLETS = Transform(
    id="bullets",
    name="Turn into bullet points",
    description="One point per line.",
    instruction=(
        "Rewrite as a bulleted list, one idea per bullet, each starting with "
        "'- '. Keep every point from the original and add none."
    ),
    allows_markdown=True,
    may_shorten=True,
    phrase="make that bullet points",
    builtin=True,
)

#: Every transform id a test in this file can select by name -- the current
#: shipped defaults, plus the retired one above.
_LIBRARY = DEFAULT_TRANSFORMS + (_BULLETS,)


def _only(*ids: str) -> tuple[Transform, ...]:
    return tuple(t for t in _LIBRARY if t.id in ids)


def _pipeline(transcript: str, selected_ids: tuple[str, ...]):
    """The engine's pure stages, in order. Returns (text, fired, expectation)."""
    library = _only(*selected_ids)
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


def test_a_shortening_transform_may_drop_what_was_said():
    """``_BULLETS`` ships (as it always did) with ``may_shorten=True`` --
    originally to relax the ratio floor for prose that gets shorter once it is
    reformatted into bullets. That flag now also lifts the entity guard (see
    OutputExpectation.protects_entities): a transform explicitly marked as
    shortening the text is trusted to drop a fact the way a real summary
    would, the same trust a user extends when they tick "makes the text
    shorter" in the transform editor. The guard still holds for a transform
    that has not made that claim -- see
    test_cleanup_guardrails.test_a_markdown_only_expectation_does_not_relax_the_entity_guard.
    """
    text, _fired, expectation = _pipeline(
        "order 6 packets of namkeen and 2 kilos of sugar, make that bullet points",
        ("bullets",),
    )
    lossy = "- Namkeen\n- Sugar"
    assert unsafe_reason(text, lossy, expectation=expectation) == ""


def test_no_transforms_selected_changes_nothing():
    """Presence in the candidate list is what makes a transform runnable now
    (see transforms.is_runnable) -- with none selected, the phrase stays in
    the text and the guardrails stay at full strength."""
    text, fired, expectation = _pipeline(REPORTED, ())
    assert fired == []
    assert "make that bullet points" in text


def test_a_shortcut_fired_email_survives_the_guardrails():
    """The second reported failure, same shape as the bullets one: a
    shortcut-run transform never goes through apply_triggers at all (see
    Engine._run_transform) -- it calls expectation_for([transform]) directly
    on the selected text, with no phrase to excise. A real sentence run
    through "Turn into an email" was rejected as "grew" even though turning
    a sentence into an email is the entire point of the transform."""
    email = next(t for t in DEFAULT_TRANSFORMS if t.id == "email")
    selected = (
        "I want to complain to the government about the road issues. They "
        "are of very bad quality and we need good roads."
    )
    model_output = (
        "Subject: Request for Urgent Repair of Deteriorating Road Conditions\n\n"
        "Dear Sir or Madam,\n\n"
        "I am writing to raise a concern about the condition of the roads in "
        "our area, which has deteriorated noticeably over recent months. The "
        "surfaces are in poor repair, with potholes and uneven sections that "
        "make daily travel difficult and, in places, unsafe for both drivers "
        "and pedestrians.\n\n"
        "I would be grateful if you could advise what steps are being taken "
        "to address this, and whether a timeline for repair work has been "
        "established.\n\n"
        "Thank you for your time and attention to this matter.\n\n"
        "Yours faithfully"
    )
    expectation = expectation_for([email])
    assert unsafe_reason(selected, model_output, expectation=expectation) == ""

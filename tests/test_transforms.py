"""Tests for :mod:`sway.transforms`.

Two things carry real risk here and are tested accordingly.

*A transform phrase can delete words.* It is excised from the transcript
rather than substituted, so a phrase that fires by accident silently removes
what the speaker said. The matching tests are mostly about *not* firing.

*A transform costs a model call.* The directive block is what merges every
fired transform into the single cleanup request; if it ever became a call per
transform, latency and the free-tier quota would both multiply.
"""

from __future__ import annotations

import pytest

from sway.hotkey import parse_binding
from sway.transforms import (
    DEFAULT_TRANSFORMS,
    Transform,
    as_matchable,
    build_directive_block,
    by_id,
    enabled_with_phrase,
    enabled_with_shortcut,
    find_shortcut_conflicts,
    is_runnable,
    shipped_expectation,
)
from sway.triggers import apply_triggers


def _only(*ids: str) -> tuple[Transform, ...]:
    """The named shipped transforms, and no others."""
    return tuple(t for t in DEFAULT_TRANSFORMS if t.id in ids)


class TestDefaults:
    def test_every_shipped_transform_is_runnable_out_of_the_box(self) -> None:
        """Unlike the checkbox era, presence in the shipped set *is* the on
        switch: every default ships with a shortcut (and a phrase), so
        nothing needs a separate toggle before it can be used."""
        assert all(is_runnable(t) for t in DEFAULT_TRANSFORMS)
        assert all(t.shortcuts for t in DEFAULT_TRANSFORMS)

    def test_every_shipped_transform_has_an_instruction_and_a_phrase(self) -> None:
        for transform in DEFAULT_TRANSFORMS:
            assert transform.instruction.strip(), transform.id
            assert transform.phrase.strip(), transform.id

    def test_every_shipped_transform_has_a_stored_example(self) -> None:
        for transform in DEFAULT_TRANSFORMS:
            assert transform.example_input.strip(), transform.id
            assert transform.example_output.strip(), transform.id

    def test_ids_are_unique(self) -> None:
        ids = [t.id for t in DEFAULT_TRANSFORMS]
        assert len(ids) == len(set(ids))

    def test_phrases_are_unique(self) -> None:
        """Two transforms on one phrase means one of them can never fire."""
        phrases = [t.phrase.lower() for t in DEFAULT_TRANSFORMS]
        assert len(phrases) == len(set(phrases))

    def test_shortcuts_are_unique(self) -> None:
        assert find_shortcut_conflicts(DEFAULT_TRANSFORMS) == {}

    def test_every_shortcut_parses(self) -> None:
        for transform in DEFAULT_TRANSFORMS:
            for shortcut in transform.shortcuts:
                parse_binding(shortcut)  # raises on failure


class TestRunnability:
    def test_a_transform_with_neither_a_phrase_nor_a_shortcut_is_not_runnable(self) -> None:
        assert is_runnable(Transform(id="x", name="X", instruction="do it")) is False

    def test_a_phrase_alone_is_runnable(self) -> None:
        assert is_runnable(Transform(id="x", name="X", instruction="do it", phrase="do it")) is True

    def test_a_shortcut_alone_is_runnable(self) -> None:
        t = Transform(id="x", name="X", instruction="do it", shortcuts=("ctrl+alt+p",))
        assert is_runnable(t) is True


class TestSelection:
    def test_only_phrase_carrying_transforms_can_be_spoken(self) -> None:
        transforms = _only("polish") + (
            Transform(id="x", name="X", instruction="do it"),  # no phrase
        )
        assert [t.id for t in enabled_with_phrase(transforms)] == ["polish"]

    def test_only_shortcut_bound_transforms_can_be_pressed(self) -> None:
        transforms = (
            Transform(id="a", name="A", instruction="i", shortcuts=("ctrl+alt+a",)),
            Transform(id="b", name="B", instruction="i"),  # no shortcut
        )
        assert [t.id for t in enabled_with_shortcut(transforms)] == ["a"]

    def test_a_transform_with_several_shortcuts_still_counts_once(self) -> None:
        transforms = (
            Transform(id="a", name="A", instruction="i", shortcuts=("ctrl+alt+a", "mouse:middle")),
        )
        assert [t.id for t in enabled_with_shortcut(transforms)] == ["a"]


class TestSpokenMatching:
    """Transforms ride the snippet matcher, so they inherit its whole-word,
    longest-first, non-overlapping rules -- plus one of their own."""

    def test_a_trailing_phrase_fires_and_is_excised(self) -> None:
        result = apply_triggers("Draft the update and make that concise.", as_matchable(_only("concise")))
        assert result.fired
        assert "concise" not in result.text
        assert "Draft the update" in result.text

    def test_the_same_words_mid_sentence_do_not_fire(self) -> None:
        """"We should polish that stone before shipping" is not a request to
        run Polish. Without this the transform would delete "polish that" from
        the middle of an ordinary sentence."""
        text = "We should polish that stone before shipping."
        result = apply_triggers(text, as_matchable(_only("polish")))
        assert not result.fired
        assert result.text == text

    def test_a_phrase_at_the_end_of_a_later_sentence_still_fires(self) -> None:
        result = apply_triggers(
            "Rewrite the changelog. Make that concise.", as_matchable(_only("concise"))
        )
        assert result.fired

    def test_a_transform_absent_from_the_candidate_list_never_fires(self) -> None:
        text = "Draft the update and make that concise."
        assert not apply_triggers(text, as_matchable(_only("polish"))).fired

    def test_a_phraseless_transform_never_matches(self) -> None:
        text = "make that concise"
        transforms = (Transform(id="x", name="X", instruction="i", phrase=""),)
        assert not apply_triggers(text, as_matchable(transforms)).fired

    def test_the_raw_transcript_is_preserved(self) -> None:
        """A false positive deletes words, so the pre-excision text has to
        survive into history for the user to recover."""
        text = "Draft the update and make that concise."
        result = apply_triggers(text, as_matchable(_only("concise")))
        assert result.raw_text == text

    def test_the_fired_id_comes_back_on_the_hit(self) -> None:
        result = apply_triggers("Tidy this up, polish that", as_matchable(_only("polish")))
        assert [h.trigger_id for h in result.hits] == ["polish"]

    # -- the reported case ---------------------------------------------
    # "I want to order groceries like Kurkure, namkeen, potatoes, sugar, make
    # that a prompt." Pinned so a future change to _may_fire_here cannot
    # quietly move the goalposts under a natural, trailing-phrase sentence.

    @pytest.mark.parametrize(
        "transform_id, text",
        [
            (
                "prompt_engineer",
                "I want to order groceries like Kurkure, namkeen, potatoes, "
                "sugar, make that a prompt.",
            ),
            ("concise", "Let the team know the release slipped, make that concise."),
            ("summarize", "Here is everything that happened on the deploy. Summarize that."),
            ("email", "tell Ali the deck is ready, make that an email"),
            ("professional", "tell them it is not happening, make that professional"),
            ("clarity", "the thing with the thing did not work, make that clearer"),
            ("polish", "we shipped it and it went fine, polish that"),
        ],
    )
    def test_a_natural_request_ending_in_a_phrase_fires(
        self, transform_id: str, text: str
    ) -> None:
        """Every shipped transform, invoked the way it is actually spoken: at
        the end of a real sentence rather than on its own."""
        result = apply_triggers(text, as_matchable(_only(transform_id)))
        assert result.fired, transform_id
        assert [h.trigger_id for h in result.hits] == [transform_id]

    def test_the_phrase_is_excised_from_a_natural_request(self) -> None:
        result = apply_triggers(
            "I want to order groceries like Kurkure, namkeen, potatoes, sugar, "
            "make that a prompt.",
            as_matchable(_only("prompt_engineer")),
        )
        assert "make that a prompt" not in result.text
        assert "Kurkure" in result.text and "sugar" in result.text

    def test_two_phrases_in_one_utterance_both_fire(self) -> None:
        """Only the trailing one can fire -- a transform is trailing-only -- so
        this pins that the *last* phrase wins rather than neither."""
        result = apply_triggers(
            "Write up the outage, summarize that",
            as_matchable(_only("summarize", "polish")),
        )
        assert [h.trigger_id for h in result.hits] == ["summarize"]

    def test_excision_does_not_strand_a_comma_against_a_full_stop(self) -> None:
        result = apply_triggers(
            "Tell them the release slipped, make that professional.",
            as_matchable(_only("professional")),
        )
        assert result.text == "Tell them the release slipped."


class TestDirectiveBlock:
    def test_nothing_fired_costs_nothing(self) -> None:
        """The empty case is the common one, and it is what keeps an ordinary
        dictation's prompt byte-identical across requests and cacheable."""
        assert build_directive_block([]) == ""

    def test_a_transform_with_a_blank_instruction_adds_nothing(self) -> None:
        assert build_directive_block([Transform(id="x", name="X", instruction="   ")]) == ""

    def test_one_transform_carries_its_instruction(self) -> None:
        block = build_directive_block([by_id(DEFAULT_TRANSFORMS, "concise")])
        assert "noticeably fewer words" in block

    def test_several_transforms_go_in_one_block(self) -> None:
        """One model call, always (D8.3). Two transforms must produce one
        directive block, never two requests."""
        fired = [by_id(DEFAULT_TRANSFORMS, "concise"), by_id(DEFAULT_TRANSFORMS, "professional")]
        block = build_directive_block(fired)
        assert "noticeably fewer words" in block
        assert "professional register" in block

    def test_conflicting_transforms_get_a_stated_precedence(self) -> None:
        """D8.4: "make it terse" and "add more detail" need a defined outcome,
        not whichever the model happened to notice last."""
        fired = [by_id(DEFAULT_TRANSFORMS, "concise"), by_id(DEFAULT_TRANSFORMS, "clarity")]
        assert "the earlier one wins" in build_directive_block(fired)

    def test_the_block_restates_the_rewrite_only_contract(self) -> None:
        """The transform instruction is untrusted user text sitting in a
        prompt. The "never answer, never add commentary" posture has to be
        restated after it, not left behind in the base prompt."""
        block = build_directive_block([by_id(DEFAULT_TRANSFORMS, "polish")])
        assert "never answer" in block


class TestShortcutConflicts:
    def test_two_transforms_on_one_chord_are_reported_both_ways(self) -> None:
        # Shortcuts are always stored in canonical form by the time they
        # reach a Transform (both the settings parser and the dashboard
        # actions layer canonicalise via parse_binding before saving), so
        # conflict detection compares canonical strings directly.
        transforms = (
            Transform(id="a", name="A", instruction="i", shortcuts=("ctrl+alt+p",)),
            Transform(id="b", name="B", instruction="i", shortcuts=("ctrl+alt+p",)),
        )
        conflicts = find_shortcut_conflicts(transforms)
        assert conflicts == {"a": ["b"], "b": ["a"]}

    def test_different_chords_do_not_conflict(self) -> None:
        transforms = (
            Transform(id="a", name="A", instruction="i", shortcuts=("ctrl+alt+p",)),
            Transform(id="b", name="B", instruction="i", shortcuts=("ctrl+alt+q",)),
        )
        assert find_shortcut_conflicts(transforms) == {}

    def test_transforms_without_a_shortcut_are_not_in_conflict(self) -> None:
        transforms = (
            Transform(id="a", name="A", instruction="i"),
            Transform(id="b", name="B", instruction="i"),
        )
        assert find_shortcut_conflicts(transforms) == {}

    def test_a_shared_binding_among_several_conflicts_even_if_the_rest_differ(self) -> None:
        transforms = (
            Transform(id="a", name="A", instruction="i", shortcuts=("ctrl+alt+p", "mouse:middle")),
            Transform(id="b", name="B", instruction="i", shortcuts=("ctrl+alt+q", "mouse:middle")),
        )
        conflicts = find_shortcut_conflicts(transforms)
        assert conflicts == {"a": ["b"], "b": ["a"]}


class TestShippedExpectation:
    def test_a_current_default_returns_its_own_flags(self) -> None:
        assert shipped_expectation("prompt_engineer", "allows_markdown") is True
        assert shipped_expectation("prompt_engineer", "may_lengthen") is True
        assert shipped_expectation("polish", "allows_markdown") is False

    def test_a_retired_default_still_returns_its_historical_flags(self) -> None:
        """"Turn into bullet points" no longer ships, but an existing user's
        copy of it must keep working exactly as it did before it was retired
        -- see _RETIRED_DEFAULT_EXPECTATIONS."""
        assert shipped_expectation("bullets", "allows_markdown") is True
        assert shipped_expectation("bullets", "may_shorten") is True

    def test_an_id_that_was_never_shipped_returns_false(self) -> None:
        assert shipped_expectation("something-a-user-made-up", "allows_markdown") is False

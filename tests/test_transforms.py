"""Tests for :mod:`sway.transforms` and the chord layer in :mod:`sway.hotkey`.

Two things carry real risk here and are tested accordingly.

*A transform phrase can delete words.* It is excised from the transcript
rather than substituted, so a phrase that fires by accident silently removes
what the speaker said. The matching tests are mostly about *not* firing.

*A transform costs a model call.* The directive block is what merges every
fired transform into the single cleanup request; if it ever became a call per
transform, latency and the free-tier quota would both multiply.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from sway.hotkey import Chord, ChordError, format_chord, parse_chord
from sway.transforms import (
    DEFAULT_TRANSFORMS,
    Transform,
    as_matchable,
    build_directive_block,
    by_id,
    enabled_with_phrase,
    enabled_with_shortcut,
    find_shortcut_conflicts,
)
from sway.triggers import apply_triggers


def _on(*ids: str) -> tuple[Transform, ...]:
    """The shipped transforms, with the named ones enabled."""
    return tuple(replace(t, enabled=t.id in ids) for t in DEFAULT_TRANSFORMS)


class TestDefaults:
    def test_every_shipped_transform_starts_disabled(self) -> None:
        """D8.5. A transform costs a request and free-tier quota, so nothing
        ships already spending them."""
        assert not any(t.enabled for t in DEFAULT_TRANSFORMS)

    def test_every_shipped_transform_has_an_instruction_and_a_phrase(self) -> None:
        for transform in DEFAULT_TRANSFORMS:
            assert transform.instruction.strip(), transform.id
            assert transform.phrase.strip(), transform.id

    def test_ids_are_unique(self) -> None:
        ids = [t.id for t in DEFAULT_TRANSFORMS]
        assert len(ids) == len(set(ids))

    def test_phrases_are_unique(self) -> None:
        """Two transforms on one phrase means one of them can never fire."""
        phrases = [t.phrase.lower() for t in DEFAULT_TRANSFORMS]
        assert len(phrases) == len(set(phrases))


class TestSelection:
    def test_only_enabled_transforms_with_a_phrase_can_be_spoken(self) -> None:
        transforms = _on("polish") + (
            Transform(id="x", name="X", instruction="do it", enabled=True),
        )
        assert [t.id for t in enabled_with_phrase(transforms)] == ["polish"]

    def test_only_enabled_transforms_with_a_shortcut_can_be_pressed(self) -> None:
        transforms = (
            Transform(id="a", name="A", instruction="i", shortcut="ctrl+alt+a", enabled=True),
            Transform(id="b", name="B", instruction="i", shortcut="ctrl+alt+b", enabled=False),
            Transform(id="c", name="C", instruction="i", enabled=True),
        )
        assert [t.id for t in enabled_with_shortcut(transforms)] == ["a"]


class TestSpokenMatching:
    """Transforms ride the snippet matcher, so they inherit its whole-word,
    longest-first, non-overlapping rules -- plus one of their own."""

    def test_a_trailing_phrase_fires_and_is_excised(self) -> None:
        result = apply_triggers("Draft the update and make that concise.", as_matchable(_on("concise")))
        assert result.fired
        assert "concise" not in result.text
        assert "Draft the update" in result.text

    def test_the_same_words_mid_sentence_do_not_fire(self) -> None:
        """"We should polish that stone before shipping" is not a request to
        run Polish. Without this the transform would delete "polish that" from
        the middle of an ordinary sentence."""
        text = "We should polish that stone before shipping."
        result = apply_triggers(text, as_matchable(_on("polish")))
        assert not result.fired
        assert result.text == text

    def test_a_phrase_at_the_end_of_a_later_sentence_still_fires(self) -> None:
        result = apply_triggers("Rewrite the changelog. Fix the grammar.", as_matchable(_on("grammar")))
        assert result.fired

    def test_a_disabled_transform_never_fires(self) -> None:
        text = "Draft the update and make that concise."
        assert not apply_triggers(text, as_matchable(DEFAULT_TRANSFORMS)).fired

    def test_the_raw_transcript_is_preserved(self) -> None:
        """A false positive deletes words, so the pre-excision text has to
        survive into history for the user to recover."""
        text = "Draft the update and make that concise."
        result = apply_triggers(text, as_matchable(_on("concise")))
        assert result.raw_text == text

    def test_the_fired_id_comes_back_on_the_hit(self) -> None:
        result = apply_triggers("Tidy this up, polish that", as_matchable(_on("polish")))
        assert [h.trigger_id for h in result.hits] == ["polish"]

    def test_excision_does_not_strand_a_comma_against_a_full_stop(self) -> None:
        result = apply_triggers(
            "Tell them the release slipped, make that professional.",
            as_matchable(_on("professional")),
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
        transforms = (
            Transform(id="a", name="A", instruction="i", shortcut="ctrl+alt+p"),
            Transform(id="b", name="B", instruction="i", shortcut="Ctrl+Alt+P"),
        )
        conflicts = find_shortcut_conflicts(transforms)
        assert conflicts == {"a": ["b"], "b": ["a"]}

    def test_different_chords_do_not_conflict(self) -> None:
        transforms = (
            Transform(id="a", name="A", instruction="i", shortcut="ctrl+alt+p"),
            Transform(id="b", name="B", instruction="i", shortcut="ctrl+alt+q"),
        )
        assert find_shortcut_conflicts(transforms) == {}

    def test_transforms_without_a_shortcut_are_not_in_conflict(self) -> None:
        transforms = (
            Transform(id="a", name="A", instruction="i"),
            Transform(id="b", name="B", instruction="i"),
        )
        assert find_shortcut_conflicts(transforms) == {}


class TestChords:
    @pytest.mark.parametrize(
        ("written", "canonical"),
        [
            ("ctrl+alt+p", "ctrl+alt+p"),
            ("Ctrl+Alt+P", "ctrl+alt+p"),
            ("alt+ctrl+p", "ctrl+alt+p"),
            ("  ctrl + alt + p  ", "ctrl+alt+p"),
            ("ctrl+shift+f5", "ctrl+shift+f5"),
        ],
    )
    def test_parsing_is_order_and_case_insensitive(self, written: str, canonical: str) -> None:
        assert str(parse_chord(written)) == canonical

    def test_equal_chords_hash_together(self) -> None:
        """Dispatch is a dict lookup keyed on Chord, so equality without a
        matching hash would silently never fire."""
        assert len({parse_chord("Ctrl+Alt+P"), parse_chord("alt+ctrl+p")}) == 1

    @pytest.mark.parametrize(
        ("bad", "because"),
        [
            ("p", "no modifier"),
            ("", "empty"),
            ("ctrl", "modifier only"),
            ("ctrl+alt", "still modifier only"),
            ("meta+p", "unknown modifier"),
        ],
    )
    def test_unusable_shortcuts_are_refused(self, bad: str, because: str) -> None:
        with pytest.raises(ChordError):
            parse_chord(bad)

    def test_a_bare_letter_is_refused_rather_than_bound(self) -> None:
        """It would fire inside anything the user typed. That is not a
        shortcut, it is a broken keyboard."""
        with pytest.raises(ChordError):
            parse_chord("p")

    def test_pretty_form_is_for_reading(self) -> None:
        assert parse_chord("ctrl+alt+p").pretty() == "Ctrl + Alt + P"
        assert parse_chord("cmd+k").pretty() == "Win + K"

    def test_format_chord_passes_through_what_it_cannot_parse(self) -> None:
        """Display paths must not raise on a hand-edited settings file."""
        assert format_chord("nonsense!") == "nonsense!"
        assert format_chord("Ctrl+Alt+P") == "ctrl+alt+p"

    def test_a_chord_is_not_equal_to_a_random_object(self) -> None:
        assert parse_chord("ctrl+alt+p") != "ctrl+alt+p"

    def test_left_and_right_modifiers_are_the_same_modifier(self) -> None:
        """Nobody means "the right Ctrl specifically" when they write
        ctrl+alt+p."""
        assert Chord(frozenset({"ctrl"}), "p") == parse_chord("ctrl+p")

"""Tests for Phase 4's pure chunk-boundary math and transcript stitching.

Both `ChunkClock` and `stitch_transcripts` are hardware-free: no ring buffer,
no VAD, no network. `ChunkStreamer` (the thread/network orchestration around
them) is deliberately left untested, same as `flow.audio.capture` and
`flow.engine` -- see CLAUDE.md.
"""

from __future__ import annotations

import pytest

from sway.audio.ring_buffer import RingBuffer
from sway.streaming import ChunkClock, max_overlap_words, stitch_transcripts

# -- ChunkClock --------------------------------------------------------------


def test_first_chunk_is_due_once_full_chunk_length_captured():
    clock = ChunkClock(start_frame=0, chunk_length_frames=100, overlap_frames=20)
    assert not clock.is_due(99)
    assert clock.is_due(100)


def test_first_chunk_range_covers_the_whole_window_no_overlap():
    clock = ChunkClock(start_frame=0, chunk_length_frames=100, overlap_frames=20)
    assert clock.read_range(100) == (0, 100)


def test_second_chunk_is_due_after_step_frames_not_full_chunk_length():
    clock = ChunkClock(start_frame=0, chunk_length_frames=100, overlap_frames=20)
    clock.advance(100)
    # step = chunk_length - overlap = 80
    assert not clock.is_due(179)
    assert clock.is_due(180)


def test_second_chunk_range_overlaps_the_first():
    clock = ChunkClock(start_frame=0, chunk_length_frames=100, overlap_frames=20)
    clock.advance(100)
    # starts 20 frames before the first chunk's right edge (the overlap)
    assert clock.read_range(180) == (80, 180)


def test_start_frame_offset_is_respected():
    clock = ChunkClock(start_frame=500, chunk_length_frames=100, overlap_frames=20)
    assert clock.next_due_frame() == 600
    clock.advance(600)
    assert clock.read_range(680) == (580, 680)


def test_tail_range_before_any_chunk_is_the_whole_clip():
    """No chunk ever fired (a short hold) -> tail covers the whole clip,
    which is exactly Phase 1's single-shot behavior.
    """
    clock = ChunkClock(start_frame=10, chunk_length_frames=100, overlap_frames=20)
    assert clock.tail_range(55) == (10, 55)


def test_tail_range_after_chunks_is_short_and_overlaps_last_chunk():
    clock = ChunkClock(start_frame=0, chunk_length_frames=100, overlap_frames=20)
    clock.advance(100)
    clock.advance(180)
    # released shortly after the second chunk -> small tail, overlapping it
    assert clock.tail_range(200) == (160, 200)


def test_chunks_emitted_tracks_advance_calls():
    clock = ChunkClock(start_frame=0, chunk_length_frames=100, overlap_frames=20)
    assert clock.chunks_emitted == 0
    clock.advance(100)
    assert clock.chunks_emitted == 1
    clock.advance(180)
    assert clock.chunks_emitted == 2


def test_step_frames_is_chunk_length_minus_overlap():
    clock = ChunkClock(start_frame=0, chunk_length_frames=100, overlap_frames=35)
    assert clock.step_frames == 65


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chunk_length_frames": 0, "overlap_frames": 0},
        {"chunk_length_frames": -1, "overlap_frames": 0},
        {"chunk_length_frames": 100, "overlap_frames": -1},
        {"chunk_length_frames": 100, "overlap_frames": 100},
        {"chunk_length_frames": 100, "overlap_frames": 150},
    ],
)
def test_invalid_construction_is_rejected(kwargs: dict):
    with pytest.raises(ValueError):
        ChunkClock(start_frame=0, **kwargs)


# -- stitch_transcripts --------------------------------------------------------


def test_empty_pieces_yield_empty_string():
    assert stitch_transcripts([]) == ""
    assert stitch_transcripts(["", "  ", None]) == ""  # type: ignore[list-item]


def test_single_piece_passes_through():
    assert stitch_transcripts(["hello world"]) == "hello world"


def test_no_overlap_just_concatenates():
    assert stitch_transcripts(["hello world", "goodbye now"]) == "hello world goodbye now"


def test_simple_word_overlap_is_deduplicated():
    result = stitch_transcripts(["the quick brown fox", "brown fox jumps over"])
    assert result == "the quick brown fox jumps over"


def test_overlap_is_case_and_punctuation_insensitive():
    result = stitch_transcripts(["Hello, world.", "world. How are you"])
    assert result == "Hello, world. How are you"


def test_three_pieces_merge_sequentially():
    pieces = [
        "one two three four",
        "three four five six",
        "five six seven eight",
    ]
    assert stitch_transcripts(pieces) == "one two three four five six seven eight"


def test_blank_pieces_in_the_middle_are_skipped():
    result = stitch_transcripts(["hello there", "", "there general kenobi"])
    assert result == "hello there general kenobi"


def test_full_duplicate_piece_is_dropped():
    # the entire next piece is just a repeat of the tail of the previous one
    result = stitch_transcripts(["good morning everyone", "good morning everyone"])
    assert result == "good morning everyone"


def test_overlap_search_is_capped_by_max_overlap_words():
    # the genuine overlap is the 3-word run "charlie delta echo", but a cap
    # of 2 only lets the search compare the last/first 2 words, so it's
    # never found -- a deliberate, documented limitation of the cap.
    pieces = ["alpha bravo charlie delta echo", "charlie delta echo foxtrot"]
    assert stitch_transcripts(pieces, max_overlap_words=2) == (
        "alpha bravo charlie delta echo charlie delta echo foxtrot"
    )
    # a cap wide enough to see the whole overlap dedups it correctly
    assert stitch_transcripts(pieces, max_overlap_words=8) == (
        "alpha bravo charlie delta echo foxtrot"
    )


def test_no_false_positive_when_words_merely_recur_out_of_order():
    result = stitch_transcripts(["red blue green", "yellow green blue"])
    # "green" isn't a matching *suffix/prefix run" (order differs), so no dedup
    assert result == "red blue green yellow green blue"


# -- max_overlap_words --------------------------------------------------------


def test_max_overlap_words_scales_with_overlap_seconds():
    assert max_overlap_words(1.0) >= 3
    assert max_overlap_words(2.0) > max_overlap_words(1.0)


def test_max_overlap_words_has_a_floor_for_tiny_overlaps():
    assert max_overlap_words(0.0) == 3


# -- the unbounded chunk read ------------------------------------------------
# ChunkClock computes a chunk's right edge, but read_since used to ignore it
# and read to the present moment. Consecutive chunks therefore shared more
# audio than the schedule intended, while stitch_transcripts is told to search
# only the *nominal* overlap -- and an overlap wider than the search window is
# one it cannot find, so the repeated words were appended verbatim.


def test_read_since_honours_an_end_bound():
    ring = RingBuffer(capacity_frames=1000)
    ring.write(b"\x01\x00" * 100)
    bounded = ring.read_since(0, 40)
    assert len(bounded) == 40 * 2
    assert len(ring.read_since(0)) == 100 * 2


def test_read_since_clamps_an_end_bound_past_what_exists():
    ring = RingBuffer(capacity_frames=1000)
    ring.write(b"\x01\x00" * 10)
    assert len(ring.read_since(0, 9999)) == 10 * 2


def test_a_chunk_reads_only_its_own_range():
    """The bug, expressed in frames: two consecutive chunks must overlap by
    the scheduled amount and no more."""
    clock = ChunkClock(start_frame=0, chunk_length_frames=100, overlap_frames=20)
    first_start, first_end = clock.read_range(100)
    clock.advance(first_end)
    # Time passes while the next chunk is still pending.
    second_start, second_end = clock.read_range(260)
    assert first_end == 100
    assert second_start == first_end - 20
    overlap = first_end - second_start
    assert overlap == 20


def test_stitch_cannot_dedup_an_overlap_wider_than_its_window():
    """Why the bound matters rather than just being tidier. This is the
    duplication the unbounded read produced: the repeated span is longer than
    max_overlap_words, so it survives into the merged transcript."""
    first = "the deploy went out at nine and the migration finished at ten"
    second = "and the migration finished at ten and Priya will look at it tomorrow"
    # A window too small to span the six repeated words.
    merged_narrow = stitch_transcripts([first, second], max_overlap_words=3)
    assert merged_narrow.count("the migration finished") == 2
    # A window wide enough finds it and drops it.
    merged_wide = stitch_transcripts([first, second], max_overlap_words=8)
    assert merged_wide.count("the migration finished") == 1

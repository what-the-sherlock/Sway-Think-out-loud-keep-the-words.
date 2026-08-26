"""Tests for sway.diff -- the word-level diff behind the transform preview
panel and the dashboard's stored before/after example."""

from __future__ import annotations

from sway.diff import DiffPart, as_dicts, change_count, word_diff


def _rejoin(parts: list[DiffPart], *, op: str) -> str:
    return "".join(p.text for p in parts if p.op in ("equal", op))


def test_identical_text_is_all_equal():
    parts = word_diff("hello world", "hello world")
    assert parts == [DiffPart("equal", "hello world")]
    assert change_count(parts) == 0


def test_pure_insertion():
    parts = word_diff("hello", "hello world")
    assert parts[0] == DiffPart("equal", "hello")
    assert any(p.op == "insert" and p.text.strip() == "world" for p in parts)
    assert change_count(parts) == 1


def test_pure_deletion():
    parts = word_diff("hello world", "hello")
    assert any(p.op == "delete" and p.text.strip() == "world" for p in parts)
    assert change_count(parts) == 1


def test_replacement_counts_as_two_changes():
    parts = word_diff("make it fast", "make it slow")
    ops = [p.op for p in parts if p.op != "equal"]
    assert "delete" in ops and "insert" in ops
    assert change_count(parts) == 2


def test_empty_before_is_all_insert():
    parts = word_diff("", "new text")
    assert all(p.op != "delete" for p in parts)
    assert _rejoin(parts, op="insert") == "new text"


def test_empty_after_is_all_delete():
    parts = word_diff("old text", "")
    assert all(p.op != "insert" for p in parts)
    assert _rejoin(parts, op="delete") == "old text"


def test_both_empty_is_no_changes():
    parts = word_diff("", "")
    assert change_count(parts) == 0


def test_rejoining_equal_and_insert_parts_reproduces_after():
    before = "the quick brown fox"
    after = "the very quick brown fox jumps"
    parts = word_diff(before, after)
    assert _rejoin(parts, op="insert") == after


def test_rejoining_equal_and_delete_parts_reproduces_before():
    before = "the quick brown fox jumps"
    after = "the quick fox"
    parts = word_diff(before, after)
    assert _rejoin(parts, op="delete") == before


def test_whitespace_is_preserved_so_output_reads_naturally():
    parts = word_diff("a  b", "a  b")
    assert "".join(p.text for p in parts) == "a  b"


def test_adjacent_same_op_tokens_are_merged_into_one_part():
    # A pure insertion of "very " should not be split into a part for
    # "very" and a separate part for the trailing space.
    parts = word_diff("a fox", "a very fast fox")
    insert_parts = [p for p in parts if p.op == "insert"]
    assert len(insert_parts) == 1
    assert insert_parts[0].text.strip() == "very fast"


def test_as_dicts_shape():
    parts = word_diff("a", "b")
    dicts = as_dicts(parts)
    assert all(set(d) == {"op", "text"} for d in dicts)
    assert any(d["op"] == "delete" and d["text"] == "a" for d in dicts)
    assert any(d["op"] == "insert" and d["text"] == "b" for d in dicts)


def test_case_sensitive_word_change_registers_as_a_change():
    parts = word_diff("Hello world", "hello world")
    assert change_count(parts) == 2  # "Hello" deleted, "hello" inserted

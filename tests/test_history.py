"""Tests for the dictation/command history store."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from flow.errors import HistoryStoreError
from flow.history import KIND_COMMAND, KIND_DICTATION, HistoryStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "history.db"


def test_add_returns_and_persists_an_entry(db_path: Path):
    with HistoryStore.open(db_path) as store:
        entry = store.add(kind=KIND_DICTATION, text="hello world", raw_text="hello, world")
        assert entry.kind == KIND_DICTATION
        assert entry.text == "hello world"
        assert store.get(entry.id) == entry


def test_add_command_entry_includes_instruction_and_focused_process(db_path: Path):
    with HistoryStore.open(db_path) as store:
        entry = store.add(
            kind=KIND_COMMAND,
            text="- milk\n- eggs",
            raw_text="milk, eggs",
            instruction="make this bullet points",
            focused_process="notepad.exe",
        )
        fetched = store.get(entry.id)
        assert fetched is not None
        assert fetched.instruction == "make this bullet points"
        assert fetched.focused_process == "notepad.exe"


def test_get_missing_entry_returns_none(db_path: Path):
    with HistoryStore.open(db_path) as store:
        assert store.get("does-not-exist") is None


def test_search_with_no_query_returns_newest_first(db_path: Path):
    with HistoryStore.open(db_path) as store:
        store.add(kind=KIND_DICTATION, text="first", raw_text="first", created_at=1.0)
        store.add(kind=KIND_DICTATION, text="second", raw_text="second", created_at=2.0)
        store.add(kind=KIND_DICTATION, text="third", raw_text="third", created_at=3.0)
        results = store.search()
        assert [e.text for e in results] == ["third", "second", "first"]


def test_search_filters_by_substring_case_insensitively(db_path: Path):
    with HistoryStore.open(db_path) as store:
        store.add(kind=KIND_DICTATION, text="Buy some Milk", raw_text="buy some milk")
        store.add(kind=KIND_DICTATION, text="Call the dentist", raw_text="call the dentist")
        results = store.search("milk")
        assert [e.text for e in results] == ["Buy some Milk"]


def test_search_matches_raw_text_and_instruction_too(db_path: Path):
    with HistoryStore.open(db_path) as store:
        store.add(
            kind=KIND_COMMAND,
            text="- milk\n- eggs",
            raw_text="milk and eggs",
            instruction="make this bullet points",
        )
        assert len(store.search("bullet")) == 1
        assert len(store.search("eggs")) == 1
        assert len(store.search("nothing matches this")) == 0


def test_search_filters_by_kind(db_path: Path):
    with HistoryStore.open(db_path) as store:
        store.add(kind=KIND_DICTATION, text="a dictation", raw_text="a dictation")
        store.add(kind=KIND_COMMAND, text="a command", raw_text="selected", instruction="do it")
        assert [e.text for e in store.search(kind=KIND_DICTATION)] == ["a dictation"]
        assert [e.text for e in store.search(kind=KIND_COMMAND)] == ["a command"]


def test_search_query_with_like_wildcards_is_matched_literally(db_path: Path):
    with HistoryStore.open(db_path) as store:
        store.add(kind=KIND_DICTATION, text="100% done", raw_text="100% done")
        store.add(kind=KIND_DICTATION, text="totally done", raw_text="totally done")
        # "%" must not act as a SQL LIKE wildcard here.
        assert [e.text for e in store.search("100%")] == ["100% done"]


def test_search_respects_limit(db_path: Path):
    with HistoryStore.open(db_path) as store:
        for i in range(5):
            store.add(kind=KIND_DICTATION, text=f"entry {i}", raw_text=f"entry {i}", created_at=float(i))
        assert len(store.search(limit=2)) == 2


def test_delete_removes_an_entry(db_path: Path):
    with HistoryStore.open(db_path) as store:
        entry = store.add(kind=KIND_DICTATION, text="gone soon", raw_text="gone soon")
        assert store.delete(entry.id) is True
        assert store.get(entry.id) is None
        assert store.delete(entry.id) is False  # already gone


def test_count_reflects_entries(db_path: Path):
    with HistoryStore.open(db_path) as store:
        assert store.count() == 0
        store.add(kind=KIND_DICTATION, text="one", raw_text="one")
        store.add(kind=KIND_DICTATION, text="two", raw_text="two")
        assert store.count() == 2


def test_prune_removes_entries_older_than_retention(db_path: Path):
    with HistoryStore.open(db_path) as store:
        now = time.time()
        store.add(kind=KIND_DICTATION, text="old", raw_text="old", created_at=now - 200 * 86_400)
        store.add(kind=KIND_DICTATION, text="recent", raw_text="recent", created_at=now)
        removed = store.prune(90, now=now)
        assert removed == 1
        assert [e.text for e in store.search()] == ["recent"]


def test_prune_zero_retention_is_a_no_op(db_path: Path):
    with HistoryStore.open(db_path) as store:
        store.add(kind=KIND_DICTATION, text="entry", raw_text="entry", created_at=1.0)
        assert store.prune(0) == 0
        assert store.count() == 1


def test_entries_persist_across_reopen(db_path: Path):
    with HistoryStore.open(db_path) as store:
        store.add(kind=KIND_DICTATION, text="persisted", raw_text="persisted")
    with HistoryStore.open(db_path) as store:
        assert [e.text for e in store.search()] == ["persisted"]


def test_open_creates_parent_directories(tmp_path: Path):
    nested = tmp_path / "a" / "b" / "history.db"
    with HistoryStore.open(nested) as store:
        store.add(kind=KIND_DICTATION, text="entry", raw_text="entry")
    assert nested.exists()


def test_open_rejects_a_schema_from_a_newer_build(db_path: Path):
    with HistoryStore.open(db_path):
        pass
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version=999")
    conn.close()

    with pytest.raises(HistoryStoreError, match="newer than this build"):
        HistoryStore.open(db_path)


# -- cross-thread access ------------------------------------------------------
# Regression test: the store is opened once on the main thread at startup
# but written from the per-dictation worker thread (flow.engine.Engine.
# _record_history) and read from the Tk UI thread when the history window is
# open (flow.history_window). Without check_same_thread=False and a lock
# around every call, sqlite3 raises "SQLite objects created in a thread can
# only be used in that same thread" on every write from the worker -- which
# flow.engine only logs and swallows, so history recording silently failed
# on every single dictation.


def test_add_works_from_a_different_thread(db_path: Path):
    with HistoryStore.open(db_path) as store:
        error: list[BaseException] = []

        def write_from_worker() -> None:
            try:
                store.add(kind=KIND_DICTATION, text="from worker", raw_text="from worker")
            except BaseException as exc:  # noqa: BLE001
                error.append(exc)

        worker = threading.Thread(target=write_from_worker)
        worker.start()
        worker.join(timeout=5.0)

        assert not error, f"cross-thread write raised: {error}"
        assert [e.text for e in store.search()] == ["from worker"]


def test_search_works_from_a_different_thread(db_path: Path):
    with HistoryStore.open(db_path) as store:
        store.add(kind=KIND_DICTATION, text="hello world", raw_text="hello world")

        result: list[str] = []
        error: list[BaseException] = []

        def read_from_worker() -> None:
            try:
                result.append(",".join(e.text for e in store.search()))
            except BaseException as exc:  # noqa: BLE001
                error.append(exc)

        worker = threading.Thread(target=read_from_worker)
        worker.start()
        worker.join(timeout=5.0)

        assert not error, f"cross-thread read raised: {error}"
        assert result == ["hello world"]

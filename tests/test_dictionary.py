"""Tests for the personal dictionary store and the pure prompt-bias builder."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from flow.dictionary import DictionaryStore, build_prompt_bias
from flow.errors import DictionaryStoreError


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "dictionary.db"


# -- DictionaryStore -----------------------------------------------------------


def test_add_many_inserts_new_terms(db_path: Path):
    with DictionaryStore.open(db_path) as store:
        added = store.add_many(["GraphQL", "Kubernetes"])
        assert added == 2
        assert store.list_terms() == ["GraphQL", "Kubernetes"]


def test_add_many_ignores_duplicates(db_path: Path):
    with DictionaryStore.open(db_path) as store:
        store.add_many(["GraphQL"])
        added = store.add_many(["GraphQL", "Kubernetes"])
        assert added == 1  # only Kubernetes was new
        assert sorted(store.list_terms()) == ["GraphQL", "Kubernetes"]


def test_add_many_strips_whitespace_and_drops_blanks(db_path: Path):
    with DictionaryStore.open(db_path) as store:
        added = store.add_many(["  GraphQL  ", "", "   "])
        assert added == 1
        assert store.list_terms() == ["GraphQL"]


def test_add_many_with_no_terms_is_a_no_op(db_path: Path):
    with DictionaryStore.open(db_path) as store:
        assert store.add_many([]) == 0
        assert store.add_many(["", "  "]) == 0


def test_remove_many_deletes_existing_terms(db_path: Path):
    with DictionaryStore.open(db_path) as store:
        store.add_many(["GraphQL", "Kubernetes", "Aashutosh"])
        removed = store.remove_many(["Kubernetes", "not-there"])
        assert removed == 1
        assert sorted(store.list_terms()) == ["Aashutosh", "GraphQL"]


def test_remove_many_with_no_terms_is_a_no_op(db_path: Path):
    with DictionaryStore.open(db_path) as store:
        store.add_many(["GraphQL"])
        assert store.remove_many([]) == 0
        assert store.list_terms() == ["GraphQL"]


def test_list_terms_is_alphabetical_case_insensitive(db_path: Path):
    with DictionaryStore.open(db_path) as store:
        store.add_many(["zebra", "Apple", "banana"])
        assert store.list_terms() == ["Apple", "banana", "zebra"]


def test_terms_persist_across_reopen(db_path: Path):
    with DictionaryStore.open(db_path) as store:
        store.add_many(["GraphQL"])
    with DictionaryStore.open(db_path) as store:
        assert store.list_terms() == ["GraphQL"]


def test_open_creates_parent_directories(tmp_path: Path):
    nested = tmp_path / "a" / "b" / "dictionary.db"
    with DictionaryStore.open(nested) as store:
        store.add_many(["term"])
    assert nested.exists()


def test_open_rejects_a_schema_from_a_newer_build(db_path: Path):
    with DictionaryStore.open(db_path):
        pass
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version=999")
    conn.close()

    with pytest.raises(DictionaryStoreError, match="newer than this build"):
        DictionaryStore.open(db_path)


# -- build_prompt_bias -----------------------------------------------------------


def test_build_prompt_bias_joins_terms_with_comma_space():
    assert build_prompt_bias(["GraphQL", "Kubernetes"]) == "GraphQL, Kubernetes"


def test_build_prompt_bias_empty_terms_returns_empty_string():
    assert build_prompt_bias([]) == ""


def test_build_prompt_bias_stops_before_exceeding_max_chars():
    terms = ["alpha", "beta", "gamma", "delta"]
    # "alpha, beta" is 11 chars; adding ", gamma" (7 more) would reach 18.
    result = build_prompt_bias(terms, max_chars=15)
    assert result == "alpha, beta"
    assert len(result) <= 15


def test_build_prompt_bias_zero_max_chars_returns_empty_string():
    assert build_prompt_bias(["GraphQL"], max_chars=0) == ""


def test_build_prompt_bias_skips_blank_terms():
    assert build_prompt_bias(["GraphQL", "  ", "Kubernetes"]) == "GraphQL, Kubernetes"


def test_build_prompt_bias_never_exceeds_max_chars_even_for_one_long_term():
    # max_chars is a hard cap: a term that alone would exceed it is dropped
    # rather than truncated mid-word (which could bias toward a fragment).
    long_term = "a" * 300
    assert build_prompt_bias([long_term], max_chars=10) == ""


def test_build_prompt_bias_includes_a_term_that_exactly_fits():
    assert build_prompt_bias(["alpha"], max_chars=5) == "alpha"


# -- cross-thread access ------------------------------------------------------
# Regression test: the store is opened once on the main thread at startup
# (flow.engine.Engine.start) but read from a different worker thread on every
# single dictation (flow.engine.Engine._build_prompt_bias). Without
# check_same_thread=False and a lock around every call, sqlite3 raises
# "SQLite objects created in a thread can only be used in that same thread"
# on every read from the worker -- which flow.engine only logs and swallows,
# so the personal dictionary silently never biased a single transcription.


def test_list_terms_works_from_a_different_thread(db_path: Path):
    with DictionaryStore.open(db_path) as store:
        store.add_many(["GraphQL", "Kubernetes"])

        result: list[str] = []
        error: list[BaseException] = []

        def read_from_worker() -> None:
            try:
                result.append(",".join(store.list_terms()))
            except BaseException as exc:  # noqa: BLE001
                error.append(exc)

        worker = threading.Thread(target=read_from_worker)
        worker.start()
        worker.join(timeout=5.0)

        assert not error, f"cross-thread read raised: {error}"
        assert result == ["GraphQL,Kubernetes"]


def test_add_many_works_from_a_different_thread(db_path: Path):
    with DictionaryStore.open(db_path) as store:
        error: list[BaseException] = []

        def write_from_worker() -> None:
            try:
                store.add_many(["Anthropic"])
            except BaseException as exc:  # noqa: BLE001
                error.append(exc)

        worker = threading.Thread(target=write_from_worker)
        worker.start()
        worker.join(timeout=5.0)

        assert not error, f"cross-thread write raised: {error}"
        assert store.list_terms() == ["Anthropic"]

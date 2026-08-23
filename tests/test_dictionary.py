"""Tests for the personal dictionary store and the pure prompt-bias builder."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import sqlite3

from sway.dictionary import (
    SOURCE_LEARNED,
    SOURCE_MANUAL,
    SOURCE_SHARED,
    DictionaryEntry,
    DictionaryStore,
    build_prompt_bias,
    candidate_terms,
    prioritise,
)
from sway.errors import DictionaryStoreError


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


# ---------------------------------------------------------------------------
# v2: provenance, importance, usage, and learning
# ---------------------------------------------------------------------------


def test_a_v1_database_migrates_and_keeps_its_terms(db_path: Path) -> None:
    """The migration runs against real user data, so losing a row here means
    losing vocabulary someone typed in by hand."""
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.executescript(
        "CREATE TABLE dictionary_entries (term TEXT PRIMARY KEY, added_at REAL NOT NULL);"
    )
    conn.execute("INSERT INTO dictionary_entries VALUES (?, ?)", ("Kubernetes", 1.0))
    conn.execute("PRAGMA user_version=1")
    conn.close()

    with DictionaryStore.open(db_path) as store:
        entries = store.list_entries()
    assert [e.term for e in entries] == ["Kubernetes"]
    # A pre-existing row was added by hand and never used. Anything else would
    # be inventing history.
    assert entries[0].source == SOURCE_MANUAL
    assert entries[0].hits == 0
    assert entries[0].pinned is False


class TestPrioritise:
    """Ordering for the prompt budget. This is not cosmetic: the budget is a
    couple of hundred characters, so the order decides which terms reach the
    transcriber at all."""

    def test_pinned_terms_come_first(self) -> None:
        entries = [
            DictionaryEntry(term="Zebra", pinned=True),
            DictionaryEntry(term="Apple", hits=99),
        ]
        assert prioritise(entries)[0] == "Zebra"

    def test_then_the_terms_actually_used(self) -> None:
        entries = [
            DictionaryEntry(term="Never", hits=0),
            DictionaryEntry(term="Often", hits=12),
            DictionaryEntry(term="Sometimes", hits=3),
        ]
        assert prioritise(entries) == ["Often", "Sometimes", "Never"]

    def test_ties_break_alphabetically_so_the_order_is_stable(self) -> None:
        """A fresh dictionary has no usage at all. Without a final tiebreak the
        order would depend on row order and the prompt would change between
        runs for no reason."""
        entries = [DictionaryEntry(term=t) for t in ("Delta", "alpha", "Charlie")]
        assert prioritise(entries) == ["alpha", "Charlie", "Delta"]

    def test_the_budget_keeps_what_matters_when_it_cannot_keep_everything(self) -> None:
        entries = [
            DictionaryEntry(term="Aardvark"),
            DictionaryEntry(term="Barnacle"),
            DictionaryEntry(term="Zeppelin", pinned=True),
        ]
        # Alphabetical order would spend the whole budget before reaching the
        # pinned term. That was the v1 behaviour.
        assert build_prompt_bias(prioritise(entries), max_chars=10) == "Zeppelin"


class TestUsage:
    def test_recording_usage_is_case_insensitive(self, db_path: Path) -> None:
        """The user typed "Kubernetes"; Whisper may well produce "kubernetes"."""
        with DictionaryStore.open(db_path) as store:
            store.add_many(["Kubernetes"])
            assert store.record_usage(["kubernetes"]) == 1
            assert store.list_entries()[0].hits == 1

    def test_pinning_an_unknown_term_reports_it(self, db_path: Path) -> None:
        with DictionaryStore.open(db_path) as store:
            assert store.set_pinned("Nope", True) is False


class TestLearning:
    def test_a_term_is_learned_only_after_the_threshold(self, db_path: Path) -> None:
        with DictionaryStore.open(db_path) as store:
            assert store.observe_candidates(["Samir"], threshold=3) == []
            assert store.observe_candidates(["Samir"], threshold=3) == []
            assert store.observe_candidates(["Samir"], threshold=3) == ["Samir"]
            assert [e.source for e in store.list_entries()] == [SOURCE_LEARNED]

    def test_a_learned_term_is_not_promoted_twice(self, db_path: Path) -> None:
        with DictionaryStore.open(db_path) as store:
            for _ in range(3):
                store.observe_candidates(["Samir"], threshold=3)
            assert store.observe_candidates(["Samir"], threshold=3) == []
            assert len(store.list_entries()) == 1

    def test_removing_a_term_clears_its_learning_progress(self, db_path: Path) -> None:
        """Otherwise a word the user deliberately deleted comes straight back
        on the very next sentence, which reads as the app overruling them."""
        with DictionaryStore.open(db_path) as store:
            for _ in range(3):
                store.observe_candidates(["Samir"], threshold=3)
            store.remove_many(["Samir"])
            assert store.observe_candidates(["Samir"], threshold=3) == []

    def test_an_existing_term_keeps_its_source(self, db_path: Path) -> None:
        """A word typed by hand does not become "learned" because Sway later
        saw it, and an import never relabels someone's own vocabulary."""
        with DictionaryStore.open(db_path) as store:
            store.add_many(["Paddle"], source=SOURCE_MANUAL)
            store.add_many(["Paddle"], source=SOURCE_SHARED)
            assert store.list_entries()[0].source == SOURCE_MANUAL


class TestCandidateTerms:
    def test_finds_mid_sentence_names_and_acronyms(self) -> None:
        found = candidate_terms("We shipped Paddle for Samir using the GDPR endpoint.")
        assert "Paddle" in found
        assert "Samir" in found
        assert "GDPR" in found

    def test_ignores_sentence_openers(self) -> None:
        """Whisper capitalises the first word of every sentence. Without this
        every dictation would propose its own opening word as a proper noun."""
        assert candidate_terms("Deploy the service. Everything passed.") == []

    def test_ignores_words_already_known(self) -> None:
        assert candidate_terms("We use Paddle here.", known=["paddle"]) == []

    def test_ignores_grammatical_capitals(self) -> None:
        assert candidate_terms("I think Monday works, and I will confirm.") == []

    def test_does_not_propose_bare_numbers(self) -> None:
        assert candidate_terms("Invoice 4471 was paid on the 9th.") == []

    def test_reports_each_word_once(self) -> None:
        assert candidate_terms("Samir met Samir and Samir.") == ["Samir"]

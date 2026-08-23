"""The personal dictionary (Phase 3): proper nouns and jargon the user adds
via ``sway dictionary`` or the Dictionary page, injected into Whisper's
``prompt`` parameter to bias transcription toward them.

:class:`DictionaryStore` mirrors :class:`~sway.latency.store.LatencyStore`'s
open/migrate/close shape at a much smaller scale. The pure functions --
:func:`prioritise`, :func:`build_prompt_bias`, :func:`candidate_terms` -- hold
no SQLite and are directly unit-testable, per CLAUDE.md's rule to test the
transcript-processing functions.

Three things the v1 store could not express, and why each matters:

* **Which terms are worth the budget.** Whisper attends to roughly its last
  ~224 tokens, so the prompt is capped at a couple of hundred characters. v1
  filled that cap *alphabetically*, which is arbitrary: a user with 300 terms
  got the ones starting with "A" and nothing else. Terms are now ordered by
  what the user pinned, then by what they actually say (see :func:`prioritise`).
* **Where a term came from.** A term typed by hand, one Sway learned, and one
  imported from a shared pack are all legitimate, but they need different
  treatment in the UI -- and the user has to be able to see and remove what was
  added on their behalf.
* **Learning.** :func:`candidate_terms` proposes words from what was actually
  dictated; ``learning_candidates`` counts them, and only a term said several
  times is promoted. One-off mishearings never make it in.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from sway.errors import DictionaryStoreError

logger = logging.getLogger(__name__)

#: Bump together with a migration in ``_migrate``.
SCHEMA_VERSION = 2

#: Where a term came from. Contract with the UI's filter row.
SOURCE_MANUAL = "manual"
SOURCE_LEARNED = "learned"
SOURCE_SHARED = "shared"
SOURCES = (SOURCE_MANUAL, SOURCE_LEARNED, SOURCE_SHARED)

#: How many times a word must be dictated before it is learned. Three is
#: deliberately low enough to be useful within a day's work and high enough
#: that a single mis-transcription never becomes a permanent bias -- and a
#: learned term is visible and removable either way.
LEARN_THRESHOLD = 3

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS dictionary_entries (
    term      TEXT    PRIMARY KEY,
    added_at  REAL    NOT NULL
);
"""

#: v2 adds provenance, importance, and usage. Every column is added with a
#: default, so existing rows migrate to "added by hand, never used" -- which is
#: exactly what they were.
_MIGRATION_V2 = """
ALTER TABLE dictionary_entries ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE dictionary_entries ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE dictionary_entries ADD COLUMN hits INTEGER NOT NULL DEFAULT 0;
ALTER TABLE dictionary_entries ADD COLUMN last_used_at REAL;

CREATE TABLE IF NOT EXISTS learning_candidates (
    term          TEXT    PRIMARY KEY,
    occurrences   INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL    NOT NULL,
    last_seen_at  REAL    NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class DictionaryEntry:
    """One term, with everything the UI and the prompt budget need."""

    term: str
    added_at: float = 0.0
    source: str = SOURCE_MANUAL
    pinned: bool = False
    hits: int = 0
    last_used_at: float | None = None


class DictionaryStore:
    """Reads and writes personal dictionary terms. Use as a context manager."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._closed = False
        #: Serialises access, because the connection is shared across
        #: threads -- see :meth:`open` for why it has to be.
        self._lock = threading.RLock()

    # -- lifecycle -----------------------------------------------------

    @classmethod
    def open(cls, path: Path | str) -> Self:
        """Open (creating if needed) the dictionary database at ``path``.

        ``check_same_thread=False`` is required, not merely convenient: the
        store is opened once on the main thread at startup but read on the
        per-dictation worker thread that builds Whisper's prompt bias.
        Without it, sqlite3 refuses every call from that worker
        ("SQLite objects created in a thread can only be used in that same
        thread"), which silently disabled the personal dictionary on every
        single dictation. Every method holds :attr:`_lock` to make the
        sharing safe.
        """
        target = Path(path)
        try:
            if target.parent and str(target.parent) not in ("", "."):
                target.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(target, isolation_level=None, check_same_thread=False)
        except (OSError, sqlite3.Error) as exc:
            raise DictionaryStoreError(f"could not open dictionary database {target}: {exc}") from exc

        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            store = cls(conn)
            store._migrate()
        except sqlite3.Error as exc:
            conn.close()
            raise DictionaryStoreError(f"could not initialise {target}: {exc}") from exc
        return store

    def _migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise DictionaryStoreError(
                f"dictionary database schema v{version} is newer than this build "
                f"(v{SCHEMA_VERSION}); upgrade Sway or delete the database"
            )
        if version == SCHEMA_VERSION:
            return
        # v0 -> v1 is initial creation. Future migrations append here.
        if version < 1:
            self._conn.executescript(_SCHEMA_V1)
        if version < 2:
            self._conn.executescript(_MIGRATION_V2)
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- writing ---------------------------------------------------------

    def add_many(self, terms: Sequence[str], *, source: str = SOURCE_MANUAL) -> int:
        """Insert terms not already present (exact match). Returns how many were new.

        An existing term keeps the source it already had: a word the user
        typed by hand does not become "learned" because Sway later saw it, and
        importing a shared pack never relabels someone's own vocabulary.
        """
        cleaned = [t.strip() for t in terms if t.strip()]
        if not cleaned:
            return 0
        if source not in SOURCES:
            raise ValueError(f"unknown source {source!r}")
        now = time.time()
        try:
            with self._lock, _transaction(self._conn):
                before = self._count()
                self._conn.executemany(
                    "INSERT OR IGNORE INTO dictionary_entries (term, added_at, source) "
                    "VALUES (?, ?, ?)",
                    [(term, now, source) for term in cleaned],
                )
                after = self._count()
        except sqlite3.Error as exc:
            raise DictionaryStoreError(f"could not add dictionary terms: {exc}") from exc
        return after - before

    def remove_many(self, terms: Sequence[str]) -> int:
        """Delete terms (exact match). Returns how many existed and were removed.

        Also forgets any learning progress for the term. Without that, a word
        the user deliberately removed would be re-learned the next time they
        said it a few times, which reads as the app overruling them.
        """
        cleaned = [t.strip() for t in terms if t.strip()]
        if not cleaned:
            return 0
        try:
            with self._lock, _transaction(self._conn):
                cursor = self._conn.executemany(
                    "DELETE FROM dictionary_entries WHERE term = ?", [(t,) for t in cleaned]
                )
                removed = max(cursor.rowcount, 0)
                self._conn.executemany(
                    "DELETE FROM learning_candidates WHERE term = ? COLLATE NOCASE",
                    [(t,) for t in cleaned],
                )
                return removed
        except sqlite3.Error as exc:
            raise DictionaryStoreError(f"could not remove dictionary terms: {exc}") from exc

    def set_pinned(self, term: str, pinned: bool) -> bool:
        """Mark a term important, so it survives the prompt budget. Returns
        whether the term existed."""
        try:
            with self._lock, _transaction(self._conn):
                cursor = self._conn.execute(
                    "UPDATE dictionary_entries SET pinned = ? WHERE term = ?",
                    (1 if pinned else 0, term.strip()),
                )
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise DictionaryStoreError(f"could not update {term!r}: {exc}") from exc

    def record_usage(self, terms: Iterable[str], *, now: float | None = None) -> int:
        """Count that these terms were actually dictated.

        Drives the prompt budget's ordering: with more terms than fit, the
        ones the user really says are the ones worth biasing toward. Matched
        case-insensitively, because the user typed "Kubernetes" and Whisper
        may well have produced "kubernetes".
        """
        wanted = [t.strip() for t in terms if t.strip()]
        if not wanted:
            return 0
        stamp = time.time() if now is None else now
        try:
            with self._lock, _transaction(self._conn):
                cursor = self._conn.executemany(
                    "UPDATE dictionary_entries SET hits = hits + 1, last_used_at = ? "
                    "WHERE term = ? COLLATE NOCASE",
                    [(stamp, term) for term in wanted],
                )
                return max(cursor.rowcount, 0)
        except sqlite3.Error as exc:
            raise DictionaryStoreError(f"could not record dictionary usage: {exc}") from exc

    def observe_candidates(
        self, terms: Sequence[str], *, threshold: int = LEARN_THRESHOLD, now: float | None = None
    ) -> list[str]:
        """Count candidate words, and promote any that cross ``threshold``.

        Returns the terms promoted by this call, so the caller can log or
        surface them. A word already in the dictionary is ignored.

        Removing a term resets its count rather than blocking it forever (see
        :meth:`remove_many`), so a word removed by mistake can be learned
        again -- but only after the full threshold, never on the next
        sentence. A permanent block was considered and rejected: it would make
        one stray click unfixable except by hand.
        """
        cleaned = [t.strip() for t in terms if t.strip()]
        if not cleaned or threshold < 1:
            return []
        stamp = time.time() if now is None else now
        try:
            with self._lock, _transaction(self._conn):
                known = {
                    row["term"].lower()
                    for row in self._conn.execute("SELECT term FROM dictionary_entries")
                }
                fresh = [t for t in cleaned if t.lower() not in known]
                if not fresh:
                    return []
                self._conn.executemany(
                    "INSERT INTO learning_candidates "
                    "  (term, occurrences, first_seen_at, last_seen_at) VALUES (?, 1, ?, ?) "
                    "ON CONFLICT(term) DO UPDATE SET "
                    "  occurrences = occurrences + 1, last_seen_at = excluded.last_seen_at",
                    [(term, stamp, stamp) for term in fresh],
                )
                rows = self._conn.execute(
                    "SELECT term FROM learning_candidates WHERE occurrences >= ?", (threshold,)
                ).fetchall()
                promoted = [row["term"] for row in rows]
                if promoted:
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO dictionary_entries "
                        "  (term, added_at, source) VALUES (?, ?, ?)",
                        [(term, stamp, SOURCE_LEARNED) for term in promoted],
                    )
                    self._conn.executemany(
                        "DELETE FROM learning_candidates WHERE term = ?",
                        [(term,) for term in promoted],
                    )
                return promoted
        except sqlite3.Error as exc:
            raise DictionaryStoreError(f"could not record learning candidates: {exc}") from exc

    # -- reading -----------------------------------------------------------

    def list_terms(self) -> list[str]:
        """Every term, alphabetically (case-insensitive).

        Kept for the CLI and for callers that only want the words.
        """
        return [entry.term for entry in self.list_entries()]

    def list_entries(self) -> list[DictionaryEntry]:
        """Every term with its provenance and usage, alphabetically.

        Alphabetical because this feeds the UI, where a list that reorders
        itself as you use it is unreadable. :func:`prioritise` is what orders
        for the prompt budget.
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT term, added_at, source, pinned, hits, last_used_at "
                    "FROM dictionary_entries ORDER BY term COLLATE NOCASE"
                ).fetchall()
        except sqlite3.Error as exc:
            raise DictionaryStoreError(f"could not read dictionary terms: {exc}") from exc
        return [
            DictionaryEntry(
                term=row["term"],
                added_at=row["added_at"],
                source=row["source"] or SOURCE_MANUAL,
                pinned=bool(row["pinned"]),
                hits=int(row["hits"] or 0),
                last_used_at=row["last_used_at"],
            )
            for row in rows
        ]

    def _count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM dictionary_entries").fetchone()[0])


class _transaction:
    """Explicit transaction for a connection in autocommit mode."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type: object, *_rest: object) -> None:
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")


# ---------------------------------------------------------------------------
# Pure functions: ordering, budgeting, and learning
# ---------------------------------------------------------------------------


def prioritise(entries: Sequence[DictionaryEntry]) -> list[str]:
    """Order terms by how much they deserve the prompt budget.

    Pinned first (the user said these matter), then by how often they are
    actually dictated, then by how recently, then alphabetically so the result
    is stable for a dictionary where nothing has been used yet.

    This exists because the budget is a couple of hundred characters and a real
    dictionary outgrows it. Filling it alphabetically -- what shipped -- meant a
    user with 300 terms was biasing transcription toward whichever ones happened
    to start with "A".
    """
    return [
        entry.term
        for entry in sorted(
            entries,
            key=lambda e: (
                not e.pinned,
                -e.hits,
                -(e.last_used_at or 0.0),
                e.term.lower(),
            ),
        )
    ]


def build_prompt_bias(terms: Sequence[str], *, max_chars: int = 200) -> str:
    """Join dictionary terms into a string for Whisper's ``prompt`` parameter.

    Whisper's prompt biases transcription toward words that appear in it, but
    the model only attends to roughly its last ~224 tokens. Rather than
    truncate mid-term (which could bias toward a fragment) or drop the whole
    dictionary once it grows past that (via a naive length check), this stops
    adding terms as soon as the next one would exceed ``max_chars``.

    Pass an already-prioritised list (see :func:`prioritise`); this function
    preserves the order it is given, so whatever matters most must come first.
    """
    if max_chars <= 0 or not terms:
        return ""
    parts: list[str] = []
    length = 0
    for raw in terms:
        term = raw.strip()
        if not term:
            continue
        added_len = len(term) + (2 if parts else 0)  # ", " separator
        if length + added_len > max_chars:
            break
        parts.append(term)
        length += added_len
    return ", ".join(parts)


#: A candidate word. Requires a letter somewhere, so bare numbers and
#: punctuation never become vocabulary.
_CANDIDATE_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’.\-]*")

#: Words that are capitalised for grammatical reasons rather than because they
#: name anything. Without these, "I", "The" and every weekday become learned
#: vocabulary within a day and crowd out the terms that matter.
_COMMON_CAPITALS = frozenset(
    "i i'm i'll i've i'd a an the this that these those and or but if then so "
    "he she it we they you my your our their his her its me him her us them "
    "monday tuesday wednesday thursday friday saturday sunday "
    "january february march april may june july august september october "
    "november december today tomorrow yesterday "
    "yes no ok okay hi hey hello thanks thank please sorry actually wait well "
    "what when where who why how is are was were be been do does did have has "
    "had will would can could should let lets there here also just now not".split()
)


def candidate_terms(text: str, *, known: Iterable[str] = ()) -> list[str]:
    """Words in ``text`` worth proposing as vocabulary.

    Two signals, both cheap and both conservative:

    * a **mid-sentence capital** -- a name, product, or client, since Whisper
      capitalises sentence openers regardless and those are excluded;
    * an **all-caps run** of two or more letters -- an acronym.

    Deliberately not "every unusual word": a stemmer or frequency list would
    propose far more, and the cost of a wrong entry is that transcription is
    biased toward a mishearing forever. Being narrow here is the point, and the
    occurrence threshold in :meth:`DictionaryStore.observe_candidates` is the
    second gate.
    """
    seen_lower = {t.strip().lower() for t in known if t.strip()}
    found: list[str] = []
    emitted: set[str] = set()

    for match in _CANDIDATE_RE.finditer(text):
        word = match.group(0).strip(".-'’")
        if len(word) < 2:
            continue
        lowered = word.lower()
        if lowered in _COMMON_CAPITALS or lowered in seen_lower or lowered in emitted:
            continue

        is_acronym = word.isupper() and word.isalpha()
        is_name = word[0].isupper() and not _starts_sentence(text, match.start())
        if not (is_acronym or is_name):
            continue

        found.append(word)
        emitted.add(lowered)
    return found


def _starts_sentence(text: str, index: int) -> bool:
    """Whether the word at ``index`` opens the text or follows a full stop.

    Whisper capitalises the first word of every sentence, so without this every
    sentence opener in every dictation would be proposed as a proper noun.
    """
    for char in reversed(text[:index]):
        if char.isspace():
            continue
        return char in ".!?…"
    return True


__all__ = [
    "LEARN_THRESHOLD",
    "SCHEMA_VERSION",
    "SOURCES",
    "SOURCE_LEARNED",
    "SOURCE_MANUAL",
    "SOURCE_SHARED",
    "DictionaryEntry",
    "DictionaryStore",
    "build_prompt_bias",
    "candidate_terms",
    "prioritise",
]

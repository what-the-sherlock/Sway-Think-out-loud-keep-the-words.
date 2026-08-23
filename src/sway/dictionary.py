"""The personal dictionary (Phase 3): proper nouns and jargon the user adds
via ``flow dictionary``, injected into Whisper's ``prompt`` parameter to bias
transcription toward them.

:class:`DictionaryStore` mirrors :class:`~flow.latency.store.LatencyStore`'s
open/migrate/close shape at a much smaller scale -- one table, no pruning.
:func:`build_prompt_bias` is kept pure (no SQLite) so it is directly
unit-testable, per CLAUDE.md's rule to test the transcript-processing
functions.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Self

from sway.errors import DictionaryStoreError

logger = logging.getLogger(__name__)

#: Bump together with a migration in ``_migrate``.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dictionary_entries (
    term      TEXT    PRIMARY KEY,
    added_at  REAL    NOT NULL
);
"""


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
        self._conn.executescript(_SCHEMA)
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

    def add_many(self, terms: Sequence[str]) -> int:
        """Insert terms not already present (exact match). Returns how many were new."""
        cleaned = [t.strip() for t in terms if t.strip()]
        if not cleaned:
            return 0
        now = time.time()
        try:
            with self._lock, _transaction(self._conn):
                before = self._count()
                self._conn.executemany(
                    "INSERT OR IGNORE INTO dictionary_entries (term, added_at) VALUES (?, ?)",
                    [(term, now) for term in cleaned],
                )
                after = self._count()
        except sqlite3.Error as exc:
            raise DictionaryStoreError(f"could not add dictionary terms: {exc}") from exc
        return after - before

    def remove_many(self, terms: Sequence[str]) -> int:
        """Delete terms (exact match). Returns how many existed and were removed."""
        cleaned = [t.strip() for t in terms if t.strip()]
        if not cleaned:
            return 0
        try:
            with self._lock, _transaction(self._conn):
                cursor = self._conn.executemany(
                    "DELETE FROM dictionary_entries WHERE term = ?", [(t,) for t in cleaned]
                )
                return max(cursor.rowcount, 0)
        except sqlite3.Error as exc:
            raise DictionaryStoreError(f"could not remove dictionary terms: {exc}") from exc

    # -- reading -----------------------------------------------------------

    def list_terms(self) -> list[str]:
        """Every term, alphabetically (case-insensitive)."""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT term FROM dictionary_entries ORDER BY term COLLATE NOCASE"
                ).fetchall()
        except sqlite3.Error as exc:
            raise DictionaryStoreError(f"could not read dictionary terms: {exc}") from exc
        return [row["term"] for row in rows]

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


def build_prompt_bias(terms: Sequence[str], *, max_chars: int = 200) -> str:
    """Join dictionary terms into a string for Whisper's ``prompt`` parameter.

    Whisper's prompt biases transcription toward words that appear in it, but
    the model only attends to roughly its last ~224 tokens. Rather than
    truncate mid-term (which could bias toward a fragment) or drop the whole
    dictionary once it grows past that (via a naive length check), this stops
    adding terms as soon as the next one would exceed ``max_chars``.
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


__all__ = ["SCHEMA_VERSION", "DictionaryStore", "build_prompt_bias"]

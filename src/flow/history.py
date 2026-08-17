"""The dictation/command history (Phase 5): every successful dictation and
voice command, persisted so the searchable history window (see
:mod:`flow.history_window`) and ``flow history list`` have something to show.

:class:`HistoryStore` mirrors :class:`~flow.dictionary.DictionaryStore`'s
open/migrate/close shape. Search is a plain ``LIKE`` scan rather than SQLite's
FTS5 extension: a personal dictation history is thousands of rows at most, far
below where substring scanning needs an index, and it avoids depending on an
SQLite build option (FTS5 is not guaranteed present on every Python
distribution) for a feature this small.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from flow.errors import HistoryStoreError

logger = logging.getLogger(__name__)

#: Bump together with a migration in ``_migrate``.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history_entries (
    id              TEXT    PRIMARY KEY,
    created_at      REAL    NOT NULL,
    kind            TEXT    NOT NULL,
    text            TEXT    NOT NULL,
    raw_text        TEXT    NOT NULL,
    instruction     TEXT,
    focused_process TEXT
);

CREATE INDEX IF NOT EXISTS idx_history_created_at ON history_entries(created_at);
"""

#: [history].kind values.
KIND_DICTATION = "dictation"
KIND_COMMAND = "command"


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One past dictation or voice command."""

    id: str
    created_at: float
    kind: str
    #: What was actually pasted (the cleaned-up transcript, or the command's
    #: transformed output).
    text: str
    #: What preceded it: the raw transcript for a dictation, or the original
    #: selected text for a command.
    raw_text: str
    #: The spoken instruction, for a command entry only.
    instruction: str | None = None
    focused_process: str | None = None


class HistoryStore:
    """Reads and writes dictation/command history. Use as a context manager."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._closed = False
        #: Serialises access, because the connection is shared across
        #: threads -- see :meth:`open` for why it has to be.
        self._lock = threading.RLock()

    # -- lifecycle -----------------------------------------------------

    @classmethod
    def open(cls, path: Path | str) -> Self:
        """Open (creating if needed) the history database at ``path``.

        ``check_same_thread=False`` is required, not merely convenient: the
        store is opened once on the main thread at startup, written from the
        per-dictation/-command worker thread via :meth:`add`, and read from
        the Tk UI thread when the history window is open. Without it, sqlite3
        refuses every call from a thread other than the one that opened the
        connection ("SQLite objects created in a thread can only be used in
        that same thread"), which silently disabled history recording on
        every single dictation. Every method holds :attr:`_lock` to make the
        sharing safe.
        """
        target = Path(path)
        try:
            if target.parent and str(target.parent) not in ("", "."):
                target.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(target, isolation_level=None, check_same_thread=False)
        except (OSError, sqlite3.Error) as exc:
            raise HistoryStoreError(f"could not open history database {target}: {exc}") from exc

        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            store = cls(conn)
            store._migrate()
        except sqlite3.Error as exc:
            conn.close()
            raise HistoryStoreError(f"could not initialise {target}: {exc}") from exc
        return store

    def _migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise HistoryStoreError(
                f"history database schema v{version} is newer than this build "
                f"(v{SCHEMA_VERSION}); upgrade Flow or delete the database"
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

    def add(
        self,
        *,
        kind: str,
        text: str,
        raw_text: str,
        instruction: str | None = None,
        focused_process: str | None = None,
        created_at: float | None = None,
    ) -> HistoryEntry:
        """Record one dictation or command. Returns the stored entry."""
        entry = HistoryEntry(
            id=uuid.uuid4().hex,
            created_at=time.time() if created_at is None else created_at,
            kind=kind,
            text=text,
            raw_text=raw_text,
            instruction=instruction,
            focused_process=focused_process,
        )
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO history_entries"
                    " (id, created_at, kind, text, raw_text, instruction, focused_process)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.id,
                        entry.created_at,
                        entry.kind,
                        entry.text,
                        entry.raw_text,
                        entry.instruction,
                        entry.focused_process,
                    ),
                )
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"could not record history entry: {exc}") from exc
        return entry

    def delete(self, entry_id: str) -> bool:
        """Remove one entry. Returns True if it existed."""
        try:
            with self._lock:
                cursor = self._conn.execute("DELETE FROM history_entries WHERE id = ?", (entry_id,))
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"could not delete history entry: {exc}") from exc
        return cursor.rowcount > 0

    def prune(self, retention_days: int, *, now: float | None = None) -> int:
        """Delete entries older than ``retention_days``. Returns rows removed."""
        if retention_days <= 0:
            return 0
        cutoff = (time.time() if now is None else now) - retention_days * 86_400
        try:
            with self._lock:
                cursor = self._conn.execute(
                    "DELETE FROM history_entries WHERE created_at < ?", (cutoff,)
                )
            return cursor.rowcount or 0
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"could not prune history: {exc}") from exc

    # -- reading -----------------------------------------------------------

    def search(
        self,
        query: str = "",
        *,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[HistoryEntry]:
        """Entries newest-first, optionally filtered by ``kind`` and a
        case-insensitive substring ``query`` matched against the pasted text,
        the raw text, and the instruction.
        """
        where: list[str] = []
        params: list[str] = []
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        query = query.strip()
        if query:
            where.append("(text LIKE ? ESCAPE '\\' OR raw_text LIKE ? ESCAPE '\\' OR instruction LIKE ? ESCAPE '\\')")
            pattern = f"%{_escape_like(query)}%"
            params.extend([pattern, pattern, pattern])

        sql = "SELECT * FROM history_entries"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(str(max(0, limit)))

        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"could not search history: {exc}") from exc
        return [_row_to_entry(row) for row in rows]

    def get(self, entry_id: str) -> HistoryEntry | None:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM history_entries WHERE id = ?", (entry_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"could not read history entry: {exc}") from exc
        return _row_to_entry(row) if row is not None else None

    def count(self) -> int:
        try:
            with self._lock:
                return int(
                    self._conn.execute("SELECT COUNT(*) FROM history_entries").fetchone()[0]
                )
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"could not count history entries: {exc}") from exc


def _escape_like(text: str) -> str:
    """Escape SQLite LIKE metacharacters so a search term is matched literally."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_entry(row: sqlite3.Row) -> HistoryEntry:
    return HistoryEntry(
        id=row["id"],
        created_at=row["created_at"],
        kind=row["kind"],
        text=row["text"],
        raw_text=row["raw_text"],
        instruction=row["instruction"],
        focused_process=row["focused_process"],
    )


__all__ = [
    "KIND_COMMAND",
    "KIND_DICTATION",
    "SCHEMA_VERSION",
    "HistoryEntry",
    "HistoryStore",
]

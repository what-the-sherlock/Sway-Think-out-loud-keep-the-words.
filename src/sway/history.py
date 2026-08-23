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

from flow.analytics import SessionMetrics, count_words
from flow.errors import HistoryStoreError

logger = logging.getLogger(__name__)

#: Bump together with a migration in ``_migrate``.
#: v2 adds the product-layer columns: the ``run_id`` join key into
#: latency.db, the derived per-session metrics (see :mod:`flow.analytics`),
#: and the corrections table.
SCHEMA_VERSION = 2

_SCHEMA_V1 = """
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

#: v1 -> v2. Every ADD COLUMN is nullable with no default, so existing rows
#: keep working and simply report "not available" for the metrics that were
#: never measured for them -- which is the honest answer, not zero.
_MIGRATION_V2 = """
ALTER TABLE history_entries ADD COLUMN run_id TEXT;
ALTER TABLE history_entries ADD COLUMN word_count INTEGER;
ALTER TABLE history_entries ADD COLUMN character_count INTEGER;
ALTER TABLE history_entries ADD COLUMN audio_ms REAL;
ALTER TABLE history_entries ADD COLUMN speech_ms REAL;
ALTER TABLE history_entries ADD COLUMN wpm REAL;
ALTER TABLE history_entries ADD COLUMN time_saved_s REAL;
ALTER TABLE history_entries ADD COLUMN language TEXT;
ALTER TABLE history_entries ADD COLUMN status TEXT;

CREATE INDEX IF NOT EXISTS idx_history_run_id ON history_entries(run_id);

CREATE TABLE IF NOT EXISTS corrections (
    id         TEXT PRIMARY KEY,
    entry_id   TEXT REFERENCES history_entries(id) ON DELETE CASCADE,
    original   TEXT NOT NULL,
    corrected  TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_corrections_original ON corrections(original);
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
    #: Joins to ``runs.id`` in latency.db, giving this entry its full stage
    #: breakdown. ``None`` for entries written before schema v2.
    run_id: str | None = None
    #: Derived metrics, computed once at record time by
    #: :func:`flow.analytics.session_metrics` because they depend on audio the
    #: app deliberately does not keep. ``None`` means "never measured" and
    #: must be rendered as such, never as zero.
    word_count: int | None = None
    character_count: int | None = None
    audio_ms: float | None = None
    speech_ms: float | None = None
    wpm: float | None = None
    time_saved_s: float | None = None
    language: str | None = None
    status: str | None = None


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
        # Migrations run in order from whatever version this database is at.
        # executescript manages its own transaction per call; each step is
        # idempotent (IF NOT EXISTS) except the ALTERs, which only run on the
        # one version step that introduces them.
        if version < 1:
            self._conn.executescript(_SCHEMA_V1)
        if version < 2:
            self._conn.executescript(_MIGRATION_V2)
            self._backfill_v2_counts()
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _backfill_v2_counts(self) -> None:
        """Recover the v2 metrics that are derivable from data already stored.

        Word and character counts come straight from the saved text, so old
        entries can report them truthfully. Speaking rate and time saved
        cannot: both need the clip's speech duration, which pre-v2 builds
        never recorded and which cannot be reconstructed from text. Those stay
        NULL, and the dashboard shows them as unavailable -- inventing a
        plausible-looking number for them is exactly the failure mode this
        product must not have.
        """
        rows = self._conn.execute("SELECT id, text FROM history_entries").fetchall()
        self._conn.executemany(
            "UPDATE history_entries SET word_count = ?, character_count = ? WHERE id = ?",
            [(count_words(row["text"]), len(row["text"]), row["id"]) for row in rows],
        )

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
        run_id: str | None = None,
        metrics: "SessionMetrics | None" = None,
        language: str | None = None,
        status: str | None = None,
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
            run_id=run_id,
            word_count=metrics.word_count if metrics else None,
            character_count=metrics.character_count if metrics else None,
            audio_ms=metrics.audio_ms if metrics else None,
            speech_ms=metrics.speech_ms if metrics else None,
            wpm=metrics.wpm if metrics else None,
            time_saved_s=metrics.time_saved_s if metrics else None,
            language=language,
            status=status,
        )
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO history_entries"
                    " (id, created_at, kind, text, raw_text, instruction, focused_process,"
                    "  run_id, word_count, character_count, audio_ms, speech_ms, wpm,"
                    "  time_saved_s, language, status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.id,
                        entry.created_at,
                        entry.kind,
                        entry.text,
                        entry.raw_text,
                        entry.instruction,
                        entry.focused_process,
                        entry.run_id,
                        entry.word_count,
                        entry.character_count,
                        entry.audio_ms,
                        entry.speech_ms,
                        entry.wpm,
                        entry.time_saved_s,
                        entry.language,
                        entry.status,
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


    # -- corrections -----------------------------------------------------

    def add_correction(
        self,
        *,
        original: str,
        corrected: str,
        entry_id: str | None = None,
        created_at: float | None = None,
    ) -> str:
        """Record that ``original`` was fixed to ``corrected``. Returns the id.

        Recorded explicitly (from the session detail view) rather than
        inferred: Flow cannot see what the user edits after the paste lands
        in another application, and guessing from clipboard activity would
        manufacture data the dashboard then presents as measured fact.
        """
        correction_id = uuid.uuid4().hex
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO corrections (id, entry_id, original, corrected, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        correction_id,
                        entry_id,
                        original,
                        corrected,
                        time.time() if created_at is None else created_at,
                    ),
                )
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"could not record correction: {exc}") from exc
        return correction_id

    def frequent_corrections(self, *, limit: int = 20) -> list[tuple[str, str, int]]:
        """``(original, corrected, count)`` triples, most-corrected first --
        the raw material for the "add all to dictionary" suggestion.
        """
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT original, corrected, COUNT(*) AS n FROM corrections"
                    " GROUP BY original, corrected ORDER BY n DESC, original ASC LIMIT ?",
                    (max(0, limit),),
                ).fetchall()
        except sqlite3.Error as exc:
            raise HistoryStoreError(f"could not read corrections: {exc}") from exc
        return [(row["original"], row["corrected"], int(row["n"])) for row in rows]


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
        run_id=row["run_id"],
        word_count=row["word_count"],
        character_count=row["character_count"],
        audio_ms=row["audio_ms"],
        speech_ms=row["speech_ms"],
        wpm=row["wpm"],
        time_saved_s=row["time_saved_s"],
        language=row["language"],
        status=row["status"],
    )


__all__ = [
    "KIND_COMMAND",
    "KIND_DICTATION",
    "SCHEMA_VERSION",
    "HistoryEntry",
    "HistoryStore",
]

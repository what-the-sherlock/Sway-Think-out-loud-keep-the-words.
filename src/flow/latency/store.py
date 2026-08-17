"""SQLite persistence for latency records.

Threading contract: a :class:`LatencyStore` owns one ``sqlite3.Connection`` and
must be used from the thread that created it. :class:`~flow.latency.tracker.LatencyTracker`
keeps its writer instance confined to the background writer thread; readers
(the ``flow report`` command) open their own short-lived instance.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Self

from flow.errors import LatencyStoreError
from flow.latency.models import RunRecord, StageTiming

logger = logging.getLogger(__name__)

#: Bump together with a migration in ``_migrate``.
SCHEMA_VERSION = 1

#: SQLite's default parameter limit is 999 on older builds; stay well under it.
_ID_CHUNK = 400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT    PRIMARY KEY,
    started_at  REAL    NOT NULL,
    total_ms    REAL    NOT NULL,
    ok          INTEGER NOT NULL,
    error       TEXT,
    metadata    TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS stage_timings (
    run_id      TEXT    NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    stage       TEXT    NOT NULL,
    started_at  REAL    NOT NULL,
    duration_ms REAL    NOT NULL,
    ok          INTEGER NOT NULL,
    PRIMARY KEY (run_id, seq)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_stage_timings_stage ON stage_timings(stage);
"""


class LatencyStore:
    """Reads and writes latency runs. Single-threaded; use as a context manager."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._conn = connection
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, path: Path | str) -> Self:
        """Open (creating if needed) the latency database at ``path``."""
        target = Path(path)
        try:
            if target.parent and str(target.parent) not in ("", "."):
                target.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(target, isolation_level=None)
        except (OSError, sqlite3.Error) as exc:
            raise LatencyStoreError(f"could not open latency database {target}: {exc}") from exc

        try:
            conn.row_factory = sqlite3.Row
            # WAL keeps the writer from blocking readers (`flow report` while the
            # app runs). NORMAL trades a crash-window of telemetry for not
            # fsyncing on the latency-sensitive path — the right trade here.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            store = cls(conn)
            store._migrate()
        except sqlite3.Error as exc:
            conn.close()
            raise LatencyStoreError(f"could not initialise {target}: {exc}") from exc
        return store

    def _migrate(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise LatencyStoreError(
                f"latency database schema v{version} is newer than this build "
                f"(v{SCHEMA_VERSION}); upgrade Flow or delete the database"
            )
        if version == SCHEMA_VERSION:
            return
        # v0 -> v1 is initial creation. Future migrations append here.
        # The DDL is idempotent (IF NOT EXISTS) and executescript manages its
        # own transaction, so this is safe to race with another process.
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- writing -----------------------------------------------------------

    def insert_runs(self, runs: Sequence[RunRecord]) -> None:
        """Persist a batch of runs in a single transaction."""
        if not runs:
            return
        run_rows = [
            (r.id, r.started_at, r.total_ms, int(r.ok), r.error, _dump_metadata(r.id, r.metadata))
            for r in runs
        ]
        stage_rows = [
            (r.id, s.seq, str(s.stage), s.started_at, s.duration_ms, int(s.ok))
            for r in runs
            for s in r.stages
        ]
        try:
            with _transaction(self._conn):
                # Re-recording the same run id is a programming error, not data
                # worth losing; REPLACE keeps the writer thread alive either way.
                self._conn.executemany(
                    "INSERT OR REPLACE INTO runs"
                    " (id, started_at, total_ms, ok, error, metadata)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    run_rows,
                )
                self._conn.executemany(
                    "INSERT OR REPLACE INTO stage_timings"
                    " (run_id, seq, stage, started_at, duration_ms, ok)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    stage_rows,
                )
        except sqlite3.Error as exc:
            raise LatencyStoreError(f"could not write latency records: {exc}") from exc

    def prune(self, retention_days: int, *, now: float | None = None) -> int:
        """Delete runs older than ``retention_days``. Returns rows removed."""
        if retention_days <= 0:
            return 0
        cutoff = (time.time() if now is None else now) - retention_days * 86_400
        try:
            with _transaction(self._conn):
                cursor = self._conn.execute("DELETE FROM runs WHERE started_at < ?", (cutoff,))
                return cursor.rowcount or 0
        except sqlite3.Error as exc:
            raise LatencyStoreError(f"could not prune latency records: {exc}") from exc

    # -- reading -----------------------------------------------------------

    def fetch_runs(
        self,
        *,
        limit: int | None = None,
        since: float | None = None,
        only_ok: bool | None = None,
    ) -> list[RunRecord]:
        """Return runs newest-first, each with its stage timings attached."""
        where: list[str] = []
        params: list[Any] = []
        if since is not None:
            where.append("started_at >= ?")
            params.append(since)
        if only_ok is not None:
            where.append("ok = ?")
            params.append(int(only_ok))

        sql = "SELECT id, started_at, total_ms, ok, error, metadata FROM runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # rowid breaks ties by insertion order. Several runs can share a
        # started_at when the wall clock does not tick between them, and
        # ordering by a random uuid instead would shuffle them arbitrarily.
        sql += " ORDER BY started_at DESC, rowid DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        try:
            rows = self._conn.execute(sql, params).fetchall()
            stages = self._fetch_stages([row["id"] for row in rows])
        except sqlite3.Error as exc:
            raise LatencyStoreError(f"could not read latency records: {exc}") from exc

        return [
            RunRecord(
                id=row["id"],
                started_at=row["started_at"],
                total_ms=row["total_ms"],
                ok=bool(row["ok"]),
                error=row["error"],
                metadata=_load_metadata(row["id"], row["metadata"]),
                stages=tuple(stages.get(row["id"], ())),
            )
            for row in rows
        ]

    def _fetch_stages(self, run_ids: Sequence[str]) -> dict[str, list[StageTiming]]:
        """Fetch every run's stages in chunked queries, never one query per run."""
        by_run: dict[str, list[StageTiming]] = {}
        for chunk in _chunked(run_ids, _ID_CHUNK):
            placeholders = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                "SELECT run_id, seq, stage, started_at, duration_ms, ok"
                f" FROM stage_timings WHERE run_id IN ({placeholders})"
                " ORDER BY run_id, seq",
                tuple(chunk),
            ).fetchall()
            for row in rows:
                by_run.setdefault(row["run_id"], []).append(
                    StageTiming(
                        stage=row["stage"],
                        seq=row["seq"],
                        started_at=row["started_at"],
                        duration_ms=row["duration_ms"],
                        ok=bool(row["ok"]),
                    )
                )
        return by_run

    def count_runs(self) -> int:
        try:
            return int(self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        except sqlite3.Error as exc:
            raise LatencyStoreError(f"could not count latency records: {exc}") from exc


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


def _chunked(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _dump_metadata(run_id: str, metadata: Any) -> str:
    try:
        return json.dumps(dict(metadata), default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        # Metadata is diagnostic; never lose a whole run because one value is odd.
        logger.warning("run %s: metadata was not JSON-serialisable, storing empty", run_id)
        return "{}"


def _load_metadata(run_id: str, raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        logger.warning("run %s: stored metadata is not valid JSON, ignoring", run_id)
        return {}
    return value if isinstance(value, dict) else {}


__all__ = ["SCHEMA_VERSION", "LatencyStore"]

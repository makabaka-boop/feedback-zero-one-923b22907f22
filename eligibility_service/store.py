"""SQLite-backed persistence with *versioned* state.

Design notes
------------

Every row in ``measurements``, ``associations`` and ``authorizations`` carries
the half-open interval ``[valid_from, valid_to)`` of revision versions in which
it existed.  A new revision never mutates historical rows; it closes them
(``valid_to = new_version``) and/or inserts replacements at
``valid_from = new_version``.  Consequently the complete state of any committed
version can be reconstructed at any later time, which is what makes an
application pinned to an old version immune to later revisions -- even across
a crash and restart.

The ``objects`` table is the registry of currently existing objects; it
enforces per-kind identifier uniqueness and drives replace/delete semantics.
Objects of different kinds are independent, so the same spelling may name a
measurement and an association.  Identifiers dropped from the registry keep
their versioned rows for historical reconstruction.

All access to the single SQLite connection is guarded by a re-entrant lock;
write transactions additionally use ``BEGIN IMMEDIATE`` so concurrent writers
(even from other processes) are serialised by SQLite itself.  ``synchronous
=FULL`` in WAL mode means a commit survives process death.
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from typing import Any, Iterator, Sequence

from .errors import StoreBusy

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY,
    committed_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS objects (
    kind TEXT NOT NULL CHECK (kind IN ('measurement', 'association', 'authorization')),
    identifier TEXT NOT NULL,
    current_version INTEGER NOT NULL,
    PRIMARY KEY (kind, identifier)
);

CREATE TABLE IF NOT EXISTS measurements (
    identifier TEXT NOT NULL,
    valid_from INTEGER NOT NULL,
    valid_to INTEGER,
    subject TEXT NOT NULL,
    collected_at INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (identifier, valid_from),
    CHECK (valid_to IS NULL OR valid_from < valid_to)
);
CREATE INDEX IF NOT EXISTS ix_measurements_window ON measurements (valid_from, valid_to);
CREATE INDEX IF NOT EXISTS ix_measurements_subject ON measurements (subject);

CREATE TABLE IF NOT EXISTS associations (
    identifier TEXT NOT NULL,
    valid_from INTEGER NOT NULL,
    valid_to INTEGER,
    subject_a TEXT NOT NULL,
    subject_b TEXT NOT NULL,
    PRIMARY KEY (identifier, valid_from),
    CHECK (valid_to IS NULL OR valid_from < valid_to)
);
CREATE INDEX IF NOT EXISTS ix_associations_window ON associations (valid_from, valid_to);
CREATE INDEX IF NOT EXISTS ix_associations_a ON associations (subject_a);
CREATE INDEX IF NOT EXISTS ix_associations_b ON associations (subject_b);

CREATE TABLE IF NOT EXISTS authorizations (
    identifier TEXT NOT NULL,
    valid_from INTEGER NOT NULL,
    valid_to INTEGER,
    subject TEXT NOT NULL,
    purpose TEXT NOT NULL,
    effect INTEGER NOT NULL CHECK (effect IN (0, 1)),
    start_at INTEGER NOT NULL,
    end_at INTEGER NOT NULL,
    PRIMARY KEY (identifier, valid_from),
    CHECK (valid_to IS NULL OR valid_from < valid_to),
    CHECK (start_at < end_at)
);
CREATE INDEX IF NOT EXISTS ix_authorizations_window ON authorizations (valid_from, valid_to);
CREATE INDEX IF NOT EXISTS ix_authorizations_subject ON authorizations (subject, purpose);

CREATE TABLE IF NOT EXISTS applications (
    application_id TEXT PRIMARY KEY,
    purpose TEXT NOT NULL,
    accepted_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('waiting', 'completed', 'failed')),
    reason_json TEXT,
    result_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_applications_status ON applications (status);
"""


class Store:
    """Owns one SQLite connection and the transaction discipline around it."""

    def __init__(
        self,
        path: str,
        *,
        busy_timeout: float = 5.0,
        retry_deadline: float = 15.0,
    ) -> None:
        self.path = path
        self._retry_deadline = retry_deadline
        self._lock = threading.RLock()
        self._in_txn = False
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,  # we manage BEGIN/COMMIT ourselves
            timeout=busy_timeout,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout * 1000)}")
        self._migrate()

    # ------------------------------------------------------------------
    # Schema / genesis
    # ------------------------------------------------------------------
    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA user_version=1")
            count = self._conn.execute("SELECT COUNT(*) AS c FROM versions").fetchone()["c"]
            if count == 0:
                # Version 1 is the committed genesis state: empty data.
                self._conn.execute(
                    "INSERT OR IGNORE INTO versions (id, committed_at) VALUES (1, ?)",
                    (time.time(),),
                )

    # ------------------------------------------------------------------
    # Basic reads
    # ------------------------------------------------------------------
    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def current_version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT MAX(id) AS v FROM versions").fetchone()
            return int(row["v"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # Write transactions
    # ------------------------------------------------------------------
    def _begin(self) -> None:
        deadline = time.monotonic() + self._retry_deadline
        delay = 0.005
        while True:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if "locked" not in text and "busy" not in text:
                    raise
                if time.monotonic() >= deadline:
                    raise StoreBusy() from exc
                time.sleep(delay)
                delay = min(delay * 2, 0.2)

    @contextlib.contextmanager
    def _transaction(self, *, new_version: bool) -> Iterator["WriteSession"]:
        with self._lock:
            if self._in_txn:
                raise RuntimeError("nested write transaction")
            self._begin()
            self._in_txn = True
            try:
                version: int | None = None
                if new_version:
                    row = self._conn.execute("SELECT MAX(id) AS v FROM versions").fetchone()
                    version = int(row["v"]) + 1
                    self._conn.execute(
                        "INSERT INTO versions (id, committed_at) VALUES (?, ?)",
                        (version, time.time()),
                    )
                session = WriteSession(self, version)
                yield session
                self._conn.execute("COMMIT")
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:  # pragma: no cover - connection already gone
                    pass
                raise
            finally:
                self._in_txn = False

    def transaction(self) -> contextlib.AbstractContextManager["WriteSession"]:
        """Serialised write that does **not** advance the revision version.

        Used for application bookkeeping (acceptance, completion, failure).
        """
        return self._transaction(new_version=False)

    @contextlib.contextmanager
    def read_snapshot(self) -> Iterator[Store]:
        """Yield the store with one consistent read snapshot (WAL).

        A deferred transaction takes its snapshot at the first read and keeps
        it until commit, so multiple queries cannot straddle a committed
        revision.
        """
        with self._lock:
            if self._in_txn:
                yield self
                return
            self._conn.execute("BEGIN DEFERRED")
            try:
                yield self
            finally:
                self._conn.execute("COMMIT")

    def revision(self) -> contextlib.AbstractContextManager["WriteSession"]:
        """Serialised write that advances the revision version."""
        return self._transaction(new_version=True)


class WriteSession:
    """Operations available inside a serialised write transaction."""

    def __init__(self, store: Store, version: int | None) -> None:
        self._store = store
        #: The new revision version for ``Store.revision()``; ``None`` for
        #: plain bookkeeping transactions.
        self.version = version

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._store._conn.execute(sql, tuple(params))

    def query_all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self._store._conn.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self._store._conn.execute(sql, tuple(params)).fetchone()

    def current_version(self) -> int:
        row = self._store._conn.execute("SELECT MAX(id) AS v FROM versions").fetchone()
        return int(row["v"])

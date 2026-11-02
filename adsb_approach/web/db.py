"""Read-only access to the collector's database. Nothing in the web process writes to it."""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote


class DatabaseUnavailable(Exception):
    """The database cannot be read now: missing, not a database, locked past the timeout, or unreadable until the
    collector opens it. The web process reports it and tries again on the next request or poll."""


class Reader(sqlite3.Connection):
    """What connect_ro returns: a connection that also remembers each table's columns once has_column has asked for
    them. A plain sqlite3.Connection holds no attributes of its own."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.table_columns: dict[str, frozenset[str]] = {}


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Whether `table` has `column` in the database `conn` reads. The web process is deployed, and starts answering,
    before the collector restarts and migrates, so a query that names a column a migration added asks first and
    falls back to NULL (procedure paths, migration 0008: approaches.path and approach_points.reference).

    A connect_ro connection remembers the answer for as long as it lives, which is one request's, or one snapshot's,
    so a migration cannot land between two answers; a long-lived reader must not use this. Any other connection asks
    SQLite each time."""
    known = conn.table_columns if isinstance(conn, Reader) else {}
    if table not in known:
        known[table] = frozenset(row[0] for row in conn.execute("SELECT name FROM pragma_table_info(?)", (table,)))
    return column in known[table]


def connect_ro(path: str | Path, *, timeout_s: float = 5.0) -> sqlite3.Connection:
    """A connection that can only read and never creates the file. Without write access to the database's directory, a
    WAL database can be read while the collector has it open, or after it left its -wal and -shm files behind; until
    then queries fail. The connection may pass between threads, but only one may use it at a time."""
    p = Path(path).absolute()
    if not os.path.isfile(p):
        raise DatabaseUnavailable(f"{p}: no such database file")
    try:
        conn = sqlite3.connect(f"file:{quote(str(p))}?mode=ro", uri=True, timeout=timeout_s, isolation_level=None,
                               check_same_thread=False, factory=Reader)
    except sqlite3.Error as exc:
        raise DatabaseUnavailable(f"{p}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def snapshot(path: str | Path) -> Generator[sqlite3.Connection, None, None]:
    """A read-only connection inside one read transaction, so every query in the block sees the same committed state.
    An SQLite error in the block is raised as DatabaseUnavailable."""
    conn = connect_ro(path)
    try:
        conn.execute("BEGIN")
        yield conn
    except sqlite3.Error as exc:
        raise DatabaseUnavailable(f"{path}: {exc}") from exc
    finally:
        conn.close()


def last_event_id(conn: sqlite3.Connection) -> int:
    """The newest event's id, 0 when there is none: where a page's event stream starts after it loads."""
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])

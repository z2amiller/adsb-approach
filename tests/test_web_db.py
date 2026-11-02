import sqlite3

import pytest

from adsb_approach.store.db import Store
from adsb_approach.web.db import DatabaseUnavailable, connect_ro, has_column, last_event_id, snapshot
from tests.webdb import before_migration_8, copy_db


def test_reads_the_replayed_database_and_refuses_to_write(replay_db):
    with snapshot(replay_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM approaches WHERE hex = 'a44540'").fetchone()[0] == 2
        assert last_event_id(conn) == conn.execute("SELECT MAX(id) FROM events").fetchone()[0] > 0
    with pytest.raises(DatabaseUnavailable, match="readonly"):
        with snapshot(replay_db) as conn:
            conn.execute("INSERT INTO runs (kind, started_at) VALUES ('live', 0)")


def test_a_missing_database_is_reported_and_never_created(tmp_path):
    missing = tmp_path / "nothing.sqlite"
    with pytest.raises(DatabaseUnavailable, match="no such database file"):
        connect_ro(missing)
    assert not missing.exists()


def test_a_snapshot_sees_only_what_the_collector_has_committed(tmp_path, replay_db):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    store = Store(db)
    try:
        before = store.conn.execute("SELECT MAX(id) FROM events").fetchone()[0]
        store.begin_batch()
        store.insert_event(1789300000.0, "approach.updated", None, None, {"hex": "abcdef"})
        with snapshot(db) as conn:
            assert last_event_id(conn) == before                 # the collector's batch is not committed yet
        store.commit_batch()
        with snapshot(db) as conn:
            assert last_event_id(conn) == before + 1
    finally:
        store.close()


def test_has_column_says_which_columns_the_database_has(tmp_path, replay_db):
    with snapshot(replay_db) as conn:
        assert has_column(conn, "approaches", "path") and has_column(conn, "approach_points", "reference")
        assert not has_column(conn, "approaches", "no_such_column") and not has_column(conn, "no_such_table", "path")
    old = before_migration_8(copy_db(replay_db, tmp_path / "old.sqlite"))
    with snapshot(old) as conn:
        assert has_column(conn, "approaches", "id") and has_column(conn, "approach_points", "t")
        assert not has_column(conn, "approaches", "path") and not has_column(conn, "approach_points", "reference")


def test_a_connection_remembers_its_answer_and_the_next_connection_asks_again(tmp_path, replay_db):
    """The collector can migrate between two requests, never within one: a read connection keeps its answer, and the
    next request's connection sees the new column. A connection that is not connect_ro's is asked each time."""
    db = before_migration_8(copy_db(replay_db, tmp_path / "old.sqlite"))
    plain = sqlite3.connect(db)
    reader = connect_ro(db)
    try:
        assert not has_column(reader, "approaches", "path") and not has_column(plain, "approaches", "path")
        plain.execute("ALTER TABLE approaches ADD COLUMN path TEXT")
        plain.commit()
        assert not has_column(reader, "approaches", "path")              # its request began before the migration
        assert has_column(plain, "approaches", "path")
        with snapshot(db) as fresh:
            assert has_column(fresh, "approaches", "path")
    finally:
        reader.close()
        plain.close()

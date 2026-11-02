import asyncio
import json
import os
import sqlite3

from adsb_approach.web.stream import (CATCHUP_LIMIT, CONNECTED_FRAME, KEEPALIVE_FRAME, RELOAD_FRAME, EventBroker,
                                      _inode, event_stream)
from tests.webdb import LiveWriter, copy_db


def drain(sub) -> list:
    items = []
    while not sub.queue.empty():
        items.append(sub.queue.get_nowait())
    return items


def query(db, sql, *args):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def test_a_page_without_history_gets_what_is_written_next(tmp_path, replay_db):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    first, second = (query(db, "SELECT id FROM approaches WHERE hex = 'a44540' AND attempt_no = ?", n)[0] for n in (1, 2))
    w = LiveWriter(db)

    async def main():
        broker = EventBroker(db)
        await broker.poll_once()
        page = broker.subscribe(None)
        await broker.poll_once()
        assert drain(page) == []
        # written the way the collector now writes a late event: arrival_id and attempt_no already in the payload
        eid = w.event("approach.confirmed", second, {"hex": "a44540", "airport": "KSFO", "runway": "28L",
                                                    "outcome": "lost_on_final", "flags": [], "landed_confirmed_by": "departure",
                                                    "arrival_id": first, "attempt_no": 2},
                      dt=0)
        await broker.poll_once()
        [event] = drain(page)
        assert (event["id"], event["kind"], event["approach_id"]) == (eid, "approach.confirmed", second)
        assert (event["payload"]["arrival_id"], event["payload"]["attempt_no"]) == (first, 2)
        assert "callsign" not in event["payload"]        # no fallback: the page gets exactly what was written
        await broker.stop()

    try:
        asyncio.run(main())
    finally:
        w.close()


def test_a_page_catches_up_from_its_last_event_then_follows_without_repeats(tmp_path, replay_db):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    newest = query(db, "SELECT MAX(id) FROM events")[0]
    w = LiveWriter(db)

    async def main():
        broker = EventBroker(db)
        await broker.poll_once()                                     # the poller is at `newest`
        a = w.event("approach.updated", None, {"hex": "aaa001"}, dt=1)
        b = w.event("approach.updated", None, {"hex": "aaa001"}, dt=2)
        page = broker.subscribe(newest - 1)                          # the page has everything before `newest`
        await broker.poll_once()
        c = w.event("approach.updated", None, {"hex": "aaa001"}, dt=3)
        await broker.poll_once()
        assert [e["id"] for e in drain(page)] == [newest, a, b, c]
        await broker.stop()

    try:
        asyncio.run(main())
    finally:
        w.close()


def test_a_page_ahead_of_the_database_or_too_far_behind_reloads(replay_db):
    newest = query(replay_db, "SELECT MAX(id) FROM events")[0]
    assert newest > CATCHUP_LIMIT + 1

    async def main():
        broker = EventBroker(replay_db)
        ahead, behind = broker.subscribe(newest + 10), broker.subscribe(newest - CATCHUP_LIMIT - 1)
        just_in_reach = broker.subscribe(newest - CATCHUP_LIMIT)
        await broker.poll_once()
        assert drain(ahead) == [None] and ahead.reload and drain(behind) == [None] and behind.reload
        assert len(drain(just_in_reach)) == CATCHUP_LIMIT and broker.clients == 1
        await broker.stop()

    asyncio.run(main())


def test_every_page_reloads_when_the_database_goes_back(tmp_path, replay_db):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    older = copy_db(replay_db, tmp_path / "older.sqlite")
    conn = sqlite3.connect(older)
    conn.execute("DELETE FROM events WHERE id > 100")
    conn.commit()
    conn.close()

    async def main():
        broker = EventBroker(db)
        await broker.poll_once()
        page = broker.subscribe(None)
        await broker.poll_once()
        os.replace(older, db)                                        # say, restored from an older copy
        await broker.poll_once()
        assert drain(page) == [None] and page.reload and broker.last_id == 100
        fresh = broker.subscribe(100)
        await broker.poll_once()
        assert drain(fresh) == [] and broker.clients == 1
        await broker.stop()

    asyncio.run(main())


def test_a_page_too_slow_to_take_its_events_is_dropped(tmp_path, replay_db):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)

    async def main():
        broker = EventBroker(db, queue_max=2)
        await broker.poll_once()
        slow = broker.subscribe(None)
        await broker.poll_once()
        for dt in range(3):
            w.event("approach.updated", None, {"hex": "aaa001"}, dt=dt)
        await broker.poll_once()
        assert drain(slow) == [None] and not slow.reload and broker.clients == 0
        await broker.stop()

    try:
        asyncio.run(main())
    finally:
        w.close()


def test_the_stream_sends_frames_keep_alives_and_reload(replay_db):
    newest = query(replay_db, "SELECT MAX(id) FROM events")[0]

    async def main():
        broker = EventBroker(replay_db)
        await broker.poll_once()
        stream = event_stream(broker, newest - 1, keepalive_s=0.05)
        assert await anext(stream) == CONNECTED_FRAME and broker.clients == 1
        await broker.poll_once()
        frame = await anext(stream)
        assert frame.startswith(f"id: {newest}\ndata: ") and frame.endswith("\n\n")
        assert json.loads(frame.split("data: ", 1)[1])["id"] == newest
        assert await anext(stream) == KEEPALIVE_FRAME
        await stream.aclose()
        assert broker.clients == 0

        ahead = event_stream(broker, newest + 5)
        assert await anext(ahead) == CONNECTED_FRAME
        await broker.poll_once()
        assert await anext(ahead) == RELOAD_FRAME
        assert await anext(ahead, None) is None                    # and the stream ends
        await broker.stop()

    asyncio.run(main())


class _FailingConn:
    """A fake connection whose read statement fails, and whose COMMIT also fails if asked to run one anyway (as SQLite's
    own connection would, if the read's error already rolled the transaction back)."""

    def __init__(self, read_error: Exception, commit_error: Exception) -> None:
        self.read_error = read_error
        self.commit_error = commit_error

    def execute(self, sql: str, *args: object) -> None:
        if sql.startswith("SELECT COALESCE(MAX(id)"):
            raise self.read_error
        if sql == "COMMIT":
            raise self.commit_error

    def close(self) -> None:
        pass


def test_a_poll_whose_read_raises_reports_that_reads_own_error(tmp_path):
    db = tmp_path / "fake.sqlite"
    db.touch()

    async def main():
        broker = EventBroker(db)
        broker._conn = _FailingConn(sqlite3.OperationalError("disk I/O error"),
                                    sqlite3.OperationalError("cannot commit - no transaction is active"))
        broker._inode = _inode(broker.db_path)
        await broker.poll_once()
        assert broker.error is not None and "disk I/O error" in broker.error
        assert "no transaction is active" not in broker.error

    asyncio.run(main())

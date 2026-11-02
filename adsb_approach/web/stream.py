"""The live event stream (GET /api/events). One poller reads new rows of `events` about once a second and
queues them for every connected page; each page's server-sent events come from its own queue."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

from .db import DatabaseUnavailable, connect_ro

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 1.0
KEEPALIVE_S = 15.0
CATCHUP_LIMIT = 2000        # a page missing more events than this reloads instead of catching up
QUEUE_MAX = 5000            # events waiting for one page before it counts as too slow and its stream ends: more than a
                            # whole catch-up, or a page catching up would be dropped, reconnect and be dropped again

CONNECTED_FRAME = ": connected\n\n"
KEEPALIVE_FRAME = ": keep-alive\n\n"
RELOAD_FRAME = "event: reload\ndata: {}\n\n"

EVENTS_SQL = "SELECT id, t, kind, approach_id, flight_id, payload_json FROM events WHERE id > ? ORDER BY id LIMIT ?"


def event_record(row: sqlite3.Row) -> dict[str, Any]:
    """A stored event as a page receives it: the collector's own payload, unchanged."""
    payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
    return {"id": row["id"], "t": row["t"], "kind": row["kind"], "approach_id": row["approach_id"],
            "flight_id": row["flight_id"], "payload": payload}


def sse_frame(event: dict[str, Any]) -> str:
    return f"id: {event['id']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"


def _inode(path: Path) -> int | None:
    try:
        return os.stat(path).st_ino
    except OSError:
        return None


class Subscriber:
    """One page's stream. `after`: the last event id the page has, or None for only what comes next. `sent`: the id of
    the newest event queued for it. None in the queue ends the stream, with `reload` telling the page to reload."""

    def __init__(self, after: int | None, queue_max: int) -> None:
        self.after = after
        self.pending = True
        self.sent: int | None = None
        self.reload = False
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(queue_max)


class EventBroker:
    """Polls the database for events and queues them for every page. Every read happens here, in one read transaction
    per poll on one read-only connection, so a new page's catch-up and the live events it follows cannot overlap or
    leave a gap. A page is caught up at the poll right after it subscribes, which subscribing brings forward."""

    def __init__(self, db_path: str | Path, *, poll_interval_s: float = POLL_INTERVAL_S,
                 queue_max: int = QUEUE_MAX) -> None:
        self.db_path = Path(db_path)
        self.stopping: Callable[[], bool] = lambda: False      # set by the server: true once it has been asked to stop
        self.poll_interval_s = poll_interval_s
        self.queue_max = queue_max
        self.last_id: int | None = None           # the newest event id seen, None before the first successful poll
        self.error: str | None = None
        self.last_poll_at: float | None = None
        self._subs: set[Subscriber] = set()
        self._conn: sqlite3.Connection | None = None
        self._inode: int | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def clients(self) -> int:
        return len(self._subs)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self.run(), name="event-poller")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        for sub in list(self._subs):
            self._end(sub, reload=False)
        self._close()

    async def run(self) -> None:
        """Poll until `stopping()` is true, then end every stream, so a server shutting down does not wait for pages to
        leave."""
        while not self.stopping():
            self._wake.clear()
            try:
                await self.poll_once()
            except Exception:                          # a bug must not stop the stream for good: the next poll retries
                log.exception("event stream: poll failed")
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_interval_s)
            except TimeoutError:
                pass
        for sub in list(self._subs):
            self._end(sub, reload=False)

    def subscribe(self, after: int | None) -> Subscriber:
        sub = Subscriber(after, self.queue_max)
        self._subs.add(sub)
        self._wake.set()
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs.discard(sub)

    async def poll_once(self) -> None:
        """Queue what is new for every page, and catch new pages up."""
        subs = list(self._subs)
        starts = [s.after for s in subs if s.pending and s.after is not None]
        try:
            newest, events = await asyncio.to_thread(self._read, self.last_id, starts)
        except DatabaseUnavailable as exc:
            if str(exc) != self.error:
                log.warning("event stream: %s", exc)
            self.error = str(exc)
            return
        self.error, self.last_poll_at = None, time.time()
        if self.last_id is not None and not 0 <= newest - self.last_id <= CATCHUP_LIMIT:
            # The database went back (replaced, or a power cut lost commits that were already streamed, so their ids
            # come round again), or the poller fell too far behind: every page reloads.
            log.warning("event stream: newest event id went from %d to %d; every page reloads", self.last_id, newest)
            for sub in subs:
                self._end(sub, reload=True)
            self.last_id = newest
            return
        for sub in subs:
            if sub not in self._subs:
                continue                               # it went away while the database was read
            if sub.pending:
                sub.pending = False
                if sub.after is None:
                    sub.sent = newest
                elif not 0 <= newest - sub.after <= CATCHUP_LIMIT:
                    self._end(sub, reload=True)        # ahead of the database, or too far behind to catch up
                    continue
                else:
                    sub.sent = sub.after
            self._deliver(sub, events)
        self.last_id = newest

    def _read(self, last_id: int | None, starts: list[int]) -> tuple[int, list[dict[str, Any]]]:
        """In one read transaction: the newest event id, and the events after the oldest position still wanted, the
        poller's own or a new page's that is not too far behind. Runs in a worker thread."""
        try:
            if self._conn is not None and _inode(self.db_path) != self._inode:
                self._close()                          # replaced or removed: the connection would read the old file
            if self._conn is None:
                self._conn = connect_ro(self.db_path)
                self._inode = _inode(self.db_path)
            conn = self._conn
            conn.execute("BEGIN")
            try:
                newest = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
                wanted = [i for i in (last_id, *starts) if i is not None and newest - CATCHUP_LIMIT <= i < newest]
                events = ([event_record(r) for r in conn.execute(EVENTS_SQL, (min(wanted), CATCHUP_LIMIT))]
                          if wanted else [])
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass                        # the read's own error is the one worth reporting
                raise
            conn.execute("COMMIT")
            return newest, events
        except sqlite3.Error as exc:
            self._close()
            raise DatabaseUnavailable(f"{self.db_path}: {exc}") from exc

    def _deliver(self, sub: Subscriber, events: list[dict[str, Any]]) -> None:
        for event in events:
            if sub.sent is not None and event["id"] <= sub.sent:
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                self._end(sub, reload=False)           # too slow: its page reconnects from the last event it received
                return
            sub.sent = event["id"]

    def _end(self, sub: Subscriber, *, reload: bool) -> None:
        """End a page's stream. What is still queued is dropped: the page reloads, or reconnects and catches up."""
        self._subs.discard(sub)
        sub.reload = reload
        while not sub.queue.empty():
            sub.queue.get_nowait()
        sub.queue.put_nowait(None)

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn, self._inode = None, None


async def event_stream(broker: EventBroker, after: int | None, *, keepalive_s: float = KEEPALIVE_S
                       ) -> AsyncGenerator[str, None]:
    """One page's server-sent events: a comment at once; then its catch-up and the live events as `id:` and `data:`
    frames, with a keep-alive comment after keepalive_s without any; and `event: reload` when the page must reload."""
    sub = broker.subscribe(after)
    try:
        yield CONNECTED_FRAME
        while True:
            try:
                item = await asyncio.wait_for(sub.queue.get(), keepalive_s)
            except TimeoutError:
                yield KEEPALIVE_FRAME
                continue
            if item is None:
                if sub.reload:
                    yield RELOAD_FRAME
                return
            yield sse_frame(item)
    finally:
        broker.unsubscribe(sub)

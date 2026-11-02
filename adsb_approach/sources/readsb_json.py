"""Asyncio client for readsb's --net-json-port: one JSON object per line, reconnecting with backoff."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass

from ..model import Position
from .base import parse_readsb_line

log = logging.getLogger(__name__)
LINE_LIMIT = 1 << 20          # a readsb line is under a kilobyte; one longer than this means a garbled stream


@dataclass
class FeedStats:
    """What the health line reports about the feed; the client updates it as lines arrive."""

    connects: int = 0
    lines: int = 0
    positions: int = 0
    last_line_at: float | None = None       # time.monotonic() when the latest line arrived

    def last_line_age_s(self) -> float | None:
        return None if self.last_line_at is None else time.monotonic() - self.last_line_at


async def readsb_json_positions(host: str, port: int, stats: FeedStats, *, backoff_initial_s: float = 1.0,
                                backoff_max_s: float = 30.0, idle_timeout_s: float = 60.0,
                                connect_timeout_s: float = 10.0,
                                sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> AsyncGenerator[Position, None]:
    """Positions from readsb's JSON port for as long as the caller iterates. A refused, dropped or garbled connection
    is retried after a pause that doubles from backoff_initial_s to backoff_max_s and starts again from
    backoff_initial_s once a line arrives, or once a connection that connected goes idle for idle_timeout_s: that
    connection was otherwise healthy, so the retry follows at once rather than after a pause grown by earlier
    failures. Lines that are not readsb positions are skipped. Cancelling the consuming task stops the client at
    once, whether it is waiting for a line or for its next attempt."""
    backoff = backoff_initial_s
    while True:
        writer: asyncio.StreamWriter | None = None
        connected = False
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port, limit=LINE_LIMIT),
                                                    connect_timeout_s)
            stats.connects += 1
            connected = True
            log.info("connected to readsb json port %s:%s", host, port)
            while True:
                raw = await asyncio.wait_for(reader.readline(), idle_timeout_s)
                if not raw:
                    raise ConnectionError("readsb closed the connection")
                stats.lines += 1
                stats.last_line_at = time.monotonic()
                backoff = backoff_initial_s
                pos = parse_readsb_line(raw)
                if pos is not None:
                    stats.positions += 1
                    yield pos
        except (OSError, ValueError) as exc:        # TimeoutError is an OSError; ValueError: a line over LINE_LIMIT
            if connected and isinstance(exc, TimeoutError):
                backoff = backoff_initial_s          # idle, not failing: this connection was otherwise fine
            log.warning("readsb json port %s:%s: %s; reconnecting in %.1f s", host, port,
                        str(exc) or type(exc).__name__, backoff)
        finally:
            if writer is not None:
                writer.close()
        await sleep(backoff)
        backoff = min(backoff * 2, backoff_max_s)

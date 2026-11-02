"""End to end: the live collector against a fake readsb that replays the 2026-09-12 JSON-port capture."""
import asyncio
import contextlib
import gzip
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

from adsb_approach.config import load_config
from adsb_approach.model import Flags
from adsb_approach.pipeline import Pipeline, load_weather, replay
from adsb_approach.service import run_live
from adsb_approach.sources.base import parse_readsb_line
from adsb_approach.sources.readsb_json import FeedStats
from adsb_approach.store.db import Store
from tests.conftest import FIXTURES, ROOT

CAPTURE = FIXTURES / "raw" / "jsonport-20260912-2011Z.jsonl.gz"
METAR = FIXTURES / "metar-20260912.json"
TYPEDB = FIXTURES / "typedb-sample.csv"
LINES = 40000                 # the first 17 minutes of the capture
TABLES = (("approaches", "id"), ("approach_points", "approach_id, t"), ("approach_gates", "approach_id, gate_ft"),
          ("approach_bins", "approach_id, bin_nm"), ("flights", "id"), ("events", "id"), ("weather", "id"))


async def start_server_retrying(handler, host: str, port: int, *, timeout_s: float = 5.0):
    """asyncio.start_server, retrying while the OS has not yet released `port` just after a previous listener on it
    was closed: rebinding the same port number right away can otherwise lose a race with the kernel."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return await asyncio.start_server(handler, host, port)
        except OSError:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.02)


def capture_lines(n: int = LINES) -> list[bytes]:
    lines: list[bytes] = []
    with gzip.open(CAPTURE, "rb") as fh:
        for line in fh:
            if line.strip():
                lines.append(line)
            if len(lines) == n:
                break
    return lines


def table(db: Path, name: str, order_by: str) -> list[tuple]:
    """Every row of a table, without the columns in which a live run and a replay differ by design."""
    conn = sqlite3.connect(db)
    try:
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({name})")
                if c[1] not in ("created_at", "run_id", "fetched_at")]
        return conn.execute(f"SELECT {', '.join(cols)} FROM {name} ORDER BY {order_by}").fetchall()
    finally:
        conn.close()


def test_run_stores_what_replay_stores_across_a_readsb_restart(tmp_path, caplog):
    lines = capture_lines()
    half = len(lines) // 2
    part = tmp_path / "part.jsonl"
    part.write_bytes(b"".join(lines))
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.service.typedb_path = str(TYPEDB)
    replay(cfg, kind="jsonl", path=part, db_path=tmp_path / "replay.sqlite", run_tag="replay", metar_path=METAR)
    cfg.service.db_path = str(tmp_path / "live.sqlite")
    cfg.service.metar_interval_s = 0
    caplog.set_level(logging.INFO)

    async def wait_until(condition) -> None:
        deadline = time.monotonic() + 60
        while not condition():
            assert time.monotonic() < deadline, "timed out"
            await asyncio.sleep(0.02)

    async def main():
        writers = []

        async def readsb(reader, writer):
            writers.append(writer)
            writer.writelines(lines[:half] if len(writers) == 1 else lines[half:])
            await writer.drain()
            await reader.read()                          # the feed stays open until one side closes it
            writer.close()

        server = await asyncio.start_server(readsb, "127.0.0.1", 0)
        cfg.source.host, cfg.source.port = "127.0.0.1", server.sockets[0].getsockname()[1]
        stats, stop = FeedStats(), asyncio.Event()
        service = asyncio.create_task(run_live(cfg, stop=stop, metar_path=METAR, stats=stats, health_interval_s=0.2,
                                               backoff_initial_s=0.05))
        await wait_until(lambda: stats.lines == half)
        server.close()                                   # readsb restarts: the feed drops and the port refuses for a while
        writers[0].close()
        await server.wait_closed()
        server = await start_server_retrying(readsb, "127.0.0.1", cfg.source.port)
        await wait_until(lambda: stats.lines == len(lines))
        await asyncio.sleep(0.3)                         # a health line or two
        stopped = time.monotonic()
        stop.set()
        run_id = await asyncio.wait_for(service, 10)
        server.close()
        return run_id, stats, time.monotonic() - stopped

    run_id, stats, stop_s = asyncio.run(main())
    assert stats.connects == 2 and stop_s < 5
    live, replayed = tmp_path / "live.sqlite", tmp_path / "replay.sqlite"
    for name, order_by in TABLES:
        assert table(live, name, order_by) == table(replayed, name, order_by), name
    conn = sqlite3.connect(live)
    assert conn.execute("SELECT kind, ended_at IS NOT NULL, note FROM runs WHERE id=?", (run_id,)).fetchone() == ("live", 1, "")
    stored, closed = conn.execute("SELECT COUNT(*), COUNT(outcome) FROM approaches").fetchone()
    assert stored > 0 and closed == stored
    messages = [r.getMessage() for r in caplog.records]
    assert sum(m.startswith("approach #") for m in messages) >= stored
    assert any(m.startswith("health: last line ") for m in messages)


class FakeReadsb(threading.Thread):
    """Accepts one connection per part of the capture, sends it that part and holds it open, like a quiet feed."""

    def __init__(self, parts: list[bytes]):
        super().__init__(daemon=True)
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.parts = parts
        self.held: list[socket.socket] = []

    def run(self) -> None:
        with contextlib.suppress(OSError):
            for part in self.parts:
                conn, _ = self.sock.accept()
                self.held.append(conn)
                with contextlib.suppress(OSError):       # a client killed mid-send
                    conn.sendall(part)

    def close(self) -> None:
        for conn in self.held:
            conn.close()
        self.sock.close()


def open_attempt_after(lines: list[bytes], start: int, tmp_path: Path) -> int:
    """How many lines to send so that an attempt is open when the feed goes quiet: found by replaying them."""
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    store = Store(tmp_path / "probe.sqlite")
    store.migrate()
    pipe = Pipeline(cfg, store, run_kind="replay", source_desc="probe", typedb=None, weather=load_weather(METAR))
    try:
        for i, line in enumerate(lines):
            pos = parse_readsb_line(line)
            if pos is not None:
                pipe.process(pos)
            if i >= start and pipe.matcher.live():
                return i + 1
    finally:
        store.close()
    raise AssertionError("no attempt is open after the start line")


def test_cli_run_sweeps_after_a_kill_and_stops_cleanly_on_sigterm(tmp_path):
    lines = capture_lines()
    cut = open_attempt_after(lines, len(lines) // 2, tmp_path)
    last = parse_readsb_line(lines[-1])
    assert last is not None
    sentinel = b'{"now": %.3f, "hex": "fffffe", "type": "adsb_icao", "lat": 0.0, "lon": 0.0, "alt_baro": 1000}\n' % (last.t + 1)
    feed = FakeReadsb([b"".join(lines[:cut]), b"".join(lines[cut:]) + sentinel])
    feed.start()
    config = tmp_path / "live.toml"
    config.write_text(f'[service]\ndb_path = "live.sqlite"\ntypedb_path = "{TYPEDB}"\n'
                      f'type_classes_path = "{ROOT / "config" / "type_classes.toml"}"\n'
                      f'airports_dir = "{ROOT / "config" / "airports"}"\nmetar_interval_s = 0\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith("ADSB_APPROACH_")}
    env.update(ADSB_APPROACH_SOURCE_HOST="127.0.0.1", ADSB_APPROACH_SOURCE_PORT=str(feed.port))
    command = [sys.executable, "-m", "adsb_approach.cli", "run", "--config", str(config), "--metar", str(METAR)]
    db = tmp_path / "live.sqlite"

    def scalar(sql: str):
        if not db.exists():
            return None
        conn = sqlite3.connect(db)
        try:
            return conn.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:                 # the schema is not there yet
            return None
        finally:
            conn.close()

    def wait_for(condition) -> None:
        deadline = time.monotonic() + 60
        while not condition():
            assert time.monotonic() < deadline, "timed out"
            time.sleep(0.05)

    started: list[subprocess.Popen] = []
    try:
        with open(tmp_path / "first.log", "w") as log1:
            first = subprocess.Popen(command, env=env, cwd=tmp_path, stdout=log1, stderr=subprocess.STDOUT)
            started.append(first)
            wait_for(lambda: (scalar("SELECT COUNT(*) FROM approaches WHERE outcome IS NULL") or 0) > 0)
            first.kill()                                 # SIGKILL: nothing gets closed
            first.wait()
        left_open = scalar("SELECT COUNT(*) FROM approaches WHERE outcome IS NULL")
        with open(tmp_path / "second.log", "w") as log2:
            second = subprocess.Popen(command, env=env, cwd=tmp_path, stdout=log2, stderr=subprocess.STDOUT)
            started.append(second)
            wait_for(lambda: scalar("SELECT COUNT(*) FROM flights WHERE hex='fffffe'") == 1)
            stopped = time.monotonic()
            second.send_signal(signal.SIGTERM)
            second.wait(timeout=15)
            stop_s = time.monotonic() - stopped
    finally:
        for proc in started:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        feed.close()
    output = (tmp_path / "second.log").read_text()
    assert second.returncode == 0 and stop_s < 10, output
    assert f"closed {left_open} approaches left open by 1 interrupted live runs" in output
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT id, kind, ended_at IS NOT NULL, note FROM runs ORDER BY id").fetchall() == [
        (1, "live", 1, "[failed: interrupted]"), (2, "live", 1, "")]
    interrupted = int(Flags.INTERRUPTED)
    assert conn.execute("SELECT COUNT(*) FROM approaches WHERE run_id=1 AND outcome='lost' AND flags & ? = ?",
                        (interrupted, interrupted)).fetchone()[0] == left_open
    assert conn.execute("SELECT COUNT(*) FROM approaches WHERE outcome IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM approaches WHERE run_id=2").fetchone()[0] > 0

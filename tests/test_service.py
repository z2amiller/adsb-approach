import asyncio
import contextlib
import dataclasses
import json
import logging
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from adsb_approach.config import load_config
from adsb_approach.model import Flags, Weather
from adsb_approach.pipeline import Pipeline, PipelineStats, load_weather
from adsb_approach.scorer import scorer_for
from adsb_approach.service import (MIN_LOCK_FREE_S, _drain, _drain_every, _ingest, _report_health, fleet_first_delay,
                                   health_line, metar_age_s, rebuild_fleet_every, rss_mb, run_live)
from adsb_approach.sources.base import readsb_object
from adsb_approach.sources.readsb_json import FeedStats
from adsb_approach.store.db import Store
from adsb_approach.weather import WeatherTable
from tests.conftest import ROOT, ground_point, synthetic_track
from tests.fleetdb import add_approach, cfg_for, new_store, norms_for, stats_row
from tests.reapi import reapi_reply


def test_health_line_reports_feed_attempts_weather_and_memory():
    stats = FeedStats(connects=1, lines=10, positions=9)
    assert health_line(stats, 0.0, 0, None, None) == (
        "health: last line never, 0.0 positions/s, 0 open attempts, METAR age none, RSS unknown")
    stats.last_line_at = time.monotonic() - 2.04
    assert health_line(stats, 54.3, 3, 1500.0, 212.4) == (
        "health: last line 2.0 s ago, 54.3 positions/s, 3 open attempts, METAR age 25 min, RSS 212 MB")


def test_health_line_adds_the_merge_counters_when_it_is_given_them():
    """The run's totals of what the "newer than" rule did with the positions of each origin, for the live check to
    read."""
    merged = PipelineStats(external_kept=120, external_dropped=480, external_local_dropped=3)
    assert health_line(FeedStats(), 0.0, 0, None, None, merged) == (
        "health: last line never, 0.0 positions/s, 0 open attempts, METAR age none, RSS unknown, "
        "adsb.lol kept 120, dropped 480, local dropped 3")


@pytest.mark.parametrize("merging", [True, False])
def test_the_health_report_logs_the_merge_counters_only_for_a_run_that_merges(tmp_path, caplog, merging):
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.external.enabled = merging
    store = Store(tmp_path / "health.sqlite")
    store.migrate()
    pipe = Pipeline(cfg, store, run_kind="live", source_desc="unit", typedb=None, weather=load_weather(None),
                    external_merge=merging)
    pipe.stats.external_kept, pipe.stats.external_dropped, pipe.stats.external_local_dropped = 7, 5, 2

    async def report_once() -> None:
        task = asyncio.create_task(_report_health(pipe, FeedStats(), [], 0.01))
        while not caplog.records:
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    try:
        with caplog.at_level(logging.INFO, logger="adsb_approach.service"):
            asyncio.run(asyncio.wait_for(report_once(), 5))
    finally:
        store.close()
    line = caplog.records[0].getMessage()
    assert line.startswith("health: ") and line.endswith(", adsb.lol kept 7, dropped 5, local dropped 2") is merging
    assert ("adsb.lol" in line) is merging


def test_metar_age_is_the_stalest_airports_and_none_while_one_has_none():
    wt = WeatherTable()
    wt.add(Weather("KSFO", 1000.0, 1015.0, None, None, None, None, None, ""))
    wt.add(Weather("KOAK", 1600.0, 1015.0, None, None, None, None, None, ""))
    assert metar_age_s(wt, ["KOAK", "KSFO"], 2200.0) == 1200.0
    assert metar_age_s(wt, ["KOAK", "KNUQ"], 2200.0) is None


def test_rss_is_measured():
    assert rss_mb() > 1.0


def test_run_refuses_a_source_it_cannot_read(tmp_path):
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.source.kind = "aircraft-json"
    cfg.service.db_path = str(tmp_path / "live.sqlite")
    with pytest.raises(ValueError, match="reads readsb-json only"):
        asyncio.run(run_live(cfg, stop=asyncio.Event()))
    assert not (tmp_path / "live.sqlite").exists()


def test_startup_reloads_recent_weather_so_a_restart_does_not_assume_standard_pressure(tmp_path):
    """Stored METARs aren't otherwise seen until the next successful poll, so an approach already close to the ground
    when the service restarts would wrongly get QNH_ASSUMED (standard pressure flips the 100 ft touchdown rule) even
    though a recent altimeter setting is sitting in the database."""
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    db = tmp_path / "live.sqlite"
    cfg.service.db_path, cfg.service.typedb_path, cfg.service.metar_interval_s = str(db), None, 0
    now = time.time()
    store = Store(db)
    store.migrate()
    store.insert_weather(Weather("KSFO", now - 300.0, 1013.25, 15.0, 10.0, 280, 10, None, "METAR KSFO recent"))
    store.close()
    track = synthetic_track(cfg.airports["KSFO"].runways["28R"], start_nm=8.0, end_nm=4.0, t0=now)

    def established() -> bool:
        conn = sqlite3.connect(db)
        try:
            return conn.execute("SELECT COUNT(*) FROM approaches").fetchone()[0] > 0
        finally:
            conn.close()

    async def main():
        async def readsb(reader, writer):
            for pos in track:
                writer.write(json.dumps(readsb_object(pos)).encode() + b"\n")
            await writer.drain()
            await reader.read()                          # the feed stays open until the test closes it
            writer.close()

        server = await asyncio.start_server(readsb, "127.0.0.1", 0)
        cfg.source.host, cfg.source.port = "127.0.0.1", server.sockets[0].getsockname()[1]
        stop = asyncio.Event()
        service = asyncio.create_task(run_live(cfg, stop=stop, backoff_initial_s=0.05))
        try:
            deadline = time.monotonic() + 10
            while not established():
                assert time.monotonic() < deadline, "no approach established"
                await asyncio.sleep(0.05)
        finally:
            stop.set()
            server.close()
        return await asyncio.wait_for(service, 10)

    asyncio.run(main())
    conn = sqlite3.connect(db)
    try:
        flags, weather_id = conn.execute("SELECT flags, weather_id FROM approaches").fetchone()
    finally:
        conn.close()
    assert weather_id is not None and not Flags(flags) & Flags.QNH_ASSUMED


def test_run_live_keeps_the_ingest_error_when_finish_also_fails(tmp_path, monkeypatch, caplog):
    """A failing finish() or final commit must not replace the task's own error (the original error is what's logged and
    raised), but its own failure must still be logged rather than silently swallowed."""
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.service.db_path = str(tmp_path / "live.sqlite")
    cfg.service.typedb_path, cfg.service.metar_interval_s = None, 0

    async def bad_source(*args, **kwargs):
        if False:
            yield None            # pragma: no cover - makes this an async generator so aclosing() works
        raise RuntimeError("ingest boom")

    monkeypatch.setattr("adsb_approach.service.readsb_json_positions", bad_source)

    from adsb_approach.pipeline import Pipeline

    def bad_finish(self, error=None):
        raise RuntimeError("finish boom")

    monkeypatch.setattr(Pipeline, "finish", bad_finish)

    with pytest.raises(RuntimeError, match="ingest boom"):
        asyncio.run(run_live(cfg, stop=asyncio.Event()))
    assert "ingest boom" in caplog.text
    assert "finish boom" in caplog.text


def test_run_waits_for_another_writer_and_releases_the_write_lock_between_batches(tmp_path):
    """Another connection writing the database, such as a replay into it, must not end the run, and between batches
    the service holds no lock, so that writer gets its turn."""
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    db = tmp_path / "live.sqlite"
    cfg.service.db_path, cfg.service.typedb_path, cfg.service.metar_interval_s = str(db), None, 0
    line = b'{"now": 1789245000.0, "hex": "fffffe", "type": "adsb_icao", "lat": 0.0, "lon": 0.0, "alt_baro": 1000}\n'
    stats, holding = FeedStats(), threading.Event()

    def other_writer() -> None:
        conn = sqlite3.connect(db, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            holding.set()
            deadline = time.monotonic() + 10
            while stats.positions < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.2)                                 # the service stores the new flight while this writer holds the lock
            conn.execute("COMMIT")
        finally:
            conn.close()

    def committed_flights() -> int:
        conn = sqlite3.connect(db)
        try:
            return conn.execute("SELECT COUNT(*) FROM flights WHERE hex='fffffe'").fetchone()[0]
        finally:
            conn.close()

    async def main():
        send, stop = asyncio.Event(), asyncio.Event()

        async def readsb(reader, writer):
            await send.wait()
            writer.write(line)
            await writer.drain()
            await reader.read()
            writer.close()

        async def until(condition) -> None:
            deadline = time.monotonic() + 10
            while not condition():
                assert time.monotonic() < deadline, "timed out"
                await asyncio.sleep(0.01)

        server = await asyncio.start_server(readsb, "127.0.0.1", 0)
        cfg.source.host, cfg.source.port = "127.0.0.1", server.sockets[0].getsockname()[1]
        service = asyncio.create_task(run_live(cfg, stop=stop, stats=stats, backoff_initial_s=0.05))
        try:
            await until(lambda: stats.connects == 1)
            threading.Thread(target=other_writer, daemon=True).start()
            await until(holding.is_set)
            send.set()
            await until(lambda: service.done() or committed_flights() == 1)
            if not service.done():
                outside = sqlite3.connect(db, timeout=0, isolation_level=None)      # no wait: the lock must be free now
                try:
                    outside.execute("BEGIN IMMEDIATE")
                    outside.execute("COMMIT")
                finally:
                    outside.close()
        finally:
            stop.set()
            server.close()
        return await asyncio.wait_for(service, 10)

    run_id = asyncio.run(main())
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT ended_at IS NOT NULL, note FROM runs WHERE id=?", (run_id,)).fetchone() == (1, "")
    assert conn.execute("SELECT COUNT(*) FROM flights WHERE hex='fffffe'").fetchone()[0] == 1


# --- the collector buffers positions and drains a burst at a time --------------------------------------------------

def test_drain_processes_the_buffer_in_order_and_opens_no_transaction_when_it_is_empty():
    calls: list = []
    pipe = SimpleNamespace(process=lambda pos: calls.append(("process", pos)))
    store = SimpleNamespace(begin_batch=lambda: calls.append(("begin",)), commit_batch=lambda: calls.append(("commit",)))
    buffer: deque = deque()

    _drain(pipe, store, buffer)
    assert calls == []                                    # nothing buffered: no transaction at all

    buffer.extend([1, 2, 3])
    _drain(pipe, store, buffer)
    assert calls == [("begin",), ("process", 1), ("process", 2), ("process", 3), ("commit",)]
    assert len(buffer) == 0                                # taken, not left for the next drain to repeat


def test_drain_tells_heard_each_position_before_processing_it():
    calls: list = []
    pipe = SimpleNamespace(process=lambda pos: calls.append(("process", pos)))
    store = SimpleNamespace(begin_batch=lambda: None, commit_batch=lambda: None)
    _drain(pipe, store, deque([1, 2]), heard=lambda pos: calls.append(("heard", pos)))
    assert calls == [("heard", 1), ("process", 1), ("heard", 2), ("process", 2)]


def test_ingest_only_buffers_and_signals_overflow_once_past_the_cap(monkeypatch, caplog):
    """_ingest must not touch the store or the pipeline at all -- that is _drain's job -- and positions already
    buffered before a drain must not be processed early. Past MAX_BUFFERED_POSITIONS it logs once and sets
    `overflow`, for _drain_every to notice instead of waiting out the rest of its interval."""
    async def fake_positions(host, port, stats, *, backoff_initial_s):
        for i in range(3):
            yield i

    monkeypatch.setattr("adsb_approach.service.readsb_json_positions", fake_positions)
    monkeypatch.setattr("adsb_approach.service.MAX_BUFFERED_POSITIONS", 2)
    cfg = SimpleNamespace(source=SimpleNamespace(host="x", port=0))
    buffer: deque = deque()
    overflow = asyncio.Event()

    with caplog.at_level("WARNING"):
        asyncio.run(_ingest(cfg, FeedStats(), 0.05, buffer, overflow))

    assert list(buffer) == [0, 1, 2]                        # every position buffered, in order, none dropped
    assert overflow.is_set()
    assert "position buffer at 3, over the 2 cap" in caplog.text
    assert caplog.text.count("position buffer at") == 1     # logged once, not once per position past the cap


def test_drain_every_wakes_at_once_on_overflow_and_still_waits_min_lock_free_s_after(monkeypatch):
    """A real interval would make this test slow for nothing: interval_s is set far longer than the test can run, so
    only overflow -- not the timeout -- can be what wakes _drain_every. sleep is a fake clock: it records what it was
    asked to wait, never actually waiting, so the test needs no real time either."""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    calls: list = []
    pipe = SimpleNamespace(process=lambda pos: calls.append(pos))
    store = SimpleNamespace(begin_batch=lambda: calls.append("begin"), commit_batch=lambda: calls.append("commit"))
    buffer: deque = deque(["p1"])
    overflow = asyncio.Event()

    async def main():
        task = asyncio.create_task(_drain_every(pipe, store, buffer, overflow, 3600.0, sleep=fake_sleep))
        await asyncio.sleep(0)          # let it start waiting on wait_for(overflow.wait(), timeout=3600)
        overflow.set()                  # wakes it now instead of 3600 s from now
        deadline = time.monotonic() + 1.0
        while calls != ["begin", "p1", "commit"] and time.monotonic() < deadline:
            await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert calls == ["begin", "p1", "commit"]
    assert sleeps == [MIN_LOCK_FREE_S]


def test_a_clean_stop_drains_whatever_is_still_buffered(tmp_path, monkeypatch):
    """MIN_LOCK_FREE_S/COMMIT_INTERVAL_S aside, a graceful stop must not lose the last, not-yet-drained second: only a
    crash or a kill may (an accepted loss). COMMIT_INTERVAL_S is set far longer than the test can run, so the periodic
    drain never ticks on its own: only run_live's shutdown path can be what writes this position."""
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    db = tmp_path / "live.sqlite"
    cfg.service.db_path, cfg.service.typedb_path, cfg.service.metar_interval_s = str(db), None, 0
    monkeypatch.setattr("adsb_approach.service.COMMIT_INTERVAL_S", 3600.0)
    line = b'{"now": 1789245000.0, "hex": "fffffe", "type": "adsb_icao", "lat": 0.0, "lon": 0.0, "alt_baro": 1000}\n'
    stats = FeedStats()

    async def main():
        stop = asyncio.Event()

        async def readsb(reader, writer):
            writer.write(line)
            await writer.drain()
            await reader.read()                          # the feed stays open until the test closes it
            writer.close()

        server = await asyncio.start_server(readsb, "127.0.0.1", 0)
        cfg.source.host, cfg.source.port = "127.0.0.1", server.sockets[0].getsockname()[1]
        service = asyncio.create_task(run_live(cfg, stop=stop, stats=stats, backoff_initial_s=0.05))
        try:
            deadline = time.monotonic() + 10
            while stats.positions < 1 and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert stats.positions == 1
            await asyncio.sleep(0.05)   # give _ingest a moment to buffer it -- COMMIT_INTERVAL_S guarantees no drain beats us to it
        finally:
            stop.set()
            server.close()
        return await asyncio.wait_for(service, 10)

    asyncio.run(main())
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM flights WHERE hex='fffffe'").fetchone()[0] == 1
    finally:
        conn.close()


def test_an_approach_whose_close_rolled_back_is_swept_although_its_run_ended(tmp_path, monkeypatch):
    """A COMMIT that fails with SQLITE_FULL or an I/O error rolls the whole batch back, and run_live still ends the run:
    an approach that batch closed is open again in an ended run. The next start must close it as interrupted."""
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    db = tmp_path / "live.sqlite"
    cfg.service.db_path, cfg.service.typedb_path, cfg.service.metar_interval_s = str(db), None, 0
    track = synthetic_track(cfg.airports["KSFO"].runways["28L"], start_nm=8.0, end_nm=4.0)
    commits = {"n": 0}
    real_commit = Store.commit_batch

    def second_commit_finds_the_disk_full(self):
        if self.conn.in_transaction:
            commits["n"] += 1
            if commits["n"] == 2:
                self.conn.execute("ROLLBACK")            # what SQLite does to a batch whose COMMIT fails this way
                raise sqlite3.OperationalError("database or disk is full")
        real_commit(self)

    async def feed(*args, **kwargs):
        for pos in track:                                # the attempt opens: first batch
            yield pos
        while commits["n"] < 1:
            await asyncio.sleep(0.01)
        yield dataclasses.replace(track[-1], t=track[-1].t + 60.0, hex="fffffe", lat=0.0, lon=0.0)  # closes it as lost
        await asyncio.sleep(3600)

    monkeypatch.setattr("adsb_approach.service.readsb_json_positions", feed)
    monkeypatch.setattr("adsb_approach.service.COMMIT_INTERVAL_S", 0.05)
    monkeypatch.setattr(Store, "commit_batch", second_commit_finds_the_disk_full)
    with pytest.raises(sqlite3.OperationalError, match="disk is full"):
        asyncio.run(run_live(cfg, stop=asyncio.Event()))
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT ended_at IS NOT NULL FROM runs").fetchone()[0] == 1       # the failed run ended
        assert conn.execute("SELECT COUNT(*) FROM approaches WHERE outcome IS NULL").fetchone()[0] == 1
    finally:
        conn.close()
    monkeypatch.undo()
    store = Store(db)                                    # the next start
    try:
        store.migrate()
        store.sweep_interrupted()
        row = store.conn.execute("SELECT outcome, flags FROM approaches").fetchone()
    finally:
        store.close()
    assert row["outcome"] == "lost" and Flags(row["flags"]) & Flags.INTERRUPTED


def db_rows(db: Path, sql: str) -> list[tuple]:
    """`sql`'s rows from the collector's database, read on a connection of the test's own; none while it is not yet
    migrated."""
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


async def run_against_feed(cfg, lines: list[bytes], until, **kwargs) -> int:
    """run_live against a fake readsb port that sends `lines` at once and then stays open, until `until()` holds (20 s
    at most). Returns the run's id."""
    async def readsb(reader, writer):
        writer.writelines(lines)
        await writer.drain()
        await reader.read()                              # the feed stays open until the test closes it
        writer.close()

    server = await asyncio.start_server(readsb, "127.0.0.1", 0)
    cfg.source.host, cfg.source.port = "127.0.0.1", server.sockets[0].getsockname()[1]
    stop = asyncio.Event()
    service = asyncio.create_task(run_live(cfg, stop=stop, backoff_initial_s=0.05, **kwargs))
    try:
        deadline = time.monotonic() + 20
        while not until():
            assert time.monotonic() < deadline, "the run never got there"
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        server.close()
    return await asyncio.wait_for(service, 10)


def lost_at_800_ft(cfg):
    """A KSFO 28R arrival at 150 kt that the local receiver loses at about 800 ft and 2.5 nm, with the positions the
    aircraft goes on to report: down the glide path to 0.05 nm, then a ground report at 20 kt, a rolled-out landing.
    Returns (seen, later). The receiver's last position is some 74 s of track time before the last of `later`, well over
    lost_timeout_s (45 s)."""
    rwy = cfg.airports["KSFO"].runways["28R"]
    final = synthetic_track(rwy, start_nm=8.0, end_nm=0.05)
    lost_at = round((8.0 - 2.5) * 3600.0 / 150.0)        # seconds into the approach, at 150 kt
    later = final[lost_at + 1:] + [ground_point(rwy, along_nm=-0.6, t=final[-1].t + 15.0, gs=20.0)]
    assert later[-1].t - final[lost_at].t > cfg.detection.lost_timeout_s + 20.0
    return final[:lost_at + 1], later


def test_run_merges_adsb_lol_positions_and_lands_an_approach_the_receiver_lost(tmp_path, monkeypatch, caplog):
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    db = tmp_path / "live.sqlite"
    key_file = tmp_path / "adsblol.key"
    key_file.write_text("test-key\n")
    cfg.service.db_path, cfg.service.typedb_path, cfg.service.metar_interval_s = str(db), None, 0
    cfg.external.enabled, cfg.external.poll_interval_s, cfg.external.api_key_file = True, 0.01, str(key_file)
    monkeypatch.setattr("adsb_approach.service.COMMIT_INTERVAL_S", 0.05)
    monkeypatch.setattr("adsb_approach.poller.IDLE_S", 0.05)
    seen, later = lost_at_800_ft(cfg)
    hexid = seen[0].hex
    lines = [json.dumps(readsb_object(p)).encode() + b"\n" for p in seen]
    calls: list[tuple[str, str | None]] = []

    def adsb_lol(url: str, timeout_s: float, api_key: str | None) -> dict:
        """What re-api would say: the aircraft's next position each time, then its last again, as adsb.lol repeats
        one until it has a newer. Dated by the track's own times, and silent about every hex not asked for."""
        calls.append((url, api_key))
        current = later[min(len(calls) - 1, len(later) - 1)]
        asked = url.partition("find_hex=")[2].split(",")
        return reapi_reply([current] if current.hex in asked else [], now=current.t + 0.2)

    def landed() -> bool:
        return db_rows(db, "SELECT 1 FROM approaches WHERE outcome = 'landed'") != []

    with caplog.at_level(logging.INFO, logger="adsb_approach.service"):
        run_id = asyncio.run(run_against_feed(cfg, lines, landed, reply_fetch=adsb_lol))
    assert [m for m in caplog.messages if "merging adsb.lol positions" in m] == [
        f"run {run_id}: merging adsb.lol positions from https://re-api.adsb.lol/ every 0.01 s, "
        f"with the key in {key_file}"]
    assert {url for url, _ in calls} == {f"https://re-api.adsb.lol/?find_hex={hexid}"}
    assert {key for _, key in calls} == {"test-key"}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        (approach,) = conn.execute("SELECT outcome, flags, landed_confirmed_by FROM approaches").fetchall()
        assert approach["outcome"] == "landed" and approach["landed_confirmed_by"] == "adsb.lol"
        assert Flags(approach["flags"]) & (Flags.EXTERNAL_TAIL | Flags.EXTERNAL_OUTCOME) == (
            Flags.EXTERNAL_TAIL | Flags.EXTERNAL_OUTCOME)
        assert conn.execute("SELECT external_merge FROM runs WHERE id = ?", (run_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM external_polls WHERE http_status = 200").fetchone()[0] >= 1
        origins = dict(conn.execute("SELECT origin, MAX(t) FROM approach_points GROUP BY origin").fetchall())
        assert set(origins) == {"local", "adsb.lol"}
        assert origins["local"] == seen[-1].t                         # the receiver's last position is the one it kept
        assert origins["adsb.lol"] > origins["local"] + cfg.detection.lost_timeout_s
    finally:
        conn.close()


@pytest.mark.parametrize("quiet_s, asks", [(30.0, False), (0.0, True)])
def test_a_live_run_asks_nothing_about_an_aircraft_the_receiver_still_hears(tmp_path, monkeypatch, quiet_s, asks):
    """The drain tells the poller what the receiver places. With the feed's positions dated now, the approach they
    establish stays heard for quiet_s after the last of them, so nothing is asked; quiet_s = 0 asks at once."""
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    db = tmp_path / "live.sqlite"
    cfg.service.db_path, cfg.service.typedb_path, cfg.service.metar_interval_s = str(db), None, 0
    cfg.external.enabled, cfg.external.poll_interval_s, cfg.external.quiet_s = True, 0.01, quiet_s
    monkeypatch.setattr("adsb_approach.service.COMMIT_INTERVAL_S", 0.05)
    monkeypatch.setattr("adsb_approach.poller.IDLE_S", 0.05)
    seen, _ = lost_at_800_ft(cfg)
    shift = time.time() - seen[-1].t
    lines = [json.dumps(readsb_object(dataclasses.replace(p, t=p.t + shift))).encode() + b"\n" for p in seen]
    calls: list[str] = []

    def adsb_lol(url: str, timeout_s: float, api_key: str | None) -> dict:
        calls.append(url)
        return reapi_reply([], now=time.time())

    first: list[float] = []

    def established_a_second_ago() -> bool:
        if not first and db_rows(db, "SELECT 1 FROM approaches"):
            first.append(time.monotonic())
        return bool(first) and time.monotonic() - first[0] > 1.0

    asyncio.run(run_against_feed(cfg, lines, established_a_second_ago, reply_fetch=adsb_lol))
    assert bool(calls) is asks


def test_run_with_the_poller_off_never_polls_and_marks_no_merge(tmp_path, monkeypatch):
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    db = tmp_path / "live.sqlite"
    cfg.service.db_path, cfg.service.typedb_path, cfg.service.metar_interval_s = str(db), None, 0
    assert cfg.external.enabled is False
    monkeypatch.setattr("adsb_approach.service.COMMIT_INTERVAL_S", 0.05)
    monkeypatch.setattr("adsb_approach.poller.IDLE_S", 0.05)
    seen, _ = lost_at_800_ft(cfg)
    lines = [json.dumps(readsb_object(p)).encode() + b"\n" for p in seen]
    calls: list[str] = []

    def adsb_lol(url: str, timeout_s: float, api_key: str | None) -> dict:
        calls.append(url)
        raise AssertionError("polled with the poller off")

    opened_at: list[float] = []

    def open_for_half_a_second() -> bool:
        """The approach a poller would be watching has been open for that long."""
        if not opened_at and db_rows(db, "SELECT 1 FROM approaches"):
            opened_at.append(time.monotonic())
        return bool(opened_at) and time.monotonic() - opened_at[0] > 0.5

    run_id = asyncio.run(run_against_feed(cfg, lines, open_for_half_a_second, reply_fetch=adsb_lol))
    assert calls == []
    assert db_rows(db, f"SELECT external_merge FROM runs WHERE id = {run_id}") == [(0,)]
    assert db_rows(db, "SELECT COUNT(*) FROM external_polls") == [(0,)]


def test_a_replay_never_polls_even_with_the_poller_enabled(tmp_path):
    from adsb_approach.pipeline import replay
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.external.enabled = True
    capture = tmp_path / "capture.jsonl"
    track = synthetic_track(cfg.airports["KSFO"].runways["28R"], start_nm=8.0, end_nm=2.7)
    capture.write_text("".join(json.dumps(readsb_object(p)) + "\n" for p in track))
    replay(cfg, kind="jsonl", path=capture, db_path=tmp_path / "replay.sqlite", run_tag="test")
    conn = sqlite3.connect(tmp_path / "replay.sqlite")
    try:
        assert conn.execute("SELECT outcome FROM approaches").fetchall() == [("lost_on_final",)]
        assert conn.execute("SELECT COUNT(*) FROM external_polls").fetchone()[0] == 0
    finally:
        conn.close()


def test_fleet_first_delay_is_zero_for_an_empty_or_stale_snapshot_and_none_for_a_fresh_one():
    now = 1789000000.0
    assert fleet_first_delay(None, 3600.0, now=now) == 0.0                       # empty: no norms have ever been built
    assert fleet_first_delay(now - 3601.0, 3600.0, now=now) == 0.0               # older than the interval: stale
    assert fleet_first_delay(now - 3600.0, 3600.0, now=now) is None              # exactly one interval old: not stale
    assert fleet_first_delay(now - 10.0, 3600.0, now=now) is None                # fresh


def test_the_rebuild_worker_rebuilds_at_once_when_given_a_zero_first_delay(tmp_path):
    """The delay run_live computes from a stale or empty snapshot (fleet_first_delay) reaches rebuild_fleet_every as
    first_delay_s, so a restart does not wait out a long interval for its first rebuild."""
    cfg = cfg_for(tmp_path, min_samples=2)
    store, run = new_store(tmp_path)
    add_approach(store, run, ended_at=time.time() - 60.0, bins=((1.5, 10.0, 140.0),))
    store.close()

    async def main():
        holder = SimpleNamespace(scorer=scorer_for(cfg))
        worker = asyncio.create_task(rebuild_fleet_every(cfg, holder, 3600.0, first_delay_s=0.0))
        deadline = time.monotonic() + 5
        while not holder.scorer.norms and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        return holder.scorer.norms

    assert asyncio.run(main()) is not None            # rebuilt well inside 5 s despite a 3600 s interval


def test_the_rebuild_worker_waits_the_full_interval_before_its_first_rebuild_by_default(tmp_path):
    cfg = cfg_for(tmp_path, min_samples=2)
    store, run = new_store(tmp_path)
    add_approach(store, run, ended_at=time.time() - 60.0, bins=((1.5, 10.0, 140.0),))
    store.close()

    async def main():
        holder = SimpleNamespace(scorer=scorer_for(cfg))
        worker = asyncio.create_task(rebuild_fleet_every(cfg, holder, 0.2))
        await asyncio.sleep(0.05)                       # well under the 0.2 s interval
        still_none = holder.scorer.norms is None
        deadline = time.monotonic() + 5
        while not holder.scorer.norms and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        return still_none, holder.scorer.norms

    still_none, norms = asyncio.run(main())
    assert still_none and norms is not None


def test_the_rebuild_worker_swaps_each_new_snapshot_into_the_scorer(tmp_path):
    """The worker rebuilds on its interval, in a thread of its own, and the scorer judges the next gates against what it
    wrote."""
    cfg = cfg_for(tmp_path, min_samples=2)
    store, run = new_store(tmp_path)
    now = time.time()
    for i in range(2):
        add_approach(store, run, ended_at=now - 60.0 - i, bins=((1.5, 10.0 + i, 140.0 + i),))
    store.close()

    async def main():
        holder = SimpleNamespace(scorer=scorer_for(cfg))
        worker = asyncio.create_task(rebuild_fleet_every(cfg, holder, 0.01))
        deadline = time.monotonic() + 10
        while not holder.scorer.norms and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        return holder.scorer.norms

    norms = asyncio.run(main())
    assert norms is not None
    norm = norms.lookup("KSFO", "28R", type_code="B738", traffic_class="airline")
    assert norm is not None and norm.text == "type:B738" and norm.bins[1.5].gs_p50 == 140.5


def test_a_rebuild_that_fails_keeps_the_norms_in_use(tmp_path, monkeypatch, caplog):
    cfg = cfg_for(tmp_path, min_samples=2)
    store, _ = new_store(tmp_path)
    store.close()
    kept = norms_for(gs=140.0)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("adsb_approach.service.rebuild", boom)

    async def main():
        holder = SimpleNamespace(scorer=scorer_for(cfg))
        holder.scorer.set_norms(kept)
        worker = asyncio.create_task(rebuild_fleet_every(cfg, holder, 0.01))
        deadline = time.monotonic() + 10
        while "fleet rebuild failed" not in caplog.text and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker
        return holder.scorer.norms

    with caplog.at_level("WARNING"):
        assert asyncio.run(main()) is kept
    assert "fleet rebuild failed, keeping the norms in use: database is locked" in caplog.text


def test_the_collector_judges_its_gates_against_the_norms_the_database_already_holds(tmp_path):
    """run_live loads the stored snapshot at startup, so an approach that closes before the first rebuild of this run is
    already measured against the last rebuild's norms."""
    cfg = cfg_for(tmp_path, rebuild_interval_min=0)
    cfg.service.metar_interval_s = 0
    db = Path(cfg.service.db_path)
    store, _ = new_store(tmp_path)
    store.replace_norms([stats_row(key_kind="class", key="airline", bin_nm=b / 2, gp=0.0, gs=130.0)
                         for b in range(16)], [], set(), built_at=time.time())
    store.close()
    track = synthetic_track(cfg.airports["KSFO"].runways["28R"], start_nm=8.0, end_nm=0.5, t0=time.time(), gs_kt=150.0)

    def closed() -> bool:
        conn = sqlite3.connect(db)
        try:
            return conn.execute("SELECT COUNT(*) FROM approaches").fetchone()[0] > 0
        finally:
            conn.close()

    async def main():
        stop = asyncio.Event()

        async def readsb(reader, writer):
            for pos in track:
                writer.write(json.dumps(readsb_object(pos)).encode() + b"\n")
            await writer.drain()
            await reader.read()
            writer.close()

        server = await asyncio.start_server(readsb, "127.0.0.1", 0)
        cfg.source.host, cfg.source.port = "127.0.0.1", server.sockets[0].getsockname()[1]
        service = asyncio.create_task(run_live(cfg, stop=stop))
        deadline = time.monotonic() + 10
        while not closed() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        stop.set()
        server.close()
        await asyncio.wait_for(service, 10)

    asyncio.run(main())
    store = Store(db)
    row = store.approach(1)
    assert row["fleet_key"] == "class:airline" and Flags.FAST in Flags(row["flags"])
    assert [g["fleet_gs_delta_kt"] for g in store.gates(1)] == [20.0, 20.0]
    store.close()


def test_a_zero_rebuild_interval_starts_no_fleet_worker(tmp_path, monkeypatch):
    """rebuild_interval_min = 0 must still turn the worker off altogether, the startup rebuild included."""
    cfg = cfg_for(tmp_path, rebuild_interval_min=0)
    cfg.service.metar_interval_s = 0
    calls: list = []

    async def fake_rebuild_fleet_every(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("adsb_approach.service.rebuild_fleet_every", fake_rebuild_fleet_every)

    async def main():
        stop = asyncio.Event()

        async def readsb(reader, writer):
            await reader.read()
            writer.close()

        server = await asyncio.start_server(readsb, "127.0.0.1", 0)
        cfg.source.host, cfg.source.port = "127.0.0.1", server.sockets[0].getsockname()[1]
        service = asyncio.create_task(run_live(cfg, stop=stop))
        await asyncio.sleep(0.2)
        stop.set()
        server.close()
        await asyncio.wait_for(service, 10)

    asyncio.run(main())
    assert calls == []

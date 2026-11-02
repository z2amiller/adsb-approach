import asyncio
import contextlib
import gzip
import json
import time
from pathlib import Path

import pytest

from adsb_approach.emergency import EmergencyWatch
from adsb_approach.model import Flags
from adsb_approach.sources.base import position_from_readsb
from adsb_approach.sources.files import iter_jsonl, iter_positions, iter_tar1090_chunks, iter_trace_file
from adsb_approach.sources.readsb_json import FeedStats, readsb_json_positions


def test_chunk_positions_leave_in_time_order_across_aircraft(tmp_path):
    # A fix reaches a snapshot up to one interval after it was received (seen_pos), so within a snapshot a stale fix
    # can follow another aircraft's newer one. The matcher closes quiet attempts on the stream clock, so the reader
    # must hand positions out in time order.
    snaps = [
        {"now": 1000.0, "aircraft": [["a1b2c3", 3000, 150.0, 298.0, 37.70, -122.20, 0.0, "adsb_icao", "UAL1"]]},
        {"now": 1008.0, "aircraft": [["a1b2c3", 2975, 150.0, 298.0, 37.71, -122.21, 0.0, "adsb_icao", "UAL1"],
                                     ["c3b2a1", 2500, 140.0, 298.0, 37.60, -122.10, 7.5, "adsb_icao", "DAL2"]]},
        {"now": 1016.0, "aircraft": [["a1b2c3", 2950, 150.0, 298.0, 37.72, -122.22, 0.0, "adsb_icao", "UAL1"],
                                     ["c3b2a1", 2475, 140.0, 298.0, 37.61, -122.11, 0.5, "adsb_icao", "DAL2"]]},
    ]
    with gzip.open(tmp_path / "chunk_1000000.gz", "wt") as fh:
        json.dump({"files": snaps}, fh)
    got = [(p.hex, p.t) for p in iter_tar1090_chunks(tmp_path)]
    assert got == [("a1b2c3", 1000.0), ("c3b2a1", 1000.5), ("a1b2c3", 1008.0), ("c3b2a1", 1015.5), ("a1b2c3", 1016.0)]


FIX = Path(__file__).parent / "fixtures"
LINE = ('{"now" : 1789243792.626,"hex":"0011c2","type":"adsb_icao","flight":"N00373  ","alt_baro":675,"alt_geom":625,'
        '"gs":65.2,"track":69.34,"baro_rate":-640,"squawk":"1200","emergency":"none","category":"A1","nav_qnh":1011.2,'
        '"nav_heading":163.12,"lat":37.439713,"lon":-122.108362,"nic":9,"rc":75,"seen_pos":0.000,'
        '"version":2,"nic_baro":1,"nac_p":10,"nac_v":2,"sil":3,"sil_type":"perhour","gva":2,"sda":2,'
        '"alert":0,"spi":0,"mlat":[],"tisb":[]}')


def test_json_port_line():
    p = position_from_readsb(json.loads(LINE))
    assert p is not None and p.t == 1789243792.626 and p.hex == "0011c2"
    assert (p.alt_baro, p.alt_geom, p.gs, p.track, p.baro_rate) == (675, 625, 65.2, 69.34, -640)
    assert p.callsign == "N00373" and p.nav_qnh == 1011.2 and p.nav_modes == frozenset() and p.on_ground is False
    assert p.source == "adsb_icao" and p.vertical_rate == -640 and p.nav_altitude_mcp is None


def test_json_port_line_carries_squawk_and_emergency():
    p = position_from_readsb(json.loads(LINE))
    assert p is not None and (p.squawk, p.emergency) == ("1200", "none")


def test_calc_track_is_used_when_track_is_missing():
    obj = json.loads(LINE)
    del obj["track"]
    obj["calc_track"] = 71.5
    p = position_from_readsb(obj)
    assert p is not None and p.track == 71.5


def test_a_bug_in_the_position_constructor_is_not_swallowed_as_bad_data(monkeypatch):
    # Position(...) is called with values already converted; a mismatch there (a coding bug, not bad readsb data)
    # must raise rather than being folded into "the line was bad" and silently dropped.
    import adsb_approach.sources.base as base

    def broken(**kwargs):
        raise TypeError("boom: simulated constructor bug")

    monkeypatch.setattr(base, "Position", broken)
    with pytest.raises(TypeError, match="boom"):
        base.parse_readsb_line(LINE)


def test_ground_and_missing_position():
    g = position_from_readsb({"hex": "ABCDEF", "alt_baro": "ground", "lat": 37.6, "lon": -122.3, "now": 5.0, "seen_pos": 0})
    assert g is not None and g.on_ground and g.alt_baro is None and g.hex == "abcdef"
    assert position_from_readsb({"hex": "abcdef", "alt_baro": 1000}) is None


def test_aircraft_json_object_uses_file_now_minus_seen_pos():
    p = position_from_readsb({"hex": "abcdef", "lat": 1.0, "lon": 2.0, "seen_pos": 2.5, "nav_modes": ["approach", "tcas"],
                              "r": "N1", "t": "B738"}, now=100.0)
    assert p.t == 97.5 and p.nav_modes == frozenset({"approach", "tcas"}) and (p.reg, p.type_code) == ("N1", "B738")


def test_tar1090_chunks_fixture():
    positions = list(iter_tar1090_chunks(FIX / "raw" / "tar1090-chunks-20260912"))
    assert len(positions) > 60000
    assert positions[0].t == pytest.approx(1789212660, abs=600)      # first chunk covers ~11:31Z
    ual = [p for p in positions if p.hex == "a44540"]
    assert len(ual) > 150 and any(p.callsign == "UAL2177" for p in ual)
    assert all(b.t >= a.t - 1.0 for a, b in zip(ual, ual[1:]))
    assert all((a.lat, a.lon, a.alt_baro) != (b.lat, b.lon, b.alt_baro) for a, b in zip(ual, ual[1:]))
    assert any(p.on_ground for p in positions)


def test_jsonl_capture_or_inline(tmp_path):
    cap = FIX / "raw" / "jsonport-20260912-2011Z.jsonl.gz"
    if cap.exists():
        first = []
        for p in iter_jsonl(cap):
            first.append(p)
            if len(first) == 100:
                break
        assert len(first) == 100 and all(p.t > 1789240000 for p in first)
    sample = tmp_path / "s.jsonl"
    sample.write_text(LINE + "\n" + LINE.replace("0011c2", "0011c3") + "\n\n")
    assert [p.hex for p in iter_jsonl(sample)] == ["0011c2", "0011c3"]


def test_trace_file(tmp_path):
    trace = {"icao": "a44540", "r": "N37415", "t": "B39M", "timestamp": 1789240000.0, "trace": [
        [0.0, 37.6, -122.3, 2000, 150.0, 298.0, 0, -700, {"flight": "UAL2177", "nav_qnh": 1015.2, "nav_modes": ["approach"]},
         "adsb_icao", 1900, None, None, None],
        [5.0, 37.61, -122.31, "ground", 20.0, 298.0, 0, None, None, "adsb_icao", None, None, None, None],
        [10.0, 37.62, -122.32, 1500, 150.0, 298.0, 8 | 4, -600, None, "adsb_icao", None, -600, None, None],
    ]}
    path = tmp_path / "trace_full_a44540.json.gz"
    with gzip.open(path, "wt") as fh:
        json.dump(trace, fh)
    ps = list(iter_trace_file(path))
    assert [p.t for p in ps] == [1789240000.0, 1789240005.0, 1789240010.0]
    assert ps[0].alt_baro == 2000 and ps[0].alt_geom == 1900 and ps[0].callsign == "UAL2177"
    assert ps[0].nav_modes == frozenset({"approach"}) and ps[0].nav_qnh == 1015.2 and (ps[0].reg, ps[0].type_code) == ("N37415", "B39M")
    assert ps[1].on_ground and ps[1].alt_baro is None
    assert ps[2].alt_baro is None and ps[2].alt_geom == 1500 and ps[2].baro_rate is None and ps[2].geom_rate == -600
    assert ps[1].callsign == "UAL2177"          # details carry forward within a trace
    assert [p.hex for p in iter_positions("traces", tmp_path)] == ["a44540"] * 3


def test_trace_details_carry_emergency_beside_squawk(tmp_path):
    trace = {"icao": "a44540", "timestamp": 1789240000.0, "trace": [
        [0.0, 37.6, -122.3, 2000, 150.0, 298.0, 0, -700,
         {"flight": "UAL2177", "squawk": "7700", "emergency": "general"}, "adsb_icao", None, None, None, None],
    ]}
    path = tmp_path / "trace_full_a44540.json"
    path.write_text(json.dumps(trace))
    [p] = list(iter_trace_file(path))
    assert (p.squawk, p.emergency) == ("7700", "general")


def test_trace_squawk_and_emergency_come_only_from_each_points_own_snapshot(tmp_path):
    """Unlike callsign, squawk and emergency must not carry forward: a snapshot is occasional, so carrying one's 7700
    onto the points after it invents a run the aircraft was never actually flying."""
    trace = {"icao": "a44540", "timestamp": 1789240000.0, "trace": [
        [0.0, 37.6, -122.3, 2000, 150.0, 298.0, 0, -700,
         {"flight": "UAL2177", "squawk": "7700", "emergency": "general"}, "adsb_icao", None, None, None, None],
        [10.0, 37.61, -122.31, 1900, 150.0, 298.0, 0, -700, None, "adsb_icao", None, None, None, None],
    ]}
    path = tmp_path / "trace_full_a44540.json"
    path.write_text(json.dumps(trace))
    ps = list(iter_trace_file(path))
    assert (ps[0].squawk, ps[0].emergency) == ("7700", "general")
    assert (ps[1].squawk, ps[1].emergency) == (None, None)      # no snapshot of its own: no information, not "7700"
    assert ps[1].callsign == "UAL2177"                          # callsign still carries forward as before


def test_a_single_squawk_snapshot_does_not_latch_across_a_gap_to_an_ordinary_one(tmp_path):
    """The 30 s run, read from a trace whose snapshots are 60 s apart: one reads 7700, the next an ordinary code, with
    five plain position-only points between them. Carried forward (the bug above), those five points would each read
    7700 too and time a run that latches near the 30 s mark, well before the ordinary code arrives -- for a squawk the
    aircraft may only have held for an instant. Read correctly, they carry no information at all, so the run is never
    seen to hold for 30 s before the ordinary code ends it."""
    trace = {"icao": "a44540", "timestamp": 1789240000.0, "trace": [
        [t, 37.6, -122.3, 2000, 150.0, 298.0, 0, -700,
         ({"flight": "UAL2177", "squawk": "7700"} if t == 0.0 else
          {"flight": "UAL2177", "squawk": "1200"} if t == 60.0 else None),
         "adsb_icao", None, None, None, None]
        for t in (0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0)
    ]}
    path = tmp_path / "trace_full_a44540.json"
    path.write_text(json.dumps(trace))
    watch = EmergencyWatch()
    for p in iter_trace_file(path):
        watch.observe(p.squawk, p.emergency, p.t)
    assert watch.latched == Flags.NONE


def test_two_squawk_snapshots_60s_apart_both_reading_7700_do_latch(tmp_path):
    """The same shape as above, but the later snapshot confirms 7700 rather than ending it: a real 60 s run, not a
    momentary code the aircraft had already left behind, so the watch latches it."""
    trace = {"icao": "a44540", "timestamp": 1789240000.0, "trace": [
        [t, 37.6, -122.3, 2000, 150.0, 298.0, 0, -700,
         {"flight": "UAL2177", "squawk": "7700"} if t in (0.0, 60.0) else None,
         "adsb_icao", None, None, None, None]
        for t in (0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0)
    ]}
    path = tmp_path / "trace_full_a44540.json"
    path.write_text(json.dumps(trace))
    watch = EmergencyWatch()
    for p in iter_trace_file(path):
        watch.observe(p.squawk, p.emergency, p.t)
    assert watch.latched == Flags.EMERGENCY_GENERAL


async def serve(handler) -> tuple[asyncio.Server, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def test_readsb_json_client_skips_bad_lines_and_reconnects_after_a_drop():
    scripts = [[LINE, "not json", "[]"], [LINE.replace("0011c2", "0011c4")]]

    async def main():
        connections = 0

        async def handler(reader, writer):
            nonlocal connections
            lines = scripts[min(connections, len(scripts) - 1)]
            connections += 1
            writer.write("".join(line + "\n" for line in lines).encode())
            await writer.drain()
            writer.close()                                  # readsb restarting: the connection drops after these lines

        server, port = await serve(handler)
        stats, got = FeedStats(), []
        async with contextlib.aclosing(readsb_json_positions("127.0.0.1", port, stats, backoff_initial_s=0.01)) as positions:
            async for p in positions:
                got.append(p.hex)
                if len(got) == 2:
                    break
        server.close()
        return got, stats

    got, stats = asyncio.run(main())
    assert got == ["0011c2", "0011c4"]
    assert (stats.connects, stats.lines, stats.positions) == (2, 4, 2) and stats.last_line_age_s() < 5


def test_readsb_json_client_reconnects_when_the_feed_goes_silent():
    async def main():
        connections = 0

        async def handler(reader, writer):
            nonlocal connections
            connections += 1
            writer.write((LINE.replace("0011c2", f"a0000{connections}") + "\n").encode())
            await writer.drain()
            await reader.read()                             # then silence, with the connection left open
            writer.close()

        server, port = await serve(handler)
        stats, got = FeedStats(), []
        async with contextlib.aclosing(readsb_json_positions("127.0.0.1", port, stats, backoff_initial_s=0.01,
                                                             idle_timeout_s=0.2)) as positions:
            async for p in positions:
                got.append(p.hex)
                if len(got) == 2:
                    break
        server.close()
        return got, stats

    got, stats = asyncio.run(main())
    assert got == ["a00001", "a00002"] and stats.connects == 2


def test_readsb_json_client_stops_at_once_when_cancelled():
    async def consume(port, stats, **kw):
        async for _ in readsb_json_positions("127.0.0.1", port, stats, **kw):
            pass

    async def seconds_to_cancel(task, ready) -> float:
        while not ready():
            await asyncio.sleep(0.01)
        started = time.monotonic()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return time.monotonic() - started

    async def main():
        async def silent(reader, writer):
            await reader.read()
            writer.close()                                  # else wait_closed() waits for it forever

        server, port = await serve(silent)
        waiting = FeedStats()
        mid_read = await seconds_to_cancel(asyncio.create_task(consume(port, waiting, idle_timeout_s=3600)),
                                           lambda: waiting.connects == 1)
        server.close()
        await server.wait_closed()
        refused = FeedStats()
        task = asyncio.create_task(consume(port, refused, backoff_initial_s=3600))
        await asyncio.sleep(0.2)                                        # refused, and now in an hour-long pause
        mid_pause = await seconds_to_cancel(task, lambda: True)
        return mid_read, mid_pause, refused.connects

    mid_read, mid_pause, connects = asyncio.run(main())
    assert mid_read < 1.0 and mid_pause < 1.0 and connects == 0


def test_readsb_json_client_backoff_doubles_on_repeated_connection_failures_and_caps():
    async def refused_port() -> int:
        server, port = await serve(lambda r, w: w.close())
        server.close()
        await server.wait_closed()
        return port                                     # nothing listens here now: every connect is refused

    async def main():
        port = await refused_port()
        sleeps: list[float] = []

        async def fake_sleep(s):
            sleeps.append(s)

        stats = FeedStats()

        async def consume():
            async for _ in readsb_json_positions("127.0.0.1", port, stats, backoff_initial_s=1.0, backoff_max_s=30.0,
                                                 sleep=fake_sleep):
                pass

        task = asyncio.create_task(consume())
        deadline = time.monotonic() + 5
        while len(sleeps) < 7 and time.monotonic() < deadline:
            await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return sleeps, stats.connects

    sleeps, connects = asyncio.run(main())
    assert sleeps[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    assert connects == 0                                                # never actually connected


def test_readsb_json_client_backoff_resets_once_a_line_arrives():
    async def main():
        connections = 0

        async def handler(reader, writer):
            nonlocal connections
            connections += 1
            if connections == 3:
                writer.write((LINE + "\n").encode())
                await writer.drain()
            writer.close()                              # EOF at once (1, 2) or right after the line (3)

        server, port = await serve(handler)
        sleeps: list[float] = []

        async def fake_sleep(s):
            sleeps.append(s)

        stats = FeedStats()

        async def consume():
            async for _ in readsb_json_positions("127.0.0.1", port, stats, backoff_initial_s=1.0, backoff_max_s=30.0,
                                                 sleep=fake_sleep):
                pass

        task = asyncio.create_task(consume())
        deadline = time.monotonic() + 5
        while len(sleeps) < 3 and time.monotonic() < deadline:
            await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        server.close()
        return sleeps

    sleeps = asyncio.run(main())
    assert sleeps[:3] == [1.0, 2.0, 1.0]                 # the line on connection 3 reset it before the next failure


def test_readsb_json_client_backoff_resets_after_an_idle_timeout_on_a_connection_that_connected():
    async def main():
        async def handler(reader, writer):
            await reader.read()                          # accept, then go silent forever
            writer.close()

        server, port = await serve(handler)
        sleeps: list[float] = []

        async def fake_sleep(s):
            sleeps.append(s)

        stats = FeedStats()

        async def consume():
            async for _ in readsb_json_positions("127.0.0.1", port, stats, backoff_initial_s=1.0, backoff_max_s=30.0,
                                                 idle_timeout_s=0.05, sleep=fake_sleep):
                pass

        task = asyncio.create_task(consume())
        deadline = time.monotonic() + 5
        while len(sleeps) < 4 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        server.close()
        return sleeps, stats.connects

    sleeps, connects = asyncio.run(main())
    assert sleeps[:4] == [1.0, 1.0, 1.0, 1.0]             # each reconnect is otherwise healthy: no growing pause
    assert connects >= 4


def test_readsb_json_client_survives_an_oversized_line(monkeypatch):
    import adsb_approach.sources.readsb_json as readsb_json
    monkeypatch.setattr(readsb_json, "LINE_LIMIT", 64)      # smaller than LINE, which is well over a kilobyte
    short_line = '{"now":5.0,"hex":"0011c5","lat":37.6,"lon":-122.3}'      # fits the small limit; LINE does not

    async def main():
        connections = 0

        async def handler(reader, writer):
            nonlocal connections
            connections += 1
            body = LINE if connections == 1 else short_line          # connection 1: oversized; connection 2: fits
            writer.write((body + "\n").encode())
            await writer.drain()
            await reader.read()
            writer.close()

        server, port = await serve(handler)
        stats, got = FeedStats(), []
        async with contextlib.aclosing(readsb_json.readsb_json_positions("127.0.0.1", port, stats,
                                                                         backoff_initial_s=0.01)) as positions:
            async for p in positions:
                got.append(p.hex)
                break
        server.close()
        return got, stats

    got, stats = asyncio.run(main())
    assert got == ["0011c5"]                              # the first (oversized) line was dropped, not fatal
    assert stats.connects == 2


def test_gzip_is_detected_by_content_not_suffix(tmp_path):
    """readsb's globe history writes trace_full_<hex>.json gzip-compressed; a capture may be gzipped under any name."""
    trace = {"icao": "a1c868", "timestamp": 1789300000.0,
             "trace": [[0.0, 37.6, -122.3, 3000, 180.0, 298.0, 0, -800, None, "adsb_icao", None, None, None, None]]}
    day = tmp_path / "2026" / "09" / "12" / "traces" / "68"
    day.mkdir(parents=True)
    (day / "trace_full_a1c868.json").write_bytes(gzip.compress(json.dumps(trace).encode()))
    (day / "trace_full_a2c868.json").write_text(json.dumps({**trace, "icao": "a2c868", "timestamp": 1789300001.0}))
    assert [p.hex for p in iter_positions("traces", tmp_path)] == ["a1c868", "a2c868"]
    capture = tmp_path / "capture.jsonl"
    capture.write_bytes(gzip.compress((LINE + "\n").encode()))
    assert [p.hex for p in iter_jsonl(capture)] == ["0011c2"]


def test_chunks_skip_altitude_updates_on_a_stale_fix(tmp_path):
    fix, moved = [37.6, -122.3], [37.61, -122.31]
    snaps = [{"now": 1000.0, "aircraft": [["a5cf3c", 1375, 140.0, 298.0, *fix, 2.0, "adsb_icao", "UAL1685", 10]]},
             {"now": 1008.0, "aircraft": [["a5cf3c", 1075, 140.0, 298.0, *fix, 10.0, "adsb_icao", "UAL1685", 12]]},
             {"now": 1016.0, "aircraft": [["a5cf3c", "ground", 20.0, 298.0, *fix, 18.0, "adsb_icao", "UAL1685", 14]]},
             {"now": 1024.0, "aircraft": [["a5cf3c", 875, 138.0, 298.0, *moved, 0.5, "adsb_icao", "UAL1685", 16]]}]
    with gzip.open(tmp_path / "chunk_1000000.gz", "wt") as fh:
        json.dump({"files": snaps}, fh)
    assert [(p.t, p.alt_baro, p.on_ground) for p in iter_tar1090_chunks(tmp_path)] == [
        (998.0, 1375, False), (998.0, None, True), (1023.5, 875, False)]


def test_trace_details_reset_on_new_leg_and_nav_fields_expire(tmp_path):
    trace = {"icao": "a44540", "timestamp": 1789240000.0, "trace": [
        [0.0, 37.6, -122.3, 1500, 150.0, 298.0, 0, -700, {"flight": "UAL2177", "nav_modes": ["approach"], "nav_qnh": 1015.2},
         "adsb_icao", None, None, None, None],
        [5.0, 37.61, -122.31, 1200, 150.0, 298.0, 0, -700, {"flight": "UAL2177"}, "adsb_icao", None, None, None, None],
        [3600.0, 37.62, -122.32, 300, 140.0, 118.0, 2, 1500, None, "adsb_icao", None, None, None, None],   # flags bit 1: new leg
    ]}
    path = tmp_path / "trace_full_a44540.json"
    path.write_text(json.dumps(trace))
    ps = list(iter_trace_file(path))
    assert ps[1].callsign == "UAL2177" and ps[1].nav_modes == frozenset() and ps[1].nav_qnh is None
    assert ps[2].callsign is None


def test_trace_reader_ignores_a_string_nav_modes(tmp_path):
    trace = {"icao": "a44540", "timestamp": 1789240000.0, "trace": [
        [0.0, 37.6, -122.3, 1500, 150.0, 298.0, 0, -700, {"flight": "UAL2177", "nav_modes": "approach"},
         "adsb_icao", None, None, None, None],
    ]}
    path = tmp_path / "trace_full_a44540.json"
    path.write_text(json.dumps(trace))
    [p] = list(iter_trace_file(path))
    assert p.nav_modes == frozenset()                      # not {"a", "p", "r", "o", "c", "h"}


def test_a_malformed_trace_point_is_skipped_without_dropping_the_rest_of_the_file(tmp_path, caplog):
    trace = {"icao": "a44540", "timestamp": 1789240000.0, "trace": [
        [0.0, 37.6, -122.3, 3000, 180.0, 298.0, 0, -800, None, "adsb_icao", None, None, None, None],
        [5.0, 37.61, -122.31, "bogus", 180.0, 298.0, 0, -800, None, "adsb_icao", None, None, None, None],
        [10.0, 37.62, -122.32, 2900, 180.0, 298.0, 0, -800, None, "adsb_icao", None, None, None, None],
    ]}
    path = tmp_path / "trace_full_a44540.json"
    path.write_text(json.dumps(trace))
    ps = list(iter_trace_file(path))
    assert [p.t for p in ps] == [1789240000.0, 1789240010.0]     # the bad point is skipped, not the points around it
    assert "trace_full_a44540.json: skipped a malformed point" in caplog.text


def test_trace_dir_skips_in_progress_readsb_tmp_files(tmp_path):
    trace = {"icao": "a1c868", "timestamp": 1789300000.0,
             "trace": [[0.0, 37.6, -122.3, 3000, 180.0, 298.0, 0, -800, None, "adsb_icao", None, None, None, None]]}
    (tmp_path / "trace_full_a1c868.json").write_text(json.dumps(trace))
    (tmp_path / "trace_full_a2c868.json.readsb_tmp").write_bytes(b"\x1f\x8b partial")
    assert [p.hex for p in iter_positions("traces", tmp_path)] == ["a1c868"]


def test_line_readers_skip_values_that_are_not_readsb_positions(tmp_path):
    bad_lat = json.dumps({"now": 5.0, "hex": "a00001", "lat": "x", "lon": -122.3})
    string_modes = json.dumps({"now": 5.0, "hex": "a00002", "lat": 37.6, "lon": -122.3, "nav_modes": "approach"})
    capture = tmp_path / "odd.jsonl"
    capture.write_text("\n".join(["[]", '"text"', "7", bad_lat, string_modes, "{broken", LINE]) + "\n")
    got = list(iter_jsonl(capture))
    assert [p.hex for p in got] == ["a00002", "0011c2"]
    assert got[0].nav_modes == frozenset()                      # not {"a", "p", "r", "o", "c", "h"}


def test_a_capture_cut_off_mid_write_yields_its_whole_lines(tmp_path, caplog):
    body = gzip.compress("".join(LINE.replace("0011c2", f"a{i:05x}") + "\n" for i in range(2000)).encode())
    capture = tmp_path / "cut.jsonl.gz"
    capture.write_bytes(body[: len(body) // 2])
    got = list(iter_jsonl(capture))
    assert 0 < len(got) < 2000 and [p.hex for p in got] == [f"a{i:05x}" for i in range(len(got))]
    assert "capture ends early" in caplog.text


def test_an_unreadable_chunk_is_skipped_with_a_warning(tmp_path, caplog):
    snaps = [{"now": 1000.0, "aircraft": [["a1b2c3", 3000, 150.0, 298.0, 37.70, -122.20, 0.0, "adsb_icao", "UAL1"]]}]
    with gzip.open(tmp_path / "chunk_1000000.gz", "wt") as fh:
        json.dump({"files": snaps}, fh)
    whole = gzip.compress(json.dumps({"files": snaps * 50}).encode())
    (tmp_path / "chunk_1000016.gz").write_bytes(whole[: len(whole) // 2])
    assert [p.hex for p in iter_tar1090_chunks(tmp_path)] == ["a1b2c3"]
    assert "chunk_1000016.gz: skipped, unreadable" in caplog.text


DAY = 1789171200.0        # 2026-09-12 00:00Z: globe-history trace files count their points from the start of the day


def trace_json(hexid: str, times: list[float]) -> str:
    """A trace file in readsb's key order (icao, timestamp, trace), one point per time."""
    rows = [[t - DAY, 37.6, -122.3, 3000, 180.0, 298.0, 0, -800, None, "adsb_icao", None, None, None, None] for t in times]
    return json.dumps({"icao": hexid, "timestamp": DAY, "trace": rows})


def test_trace_dir_decodes_a_file_only_while_the_merge_is_inside_it(tmp_path, monkeypatch):
    import adsb_approach.sources.files as files
    for n, hexid in enumerate(("a00001", "a00002", "a00003")):     # one aircraft an hour, each heard for ten minutes
        (tmp_path / f"trace_full_{hexid}.json").write_text(trace_json(hexid, [DAY + 3600 * n + s for s in range(0, 600, 60)]))
    decoded, peak = 0, 0
    real = files.iter_trace_file

    def counting(path):
        nonlocal decoded, peak
        decoded += 1
        peak = max(peak, decoded)
        try:
            yield from real(path)
        finally:
            decoded -= 1

    monkeypatch.setattr(files, "iter_trace_file", counting)
    assert [p.hex for p in files.iter_positions("traces", tmp_path)] == ["a00001"] * 10 + ["a00002"] * 10 + ["a00003"] * 10
    assert peak == 1                                            # merging every file at once holds all three


def test_trace_dir_interleaves_files_and_leaves_equal_times_in_path_order(tmp_path):
    """Exactly what a merge of every file at once gives: overlapping files interleave, a file stays in the merge across
    the gap between its legs, and equal times leave in path order."""
    import heapq
    import adsb_approach.sources.files as files
    base = DAY + 68000
    for d, hexid, offsets in (("00", "a00001", [0, 5, 3600]), ("01", "a00002", [5, 3600]), ("02", "a00003", [2, 5])):
        (tmp_path / d).mkdir()
        (tmp_path / d / f"trace_full_{hexid}.json").write_text(trace_json(hexid, [base + s for s in offsets]))
    got = [(p.t - base, p.hex) for p in files.iter_positions("traces", tmp_path)]
    assert got == [(0, "a00001"), (2, "a00003"), (5, "a00001"), (5, "a00002"), (5, "a00003"), (3600, "a00001"), (3600, "a00002")]
    every = heapq.merge(*[files.iter_trace_file(f) for f in files._trace_files(tmp_path)], key=lambda p: p.t)
    assert got == [(p.t - base, p.hex) for p in every]


def test_trace_start_comes_from_the_head_of_a_readsb_file(tmp_path, monkeypatch):
    import adsb_approach.sources.files as files
    readsb = tmp_path / "trace_full_a1c868.json"                # as readsb 3.16 writes it, gzip under a .json name
    readsb.write_bytes(gzip.compress(b'{"icao":"a1c868",\n"version": "readsb 3.16.16 d9a4c62",\n"timestamp": 1789171200.000,\n'
                                     b'"trace":[\n[79972.45,39.314948,-120.608337,38000,452.6,342.2,1,0,null,"adsb_icao",null,null,null,null]\n]}'))
    parsed = []
    real = files._load_json
    monkeypatch.setattr(files, "_load_json", lambda path: parsed.append(path) or real(path))
    assert files._trace_start(str(readsb)) == pytest.approx(DAY + 79972.45) and parsed == []
    other = tmp_path / "trace_full_a2c868.json"                 # timestamp after a trace longer than the head: full parse
    rows = json.loads(trace_json("a2c868", [DAY + s for s in range(100, 400)]))["trace"]
    other.write_text(json.dumps({"icao": "a2c868", "trace": rows, "timestamp": DAY}))
    assert files._trace_start(str(other)) == pytest.approx(DAY + 100) and parsed == [str(other)]


def test_unreadable_trace_files_are_skipped_with_a_warning(tmp_path, caplog):
    (tmp_path / "trace_full_a00001.json").write_text(trace_json("a00001", [DAY + 68800, DAY + 68860]))
    (tmp_path / "trace_full_a00002.json").write_bytes(gzip.compress(trace_json("a00002", [DAY + 68800]).encode())[:25])
    (tmp_path / "trace_full_a00003.json").write_text("not json at all")
    body = gzip.compress(trace_json("a00004", [DAY + 68800 + s for s in range(2000)]).encode())
    (tmp_path / "trace_full_a00004.json").write_bytes(body[: len(body) // 2])       # head readable, the rest cut off
    assert [p.hex for p in iter_positions("traces", tmp_path)] == ["a00001", "a00001"]
    for hexid in ("a00002", "a00003", "a00004"):
        assert f"trace_full_{hexid}.json: skipped" in caplog.text


def test_a_fetched_trace_decodes_as_its_file_would_and_keeps_its_source_types(tmp_path):
    from adsb_approach.sources.files import decode_trace
    from tests.conftest import T0, ground_point, make_sfo_28r, synthetic_track
    from tests.traces import trace_body
    rwy = make_sfo_28r()
    positions = synthetic_track(rwy, start_nm=1.0, end_nm=0.5, t0=T0) + [ground_point(rwy, along_nm=-0.5, t=T0 + 30)]
    body = trace_body("a44540", positions, sources={1: "mlat", 2: "tisb_icao"})
    path = tmp_path / "trace_full_a44540.json"
    path.write_bytes(body)
    decoded = decode_trace(body, "trace_full_a44540.json")
    assert decoded == list(iter_trace_file(path)) and len(decoded) == len(positions)
    assert [p.source for p in decoded[:4]] == ["adsb_icao", "mlat", "tisb_icao", "adsb_icao"]
    assert decoded[-1].on_ground and decoded[-1].t == T0 + 30
    assert decode_trace(gzip.decompress(body), "plain") == decoded
    assert decode_trace(gzip.compress(b'{"icao": "a44540", "timestamp": 1789240000, "trace": []}'), "empty") == []


@pytest.mark.parametrize("body", [
    b"",
    b"<html><body>429 Too Many Requests</body></html>",
    gzip.compress(b'{"icao": "a44540"}'),
    gzip.compress(b'{"icao": "a44540", "timestamp": "soon", "trace": []}'),
    gzip.compress(b"[1, 2]"),
    gzip.compress(b'{"icao": "a44540", "timestamp": 1789240000, "trace": [[0, 37.6, -122.3]]}')[:20],
])
def test_a_body_that_is_not_a_trace_is_a_decode_error(body):
    from adsb_approach.sources.files import TraceDecodeError, decode_trace
    with pytest.raises(TraceDecodeError, match="trace_full_a44540.json: not a trace"):
        decode_trace(body, "trace_full_a44540.json")

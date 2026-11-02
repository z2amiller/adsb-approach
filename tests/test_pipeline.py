import json
import re
import sqlite3
from pathlib import Path

import pytest

from adsb_approach.cli import main
from adsb_approach.config import load_config
from adsb_approach.pipeline import format_stats, replay
from adsb_approach.sources.base import position_from_readsb, readsb_object
from tests.conftest import ROOT, T0, climb_segment, ground_point, make_ksfo, make_sfo_28r, synthetic_track


def write_jsonl(path: Path, positions) -> Path:
    path.write_text("".join(json.dumps(readsb_object(p)) + "\n" for p in positions))
    return path


def test_readsb_object_round_trips():
    p = synthetic_track(make_sfo_28r(), start_nm=3, end_nm=2.99)[0]
    q = position_from_readsb(readsb_object(p))
    assert q == p


def test_readsb_object_round_trips_a_non_none_emergency_value():
    from dataclasses import replace
    p = replace(synthetic_track(make_sfo_28r(), start_nm=3, end_nm=2.99)[0], emergency="general")
    assert position_from_readsb(readsb_object(p)) == p


def test_replay_jsonl_lands_one_approach(tmp_path):
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.airports = {"KSFO": make_ksfo()}
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=0.05)
    pts.append(ground_point(rwy, along_nm=-0.5, t=pts[-1].t + 20))
    src = write_jsonl(tmp_path / "in.jsonl", pts)
    db = tmp_path / "out.sqlite"
    run_id, stats = replay(cfg, kind="jsonl", path=src, db_path=db, run_tag="unit",
                           typedb_path=ROOT / "tests" / "fixtures" / "typedb-sample.csv")
    assert stats.positions == len(pts) and stats.opened == 1 and stats.closed == 1
    assert stats.by_outcome == {"landed": 1} and stats.by_runway == {"KSFO/28R": 1}
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    assert run["kind"] == "replay" and run["note"] == "unit" and run["ended_at"] is not None
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM events ORDER BY id")]
    assert kinds[0] == "approach.started" and kinds[-1] == "approach.completed" and "approach.updated" in kinds
    flight = conn.execute("SELECT * FROM flights").fetchone()
    assert flight["callsign"] == "UAL2177" and flight["type_code"] == "B39M"
    text = format_stats(stats)
    assert "landed" in text and "KSFO/28R" in text


def test_squawking_7700_for_60s_closes_with_emergency_general(tmp_path):
    """End to end: a run held well past the 30 s latch closes with the flag, whatever it later reverts to."""
    from dataclasses import replace
    from adsb_approach.model import Flags
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.airports = {"KSFO": make_ksfo()}
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=0.05)
    t0 = pts[0].t
    squawking = [replace(p, squawk="7700") if p.t < t0 + 60 else p for p in pts]
    squawking.append(ground_point(rwy, along_nm=-0.5, t=squawking[-1].t + 20))
    src = write_jsonl(tmp_path / "in.jsonl", squawking)
    db = tmp_path / "out.sqlite"
    replay(cfg, kind="jsonl", path=src, db_path=db, run_tag="emerg-60s")
    outcome, flags = sqlite3.connect(db).execute("SELECT outcome, flags FROM approaches").fetchone()
    assert outcome == "landed" and Flags.EMERGENCY_GENERAL in Flags(flags)


def test_a_5s_dial_through_closes_without_the_emergency_flag(tmp_path):
    """The same approach, but the 7700 lasts only 5 s -- short of the 30 s latch -- so it closes clean."""
    from dataclasses import replace
    from adsb_approach.model import Flags
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.airports = {"KSFO": make_ksfo()}
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=0.05)
    t0 = pts[0].t
    dialed = [replace(p, squawk="7700") if t0 + 20 <= p.t < t0 + 25 else p for p in pts]
    dialed.append(ground_point(rwy, along_nm=-0.5, t=dialed[-1].t + 20))
    src = write_jsonl(tmp_path / "in.jsonl", dialed)
    db = tmp_path / "out.sqlite"
    replay(cfg, kind="jsonl", path=src, db_path=db, run_tag="dial-through")
    outcome, flags = sqlite3.connect(db).execute("SELECT outcome, flags FROM approaches").fetchone()
    assert outcome == "landed" and Flags.EMERGENCY_GENERAL not in Flags(flags)


def test_completed_and_reclassified_approaches_are_logged(tmp_path, caplog):
    import logging
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.airports = {"KSFO": make_ksfo()}
    rwy = make_sfo_28r()
    down = synthetic_track(rwy, start_nm=8, end_nm=2.7)
    back = climb_segment(rwy, start_along_nm=-0.7, end_along_nm=-3.0, start_alt_ft=1300, vr_fpm=1500, t0=down[-1].t + 80)
    src = write_jsonl(tmp_path / "in.jsonl", down + back)
    caplog.set_level(logging.INFO, logger="adsb_approach.pipeline")
    replay(cfg, kind="jsonl", path=src, db_path=tmp_path / "out.sqlite", run_tag="log")
    lines = [r.getMessage() for r in caplog.records if r.name == "adsb_approach.pipeline"]
    assert len(lines) == 2
    assert re.fullmatch(r"approach #1 a44540 UAL2177 KSFO/28R lost_on_final, lowest \d+ ft at 2\.7\d nm, flags [A-Z0-9_,]+", lines[0])
    assert re.fullmatch(r"approach #1 a44540 UAL2177 KSFO/28R reclassified lost_on_final -> go_around, flags [A-Z0-9_,]+", lines[1])


def test_cli_replay_and_migrate(tmp_path, capsys):
    rwy = make_sfo_28r()
    src = write_jsonl(tmp_path / "in.jsonl", synthetic_track(rwy, start_nm=8, end_nm=2.7))
    db = tmp_path / "cli.sqlite"
    assert main(["migrate", "--db", str(db)]) == 0
    rc = main(["replay", "--config", str(ROOT / "config" / "adsb-approach.example.toml"), "--kind", "jsonl", "--from", str(src),
               "--db", str(db), "--run-tag", "cli", "--typedb", str(ROOT / "tests" / "fixtures" / "typedb-sample.csv")])
    out = capsys.readouterr().out
    assert rc == 0 and "lost_on_final" in out and "run " in out


def test_replay_persists_flight_last_seen(tmp_path):
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.airports = {"KSFO": make_ksfo()}
    pts = synthetic_track(make_sfo_28r(), start_nm=8, end_nm=2.7)
    src = write_jsonl(tmp_path / "in.jsonl", pts)
    db = tmp_path / "out.sqlite"
    replay(cfg, kind="jsonl", path=src, db_path=db, run_tag="unit",
           typedb_path=ROOT / "tests" / "fixtures" / "typedb-sample.csv")
    first_seen, last_seen = sqlite3.connect(db).execute("SELECT first_seen, last_seen FROM flights").fetchone()
    assert first_seen == pts[0].t and last_seen == pts[-1].t


def test_an_active_flights_last_seen_trails_the_newest_position_by_at_most_30_s(tmp_path):
    """touch_flight only ran from the tracker's on_flight_update before this (a callsign resolving, a new session, a
    track's expiry, or the run ending): an open session's stored last_seen could otherwise sit at its first position for
    as long as the session stayed open. The pipeline now also persists it as positions arrive, at this cadence, so a
    still-open go-around's flight row stays usably fresh for the detail drawer's tar1090 link."""
    from adsb_approach.pipeline import Pipeline, load_weather
    from adsb_approach.store.db import Store
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.airports = {"KSFO": make_ksfo()}
    pts = synthetic_track(make_sfo_28r(), start_nm=8, end_nm=2.7)     # ~127 1-second-apart positions, one session
    store = Store(tmp_path / "s.sqlite")
    store.migrate()
    pipe = Pipeline(cfg, store, run_kind="replay", source_desc="unit", typedb=None, weather=load_weather(None), note="unit")
    worst_lag = 0.0
    touches = 0
    for pos in pts:
        before = store.conn.execute("SELECT last_seen FROM flights WHERE hex = ?", (pos.hex,)).fetchone()
        pipe.process(pos)
        after = store.conn.execute("SELECT last_seen FROM flights WHERE hex = ?", (pos.hex,)).fetchone()[0]
        if before is not None and after != before[0]:
            touches += 1
        worst_lag = max(worst_lag, pos.t - after)
    assert worst_lag <= 30.0
    assert touches >= 3          # the 127 s stream is actually touched periodically, not only once at the start


@pytest.mark.parametrize("outcome", ["go_around", "touch_and_go"])
def test_reclassified_go_around_moves_the_outcome_count(tmp_path, outcome):
    from adsb_approach.pipeline import Pipeline, load_weather
    from adsb_approach.store.db import Store
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    store = Store(tmp_path / "s.sqlite")
    store.migrate()
    pipe = Pipeline(cfg, store, run_kind="replay", source_desc="unit", typedb=None, weather=load_weather(None), note="unit")
    where = {"hex": "a44540", "airport": "KSFO", "runway": "28L"}
    pipe.events.publish("approach.completed", T0, None, None, {"outcome": "lost_on_final", **where})
    pipe.events.publish("approach.reclassified", T0 + 90, None, None,
                        {"from": "lost_on_final", "outcome": outcome, "flags": ["INFERRED_GO_AROUND"], **where})
    assert pipe.stats.by_outcome == {outcome: 1}
    assert [g["hex"] for g in pipe.stats.go_arounds] == ["a44540"]
    assert "inferred" in format_stats(pipe.stats)


def test_airframe_back_inside_the_session_gap_keeps_one_flight(tmp_path):
    from dataclasses import replace
    from adsb_approach.pipeline import Pipeline, load_weather
    from adsb_approach.store.db import Store
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.airports = {"KSFO": make_ksfo()}
    rwy = make_sfo_28r()
    first = synthetic_track(rwy, start_nm=8, end_nm=7.5)
    back = synthetic_track(rwy, start_nm=8, end_nm=7.5, t0=first[-1].t + 4300.0)      # beyond 1 h plus the 600 s expiry cadence
    other = [replace(first[0], hex="ac2706", callsign="UAL2432", lat=first[0].lat + 1.0, t=first[-1].t + s)
             for s in range(60, 4300, 60)]                                              # keeps stream time moving
    store = Store(tmp_path / "s.sqlite")
    store.migrate()
    pipe = Pipeline(cfg, store, run_kind="replay", source_desc="unit", typedb=None, weather=load_weather(None), note="unit")
    pipe.feed(sorted(first + other + back, key=lambda p: p.t))
    pipe.finish()
    rows = store.conn.execute("SELECT first_seen, last_seen FROM flights WHERE hex='a44540'").fetchall()
    assert [tuple(r) for r in rows] == [(first[0].t, back[-1].t)]


def test_replay_twice_into_one_database(tmp_path):
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.airports = {"KSFO": make_ksfo()}
    rwy = make_sfo_28r()
    early = synthetic_track(rwy, callsign=None, start_nm=8, end_nm=7.9)          # the callsign arrives late
    pts = early + synthetic_track(rwy, start_nm=7.85, end_nm=0.05, t0=early[-1].t + 1)
    pts.append(ground_point(rwy, along_nm=-0.5, t=pts[-1].t + 20))
    src = write_jsonl(tmp_path / "in.jsonl", pts)
    db = tmp_path / "out.sqlite"
    typedb = ROOT / "tests" / "fixtures" / "typedb-sample.csv"
    replay(cfg, kind="jsonl", path=src, db_path=db, run_tag="one", typedb_path=typedb)
    replay(cfg, kind="jsonl", path=src, db_path=db, run_tag="two", typedb_path=typedb)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM flights").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(DISTINCT run_id), COUNT(*) FROM approaches").fetchone() == (2, 2)


def test_cli_fetch_metar_creates_the_output_directory(tmp_path, monkeypatch, capsys):
    import adsb_approach.weather as weather
    calls = []

    def fake_fetch(ids, hours=0, timeout=10.0):
        calls.append((ids, hours))
        return [{"icaoId": "KSFO", "obsTime": 1789245300, "altim": 1015.2, "rawOb": "KSFO 122035Z"}]

    monkeypatch.setattr(weather, "fetch_metar_raw", fake_fetch)
    out = tmp_path / "data" / "metar.json"                    # no data/ yet, as in a fresh clone
    assert main(["fetch-metar", "--ids", "ksfo, KOAK", "--hours", "12", "--out", str(out)]) == 0
    assert calls == [(["KSFO", "KOAK"], 12)]
    assert json.loads(out.read_text())[0]["icaoId"] == "KSFO"
    assert "1 observations" in capsys.readouterr().out


def test_fetch_typedb_replaces_the_file_only_with_a_complete_download(tmp_path, monkeypatch, capsys):
    import gzip
    import io
    import urllib.request
    body = gzip.compress(b"a44540;N37415;B39M;00;BOEING 737 MAX 9\n" * 500)
    replies = [body, body[: len(body) // 2]]
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout: io.BytesIO(replies.pop(0)))
    out = tmp_path / "data" / "aircraft.csv.gz"                  # no data/ yet, as in a fresh clone
    assert main(["fetch-typedb", "--out", str(out)]) == 0
    assert out.read_bytes() == body and out.stat().st_mode & 0o777 == 0o644
    with pytest.raises(EOFError):
        main(["fetch-typedb", "--out", str(out)])                  # the connection dropped half-way through
    assert out.read_bytes() == body and [p.name for p in out.parent.iterdir()] == ["aircraft.csv.gz"]


def test_replay_records_the_config_hash(tmp_path):
    from adsb_approach.config import config_hash
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    src = write_jsonl(tmp_path / "in.jsonl", synthetic_track(make_sfo_28r(), start_nm=8, end_nm=7.5))
    db = tmp_path / "out.sqlite"
    run_id, _ = replay(cfg, kind="jsonl", path=src, db_path=db, run_tag="hash")
    stored = sqlite3.connect(db).execute("SELECT config_hash FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    assert stored == config_hash(cfg) and len(stored) == 16


def test_replay_refuses_missing_inputs_before_touching_the_database(tmp_path):
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    db = tmp_path / "out.sqlite"
    src = write_jsonl(tmp_path / "in.jsonl", synthetic_track(make_sfo_28r(), start_nm=8, end_nm=7.5))
    for kind, path in (("chunks", tmp_path / "no-such-dir"), ("chunks", tmp_path), ("traces", tmp_path),
                       ("jsonl", tmp_path / "no-such.jsonl")):
        with pytest.raises(FileNotFoundError):
            replay(cfg, kind=kind, path=path, db_path=db, run_tag="missing")
    with pytest.raises(FileNotFoundError):
        replay(cfg, kind="jsonl", path=src, db_path=db, run_tag="missing", typedb_path=tmp_path / "no-such.csv.gz")
    assert not db.exists()


def test_replay_ends_the_run_when_the_source_fails(tmp_path, monkeypatch):
    import adsb_approach.pipeline as pipeline
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.airports = {"KSFO": make_ksfo()}
    pts = synthetic_track(make_sfo_28r(), start_nm=8, end_nm=1.0)
    src = write_jsonl(tmp_path / "in.jsonl", pts)

    def failing_source(kind, path):
        yield from pts[: len(pts) * 3 // 4]
        raise OSError("read error on the capture disk")

    monkeypatch.setattr(pipeline, "iter_positions", failing_source)
    db = tmp_path / "out.sqlite"
    with pytest.raises(OSError, match="read error"):
        replay(cfg, kind="jsonl", path=src, db_path=db, run_tag="cut")
    conn = sqlite3.connect(db)
    ended_at, note = conn.execute("SELECT ended_at, note FROM runs").fetchone()
    assert ended_at is not None and note.startswith("cut [failed: OSError")
    assert conn.execute("SELECT COUNT(*), COUNT(outcome) FROM approaches").fetchone() == (1, 1)


def test_a_capture_cut_off_mid_write_replays_what_it_holds(tmp_path, caplog):
    import gzip
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.airports = {"KSFO": make_ksfo()}
    pts = synthetic_track(make_sfo_28r(), start_nm=8, end_nm=1.0)
    body = gzip.compress("".join(json.dumps(readsb_object(p)) + "\n" for p in pts).encode())
    src = tmp_path / "cut.jsonl.gz"
    src.write_bytes(body[: len(body) * 3 // 4])                # an interrupted `nc | gzip`
    db = tmp_path / "out.sqlite"
    _, stats = replay(cfg, kind="jsonl", path=src, db_path=db, run_tag="cut")
    assert 0 < stats.positions < len(pts) and "capture ends early" in caplog.text
    conn = sqlite3.connect(db)
    ended_at, note = conn.execute("SELECT ended_at, note FROM runs").fetchone()
    assert ended_at is not None and note == "cut"
    assert conn.execute("SELECT COUNT(*), COUNT(outcome) FROM approaches").fetchone() == (1, 1)


def _merged_pipeline(tmp_path, *, name: str = "merge.sqlite", external_merge: bool = False):
    from adsb_approach.pipeline import Pipeline, load_weather
    from adsb_approach.store.db import Store
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.airports = {"KSFO": make_ksfo()}
    store = Store(tmp_path / name)
    store.migrate()
    pipe = Pipeline(cfg, store, run_kind="live", source_desc="unit", typedb=None, weather=load_weather(None),
                    note="unit", external_merge=external_merge)
    return pipe, store


def test_an_adsb_lol_position_not_newer_than_the_aircraft_s_latest_is_dropped(tmp_path):
    from dataclasses import replace
    local = replace(synthetic_track(make_sfo_28r(), start_nm=3, end_nm=2.99)[0], t=100.0)
    pipe, _ = _merged_pipeline(tmp_path)
    pipe.process(local)
    for t in (100.0, 99.5):
        pipe.process(replace(local, t=t, origin="adsb.lol"))
    assert pipe.stats.external_dropped == 2 and pipe.stats.external_kept == 0
    pipe.process(replace(local, t=100.5, origin="adsb.lol"))
    assert pipe.stats.external_kept == 1
    assert pipe.tracker.get(local.hex).last.t == 100.5


def test_a_local_position_older_than_an_accepted_adsb_lol_one_is_dropped(tmp_path):
    from dataclasses import replace
    base = synthetic_track(make_sfo_28r(), start_nm=3, end_nm=2.99)[0]
    pipe, _ = _merged_pipeline(tmp_path)
    pipe.process(replace(base, t=100.5, origin="adsb.lol"))
    assert pipe.stats.external_local_dropped == 0
    pipe.process(replace(base, t=100.2))
    assert pipe.tracker.get(base.hex).last.t == 100.5
    assert pipe.stats.external_local_dropped == 1
    pipe.process(replace(base, t=101.0))
    assert pipe.tracker.get(base.hex).last.t == 101.0
    assert pipe.stats.external_local_dropped == 1                     # a newer local one is taken, not counted


def test_an_adsb_lol_position_for_an_aircraft_never_seen_is_taken(tmp_path):
    from dataclasses import replace
    p = synthetic_track(make_sfo_28r(), hexid="3c6444", start_nm=3, end_nm=2.99)[0]
    pipe, _ = _merged_pipeline(tmp_path)
    pipe.process(replace(p, origin="adsb.lol"))
    assert pipe.stats.external_kept == 1 and pipe.stats.external_dropped == 0
    assert pipe.tracker.get("3c6444") is not None


def test_a_fix_repeated_in_later_polls_is_admitted_once(tmp_path):
    """The same fix in four polls' replies (re-api repeats a position until it has a newer one) reads as one time, so
    the newer-than rule drops the repeats; a time a float step apart would admit a third of them again."""
    from adsb_approach.poller import positions_from_reply
    from tests.reapi import repeated_fix_replies
    pipe, _ = _merged_pipeline(tmp_path)
    for reply in repeated_fix_replies(fixes=40, polls_per_fix=4):
        for p in positions_from_reply(reply, clock_offset_s=0.10):
            pipe.process(p)
    assert pipe.stats.external_kept == 40 and pipe.stats.external_dropped == 120


def test_a_merged_pipeline_marks_its_run(tmp_path):
    pipe, store = _merged_pipeline(tmp_path, external_merge=True)
    assert store.conn.execute("SELECT external_merge FROM runs WHERE id=?", (pipe.run_id,)).fetchone()[0] == 1
    plain, plain_store = _merged_pipeline(tmp_path, name="plain.sqlite")
    assert plain_store.conn.execute("SELECT external_merge FROM runs WHERE id=?", (plain.run_id,)).fetchone()[0] == 0


# ---------------------------------------------------- replaying with stored adsb.lol positions
def _local(hexid: str, t: float):
    from dataclasses import replace
    return replace(synthetic_track(make_sfo_28r(), hexid=hexid, start_nm=3, end_nm=2.99)[0], t=t)


def _stored(hexid: str, t: float, arrived_at: float):
    from dataclasses import replace
    return arrived_at, replace(_local(hexid, t), origin="adsb.lol")


def test_replay_merge_feeds_only_watched_aircraft_in_arrival_order():
    from adsb_approach.events import EventBus
    from adsb_approach.poller import WatchList, replay_merge
    watch = WatchList(20 * 60.0)
    bus = EventBus()
    bus.subscribe(watch.on_event)                         # as replay() wires it to the pipeline's bus
    local = [_local("a44540", t) for t in (100.0, 102.0, 104.0, 106.0)]
    external = [
        _stored("a44540", 99.4, 99.5),                    # arrives before its approach.started: not watched yet
        _stored("a44540", 100.4, 100.5),                  # the first one after it: the watch must be read as it is
        _stored("3c6444", 101.4, 101.5),                  # never watched
        _stored("a44540", 101.4, 101.5),
        _stored("a44540", 103.9, 104.0),                  # arrives with a local position at 104.0: local goes first
        _stored("a44540", 107.4, 107.5),                  # arrives after its approach.completed
    ]
    merged = []
    for p in replay_merge(iter(local), iter(external), watch):
        merged.append((p.origin, p.hex, p.t))
        if (p.origin, p.t) == ("local", 100.0):
            bus.publish("approach.started", p.t, 1, 1, {"hex": p.hex})
        if (p.origin, p.t) == ("local", 106.0):
            bus.publish("approach.completed", p.t, 1, 1, {"hex": p.hex, "outcome": "landed"})
    assert merged == [("local", "a44540", 100.0), ("adsb.lol", "a44540", 100.4), ("adsb.lol", "a44540", 101.4),
                      ("local", "a44540", 102.0), ("local", "a44540", 104.0), ("adsb.lol", "a44540", 103.9),
                      ("local", "a44540", 106.0)]


def _live_database(path: Path, positions) -> Path:
    """A collector's database holding `positions` as it stored them, each arriving 1.5 s after its fix."""
    from adsb_approach.store.db import Store
    store = Store(path)
    store.migrate()
    run = store.new_run("live", "unit", external_merge=True)
    for p in positions:
        poll_id = store.record_poll(run_id=run, requested_at=p.t + 0.5, arrived_at=p.t + 1.5, asked=1, returned=1,
                                    http_status=200, error=None, refused=False)
        store.insert_external_positions(poll_id, p.t + 1.5, [p])
    store.close()
    return path


def _lost_at_2_5_nm_with_adsb_lol_to_the_roll_out(tmp_path, cfg):
    """The receiver loses the aircraft at 2.5 nm, about 60 s from touchdown; adsb.lol, stored in a collector's
    database, follows it to the roll-out, later than the lost_timeout_s that would have closed the approach."""
    from dataclasses import replace
    rwy = make_sfo_28r()
    local = synthetic_track(rwy, start_nm=8, end_nm=2.5)
    lol = synthetic_track(rwy, start_nm=2.45, end_nm=0.05, t0=local[-1].t + 1)
    roll = [ground_point(rwy, along_nm=-0.2 - 0.03 * i, t=lol[-1].t + 5 + i, gs=gs)
            for i, gs in enumerate((28.0, 22.0, 15.0))]
    assert roll[0].t - local[-1].t > cfg.detection.lost_timeout_s
    return (write_jsonl(tmp_path / "in.jsonl", local),
            _live_database(tmp_path / "live.sqlite", [replace(p, origin="adsb.lol") for p in lol + roll]))


def test_a_replay_with_stored_adsb_lol_positions_lands_what_the_receiver_lost(tmp_path):
    from adsb_approach.model import Flags
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.airports = {"KSFO": make_ksfo()}
    src, live = _lost_at_2_5_nm_with_adsb_lol_to_the_roll_out(tmp_path, cfg)
    db = tmp_path / "out.sqlite"
    merged_id, merged = replay(cfg, kind="jsonl", path=src, db_path=db, run_tag="merged", external_from=live)
    plain_id, plain = replay(cfg, kind="jsonl", path=src, db_path=db, run_tag="plain")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    runs = {r["id"]: r for r in conn.execute("SELECT * FROM runs")}
    assert runs[merged_id]["external_merge"] == 1 and runs[plain_id]["external_merge"] == 0
    assert runs[merged_id]["source"] == f"jsonl:{src}+external:{live}" and runs[plain_id]["source"] == f"jsonl:{src}"
    rows = {r["run_id"]: r for r in conn.execute("SELECT * FROM approaches")}
    assert rows[merged_id]["outcome"] == "landed"
    assert Flags.EXTERNAL_TAIL | Flags.EXTERNAL_OUTCOME in Flags(rows[merged_id]["flags"])
    assert rows[merged_id]["landed_confirmed_by"] == "adsb.lol"
    assert rows[plain_id]["outcome"] == "lost_on_final"
    assert Flags(rows[plain_id]["flags"]) & (Flags.EXTERNAL_TAIL | Flags.EXTERNAL_OUTCOME) == Flags.NONE
    assert merged.external_kept > 0 and plain.external_kept == 0


def test_the_replay_command_takes_external_from(tmp_path, capsys):
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    src, live = _lost_at_2_5_nm_with_adsb_lol_to_the_roll_out(tmp_path, cfg)
    db = tmp_path / "cli.sqlite"
    rc = main(["replay", "--config", str(ROOT / "config" / "adsb-approach.example.toml"), "--kind", "jsonl",
               "--from", str(src), "--db", str(db), "--run-tag", "cli", "--external-from", str(live)])
    out = capsys.readouterr().out
    assert rc == 0 and f"run 1 written to {db}" in out and "landed" in out and "lost_on_final" not in out


def test_replay_refuses_a_missing_external_database_before_touching_the_database(tmp_path):
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    src = write_jsonl(tmp_path / "in.jsonl", synthetic_track(make_sfo_28r(), start_nm=8, end_nm=7.5))
    db = tmp_path / "out.sqlite"
    with pytest.raises(FileNotFoundError):
        replay(cfg, kind="jsonl", path=src, db_path=db, run_tag="missing", external_from=tmp_path / "no-such.sqlite")
    assert not db.exists() and not (tmp_path / "no-such.sqlite").exists()       # nor was it created as an empty one

import sqlite3
import subprocess
import sys
import threading
from dataclasses import replace

import pytest

from adsb_approach.config import StabilityCfg
from adsb_approach.geometry import NM_FT
from adsb_approach.model import Flags, Outcome, Position, Weather
from adsb_approach.reference import Reference
from adsb_approach.scorer import ScoredPoint, Scorer
from adsb_approach.store.db import (
    BACKFILL_FLAGS, MERGED_BACKFILL_FLAGS, MIGRATIONS_DIR, POINT_COLUMNS, PollPause, Store, current_poll_pause,
    external_positions)
from tests.conftest import QUIET_BRIDGE, make_ksfo, make_sfo_28r, path_track, synthetic_track


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.migrate()
    return s


def test_migrate_is_idempotent_and_creates_tables(store):
    store.migrate()
    names = {r["name"] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "flights", "approaches", "approach_gates", "approach_points", "approach_bins", "weather",
            "events", "annotations", "fleet_stats", "coverage_daily", "aircraft", "schema_version",
            "external_lookups", "external_polls", "external_positions"} <= names
    assert store.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 9


def test_run_flight_approach_round_trip(store):
    run = store.new_run("replay", "chunks", note="test")
    fid = store.insert_flight("a44540", "UAL2177", "N37415", "B39M", "airline", 1789240000.0)
    aid = store.open_approach(run_id=run, flight_id=fid, arrival_id=None, attempt_no=1, hex="a44540", callsign="UAL2177",
                              type_code="B39M", traffic_class="airline", approach_cat="C", airport="KSFO", runway="28L",
                              established_at=1789240100.0, first_along_nm=8.2, weather_id=None,
                              reference_source="cifp:CIFP_260903", prev_approach_id=None)
    assert store.approach(aid)["arrival_id"] == aid
    gates = [dict(gate_ft=1000, observed=True, t=1789240200.0, along_nm=3.1, gp_dev_ft=12.0, gp_dev_deg=0.04, gp_dots=0.11,
                  loc_dev_ft=-30.0, loc_dev_deg=-0.05, loc_dots=-0.06, gs=140.0, gs_corr=150.0, ias_est=148.0,
                  sink_fpm=750.0, nav_modes="approach", fleet_gp_delta_ft=None, fleet_gs_delta_kt=None),
             dict(gate_ft=500, observed=False, t=None, along_nm=None, gp_dev_ft=None, gp_dev_deg=None, gp_dots=None,
                  loc_dev_ft=None, loc_dev_deg=None, loc_dots=None, gs=None, gs_corr=None, ias_est=None, sink_fpm=None,
                  nav_modes=None, fleet_gp_delta_ft=None, fleet_gs_delta_kt=None)]
    points = [dict(t=1789240100.0 + i, lat=37.6, lon=-122.3, alt_baro=2000 - 100 * i, alt_geom=None, alt_qnh=2050.0 - 100 * i,
                   alt_corr=2060.0 - 100 * i, temp_corrected=True, gs=150.0, track=298.0, baro_rate=-700, geom_rate=None,
                   along_nm=6.0 - 0.3 * i, cross_ft=10.0, hat_ft=2047.0 - 100 * i, gp_dev_ft=5.0, gp_dev_deg=0.02,
                   loc_dev_ft=10.0, loc_dev_deg=0.01, gs_corr=160.0, sink_fpm=700.0, nav_altitude_mcp=3000, nav_qnh=1015.2,
                   nav_modes="approach", source="adsb_icao") for i in range(3)]
    bins = [dict(bin_nm=5.5, n=2, gp_dev_mean_ft=5.0, loc_dev_mean_ft=10.0, gs_corr_mean=160.0, sink_mean_fpm=700.0),
            dict(bin_nm=5.0, n=1, gp_dev_mean_ft=5.0, loc_dev_mean_ft=10.0, gs_corr_mean=160.0, sink_mean_fpm=700.0)]
    summary = dict(duration_s=120.0, first_along_nm=8.2, closest_along_nm=2.7, lowest_hat_ft=800.0, points=3,
                   descent_angle_deg=3.02, gp_dev_mean_ft=5.0, gp_dev_rms_ft=6.0, gp_dev_max_abs_ft=12.0,
                   loc_dev_rms_ft=15.0, loc_dev_max_abs_ft=30.0, gs_at_4nm=145.0, gs_at_2nm=None, sink_max_fpm=800.0)
    store.close_approach(aid, outcome=Outcome.LOST_ON_FINAL, flags=Flags.PRESUMED_LANDED | Flags.GATE500_UNOBSERVED,
                         ended_at=1789240220.0, summary=summary, gates=gates, points=points, bins=bins, ga=None)
    row = store.approach(aid)
    assert row["outcome"] == "lost_on_final" and row["flags"] == int(Flags.PRESUMED_LANDED | Flags.GATE500_UNOBSERVED)
    assert row["lowest_hat_ft"] == 800.0 and row["points"] == 3 and row["duration_s"] == 120.0
    assert [g["gate_ft"] for g in store.gates(aid)] == [1000, 500] and store.gates(aid)[1]["observed"] == 0
    assert len(store.points(aid)) == 3 and store.points(aid)[0]["nav_modes"] == "approach"
    assert (store.points(aid)[0]["origin"], store.points(aid)[0]["source"]) == ("local", "adsb_icao")
    assert [b["bin_nm"] for b in store.bins(aid)] == [5.0, 5.5]
    assert [r["id"] for r in store.approaches(run_id=run)] == [aid]
    store.set_confirmation(aid, "departure", Flags.PRESUMED_LANDED | Flags.GATE500_UNOBSERVED | Flags.LANDING_CONFIRMED)
    assert store.approach(aid)["landed_confirmed_by"] == "departure"
    store.reclassify(aid, Outcome.GO_AROUND, Flags.INFERRED_GO_AROUND)
    assert store.approach(aid)["outcome"] == "go_around" and store.approach(aid)["flags"] == int(Flags.INFERRED_GO_AROUND)


def test_arrival_chain_and_go_around_fields(store):
    run = store.new_run("live", "readsb-json")
    fid = store.insert_flight("ac2706", "UAL2432", None, None, "airline", 1.0)
    a1 = store.open_approach(run_id=run, flight_id=fid, arrival_id=None, attempt_no=1, hex="ac2706", callsign="UAL2432",
                             type_code=None, traffic_class="airline", approach_cat=None, airport="KSFO", runway="28L",
                             established_at=10.0, first_along_nm=7.0, weather_id=None, reference_source="x", prev_approach_id=None)
    a2 = store.open_approach(run_id=run, flight_id=fid, arrival_id=a1, attempt_no=2, hex="ac2706", callsign="UAL2432",
                             type_code=None, traffic_class="airline", approach_cat=None, airport="KSFO", runway="28R",
                             established_at=50.0, first_along_nm=3.0, weather_id=None, reference_source="x", prev_approach_id=a1)
    empty = dict(duration_s=40.0, first_along_nm=7.0, closest_along_nm=3.0, lowest_hat_ft=900.0, points=0, descent_angle_deg=None,
                 gp_dev_mean_ft=None, gp_dev_rms_ft=None, gp_dev_max_abs_ft=None, loc_dev_rms_ft=None, loc_dev_max_abs_ft=None,
                 gs_at_4nm=None, gs_at_2nm=None, sink_max_fpm=None)
    store.close_approach(a1, outcome=Outcome.RUNWAY_CHANGE, flags=Flags.NONE, ended_at=50.0, summary=empty, gates=[], points=[], bins=[], ga=None)
    store.close_approach(a2, outcome=Outcome.GO_AROUND, flags=Flags.SIDESTEP, ended_at=90.0, summary=empty, gates=[], points=[], bins=[],
                         ga=dict(initiated_at=80.0, initiated_along_nm=1.2, initiated_hat_ft=400.0, initiated_gs=140.0))
    r1, r2 = store.approach(a1), store.approach(a2)
    assert (r1["next_approach_id"], r2["prev_approach_id"], r2["arrival_id"], r2["attempt_no"]) == (a2, a1, a1, 2)
    assert r2["ga_initiated_hat_ft"] == 400.0 and r1["outcome"] == "runway_change"
    assert [r["id"] for r in store.arrival(a1)] == [a1, a2]


def test_weather_and_events(store):
    w = Weather("KSFO", 1789239360.0, 1015.3, 20.6, 11.7, 280, 14, 24, "METAR KSFO 121856Z")
    wid = store.insert_weather(w)
    assert store.insert_weather(w) == wid                       # duplicate observation → same row
    assert store.weather_row(wid)["altim_hpa"] == 1015.3
    e1 = store.insert_event(1.0, "approach.started", approach_id=None, flight_id=None, payload={"a": 1})
    e2 = store.insert_event(2.0, "health", approach_id=None, flight_id=None, payload={})
    assert e2 == e1 + 1 and store.events(since_id=e1)[0]["kind"] == "health"


def test_weather_since_returns_observations_at_or_after_a_time_oldest_first(store):
    old = Weather("KSFO", 1000.0, 1013.0, 15.0, 10.0, 280, 10, None, "old")
    new = Weather("KOAK", 2000.0, 1015.0, 16.0, 11.0, 270, 12, 18, "new")
    store.insert_weather(new)                     # inserted out of order: the query must sort, not rely on rowid
    store.insert_weather(old)
    assert store.weather_since(1500.0) == [new]
    assert store.weather_since(1000.0) == [old, new]
    assert store.weather_since(2000.1) == []


def test_foreign_keys_enforced(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.open_approach(run_id=999, flight_id=None, arrival_id=None, attempt_no=1, hex="x", callsign=None, type_code=None,
                            traffic_class="other", approach_cat=None, airport="KSFO", runway="28R", established_at=1.0,
                            first_along_nm=1.0, weather_id=None, reference_source="x", prev_approach_id=None)


def test_migration_killed_before_its_version_row_leaves_no_partial_schema(tmp_path):
    db_path = tmp_path / "power-cut.sqlite"
    child = ("import os, sys\n"
             "import adsb_approach.store.db as db\n"
             "db.time.time = lambda: os._exit(3)\n"        # dies after the script ran, while building the version row
             "db.Store(sys.argv[1]).migrate()\n")
    assert subprocess.run([sys.executable, "-c", child, str(db_path)]).returncode == 3
    s = Store(db_path)
    assert s.conn.execute("SELECT name FROM sqlite_master WHERE name='runs'").fetchone() is None
    s.migrate()
    assert s.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 9


def test_failing_migration_rolls_back_the_whole_script(tmp_path, monkeypatch):
    import adsb_approach.store.db as db
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    sql = (db.MIGRATIONS_DIR / "0001_init.sql").read_text()
    (migrations / "0001_init.sql").write_text(sql + "\nCREATE TABLE runs (id INTEGER);\n")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations)
    s = Store(tmp_path / "f.sqlite")
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        s.migrate()
    assert [r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")] == ["schema_version"]
    (migrations / "0001_init.sql").write_text(sql)
    s.migrate()
    assert s.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 1


def open_attempt(store, run_id, hexid="a44540", established_at=10.0, prev=None):
    return store.open_approach(run_id=run_id, flight_id=None, arrival_id=prev, attempt_no=1 if prev is None else 2, hex=hexid,
                               callsign=None, type_code=None, traffic_class="other", approach_cat=None, airport="KSFO",
                               runway="28R", established_at=established_at, first_along_nm=8.0, weather_id=None,
                               reference_source="x", prev_approach_id=prev)


def open_attempt_before_0008(store, run_id, hexid="a44540"):
    """open_attempt for a database still at an older schema, which Store.open_approach cannot write to: it sets
    approaches.path, a column migration 0008 adds."""
    aid = store.conn.execute(
        "INSERT INTO approaches (run_id, attempt_no, hex, traffic_class, airport, runway, established_at, first_along_nm, "
        "reference_source, created_at) VALUES (?, 1, ?, 'other', 'KSFO', '28R', 10.0, 8.0, 'x', 1.0)", (run_id, hexid)).lastrowid
    store.conn.execute("UPDATE approaches SET arrival_id=? WHERE id=?", (aid, aid))
    return aid


def close_attempt(store, approach_id, outcome, ended_at):
    store.close_approach(approach_id, outcome=outcome, flags=Flags.NONE, ended_at=ended_at, summary={}, gates=[], points=[],
                         bins=[], ga=None)


def test_units_of_work_commit_with_their_batch_and_fail_alone(store, tmp_path):
    reader = sqlite3.connect(tmp_path / "t.sqlite")
    run = store.new_run("live", "readsb-json")
    store.begin_batch()
    a1 = open_attempt(store, run)
    a2 = open_attempt(store, run, established_at=50.0, prev=a1)          # links a1 forward in the same unit of work
    with pytest.raises(sqlite3.IntegrityError):
        with store.atomic():
            store.conn.execute("UPDATE approaches SET runway='28L' WHERE id=?", (a1,))
            open_attempt(store, 999)                                       # no such run: this whole unit rolls back
    close_attempt(store, a1, Outcome.RUNWAY_CHANGE, 50.0)
    assert reader.execute("SELECT COUNT(*) FROM approaches").fetchone()[0] == 0        # nothing committed yet
    store.commit_batch()
    assert reader.execute("SELECT id, runway, outcome, next_approach_id, prev_approach_id FROM approaches ORDER BY id").fetchall() == [
        (a1, "28R", "runway_change", a2, None), (a2, "28R", None, None, a1)]


def test_atomic_reraises_its_own_error_when_sqlite_has_already_rolled_back(store):
    with pytest.raises(ValueError, match="boom"):
        with store.atomic():
            store.conn.execute("ROLLBACK")            # simulates SQLite already unwinding the whole transaction
            raise ValueError("boom")
    assert not store.conn.in_transaction


def test_failing_unit_outside_a_batch_leaves_no_open_savepoint(store, tmp_path):
    reader = sqlite3.connect(tmp_path / "t.sqlite")
    run = store.new_run("live", "readsb-json")
    with pytest.raises(sqlite3.IntegrityError):
        open_attempt(store, 999)                       # no such run: this unit rolls back
    assert not store.conn.in_transaction
    a1 = open_attempt(store, run)                       # not in a batch: commits on its own
    assert reader.execute("SELECT COUNT(*) FROM approaches WHERE id=?", (a1,)).fetchone()[0] == 1


def test_conn_rejects_use_from_another_thread(store):
    """Store is documented as one connection used by one thread; check_same_thread must not paper over a violation."""
    errors = []

    def other_thread():
        try:
            store.conn.execute("SELECT 1")
        except sqlite3.ProgrammingError as exc:
            errors.append(exc)

    t = threading.Thread(target=other_thread)
    t.start()
    t.join()
    assert len(errors) == 1
    assert store.conn.execute("SELECT 1").fetchone()[0] == 1          # still fine from its own thread


def test_sweep_closes_what_a_crashed_live_run_left_open(store):
    crashed = store.new_run("live", "readsb-json")
    left_open = open_attempt(store, crashed, established_at=100.0)
    finished = open_attempt(store, crashed, hexid="ac2706", established_at=50.0)
    close_attempt(store, finished, Outcome.LANDED, 90.0)
    replaying = store.new_run("replay", "chunks", note="elsewhere")
    in_replay = open_attempt(store, replaying, hexid="a1b2c3")
    assert store.sweep_interrupted() == (1, 1)
    row = store.approach(left_open)
    assert (row["outcome"], Flags(row["flags"]), row["ended_at"]) == ("lost", Flags.INTERRUPTED, 100.0)
    assert store.approach(finished)["outcome"] == "landed" and store.approach(in_replay)["outcome"] is None
    ended_at, note = store.conn.execute("SELECT ended_at, note FROM runs WHERE id=?", (crashed,)).fetchone()
    assert ended_at is not None and note == "[failed: interrupted]"
    assert store.conn.execute("SELECT ended_at FROM runs WHERE id=?", (replaying,)).fetchone()[0] is None
    assert store.sweep_interrupted() == (0, 0)


def test_sweep_leaves_a_run_whose_collector_is_still_running(tmp_path):
    first = Store(tmp_path / "t.sqlite")
    first.migrate()
    assert first.sweep_interrupted() == (0, 0)                        # a collector starts: the sweep, then its run
    run = first.new_run("live", "readsb-json")
    collecting = open_attempt(first, run, established_at=100.0)
    second = Store(tmp_path / "t.sqlite")                              # another `run` on the same database
    with pytest.raises(RuntimeError, match="another live collector"):
        second.sweep_interrupted()
    assert second.approach(collecting)["outcome"] is None
    assert second.conn.execute("SELECT ended_at FROM runs WHERE id=?", (run,)).fetchone()[0] is None
    first.close()                                                      # gone without ending its run, as in a crash
    assert second.sweep_interrupted() == (1, 1)
    second.close()


def test_sweep_closes_an_approach_left_open_in_a_live_run_that_ended(store):
    # run_live ends a failed run even after its last batch rolled back (SQLITE_FULL or IOERR at COMMIT), so an approach
    # that batch closed is open again in an ended run. The sweep holds the live lock: no collector can still own it.
    ended = store.new_run("live", "readsb-json")
    left_open = open_attempt(store, ended, established_at=100.0)
    store.end_run(ended, error="OperationalError: database or disk is full")
    replaying = store.new_run("replay", "chunks", note="elsewhere")
    in_replay = open_attempt(store, replaying, hexid="a1b2c3")
    assert store.sweep_interrupted() == (1, 0)
    row = store.approach(left_open)
    assert (row["outcome"], Flags(row["flags"]), row["ended_at"]) == ("lost", Flags.INTERRUPTED, 100.0)
    assert store.approach(in_replay)["outcome"] is None


def old_schema_run(store):
    """A live run on a database that predates runs.external_merge (migration 7), which new_run writes."""
    cur = store.conn.execute("INSERT INTO runs (kind, started_at, source) VALUES ('live', 1.0, 'readsb-json')")
    return cur.lastrowid


def test_migration_2_keeps_stored_points_local_and_adds_the_lookup_table(tmp_path, monkeypatch):
    import adsb_approach.store.db as db
    only_first = tmp_path / "migrations"
    only_first.mkdir()
    (only_first / "0001_init.sql").write_text((db.MIGRATIONS_DIR / "0001_init.sql").read_text())
    every = db.MIGRATIONS_DIR
    monkeypatch.setattr(db, "MIGRATIONS_DIR", only_first)
    s = Store(tmp_path / "before.sqlite")
    try:
        s.migrate()                                        # a database as migration 1 left it
        aid = open_attempt_before_0008(s, old_schema_run(s))
        s.conn.execute("INSERT INTO approach_points (approach_id, t, along_nm, hat_ft, temp_corrected, source) "
                       "VALUES (?, 100.0, 3.0, 950.0, 1, 'adsb_icao')", (aid,))
        monkeypatch.setattr(db, "MIGRATIONS_DIR", every)
        s.migrate()
        point = s.conn.execute("SELECT source, origin, on_ground, qnh_assumed FROM approach_points").fetchone()
        assert tuple(point) == ("adsb_icao", "local", None, None)
        assert s.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 9
        s.conn.execute("INSERT INTO external_lookups (approach_id, hex, closed_outcome, due_at) "
                       "VALUES (?, 'a44540', 'lost_on_final', 280.0)", (aid,))
        lookup = s.conn.execute("SELECT status, attempts, last_fetch_at, http_status, last_error, points_added, note "
                                "FROM external_lookups").fetchone()
        assert tuple(lookup) == ("pending", 0, None, None, None, 0, None)
        plan = s.conn.execute("EXPLAIN QUERY PLAN SELECT * FROM external_lookups WHERE status IN ('pending', 'retry') "
                              "ORDER BY due_at").fetchall()
        assert any("external_lookups_due" in row[3] for row in plan)
        with pytest.raises(sqlite3.IntegrityError):
            s.conn.execute("UPDATE external_lookups SET status = 'finished'")
    finally:
        s.close()


def test_migration_3_keeps_every_lookup_as_it_was_and_adds_the_offset_and_gap_columns(tmp_path, monkeypatch):
    import adsb_approach.store.db as db
    first_two = tmp_path / "migrations"
    first_two.mkdir()
    for name in ("0001_init.sql", "0002_external.sql"):
        (first_two / name).write_text((db.MIGRATIONS_DIR / name).read_text())
    every = db.MIGRATIONS_DIR
    monkeypatch.setattr(db, "MIGRATIONS_DIR", first_two)
    s = Store(tmp_path / "before.sqlite")
    try:
        s.migrate()                                        # a database as migration 2 left it, holding two lookups
        run = old_schema_run(s)
        done, waiting = open_attempt_before_0008(s, run, hexid="a1b2c3"), open_attempt_before_0008(s, run, hexid="a1b2c4")
        s.conn.execute("INSERT INTO external_lookups (approach_id, hex, closed_outcome, due_at, status, attempts, "
                       "last_fetch_at, http_status, points_added, note, overlap_n, offset_s, offset_spread_s, "
                       "offset_ft, offset_spread_ft) VALUES (?, 'a1b2c3', 'lost_on_final', 280.0, 'done', 1, 281.0, "
                       "200, 42, 'landed', 12, 0.8, 0.2, -25.0, 6.0)", (done,))
        s.conn.execute("INSERT INTO external_lookups (approach_id, hex, closed_outcome, due_at) "
                       "VALUES (?, 'a1b2c4', 'go_around', 300.0)", (waiting,))
        before = [tuple(r) for r in s.conn.execute("SELECT * FROM external_lookups ORDER BY approach_id")]
        monkeypatch.setattr(db, "MIGRATIONS_DIR", every)
        s.migrate()
        assert s.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 9
        rows = list(s.conn.execute("SELECT * FROM external_lookups ORDER BY approach_id"))
        assert [tuple(r)[:len(before[0])] for r in rows] == before              # every value each row held
        assert [(r["match_n"], r["match_offset_s"], r["match_spread_s"], r["applied_offset_s"], r["offset_source"],
                 r["gaps_filled"], r["gap_points_added"]) for r in rows] == [(None, None, None, None, None, 0, 0)] * 2
        with pytest.raises(sqlite3.IntegrityError):
            s.conn.execute("UPDATE external_lookups SET offset_source = 'guessed'")
    finally:
        s.close()


FLEET_ROW = dict(airport="KSFO", runway="28R", key_kind="type", key="B738", bin_nm=1.5, n=40, key_n=142, gp_p25=-10.0,
                 gp_p50=0.0, gp_p75=10.0, gs_p25=138.0, gs_p50=142.0, gs_p75=146.0)
COVERAGE_ROW = dict(airport="KSFO", runway="28R", day="2026-09-12", n=7, lowest_hat_p25=40.0, lowest_hat_p50=60.0,
                    lowest_hat_p75=90.0, closest_nm_p50=0.3, gate1000_observed_frac=1.0, gate500_observed_frac=0.5,
                    backfill_n=3, backfill_frac=3 / 7)


def test_a_rebuild_replaces_fleet_stats_and_keeps_only_the_coverage_days_it_is_told_to(store):
    """fleet_stats is rewritten whole, while a coverage day the rebuild did not compute again stays exactly as it was,
    and a day outside the window goes."""
    twelfth, thirteenth = ("KSFO", "28R", "2026-09-12"), ("KSFO", "28R", "2026-09-13")
    store.replace_norms([FLEET_ROW], [COVERAGE_ROW, {**COVERAGE_ROW, "day": "2026-09-13", "n": 9}],
                        {twelfth, thirteenth}, built_at=100.0)
    assert [dict(r) for r in store.fleet_stats_rows()] == [{**FLEET_ROW, "built_at": 100.0}]
    assert [(r["day"], r["n"], r["built_at"]) for r in store.coverage_daily_rows()] == [
        ("2026-09-12", 7, 100.0), ("2026-09-13", 9, 100.0)]
    store.replace_norms([{**FLEET_ROW, "key": "A20N"}], [{**COVERAGE_ROW, "day": "2026-09-13", "n": 11}],
                        {twelfth, thirteenth}, built_at=200.0)
    assert [(r["key"], r["built_at"]) for r in store.fleet_stats_rows()] == [("A20N", 200.0)]
    assert [(r["day"], r["n"], r["built_at"]) for r in store.coverage_daily_rows()] == [
        ("2026-09-12", 7, 100.0), ("2026-09-13", 11, 200.0)]      # the 12th was kept, untouched, the 13th rewritten
    store.replace_norms([], [], {thirteenth}, built_at=300.0)
    assert store.fleet_stats_rows() == [] and [r["day"] for r in store.coverage_daily_rows()] == ["2026-09-13"]


def test_the_reflag_writes_touch_only_the_bins_the_deltas_the_flags_and_the_key(store):
    run = store.new_run("live", "readsb-json")
    aid = open_attempt(store, run)
    gates = [dict(gate_ft=g, observed=True, t=100.0, along_nm=a, gp_dev_ft=5.0, gp_dev_deg=0.02, gp_dots=0.06,
                  loc_dev_ft=10.0, loc_dev_deg=0.01, loc_dots=0.01, gs=140.0, gs_corr=150.0, ias_est=148.0,
                  sink_fpm=700.0, nav_modes="approach", fleet_gp_delta_ft=None, fleet_gs_delta_kt=None)
             for g, a in ((1000, 3.2), (500, 1.6))]
    store.close_approach(aid, outcome=Outcome.LANDED, flags=Flags.PRESUMED_LANDED, ended_at=500.0, summary={},
                         gates=gates, points=[], ga=None,
                         bins=[dict(bin_nm=3.0, n=4, gp_dev_mean_ft=5.0, loc_dev_mean_ft=1.0, gs_corr_mean=150.0,
                                    sink_mean_fpm=-700.0)])
    assert [r["id"] for r in store.approaches_to_reflag(since=400.0)] == [aid]
    assert store.approaches_to_reflag(since=501.0) == [] and store.approaches_to_reflag(since=0.0, until=500.0) == []
    store.replace_bins(aid, [dict(bin_nm=1.5, n=2, gp_dev_mean_ft=3.0, loc_dev_mean_ft=0.5, gs_corr_mean=151.0,
                                  sink_mean_fpm=-600.0)])
    store.set_gate_deltas(aid, [(1000, 4.0, 8.0), (500, None, None)])
    store.set_fleet(aid, flags=Flags.PRESUMED_LANDED | Flags.FAST, fleet_key="type:B738")
    assert [(b["bin_nm"], b["gs_corr_mean"]) for b in store.bins(aid)] == [(1.5, 151.0)]
    assert [(g["fleet_gp_delta_ft"], g["fleet_gs_delta_kt"]) for g in store.gates(aid)] == [(4.0, 8.0), (None, None)]
    assert [(g["gs_corr"], g["gp_dev_ft"], g["along_nm"]) for g in store.gates(aid)] == [(150.0, 5.0, 3.2),
                                                                                         (150.0, 5.0, 1.6)]
    row = store.approach(aid)
    assert (row["outcome"], row["ended_at"], row["fleet_key"]) == ("landed", 500.0, "type:B738")
    assert Flags(row["flags"]) == Flags.PRESUMED_LANDED | Flags.FAST


def test_a_store_can_be_told_to_wait_longer_for_another_writer(tmp_path):
    """The collector keeps sqlite3.connect's own 5 s; the fleet rebuild and reflag ask for 30 s, since they write
    beside it."""
    collector = Store(tmp_path / "t.sqlite")
    patient = Store(tmp_path / "t.sqlite", busy_timeout_s=30.0)
    assert collector.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert patient.conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    collector.close()
    patient.close()


def test_a_writer_gets_database_is_locked_while_another_holds_the_write_lock_this_is_the_starvation_the_service_buffer_and_reflags_batching_exist_to_avoid(tmp_path):
    """Two connections, the first holding the write lock: the second's own write fails at once with "database is
    locked" once its (short, so the test stays fast) busy timeout runs out, instead of waiting past it -- and it
    succeeds as soon as the first commits and releases the lock. service.py's drain (a lock-free gap after every
    commit) and fleet.reflag's group commits both exist to keep a collector or a reflag run from holding the lock
    this way while another writer is waiting on it."""
    db = tmp_path / "t.sqlite"
    holder = Store(db)
    holder.migrate()
    waiter = Store(db, busy_timeout_s=0.2)

    holder.begin_batch()
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        waiter.begin_batch()

    holder.commit_batch()
    waiter.begin_batch()          # the lock is free now that the holder has committed
    waiter.commit_batch()
    holder.close()
    waiter.close()


# --- the backfill columns, and the days a UTC rebuild wrote ---------------------------------------------------------

def _schema_at_0004(db) -> Store:
    """A database as migration 0004 left it: everything before the backfill columns, and nothing of 0005."""
    store = Store(db)
    for sql in sorted(MIGRATIONS_DIR.glob("*.sql")):        # 0001 creates schema_version itself
        version = int(sql.name.split("_")[0])
        if version >= 5:
            break
        store.conn.executescript(sql.read_text(encoding="utf-8"))
        store.conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, 0.0)", (version,))
    return store


def test_the_migration_adds_the_backfill_columns_and_empties_the_days_a_utc_rebuild_wrote(tmp_path):
    """A stored `day` was a UTC day and now means the configured local day, so every row means something else and the
    rebuild must write them all again."""
    store = _schema_at_0004(tmp_path / "old.sqlite")
    store.conn.execute("INSERT INTO coverage_daily (airport, runway, day, n, built_at) VALUES ('KSFO','28R','2026-09-12',7,1.0)")
    assert store.conn.execute("SELECT COUNT(*) FROM coverage_daily").fetchone()[0] == 1
    assert {r[1] for r in store.conn.execute("PRAGMA table_info(coverage_daily)")} & {"backfill_n", "backfill_frac"} == set()

    store.migrate()

    assert store.conn.execute("SELECT COUNT(*) FROM coverage_daily").fetchone()[0] == 0
    assert store.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 9
    assert {"backfill_n", "backfill_frac"} <= {r[1] for r in store.conn.execute("PRAGMA table_info(coverage_daily)")}
    store.migrate()                                              # and the emptying does not happen twice
    store.close()


DAY_T = 1789240000.0                                   # 2026-09-12 19:06:40Z
ONE_BUCKET = lambda t: "day"                           # noqa: E731 - every approach in one day, so the counts add up


def closed_attempt(store, run_id, *, ended_at, flags=Flags.NONE, runway="28R", close=True):
    """One attempt on KSFO, closed as landed unless `close` is false, for the day-count tests."""
    aid = store.open_approach(run_id=run_id, flight_id=None, arrival_id=None, attempt_no=1, hex="a44540",
                              callsign="UAL1", type_code="B738", traffic_class="airline", approach_cat="C",
                              airport="KSFO", runway=runway, established_at=ended_at - 300.0, first_along_nm=8.0,
                              weather_id=None, reference_source="cifp:test", prev_approach_id=None)
    if close:
        store.close_approach(aid, outcome=Outcome.LANDED, flags=flags, ended_at=ended_at, summary={}, gates=[],
                             points=[], bins=[], ga=None)
    return aid


def test_the_day_counts_leave_out_replays_open_attempts_and_interrupted_ones(store):
    """Every filter coverage_day_counts applies, now that it folds the rows in Python rather than in SQL."""
    live, replay = store.new_run("live", "test"), store.new_run("replay", "chunks")
    closed_attempt(store, live, ended_at=DAY_T)
    closed_attempt(store, live, ended_at=DAY_T + 60.0, runway="28L")
    closed_attempt(store, live, ended_at=DAY_T + 120.0)
    closed_attempt(store, live, ended_at=DAY_T - 7200.0)                 # before `since`
    closed_attempt(store, live, ended_at=DAY_T, flags=Flags.INTERRUPTED)  # no points, gates or summary
    closed_attempt(store, live, ended_at=DAY_T, close=False)             # still open
    closed_attempt(store, replay, ended_at=DAY_T)
    counts = store.coverage_day_counts(since=DAY_T - 3600.0, day_of=ONE_BUCKET)
    assert [(c["runway"], c["n"]) for c in counts] == [("28L", 1), ("28R", 2)]
    with_replays = store.coverage_day_counts(since=DAY_T - 3600.0, day_of=ONE_BUCKET, include_replays=True)
    assert [(c["runway"], c["n"]) for c in with_replays] == [("28L", 1), ("28R", 3)]
    assert [(c["airport"], c["day"]) for c in with_replays] == [("KSFO", "day"), ("KSFO", "day")]


def test_the_day_counts_say_how_many_approaches_carry_adsb_lol_positions(store):
    """EXTERNAL_OUTCOME rides along on a spliced approach and counts through its tail flag; a flag that says nothing
    about where the points came from does not count at all."""
    live = store.new_run("live", "test")
    closed_attempt(store, live, ended_at=DAY_T)
    closed_attempt(store, live, ended_at=DAY_T + 60.0, flags=Flags.EXTERNAL_TAIL)
    closed_attempt(store, live, ended_at=DAY_T + 120.0, flags=Flags.EXTERNAL_GAPS)
    closed_attempt(store, live, ended_at=DAY_T + 180.0, flags=Flags.EXTERNAL_TAIL | Flags.EXTERNAL_OUTCOME)
    closed_attempt(store, live, ended_at=DAY_T + 240.0, flags=Flags.LATE_ESTABLISHED)
    closed_attempt(store, live, ended_at=DAY_T + 300.0, flags=Flags.EXTERNAL_TAIL | Flags.INTERRUPTED)
    counts = store.coverage_day_counts(since=DAY_T - 3600.0, day_of=ONE_BUCKET)
    assert [(c["n"], c["backfill_n"]) for c in counts] == [(5, 3)]
    assert BACKFILL_FLAGS == Flags.EXTERNAL_TAIL | Flags.EXTERNAL_GAPS


# --- emergency squawks: add_flags and the sticky rule ---------------------------------------------------------------

def test_add_flags_ors_new_bits_leaving_the_rest_alone(store):
    run = store.new_run("live", "readsb-json")
    aid = open_attempt(store, run)
    close_attempt(store, aid, Outcome.LOST, 50.0)
    store.add_flags(aid, Flags.EMERGENCY_GENERAL)
    assert Flags(store.approach(aid)["flags"]) == Flags.EMERGENCY_GENERAL
    store.add_flags(aid, Flags.MINIMUM_FUEL)                      # a second, unrelated bit
    assert Flags(store.approach(aid)["flags"]) == Flags.EMERGENCY_GENERAL | Flags.MINIMUM_FUEL
    store.add_flags(aid, Flags.EMERGENCY_GENERAL)                 # already set: OR is idempotent
    assert Flags(store.approach(aid)["flags"]) == Flags.EMERGENCY_GENERAL | Flags.MINIMUM_FUEL


def test_close_approach_keeps_a_latch_the_row_already_carries(store):
    run = store.new_run("live", "readsb-json")
    aid = open_attempt(store, run)
    store.add_flags(aid, Flags.EMERGENCY_GENERAL)                 # stamped at open, before this attempt closes
    store.close_approach(aid, outcome=Outcome.LOST, flags=Flags.PRESUMED_LANDED, ended_at=50.0, summary={},
                         gates=[], points=[], bins=[], ga=None)
    assert Flags(store.approach(aid)["flags"]) == Flags.PRESUMED_LANDED | Flags.EMERGENCY_GENERAL


def test_reclassify_keeps_a_latch_its_own_flags_argument_does_not_carry(store):
    """reclassify writes back a copy of flags the matcher captured at close; without the mask, a flag latched just after
    the close (the matcher's path 4) would be lost right here."""
    run = store.new_run("live", "readsb-json")
    aid = open_attempt(store, run)
    close_attempt(store, aid, Outcome.LOST_ON_FINAL, 50.0)
    store.add_flags(aid, Flags.EMERGENCY_NORDO)                   # a path-4 latch after the close
    store.reclassify(aid, Outcome.GO_AROUND, Flags.INFERRED_GO_AROUND)
    row = store.approach(aid)
    assert row["outcome"] == "go_around"
    assert Flags(row["flags"]) == Flags.INFERRED_GO_AROUND | Flags.EMERGENCY_NORDO


def test_set_confirmation_keeps_a_latch_its_own_flags_argument_does_not_carry(store):
    run = store.new_run("live", "readsb-json")
    aid = open_attempt(store, run)
    close_attempt(store, aid, Outcome.LOST_ON_FINAL, 50.0)
    store.add_flags(aid, Flags.EMERGENCY_UNLAWFUL)
    store.set_confirmation(aid, "ground", Flags.PRESUMED_LANDED | Flags.LANDING_CONFIRMED)
    row = store.approach(aid)
    assert row["landed_confirmed_by"] == "ground"
    assert Flags(row["flags"]) == Flags.PRESUMED_LANDED | Flags.LANDING_CONFIRMED | Flags.EMERGENCY_UNLAWFUL


def test_set_fleet_keeps_a_latch_reflags_own_mask_does_not_carry(store):
    run = store.new_run("live", "readsb-json")
    aid = open_attempt(store, run)
    close_attempt(store, aid, Outcome.LANDED, 50.0)
    store.add_flags(aid, Flags.MINIMUM_FUEL)
    store.set_fleet(aid, flags=Flags.FAST, fleet_key="type:B738")
    row = store.approach(aid)
    assert row["fleet_key"] == "type:B738"
    assert Flags(row["flags"]) == Flags.FAST | Flags.MINIMUM_FUEL


def test_a_coverage_row_carries_the_backfill_count_and_its_share(store):
    store.replace_norms([], [COVERAGE_ROW], {("KSFO", "28R", "2026-09-12")}, built_at=1789240000.0)
    row = store.coverage_daily_rows()[0]
    assert (row["n"], row["backfill_n"]) == (7, 3) and row["backfill_frac"] == 3 / 7
    assert row["built_at"] == 1789240000.0


def _open(store, run, *, path=None, **kw):
    return store.open_approach(run_id=run, flight_id=None, arrival_id=None, attempt_no=1, hex="407699", callsign="VIR19Z",
                               type_code="B789", traffic_class="airline", approach_cat="C", airport="KSFO", runway="28R",
                               established_at=1789240100.0, first_along_nm=8.2, weather_id=None,
                               reference_source="cifp:CIFP_260903", prev_approach_id=None, path=path, **kw)


def _close(store, aid, *, points, **kw):
    store.close_approach(aid, outcome=Outcome.LANDED, flags=Flags.NONE, ended_at=1789240220.0, summary={}, gates=[],
                         points=points, bins=[], ga=None, **kw)


def _scorer() -> Scorer:
    return Scorer(StabilityCfg(), temp_max_age_s=5400.0, qnh_max_age_s=10800.0, gates_ft=[1000, 500], gate_max_bracket_s=30.0,
                  bin_nm=0.5, bin_max_nm=8.0)


def test_an_approach_stores_its_path_and_each_point_its_reference(store):
    rwy = make_sfo_28r(paths=(QUIET_BRIDGE,))
    ref, sc, apt = Reference(rwy, QUIET_BRIDGE), _scorer(), make_ksfo(rwy)
    track = path_track(ref, start_dtg_nm=3.0, end_dtg_nm=2.97)
    rows = [sc.score(rwy, apt, p, None, ref=ref).as_row() for p in track]
    rows.append(replace(sc.score(rwy, apt, track[-1], None), t=track[-1].t + 1.0).as_row())      # then the centreline
    run = store.new_run("replay", "chunks")
    flown = _open(store, run, path="quiet-bridge")
    assert store.approach(flown)["path"] == "quiet-bridge"
    _close(store, flown, points=rows, path="quiet-bridge")
    assert store.approach(flown)["path"] == "quiet-bridge"
    assert [p["reference"] for p in store.points(flown)] == ["quiet-bridge"] * len(track) + [None]
    late = _open(store, run)                                          # a path found after the attempt opened
    assert store.approach(late)["path"] is None
    _close(store, late, points=[], path="quiet-bridge")
    assert store.approach(late)["path"] == "quiet-bridge"
    unnamed = _open(store, run, path="quiet-bridge")                  # a close that names no path keeps the one it opened with
    _close(store, unnamed, points=[])
    assert store.approach(unnamed)["path"] == "quiet-bridge"
    straight = _open(store, run)
    _close(store, straight, points=rows[-1:])
    assert store.approach(straight)["path"] is None and [p["reference"] for p in store.points(straight)] == [None]


def test_an_open_approach_can_be_told_its_path_changed(store):
    flown = _open(store, store.new_run("replay", "chunks"), path="rnp-y")
    store.set_path(flown, "quiet-bridge")
    assert store.approach(flown)["path"] == "quiet-bridge" and store.approach(flown)["outcome"] is None
    _close(store, flown, points=[])                                   # a close that names no path keeps it
    assert store.approach(flown)["path"] == "quiet-bridge"


def test_a_stored_point_without_a_reference_column_reads_as_the_centreline(store):
    rwy = make_sfo_28r()
    row = _scorer().score(rwy, make_ksfo(rwy), synthetic_track(rwy)[0], None).as_row()
    aid = _open(store, store.new_run("replay", "chunks"))
    _close(store, aid, points=[row])
    columns = ", ".join(c for c in POINT_COLUMNS if c != "reference")
    old = store.conn.execute(f"SELECT {columns} FROM approach_points WHERE approach_id=?", (aid,)).fetchone()
    assert "reference" not in old.keys()                              # a row as an unmigrated database returns it
    back = ScoredPoint.from_row(old)
    assert back.reference is None and back.track_dev_deg is None and back.along_ft == pytest.approx(row["along_nm"] * NM_FT)
    assert ScoredPoint.from_row(store.points(aid)[0]).reference is None


# --- live merge: the poll tables, add_points and the merged run's coverage ------------------------------------------

def test_a_run_records_whether_it_merged_adsb_lol(store):
    merged = store.new_run("live", "readsb-json", external_merge=True)
    plain = store.new_run("live", "readsb-json")
    assert store.conn.execute("SELECT external_merge FROM runs WHERE id=?", (merged,)).fetchone()[0] == 1
    assert store.conn.execute("SELECT external_merge FROM runs WHERE id=?", (plain,)).fetchone()[0] == 0


def poll(store, run, arrived_at, *, http_status=200, error=None, refused=False, returned=3, retry_after_s=None):
    """One re-api poll that arrived at `arrived_at`, a second after it was asked for."""
    return store.record_poll(run_id=run, requested_at=arrived_at - 1.0, arrived_at=arrived_at, asked=2,
                             returned=returned if http_status == 200 else None, http_status=http_status, error=error,
                             refused=refused, retry_after_s=retry_after_s)


def test_the_pause_comes_from_the_newest_poll_only(store):
    run = store.new_run("live", "readsb-json", external_merge=True)
    assert current_poll_pause(store.conn, 900.0, 1500.0) is None                    # nothing polled yet
    poll(store, run, 1000.0, http_status=429, error="HTTP 429", refused=True)
    assert current_poll_pause(store.conn, 900.0, 1500.0) == PollPause(1900.0, 429, "HTTP 429")
    assert current_poll_pause(store.conn, 900.0, 1900.0) is None                    # it ends at arrived_at + backoff
    poll(store, run, 900.0, http_status=403, error="HTTP 403", refused=True)       # older, though written later
    assert current_poll_pause(store.conn, 900.0, 1500.0) == PollPause(1900.0, 429, "HTTP 429")
    poll(store, run, 1100.0)                                                        # a 200: the refusal is over
    assert current_poll_pause(store.conn, 900.0, 1500.0) is None
    poll(store, run, 1200.0, http_status=None, error="api_key_file: FileNotFoundError: no such file", refused=True)
    assert current_poll_pause(store.conn, 900.0, 1500.0) == PollPause(
        2100.0, None, "api_key_file: FileNotFoundError: no such file")
    poll(store, run, 1300.0, http_status=None, error="TimeoutError: timed out")     # a timeout skips one poll only
    assert current_poll_pause(store.conn, 900.0, 1500.0) is None


def test_the_pause_follows_retry_after_when_the_refusal_had_one(store):
    run = store.new_run("live", "readsb-json", external_merge=True)
    poll(store, run, 1000.0, http_status=429, error="HTTP 429 (Retry-After 40 s)", refused=True, retry_after_s=40.0)
    assert current_poll_pause(store.conn, 900.0, 1039.0) == PollPause(1040.0, 429, "HTTP 429 (Retry-After 40 s)")
    assert current_poll_pause(store.conn, 900.0, 1040.0) is None                    # not the 900 s backoff
    poll(store, run, 2000.0, http_status=429, error="HTTP 429", refused=True)       # no header: the backoff again
    assert current_poll_pause(store.conn, 900.0, 2899.0) == PollPause(2900.0, 429, "HTTP 429")
    assert [r[0] for r in store.conn.execute("SELECT retry_after_s FROM external_polls ORDER BY id")] == [40.0, None]
    poll(store, run, 3000.0, http_status=429, error="HTTP 429 (Retry-After 0 s)", refused=True, retry_after_s=0.0)
    assert current_poll_pause(store.conn, 900.0, 3000.0) is None                    # 0 means at once, not the backoff


def test_the_pause_copes_with_a_database_before_retry_after():
    # The web reads the collector's database read-only; until the collector has restarted on this code, its
    # external_polls has no retry_after_s, and the pause falls back to the backoff.
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE external_polls (id INTEGER PRIMARY KEY, run_id INTEGER, requested_at REAL, "
                 "arrived_at REAL, asked INTEGER, returned INTEGER, http_status INTEGER, error TEXT, refused INTEGER)")
    conn.execute("INSERT INTO external_polls VALUES (1, 1, 999.0, 1000.0, 2, NULL, 429, 'HTTP 429', 1)")
    assert current_poll_pause(conn, 900.0, 1500.0) == PollPause(1900.0, 429, "HTTP 429")


def test_the_pause_is_none_on_a_database_without_the_poll_table(store):
    store.conn.execute("DROP TABLE external_positions")
    store.conn.execute("DROP TABLE external_polls")
    assert current_poll_pause(store.conn, 900.0, 1500.0) is None


def test_a_poll_row_keeps_what_the_poll_came_to(store):
    run = store.new_run("live", "readsb-json", external_merge=True)
    ok = poll(store, run, 100.0, returned=7)
    refused = poll(store, run, 102.0, http_status=401, error="HTTP 401", refused=True)
    rows = {r["id"]: tuple(r) for r in store.conn.execute(
        "SELECT id, run_id, requested_at, arrived_at, asked, returned, http_status, error, refused "
        "FROM external_polls")}
    assert rows[ok] == (ok, run, 99.0, 100.0, 2, 7, 200, None, 0)
    assert rows[refused] == (refused, run, 101.0, 102.0, 2, None, 401, "HTTP 401", 1)


def ext_position(t, **changes):
    """A position with every field set, so a round trip loses nothing unnoticed."""
    p = Position(t=t, hex="a44540", lat=37.6 + t / 1e5, lon=-122.4, alt_baro=1200, on_ground=False, alt_geom=1275,
                 gs=141.5, track=284.0, baro_rate=-700, geom_rate=-650, callsign="UAL2177", squawk="4521",
                 category="A3", nav_qnh=1013.6, nav_altitude_mcp=3000, nav_modes=frozenset({"approach", "autopilot"}),
                 source="adsb_icao", reg="N37415", type_code="B39M", emergency="none")
    return replace(p, **changes)


def test_external_positions_are_kept_once_per_hex_and_time_and_read_back_in_arrival_order(store):
    run = store.new_run("live", "readsb-json", external_merge=True)
    first = ext_position(100.0)
    second = ext_position(101.0, on_ground=True, alt_baro=None, nav_modes=frozenset())
    third = ext_position(99.0, hex="a0b1c2", reg=None, type_code=None, emergency=None, callsign=None)
    one, two = poll(store, run, 110.0), poll(store, run, 112.0)
    assert store.insert_external_positions(one, 110.0, [second, first]) == 2
    assert store.insert_external_positions(two, 112.0, [first, second]) == 0       # the same two again
    assert store.insert_external_positions(two, 112.0, [first, second, third]) == 1
    # arrival order, then time: the later poll's position comes last though it is the oldest fix
    got = list(store.external_positions())
    assert [(arrived, p.hex, p.t) for arrived, p in got] == [
        (110.0, "a44540", 100.0), (110.0, "a44540", 101.0), (112.0, "a0b1c2", 99.0)]
    assert [p for _, p in got] == [replace(p, origin="adsb.lol") for p in (first, second, third)]
    assert first.origin == "local" and all(p.origin == "adsb.lol" for _, p in got)
    assert got[1][1].on_ground is True and got[1][1].nav_modes == frozenset()
    assert got[0][1].nav_modes == frozenset({"approach", "autopilot"})
    kept = store.conn.execute("SELECT poll_id FROM external_positions WHERE hex='a44540' AND t=100.0").fetchone()
    assert kept[0] == one                                                           # kept from the poll that brought it


def test_external_positions_can_be_read_through_a_connection_that_is_not_the_stores(store):
    run = store.new_run("live", "readsb-json", external_merge=True)
    store.insert_external_positions(poll(store, run, 110.0), 110.0, [ext_position(100.0)])
    reader = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)               # the web's read-only connection
    try:
        assert [(arrived, p.t, p.origin) for arrived, p in external_positions(reader)] == [(110.0, 100.0, "adsb.lol")]
    finally:
        reader.close()


def test_add_points_appends_to_a_closed_approach_and_never_replaces(store):
    run = store.new_run("live", "readsb-json")
    aid = open_attempt(store, run)
    store.close_approach(aid, outcome=Outcome.LOST, flags=Flags.NONE, ended_at=50.0, summary={}, gates=[],
                         points=[dict(t=1.0, lat=37.0, lon=-122.0), dict(t=2.0, lat=37.1, lon=-122.0)], bins=[],
                         ga=None)
    store.add_points(aid, [dict(t=2.0, lat=38.0, lon=-122.0), dict(t=3.0, lat=37.2, lon=-122.0, origin="adsb.lol")])
    assert [(r["t"], r["lat"], r["origin"]) for r in store.points(aid)] == [
        (1.0, 37.0, "local"), (2.0, 37.1, "local"), (3.0, 37.2, "adsb.lol")]


def test_points_added_for_the_charts_only_are_left_out_of_the_scored_ones(store):
    """A go-around's climb-out is stored by add_points(charts_only=True): the drawer reads it with every other point,
    the fleet rebuild's re-scoring does not."""
    run = store.new_run("live", "readsb-json")
    aid = open_attempt(store, run)
    store.close_approach(aid, outcome=Outcome.LOST, flags=Flags.NONE, ended_at=50.0, summary={}, gates=[],
                         points=[dict(t=1.0, lat=37.0, lon=-122.0), dict(t=2.0, lat=37.1, lon=-122.0)], bins=[],
                         ga=None)
    store.add_points(aid, [dict(t=3.0, lat=37.2, lon=-122.0)], charts_only=True)
    store.add_points(aid, [dict(t=4.0, lat=37.3, lon=-122.0)])
    store.add_points(aid, [dict(t=3.0, lat=38.0, lon=-122.0), dict(t=5.0, lat=37.4, lon=-122.0)], charts_only=True)
    assert [(r["t"], r["charts_only"]) for r in store.points(aid)] == [
        (1.0, 0), (2.0, 0), (3.0, 1), (4.0, 0), (5.0, 1)]                   # a point already held is left as stored
    assert [r["t"] for r in store.points(aid, scored_only=True)] == [1.0, 2.0, 4.0]


def test_close_approach_records_who_confirmed_the_landing(store):
    run = store.new_run("live", "readsb-json")
    confirmed, plain = open_attempt(store, run), open_attempt(store, run, hexid="a0b1c2")
    store.close_approach(confirmed, outcome=Outcome.LANDED, flags=Flags.LANDING_CONFIRMED, ended_at=50.0, summary={},
                         gates=[], points=[], bins=[], ga=None, landed_confirmed_by="adsb.lol")
    close_attempt(store, plain, Outcome.LANDED, 50.0)
    assert store.approach(confirmed)["landed_confirmed_by"] == "adsb.lol"
    assert store.approach(plain)["landed_confirmed_by"] is None


def test_coverage_counts_a_merged_run_s_endings_only(store):
    """A merged run fills gaps live, so EXTERNAL_GAPS no longer says the receiver needed adsb.lol; only the ending it
    was given (EXTERNAL_TAIL) does. Every other run keeps the two flags it always counted."""
    before = store.new_run("live", "readsb-json")
    merged = store.new_run("live", "readsb-json", external_merge=True)
    closed_attempt(store, before, ended_at=DAY_T)
    closed_attempt(store, before, ended_at=DAY_T + 60.0, flags=Flags.EXTERNAL_GAPS)
    closed_attempt(store, before, ended_at=DAY_T + 120.0, flags=Flags.EXTERNAL_TAIL)
    closed_attempt(store, merged, ended_at=DAY_T + 180.0)
    closed_attempt(store, merged, ended_at=DAY_T + 240.0, flags=Flags.EXTERNAL_GAPS)
    closed_attempt(store, merged, ended_at=DAY_T + 300.0, flags=Flags.EXTERNAL_TAIL)
    counts = store.coverage_day_counts(since=DAY_T - 3600.0, day_of=ONE_BUCKET)
    assert [(c["n"], c["backfill_n"]) for c in counts] == [(6, 3)]      # the two pre-merge ones and the merged tail
    assert MERGED_BACKFILL_FLAGS == Flags.EXTERNAL_TAIL


def test_the_migration_leaves_every_earlier_run_unmerged_so_past_days_keep_their_numbers(tmp_path):
    """An approach from before the switch counts either flag, as the day it was counted on did."""
    store = _schema_at_0004(tmp_path / "old.sqlite")
    run = old_schema_run(store)
    aid = open_attempt_before_0008(store, run)         # Store.close_approach writes approaches.path, which 0008 adds
    store.conn.execute("UPDATE approaches SET outcome='landed', flags=?, ended_at=? WHERE id=?",
                       (int(Flags.EXTERNAL_GAPS), DAY_T, aid))
    store.migrate()                                    # 0005 to 0008
    assert {5, 7, 8} <= {r[0] for r in store.conn.execute("SELECT version FROM schema_version")}
    assert store.conn.execute("SELECT external_merge FROM runs WHERE id=?", (run,)).fetchone()[0] == 0
    counts = store.coverage_day_counts(since=DAY_T - 3600.0, day_of=ONE_BUCKET)
    assert [(c["n"], c["backfill_n"]) for c in counts] == [(1, 1)]
    store.close()

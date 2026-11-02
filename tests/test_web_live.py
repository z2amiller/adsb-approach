import json

import pytest

from adsb_approach.model import Flags, Outcome
from adsb_approach.web.db import snapshot
from adsb_approach.web.live import FEED_SILENT_AFTER_S, health_report, live_cards
from tests.webdb import LiveWriter, copy_db


def cards(db):
    with snapshot(db) as conn:
        return live_cards(conn)["cards"]


def test_the_live_writer_closes_an_attempt_with_the_collectors_completed_payload(tmp_path, replay_db):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        aid = w.open("aaa001", "UAL1", along_nm=6.0, hat_ft=1900)
        w.update(aid, "aaa001", dt=5, along_nm=4.5, hat_ft=1400)
        w.close_attempt(aid, "aaa001", Outcome.LANDED, dt=9)
    finally:
        w.close()
    with snapshot(db) as conn:
        def completed(order):
            return json.loads(conn.execute("SELECT payload_json FROM events WHERE kind = 'approach.completed' "
                                           f"ORDER BY id {order} LIMIT 1").fetchone()[0])
        collector, written = completed("ASC"), completed("DESC")
    assert set(written) == set(collector)
    assert (written["arrival_id"], written["attempt_no"], written["lowest_hat_ft"], written["closest_along_nm"],
            written["gate1000"]) == (aid, 1, 1400, 4.5, None)


def test_rotorcraft_cards_are_left_out_unless_requested(tmp_path, replay_db):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        w.open("aaa001", "JOLLY93", traffic_class="rotorcraft", runway="28L", along_nm=6.0, hat_ft=1900)
        w.open("aaa002", "UAL1", runway="28R", along_nm=5.0, hat_ft=1500, dt=1)
        with snapshot(db) as conn:
            assert [c["hex"] for c in live_cards(conn)["cards"]] == ["aaa002"]
            shown = {c["hex"] for c in live_cards(conn, rotorcraft=True)["cards"]}
        assert shown == {"aaa001", "aaa002"}
    finally:
        w.close()


def test_a_card_with_no_traffic_class_is_not_hidden_as_a_rotorcraft(tmp_path, replay_db):
    # The same NULL trap as the arrivals filter: NULL != 'rotorcraft' is NULL, not true, so an attempt that was never
    # classified would be dropped from the live cards as if it were a helicopter.
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        unknown = w.open("aaa020", "N00141", runway="28L", along_nm=6.0, hat_ft=1900)
        w.open("aaa021", "JOLLY93", traffic_class="rotorcraft", runway="28R", along_nm=5.0, hat_ft=1500, dt=1)
        w.store.conn.execute("UPDATE approaches SET traffic_class = NULL WHERE id = ?", (unknown,))
        with snapshot(db) as conn:
            assert [c["hex"] for c in live_cards(conn)["cards"]] == ["aaa020"]
            shown = {c["hex"] for c in live_cards(conn, rotorcraft=True)["cards"]}
        assert shown == {"aaa020", "aaa021"}
    finally:
        w.close()


GA_TYPES = frozenset({"C172", "C152"})


def test_ga_cards_are_left_out_unless_requested(tmp_path, replay_db):
    # Same shape as rotorcraft, but keyed on type_code against the configured list, not traffic_class.
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        w.open("aaa030", "N0152AB", type_code="C172", traffic_class="other", runway="28L", along_nm=6.0, hat_ft=1900)
        w.open("aaa031", "UAL1", runway="28R", along_nm=5.0, hat_ft=1500, dt=1)
        with snapshot(db) as conn:
            assert [c["hex"] for c in live_cards(conn, ga_types=GA_TYPES)["cards"]] == ["aaa031"]
            shown = {c["hex"] for c in live_cards(conn, ga=True, ga_types=GA_TYPES)["cards"]}
        assert shown == {"aaa030", "aaa031"}
    finally:
        w.close()


def test_a_card_with_no_type_code_is_not_hidden_as_ga(tmp_path, replay_db):
    # The same NULL trap as rotorcraft, one column over -- SQL's NOT IN is NULL, not true, when type_code is NULL, so an
    # attempt that was never classified would be dropped from the live cards as if it were GA.
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        w.open("aaa040", "N00141", type_code=None, runway="28L", along_nm=6.0, hat_ft=1900)
        w.open("aaa041", "N0152AB", type_code="C172", traffic_class="other", runway="28R", along_nm=5.0, hat_ft=1500, dt=1)
        with snapshot(db) as conn:
            assert [c["hex"] for c in live_cards(conn, ga_types=GA_TYPES)["cards"]] == ["aaa040"]
            shown = {c["hex"] for c in live_cards(conn, ga=True, ga_types=GA_TYPES)["cards"]}
        assert shown == {"aaa040", "aaa041"}
    finally:
        w.close()


def test_an_empty_ga_list_hides_no_cards_and_is_valid_sql(tmp_path, replay_db):
    # An empty ga_types must not exclude anything, and must never reach `NOT IN ()`, a SQL syntax error in most engines
    # (SQLite 3.50, the version this project runs, happens to accept an empty IN-list as always-vacuously-true, so the
    # cards shown would be the same with or without the guard -- the statement-text assertion below is the one that
    # actually distinguishes a present guard from a missing one).
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        w.open("aaa050", "N0152AB", type_code="C172", traffic_class="other", along_nm=6.0, hat_ft=1900)
        with snapshot(db) as conn:
            statements: list[str] = []
            conn.set_trace_callback(statements.append)
            assert [c["hex"] for c in live_cards(conn)["cards"]] == ["aaa050"]
            conn.set_trace_callback(None)
        [sql] = [s for s in statements if "FROM approaches" in s and "outcome IS NULL" in s]
        assert "NOT IN" not in sql          # type_code is always a selected column; only the operator marks the clause
    finally:
        w.close()


def test_an_open_rotorcraft_with_an_emergency_bypasses_the_exclusion_and_reports_its_flags(tmp_path, replay_db):
    # A medevac helicopter that latches an emergency mid-approach must appear on the live page too, with its
    # EMERGENCY_MASK flag names read straight from the open row (the matcher's stamping path 2 keeps it current as the
    # attempt runs).
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        heli = w.open("aaa110", "JOLLY95", traffic_class="rotorcraft", along_nm=6.0, hat_ft=1900)
        w.set_flags(heli, Flags.EMERGENCY_GENERAL | Flags.EMERGENCY_NORDO)
        with snapshot(db) as conn:
            shown = {c["hex"]: c for c in live_cards(conn)["cards"]}       # rotorcraft=False, the default
        assert "aaa110" in shown
        assert set(shown["aaa110"]["emergency"]) == {"EMERGENCY_GENERAL", "EMERGENCY_NORDO"}
    finally:
        w.close()


def test_an_open_ga_type_with_an_emergency_bypasses_the_exclusion(tmp_path, replay_db):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        cessna = w.open("aaa111", "N044GB", type_code="C172", traffic_class="other", along_nm=6.0, hat_ft=1900)
        w.set_flags(cessna, Flags.EMERGENCY_DOWNED)
        with snapshot(db) as conn:
            shown = {c["hex"]: c for c in live_cards(conn, ga_types=GA_TYPES)["cards"]}     # ga=False, the default
        assert "aaa111" in shown and shown["aaa111"]["emergency"] == ["EMERGENCY_DOWNED"]
    finally:
        w.close()


def test_minimum_fuel_alone_does_not_bypass_the_live_exclusions_or_appear_as_emergency(tmp_path, replay_db):
    # MINIMUM_FUEL is attention-only, not in EMERGENCY_MASK -- no bypass, and no "emergency" entry.
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        heli = w.open("aaa112", "JOLLY96", traffic_class="rotorcraft", along_nm=6.0, hat_ft=1900)
        w.set_flags(heli, Flags.MINIMUM_FUEL)
        with snapshot(db) as conn:
            hidden = {c["hex"] for c in live_cards(conn)["cards"]}
            shown = {c["hex"]: c for c in live_cards(conn, rotorcraft=True)["cards"]}
        assert "aaa112" not in hidden
        assert shown["aaa112"]["emergency"] == []
    finally:
        w.close()


def test_a_replayed_database_has_no_cards_and_no_collector(replay_db):
    with snapshot(replay_db) as conn:
        result = live_cards(conn)
        newest = conn.execute("SELECT MAX(id) FROM events").fetchone()[0]
    assert result == {"cards": [], "last_event_id": newest}
    report = health_report(replay_db, now=1789300000.0)
    assert report["status"] == "warning" and report["db"]["ok"] is True and report["db"]["size_bytes"] > 0
    assert report["collector"] == {"state": "none"} and report["feed"]["state"] == "unknown"
    assert report["fleet"] == {"built_at": None, "age_s": None}          # a replay never rebuilds norms


def test_cards_show_each_aircraft_once_nearest_the_threshold_first(tmp_path, replay_db):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        ual = w.open("aaa001", "UAL1", runway="28L", along_nm=6.0, hat_ft=1900)
        w.update(ual, "aaa001", dt=5, along_nm=5.0, hat_ft=1600)
        dal = w.open("aaa002", "DAL2", runway="28R", dt=6, along_nm=3.0, hat_ft=1000)
        w.update(dal, "aaa002", dt=8, along_nm=2.5, hat_ft=800, runway="28R")
        assert [(c["callsign"], c["along_nm"], c["hat_ft"]) for c in cards(db)] == [("DAL2", 2.5, 800), ("UAL1", 5.0, 1600)]

        # UAL1 sidesteps to 28R: both of its attempts are open for a moment, and its card shows the new one
        side = w.open("aaa001", "UAL1", runway="28R", dt=10, along_nm=4.8, arrival_id=ual, attempt_no=2,
                      prev_approach_id=ual, sidestep=True)
        ual_card = cards(db)[1]
        assert (ual_card["approach_id"], ual_card["runway"], ual_card["attempt_no"], ual_card["along_nm"],
                ual_card["hat_ft"], ual_card["arrival_id"], ual_card["t"]) == (side, "28R", 2, 4.8, 1900.0, ual, w.t0 + 10)
        w.close_attempt(ual, "aaa001", Outcome.RUNWAY_CHANGE, dt=11)
        w.update(side, "aaa001", dt=11, along_nm=4.6, hat_ft=1450, runway="28R")
        assert [(c["callsign"], c["runway"], c["hat_ft"]) for c in cards(db)] == [("DAL2", "28R", 800), ("UAL1", "28R", 1450)]

        w.close_attempt(dal, "aaa002", Outcome.LANDED, dt=40, runway="28R")
        assert [c["approach_id"] for c in cards(db)] == [side]
        w.end_run()
        assert cards(db) == []
    finally:
        w.close()


def test_cards_read_the_events_through_their_time_index(tmp_path, replay_db):
    """Every page load asks for /api/live, and events grow by every approach.updated until they are pruned: the query
    must search events_t from the oldest open attempt, not scan the whole table."""
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        aid = w.open("aaa001", "UAL1")
        w.update(aid, "aaa001", dt=1, along_nm=5.9, hat_ft=1880)
    finally:
        w.close()
    statements: list[str] = []
    with snapshot(db) as conn:
        conn.set_trace_callback(statements.append)
        assert [c["approach_id"] for c in live_cards(conn)["cards"]] == [aid]
        conn.set_trace_callback(None)
        [sql] = [s for s in statements if "FROM events" in s and "approach.updated" in s]
        plan = [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", [0] * sql.count("?"))]
    assert not any(step.startswith("SCAN events") for step in plan), plan


def test_a_callsign_decoded_after_establishment_appears_in_live_cards(tmp_path, replay_db):
    """The approaches row keeps whatever callsign and type_code were known when the attempt opened; approach.updated
    carries their current values as they are decoded later. A card must show the newer value, not the row's."""
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        aid = w.open("aaa001", "", along_nm=6.0, hat_ft=1900)
        w.event("approach.updated", aid, {"hex": "aaa001", "runway": "28L", "airport": "KSFO", "callsign": "UAL123",
                                          "type_code": "B738", "along_nm": 5.0, "hat_ft": 1600, "gp_dev_ft": -20,
                                          "gp_dots": -0.2, "loc_dev_ft": 15, "loc_dots": 0.1, "gs": 140.0,
                                          "gs_corr": 150.0, "sink_fpm": -750}, dt=5)
        [card] = cards(db)
        assert card["callsign"] == "UAL123"
    finally:
        w.close()


def test_health_follows_the_collector_and_its_feed(tmp_path, replay_db):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        running = health_report(db, now=w.t0 + 60)
        assert (running["status"], running["collector"]["state"], running["feed"]["state"]) == ("ok", "running", "ok")
        assert running["feed"]["quiet_s"] == pytest.approx(60)
        silent = health_report(db, now=w.t0 + FEED_SILENT_AFTER_S + 1)
        assert (silent["status"], silent["feed"]["state"]) == ("warning", "silent")

        eid = w.event("approach.updated", None, {"hex": "aaa003"}, dt=FEED_SILENT_AFTER_S)
        again = health_report(db, now=w.t0 + FEED_SILENT_AFTER_S + 1)
        assert (again["feed"]["state"], again["feed"]["last_event_id"]) == ("ok", eid)
        assert again["feed"]["quiet_s"] == pytest.approx(1)

        w.end_run()
        stopped = health_report(db, now=w.t0 + FEED_SILENT_AFTER_S + 2)
        assert (stopped["status"], stopped["collector"]["state"], stopped["feed"]["state"]) == ("warning", "stopped", "unknown")
        w.run_id = w.store.new_run("live", "readsb-json:test")
        w.end_run(error="OSError: disk full")
        failed = health_report(db, now=w.t0)
        assert failed["collector"]["state"] == "failed" and "disk full" in failed["collector"]["note"]
    finally:
        w.close()


def test_health_reports_the_fleet_norms_age(tmp_path, replay_db):
    """/api/health always carries the norms' built_at and age, whatever status says, so a rebuild that has quietly
    stopped is visible where the service is checked."""
    from tests.fleetdb import stats_row
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        w.store.replace_norms([stats_row()], [], set(), built_at=w.t0 - 7200.0)     # two hours stale
        report = health_report(db, now=w.t0)
    finally:
        w.close()
    assert report["fleet"] == {"built_at": w.t0 - 7200.0, "age_s": 7200.0}
    assert report["status"] == "ok"        # the collector is running and fed; a stale rebuild alone is not a warning


def test_health_on_an_unreadable_database_still_carries_the_fleet_and_external_keys(tmp_path):
    report = health_report(tmp_path / "missing.sqlite", now=1789000000.0)
    assert report["status"] == "error" and report["fleet"] == {"built_at": None, "age_s": None}
    assert report["external"] == {"enabled": False, "watching": None, "last_success_at": None, "paused_until": None,
                                  "paused_status": None, "paused_error": None, "failures_last_hour": None}


def test_health_reports_the_poller_its_pause_and_its_failures(tmp_path, replay_db):
    from adsb_approach.config import ExternalCfg
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        w.store.record_poll(run_id=w.run_id, requested_at=w.t0 + 99, arrived_at=w.t0 + 100, asked=3, returned=3,
                            http_status=200, error=None, refused=False)
        w.store.record_poll(run_id=w.run_id, requested_at=w.t0 + 109, arrived_at=w.t0 + 110, asked=3, returned=None,
                            http_status=429, error="HTTP 429", refused=True)
        off = health_report(db, now=w.t0 + 300)
        on = health_report(db, now=w.t0 + 300, external=ExternalCfg(enabled=True))
        past_pause = health_report(db, now=w.t0 + 1100, external=ExternalCfg(enabled=True))
        later = health_report(db, now=w.t0 + 110 + 3601, external=ExternalCfg(enabled=True))
    finally:
        w.close()
    assert off["external"] == {"enabled": False, "watching": 3, "last_success_at": w.t0 + 100,
                               "paused_until": w.t0 + 110 + 900, "paused_status": 429, "paused_error": "HTTP 429",
                               "failures_last_hour": 1}
    assert (off["status"], on["status"], on["external"]["enabled"]) == ("ok", "warning", True)
    # the pause has ended, but the 429 is still within the hour: a bare failure count doesn't mean "warning"
    assert (past_pause["external"]["paused_until"], past_pause["external"]["failures_last_hour"]) == (None, 1)
    assert past_pause["status"] == "ok"
    assert (later["external"]["paused_until"], later["external"]["failures_last_hour"]) == (None, 0)


def test_cards_carry_the_path_label(tmp_path, replay_db):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        flown = w.open("aaa001", "VIR19Z", runway="28R", along_nm=6.0, hat_ft=1900, path="quiet-bridge",
                       path_label="Quiet Bridge")
        w.update(flown, "aaa001", dt=5, along_nm=5.5, hat_ft=1800, runway="28R")
        before = w.open("aaa002", "UAL1", runway="28L", along_nm=7.0, hat_ft=2200, dt=1)
        # an attempt established before the deploy: its events have no path and no path_label at all
        w.store.conn.execute("UPDATE events SET payload_json = json_remove(payload_json, '$.path', '$.path_label') "
                             "WHERE approach_id = ?", (before,))
        w.update(before, "aaa002", dt=6, along_nm=6.5, hat_ft=2100)
        w.store.conn.execute("UPDATE events SET payload_json = json_remove(payload_json, '$.path', '$.path_label') "
                             "WHERE approach_id = ?", (before,))
    finally:
        w.close()
    by_hex = {c["hex"]: c for c in cards(db)}
    assert (by_hex["aaa001"]["path"], by_hex["aaa001"]["path_label"]) == ("quiet-bridge", "Quiet Bridge")
    assert by_hex["aaa001"]["along_nm"] == 5.5
    assert (by_hex["aaa002"]["path"], by_hex["aaa002"]["path_label"]) == (None, None)
    assert by_hex["aaa002"]["along_nm"] == 6.5


def test_health_reports_a_pause_for_a_key_file_that_cannot_be_read(tmp_path, replay_db):
    """A key-file failure is a refused poll with no HTTP status (poller.read_api_key), so the banner needs the error."""
    from adsb_approach.config import ExternalCfg
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        w.store.record_poll(run_id=w.run_id, requested_at=w.t0 + 10, arrived_at=w.t0 + 10, asked=2, returned=None,
                            http_status=None, error="api_key_file: FileNotFoundError: gone", refused=True)
        report = health_report(db, now=w.t0 + 20, external=ExternalCfg(enabled=True))
    finally:
        w.close()
    assert report["external"] == {"enabled": True, "watching": 2, "last_success_at": None,
                                  "paused_until": w.t0 + 10 + 900, "paused_status": None,
                                  "paused_error": "api_key_file: FileNotFoundError: gone", "failures_last_hour": 1}
    assert report["status"] == "warning"


def test_health_copes_with_a_database_older_than_the_poller(tmp_path, replay_db):
    """The web opens the database read-only and never migrates it, so a collector that has not restarted on the code
    that adds external_polls leaves the table missing."""
    from adsb_approach.config import ExternalCfg
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        w.store.conn.execute("DROP TABLE external_positions")
        w.store.conn.execute("DROP TABLE external_polls")
        report = health_report(db, now=w.t0 + 300, external=ExternalCfg(enabled=True))
    finally:
        w.close()
    assert report["external"] == {"enabled": True, "watching": None, "last_success_at": None, "paused_until": None,
                                  "paused_status": None, "paused_error": None, "failures_last_hour": None}
    assert report["status"] != "error"

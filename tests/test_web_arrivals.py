import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from adsb_approach.model import Flags, Outcome
from adsb_approach.reference import reference_labels
from adsb_approach.store.db import Store
from adsb_approach.web.app import create_app
from adsb_approach.web.arrivals import ArrivalFilter, decode_cursor, list_arrivals, parse_limit
from adsb_approach.web.db import snapshot
from tests.webdb import LiveWriter, before_migration_8, copy_db

SIDESTEPS = {"a7ccce", "ab0f89", "a4b5b6", "ac2706", "a4dfd2"}       # the five SFO runway changes (test_replay_golden)


def fetch(db, ga_types=frozenset(), labels=None, **params):
    params = {k: str(v) for k, v in params.items()}
    limit = parse_limit(params.pop("limit", None))
    cursor = decode_cursor(params.pop("cursor", None))
    with snapshot(db) as conn:
        return list_arrivals(conn, ArrivalFilter.parse(params, ga_types=ga_types), limit=limit, cursor=cursor,
                             labels=labels)


def every_page(db, **params) -> list[dict]:
    rows, cursor = [], None
    while True:
        page = fetch(db, **params, **({"cursor": cursor} if cursor else {}))
        rows += page["arrivals"]
        cursor = page["next_cursor"]
        if cursor is None:
            return rows


def sql(db, query, *args):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(query, args).fetchall()
    finally:
        conn.close()


def test_pages_list_every_arrival_once_newest_first(replay_db):
    paged = every_page(replay_db, limit=7)
    whole = every_page(replay_db, limit=200)          # more arrivals than one page of 200 holds: follow next_cursor
    assert [a["arrival_id"] for a in paged] == [a["arrival_id"] for a in whole]
    expected = sql(replay_db, "SELECT COUNT(DISTINCT arrival_id) FROM approaches WHERE outcome IS NOT NULL")[0][0]
    assert len(paged) == len({a["arrival_id"] for a in paged}) == expected
    starts = [a["started_at"] for a in paged]
    assert starts == sorted(starts, reverse=True)
    assert fetch(replay_db, limit=200)["last_event_id"] == sql(replay_db, "SELECT MAX(id) FROM events")[0][0]


def test_a_go_around_and_the_landing_after_it_are_one_arrival(replay_db):
    [arrival] = [a for a in fetch(replay_db, limit=200)["arrivals"] if a["hex"] == "a44540"]
    first, second = arrival["attempts"]
    assert (first["outcome"], second["outcome"]) == ("go_around", "lost_on_final")
    assert (first["attempt_no"], second["attempt_no"]) == (1, 2) and first["runway"] == second["runway"] == "28L"
    assert arrival["callsign"] == "UAL2177" and arrival["airport"] == "KSFO" and arrival["attention"] is True
    assert {"INFERRED_GO_AROUND", "LANDING_CONFIRMED"} <= set(arrival["flags"])
    assert arrival["started_at"] == first["established_at"] and arrival["ended_at"] == second["ended_at"]
    assert first["gate1000"]["observed"] is True and -90 < first["gate1000"]["gp_dev_ft"] < -10     # observed -48 ft
    assert second["gate1000"]["observed"] is False and second["gate1000"]["gp_dev_ft"] is None


def test_the_runway_change_filter_finds_the_five_sfo_sidesteps(replay_db):
    found = every_page(replay_db, outcome="runway_change", limit=2)
    assert len(found) == 5 and {a["hex"] for a in found} == SIDESTEPS
    for a in found:
        first, second = a["attempts"][:2]
        assert (first["outcome"], first["runway"], second["runway"]) == ("runway_change", "28L", "28R")
        assert "SIDESTEP" in second["flags"]


def test_an_arrival_is_listed_when_any_attempt_passes_every_filter(replay_db):
    # UAL2432 changed from 28L to 28R: its landing attempt on 28R passes both filters, its 28L attempt neither
    assert "ac2706" in {a["hex"] for a in every_page(replay_db, runway="KSFO/28R", outcome_not="runway_change", limit=200)}
    assert "ac2706" not in {a["hex"] for a in every_page(replay_db, runway="KSFO/28L", outcome_not="runway_change", limit=200)}


def test_flags_any_and_flags_none(replay_db):
    assert {a["hex"] for a in every_page(replay_db, flags_any="SIDESTEP", limit=200)} == SIDESTEPS
    unconfirmed = every_page(replay_db, outcome="lost_on_final", flags_none="LANDING_CONFIRMED", limit=200)
    expected = sql(replay_db, "SELECT COUNT(DISTINCT arrival_id) FROM approaches WHERE outcome = 'lost_on_final' "
                              "AND flags & ? = 0", int(Flags.LANDING_CONFIRMED))[0][0]
    assert len(unconfirmed) == expected > 0


def test_attention_lists_go_arounds_touch_and_goes_abandoned_and_unstable(replay_db):
    found = {a["arrival_id"]: a for a in every_page(replay_db, preset="attention", limit=200)}
    attention_flags = int(Flags.FAST | Flags.SLOW | Flags.UNSTABLE_1000 | Flags.UNSTABLE_500 | Flags.STEEP
                          | Flags.SHALLOW | Flags.LATE_ESTABLISHED)
    triggered = {r[0] for r in sql(replay_db, "SELECT arrival_id FROM approaches WHERE outcome IN "
                                              "('go_around', 'touch_and_go', 'abandoned') OR flags & ? != 0",
                                   attention_flags)}
    # This fixture's minimal typedb-sample.csv leaves several real airline arrivals (UAL, SkyWest) without a type_code,
    # so classify() falls through to "other" — exercising the same circuit exclusion the measured data called for, even
    # though these particular ones are not light aircraft.
    circuits = {r[0] for r in sql(replay_db, "SELECT arrival_id FROM approaches GROUP BY arrival_id "
                                             "HAVING COUNT(*) > 1 AND (SELECT traffic_class FROM approaches a2 "
                                             "WHERE a2.arrival_id = approaches.arrival_id "
                                             "ORDER BY a2.attempt_no DESC, a2.id DESC LIMIT 1) = 'other'")}
    expected = triggered - circuits
    assert expected and circuits and set(found) == expected and all(a["attention"] for a in found.values())


def test_a_circuit_arrival_is_not_attention_even_with_a_go_around(tmp_path, replay_db):
    # Six of nine repeat-attempt arrivals measured in a live database were light singles circuiting at a nearby airport,
    # so a repeat-attempt arrival of the "other" class is not attention even when an attempt went around.
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        a1 = w.open("aaa010", "N0152AB", traffic_class="other")
        w.close_attempt(a1, "aaa010", Outcome.GO_AROUND, dt=60)
        a2 = w.open("aaa010", "N0152AB", traffic_class="other", dt=70, arrival_id=a1, attempt_no=2,
                    prev_approach_id=a1)
        w.close_attempt(a2, "aaa010", Outcome.GO_AROUND, dt=130)
        a3 = w.open("aaa010", "N0152AB", traffic_class="other", dt=140, arrival_id=a1, attempt_no=3,
                    prev_approach_id=a2)
        w.close_attempt(a3, "aaa010", Outcome.LANDED, dt=200)
    finally:
        w.close()

    arrival = {a["arrival_id"]: a for a in fetch(db, limit=200)["arrivals"]}[a1]
    assert len(arrival["attempts"]) == 3 and arrival["traffic_class"] == "other"
    assert arrival["attention"] is False                                        # the accepted cost: hides a go-around
    assert a1 not in {a["arrival_id"] for a in fetch(db, preset="attention", limit=200)["arrivals"]}


def test_the_same_class_with_one_attempt_is_still_attention(tmp_path, replay_db):
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        a1 = w.open("aaa011", "N0152AB", traffic_class="other")
        w.close_attempt(a1, "aaa011", Outcome.GO_AROUND, dt=60)
    finally:
        w.close()

    arrival = {a["arrival_id"]: a for a in fetch(db, limit=200)["arrivals"]}[a1]
    assert len(arrival["attempts"]) == 1
    assert arrival["attention"] is True
    assert a1 in {a["arrival_id"] for a in fetch(db, preset="attention", limit=200)["arrivals"]}


def test_a_multi_attempt_airline_arrival_stays_attention(tmp_path, replay_db):
    # the KSFO case: three genuine repeated approaches by one airliner must not be hidden
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        a1 = w.open("aaa012", "SKW3386", traffic_class="airline")
        w.close_attempt(a1, "aaa012", Outcome.GO_AROUND, dt=60)
        a2 = w.open("aaa012", "SKW3386", traffic_class="airline", dt=70, arrival_id=a1, attempt_no=2,
                    prev_approach_id=a1)
        w.close_attempt(a2, "aaa012", Outcome.GO_AROUND, dt=130)
        a3 = w.open("aaa012", "SKW3386", traffic_class="airline", dt=140, arrival_id=a1, attempt_no=3,
                    prev_approach_id=a2)
        w.close_attempt(a3, "aaa012", Outcome.LANDED, dt=200)
    finally:
        w.close()

    arrival = {a["arrival_id"]: a for a in fetch(db, limit=200)["arrivals"]}[a1]
    assert len(arrival["attempts"]) == 3
    assert arrival["attention"] is True
    assert a1 in {a["arrival_id"] for a in fetch(db, preset="attention", limit=200)["arrivals"]}


def test_a_missing_traffic_class_does_not_count_as_a_circuit(tmp_path, replay_db):
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        a1 = w.open("aaa013", "N00141", traffic_class="other")
        w.close_attempt(a1, "aaa013", Outcome.GO_AROUND, dt=60)
        a2 = w.open("aaa013", "N00141", traffic_class="other", dt=70, arrival_id=a1, attempt_no=2, prev_approach_id=a1)
        w.close_attempt(a2, "aaa013", Outcome.LANDED, dt=130)
    finally:
        w.close()
    conn = sqlite3.connect(db)
    conn.execute("UPDATE approaches SET traffic_class = NULL WHERE arrival_id = ?", (a1,))
    conn.commit()
    conn.close()

    arrival = {a["arrival_id"]: a for a in fetch(db, limit=200)["arrivals"]}[a1]
    assert arrival["traffic_class"] is None
    assert arrival["attention"] is True
    assert a1 in {a["arrival_id"] for a in fetch(db, preset="attention", limit=200)["arrivals"]}


def test_the_time_window_is_on_when_an_attempt_ended(replay_db):
    since, until = 1789226777.0, 1789226780.0          # around the end of UAL2177's go-around, 1789226778.5
    found = {a["arrival_id"] for a in every_page(replay_db, since=since, until=until, limit=200)}
    expected = {r[0] for r in sql(replay_db, "SELECT arrival_id FROM approaches WHERE ended_at >= ? AND ended_at < ?",
                                  since, until)}
    assert found == expected and sql(replay_db, "SELECT arrival_id FROM approaches WHERE hex = 'a44540'")[0][0] in found


def test_arrival_id_selects_one_arrival_if_it_passes_the_filters(replay_db):
    [(arrival_id,)] = sql(replay_db, "SELECT DISTINCT arrival_id FROM approaches WHERE hex = 'a44540'")
    assert [a["hex"] for a in fetch(replay_db, arrival_id=arrival_id)["arrivals"]] == ["a44540"]
    assert fetch(replay_db, arrival_id=arrival_id, outcome="landed")["arrivals"] == []


def test_rotorcraft_attempts_are_left_out_unless_requested(tmp_path, replay_db):
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        heli = w.open("aaa001", "JOLLY93", traffic_class="rotorcraft", along_nm=6.0, hat_ft=1900)
        w.close_attempt(heli, "aaa001", Outcome.GO_AROUND, dt=60)
        air = w.open("aaa002", "UAL1", runway="28R", along_nm=5.0, hat_ft=1500, dt=1)
        w.close_attempt(air, "aaa002", Outcome.LANDED, dt=61)
    finally:
        w.close()

    def hexes(**params):
        return {a["hex"] for a in fetch(db, **params)["arrivals"]}

    assert "aaa001" not in hexes(limit=200) and "aaa002" in hexes(limit=200)      # rotorcraft hidden by default
    assert {"aaa001", "aaa002"} <= hexes(rotorcraft=1, limit=200)                 # shown when asked
    assert "aaa001" not in hexes(preset="attention", limit=200)                   # combines with every filter...
    assert "aaa001" in hexes(preset="attention", rotorcraft=1, limit=200)         # ...including preset=attention


def test_an_attempt_with_no_traffic_class_is_not_hidden_as_a_rotorcraft(tmp_path, replay_db):
    # The exclusion is "hide rotorcraft", not "hide anything unclassified". SQL's NULL != 'rotorcraft' is NULL rather
    # than true, so without COALESCE a never-classified attempt fails the default filter and the arrival disappears from
    # every query, rotorcraft or not.
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        unknown = w.open("aaa020", "N00141", traffic_class="other", along_nm=6.0, hat_ft=1900)
        w.close_attempt(unknown, "aaa020", Outcome.GO_AROUND, dt=60)   # attention, so the preset is testable too
        heli = w.open("aaa021", "JOLLY93", traffic_class="rotorcraft", runway="28R", along_nm=5.0, hat_ft=1500, dt=1)
        w.close_attempt(heli, "aaa021", Outcome.LANDED, dt=61)
    finally:
        w.close()
    conn = sqlite3.connect(db)
    conn.execute("UPDATE approaches SET traffic_class = NULL WHERE arrival_id = ?", (unknown,))
    conn.commit()
    conn.close()

    def hexes(**params):
        return {a["hex"] for a in fetch(db, **params)["arrivals"]}

    assert "aaa020" in hexes(limit=200)                                          # unclassified: shown by default
    assert "aaa021" not in hexes(limit=200)                                      # ...while a rotorcraft is still hidden
    assert {"aaa020", "aaa021"} <= hexes(rotorcraft=1, limit=200)
    assert "aaa020" in hexes(preset="attention", limit=200)                      # and through the other filters too


GA_TYPES = frozenset({"C172", "C152"})


def test_ga_attempts_are_left_out_unless_requested(tmp_path, replay_db):
    # Same shape as rotorcraft, but keyed on type_code against the configured list, not traffic_class.
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        cessna = w.open("aaa030", "N0152AB", type_code="C172", traffic_class="other", along_nm=6.0, hat_ft=1900)
        w.close_attempt(cessna, "aaa030", Outcome.GO_AROUND, dt=60)
        air = w.open("aaa031", "UAL1", runway="28R", along_nm=5.0, hat_ft=1500, dt=1)
        w.close_attempt(air, "aaa031", Outcome.LANDED, dt=61)
    finally:
        w.close()

    def hexes(**params):
        return {a["hex"] for a in fetch(db, ga_types=GA_TYPES, **params)["arrivals"]}

    assert "aaa030" not in hexes(limit=200) and "aaa031" in hexes(limit=200)      # GA hidden by default
    assert {"aaa030", "aaa031"} <= hexes(ga=1, limit=200)                         # shown when asked
    assert "aaa030" not in hexes(preset="attention", limit=200)                   # combines with every filter...
    assert "aaa030" in hexes(preset="attention", ga=1, limit=200)                 # ...including preset=attention


def test_an_attempt_with_no_type_code_is_not_hidden_as_ga(tmp_path, replay_db):
    # The same NULL trap as rotorcraft, one column over. SQL's NULL NOT IN (...) is NULL rather than true, so without
    # COALESCE a never-classified attempt fails the default GA filter and the arrival disappears from every query, GA or
    # not. There are real rows like this: 4 approaches in the live database have an empty type_code.
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        unknown = w.open("aaa040", "N00141", type_code=None, traffic_class="other", along_nm=6.0, hat_ft=1900)
        w.close_attempt(unknown, "aaa040", Outcome.GO_AROUND, dt=60)   # attention, so the preset is testable too
        cessna = w.open("aaa041", "N0152AB", type_code="C172", traffic_class="other", runway="28R", along_nm=5.0,
                        hat_ft=1500, dt=1)
        w.close_attempt(cessna, "aaa041", Outcome.LANDED, dt=61)
    finally:
        w.close()

    def hexes(**params):
        return {a["hex"] for a in fetch(db, ga_types=GA_TYPES, **params)["arrivals"]}

    assert "aaa040" in hexes(limit=200)                                          # no type_code: shown by default
    assert "aaa041" not in hexes(limit=200)                                      # ...while GA is still hidden
    assert {"aaa040", "aaa041"} <= hexes(ga=1, limit=200)
    assert "aaa040" in hexes(preset="attention", limit=200)                      # and through the other filters too


def test_an_empty_ga_list_hides_nothing_and_is_valid_sql(tmp_path, replay_db):
    # The shipped type_classes.toml has a ga list, but a user's need not. An empty ga_types must not exclude anything,
    # and must never reach `NOT IN ()`, a SQL syntax error in most engines (SQLite 3.50, the version this project runs,
    # happens to accept an empty IN-list as always-vacuously-true, so the query below would give the same *rows* with or
    # without the guard -- the assertion on the built SQL text is the one that actually distinguishes a present guard
    # from a missing one).
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        cessna = w.open("aaa050", "N0152AB", type_code="C172", traffic_class="other", along_nm=6.0, hat_ft=1900)
        w.close_attempt(cessna, "aaa050", Outcome.GO_AROUND, dt=60)
    finally:
        w.close()
    assert "aaa050" in {a["hex"] for a in fetch(db, ga_types=frozenset(), limit=200)["arrivals"]}
    sql, _ = ArrivalFilter.parse({}, ga_types=frozenset()).where(has_path=True)
    assert "type_code" not in sql and "NOT IN ()" not in sql


def test_an_emergency_on_a_rotorcraft_bypasses_the_exclusion_in_both_views(tmp_path, replay_db):
    # A medevac helicopter must not be hidden by the rotorcraft exclusion in either view -- the row's own
    # "emergency"/"attention" and the SQL filter must agree, the way the circuit tests do.
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        heli = w.open("001158", "JOLLY93", traffic_class="rotorcraft", along_nm=6.0, hat_ft=1900)
        w.close_attempt(heli, "001158", Outcome.LANDED, dt=60, flags=Flags.EMERGENCY_GENERAL)
    finally:
        w.close()

    def hexes(**params):
        return {a["hex"] for a in fetch(db, **params)["arrivals"]}

    assert "001158" in hexes(limit=200)                                    # not hidden, though rotorcraft=0 (default)
    assert "001158" in hexes(preset="attention", limit=200)
    arrival = {a["arrival_id"]: a for a in fetch(db, limit=200)["arrivals"]}[heli]
    assert arrival["emergency"] is True and arrival["attention"] is True


def test_an_emergency_on_a_ga_type_bypasses_the_exclusion_in_both_views(tmp_path, replay_db):
    # The same bypass, one column over -- a C172 with an engine failure must not be hidden as GA.
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        cessna = w.open("aaa103", "N044GA", type_code="C172", traffic_class="other", along_nm=6.0, hat_ft=1900)
        w.close_attempt(cessna, "aaa103", Outcome.LANDED, dt=60, flags=Flags.EMERGENCY_UNLAWFUL)
    finally:
        w.close()

    def hexes(**params):
        return {a["hex"] for a in fetch(db, ga_types=GA_TYPES, **params)["arrivals"]}

    assert "aaa103" in hexes(limit=200)                                     # not hidden, though ga=0 (default)
    assert "aaa103" in hexes(preset="attention", limit=200)
    arrival = {a["arrival_id"]: a for a in fetch(db, ga_types=GA_TYPES, limit=200)["arrivals"]}[cessna]
    assert arrival["emergency"] is True and arrival["attention"] is True


def test_an_emergency_on_a_circuit_arrival_bypasses_the_circuit_rule_with_every_attempt(tmp_path, replay_db):
    # Contrasting the circuit test above: the same three-attempt "other"-class shape, but the last attempt latches an
    # emergency -- the whole arrival must show, in both views, with every attempt, not only the one that holds the flag
    # (arrival_rows fetches by arrival_id, so this also proves the SQL side's bypass -- without it, the arrival_id is
    # never selected at all, and the dict lookups below raise KeyError).
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        a1 = w.open("aaa101", "N0152CD", traffic_class="other")
        w.close_attempt(a1, "aaa101", Outcome.GO_AROUND, dt=60)
        a2 = w.open("aaa101", "N0152CD", traffic_class="other", dt=70, arrival_id=a1, attempt_no=2,
                    prev_approach_id=a1)
        w.close_attempt(a2, "aaa101", Outcome.GO_AROUND, dt=130)
        a3 = w.open("aaa101", "N0152CD", traffic_class="other", dt=140, arrival_id=a1, attempt_no=3,
                    prev_approach_id=a2)
        w.close_attempt(a3, "aaa101", Outcome.LANDED, dt=200, flags=Flags.EMERGENCY_DOWNED)
    finally:
        w.close()

    for params in ({"limit": 200}, {"preset": "attention", "limit": 200}):
        arrival = {a["arrival_id"]: a for a in fetch(db, **params)["arrivals"]}[a1]
        assert len(arrival["attempts"]) == 3
        assert arrival["emergency"] is True and arrival["attention"] is True


def test_an_emergency_on_an_open_attempt_bypasses_the_ga_exclusion_too(tmp_path, replay_db):
    # Every bypass above has the flag on the same closed attempt list_arrivals reads it from. Here it is latched on the
    # attempt still in progress -- exactly what the matcher's stamping path 2 writes while an approach is open -- with a
    # first attempt already closed and holding nothing. Without the arrival-level fix, neither row satisfies where()
    # (the closed one fails the GA test, the open one fails outcome IS NOT NULL), and the arrival never appears at all.
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        first = w.open("aaa104", "N045GA", type_code="C172", along_nm=6.0, hat_ft=1900)
        w.close_attempt(first, "aaa104", Outcome.GO_AROUND, dt=60)                                  # closed, no flag
        second = w.open("aaa104", "N045GA", type_code="C172", dt=70, arrival_id=first, attempt_no=2,
                        prev_approach_id=first)
        w.set_flags(second, Flags.EMERGENCY_GENERAL)                                                # still open
    finally:
        w.close()

    def hexes(**params):
        return {a["hex"] for a in fetch(db, ga_types=GA_TYPES, **params)["arrivals"]}

    assert "aaa104" in hexes(limit=200)                             # not hidden as GA, though ga=0 (default)
    arrival = {a["arrival_id"]: a for a in fetch(db, ga_types=GA_TYPES, limit=200)["arrivals"]}[first]
    assert len(arrival["attempts"]) == 2 and arrival["emergency"] is True


def test_an_emergency_on_an_open_rotorcraft_attempt_bypasses_the_exclusion_too(tmp_path, replay_db):
    # Same shape as the GA case above, one column over.
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        first = w.open("aaa105", "JOLLY95", traffic_class="rotorcraft", along_nm=6.0, hat_ft=1900)
        w.close_attempt(first, "aaa105", Outcome.GO_AROUND, dt=60)
        second = w.open("aaa105", "JOLLY95", traffic_class="rotorcraft", dt=70, arrival_id=first, attempt_no=2,
                        prev_approach_id=first)
        w.set_flags(second, Flags.EMERGENCY_GENERAL)
    finally:
        w.close()

    def hexes(**params):
        return {a["hex"] for a in fetch(db, **params)["arrivals"]}

    assert "aaa105" in hexes(limit=200)                             # not hidden as rotorcraft, though rotorcraft=0
    arrival = {a["arrival_id"]: a for a in fetch(db, limit=200)["arrivals"]}[first]
    assert len(arrival["attempts"]) == 2 and arrival["emergency"] is True


def test_an_emergency_on_an_open_attempt_makes_the_arrival_attention_though_no_closed_attempt_is(tmp_path, replay_db):
    # Red implies attention. The open-attempt tests beside this one close their first attempt as a go-around, itself an
    # attention outcome, so "attention" held for that reason alone and could not show this. Here the closed attempt is a
    # plain lost_on_final -- attention neither by outcome nor by flags -- and the emergency sits only on the attempt
    # still open, so only the emergency can make the arrival attention: the row's field and preset=attention must both
    # say so, and agree.
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        first = w.open("aab108", "UAL108", along_nm=6.0, hat_ft=1900)
        w.close_attempt(first, "aab108", Outcome.LOST_ON_FINAL, dt=60)                              # not attention
        second = w.open("aab108", "UAL108", dt=70, arrival_id=first, attempt_no=2, prev_approach_id=first)
        w.set_flags(second, Flags.EMERGENCY_GENERAL)                                                # still open
    finally:
        w.close()
    row = {a["arrival_id"]: a for a in fetch(db, limit=200)["arrivals"]}[first]
    assert row["emergency"] is True and row["attention"] is True
    assert first in {a["arrival_id"] for a in fetch(db, preset="attention", limit=200)["arrivals"]}


def test_an_emergency_on_an_open_attempt_of_a_circuit_arrival_bypasses_the_circuit_rule_too(tmp_path, replay_db):
    # Contrasting the closed-attempt circuit test above: the flag lands on the third attempt while it is still open, not
    # on a third closed landing.
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        a1 = w.open("aaa106", "N0152EF", traffic_class="other")
        w.close_attempt(a1, "aaa106", Outcome.GO_AROUND, dt=60)
        a2 = w.open("aaa106", "N0152EF", traffic_class="other", dt=70, arrival_id=a1, attempt_no=2,
                    prev_approach_id=a1)
        w.close_attempt(a2, "aaa106", Outcome.GO_AROUND, dt=130)
        a3 = w.open("aaa106", "N0152EF", traffic_class="other", dt=140, arrival_id=a1, attempt_no=3,
                    prev_approach_id=a2)
        w.set_flags(a3, Flags.EMERGENCY_DOWNED)
    finally:
        w.close()

    for params in ({"limit": 200}, {"preset": "attention", "limit": 200}):
        arrival = {a["arrival_id"]: a for a in fetch(db, **params)["arrivals"]}[a1]
        assert len(arrival["attempts"]) == 3
        assert arrival["emergency"] is True and arrival["attention"] is True


def test_minimum_fuel_is_attention_only_no_bypass_and_no_emergency(tmp_path, replay_db):
    # MINIMUM_FUEL joins attention (LATCH_MASK) but is not in EMERGENCY_MASK, so it gets none of the bypasses and never
    # sets "emergency".
    db = copy_db(replay_db, tmp_path / "arrivals.sqlite")
    w = LiveWriter(db)
    try:
        heli = w.open("aaa102", "JOLLY94", traffic_class="rotorcraft", along_nm=6.0, hat_ft=1900)
        w.close_attempt(heli, "aaa102", Outcome.LANDED, dt=60, flags=Flags.MINIMUM_FUEL)
    finally:
        w.close()

    def hexes(**params):
        return {a["hex"] for a in fetch(db, **params)["arrivals"]}

    assert "aaa102" not in hexes(limit=200)                                 # no bypass: still hidden as a rotorcraft
    assert "aaa102" in hexes(rotorcraft=1, limit=200)
    assert "aaa102" in hexes(rotorcraft=1, preset="attention", limit=200)   # attention, once it is shown at all
    arrival = {a["arrival_id"]: a for a in fetch(db, rotorcraft=1, limit=200)["arrivals"]}[heli]
    assert arrival["attention"] is True and arrival["emergency"] is False


@pytest.mark.parametrize("params, message", [
    ({"colour": "red"}, "unknown parameter 'colour'"),
    ({"outcome": "crashed"}, "unknown outcome 'crashed'"),
    ({"flags_any": "WOBBLY"}, "unknown flag 'WOBBLY'"),
    ({"runway": "28L"}, "is not AIRPORT/RUNWAY"),
    ({"preset": "boring"}, "unknown preset 'boring'"),
    ({"approach": "x"}, "approach: unknown value 'x'"),
    ({"since": "yesterday"}, "is not a number"),
    ({"arrival_id": "1.5"}, "is not a whole number"),
])
def test_a_bad_filter_is_an_error_that_names_it(params, message):
    with pytest.raises(ValueError, match=re.escape(message)):
        ArrivalFilter.parse(params)


@pytest.mark.parametrize("text", ["0", "201", "ten"])
def test_a_bad_limit_is_an_error(text):
    with pytest.raises(ValueError, match="limit"):
        parse_limit(text)


QUIET_BRIDGE = ("quiet-bridge", "Quiet Bridge")


def empty_db(path):
    """A migrated database with nothing in it, so a test can count exactly what it wrote."""
    store = Store(path)
    store.migrate()
    store.close()
    return path


def attempts_of(db, **kw):
    return [a for arrival in fetch(db, **kw)["arrivals"] for a in arrival["attempts"]]


def test_attempts_carry_path_label_and_references(tmp_path, example_cfg):
    db = empty_db(tmp_path / "paths.sqlite")
    w = LiveWriter(db)
    try:
        flown = w.open("aaa001", "VIR19Z", runway="28R", path="quiet-bridge", path_label="Quiet Bridge")
        w.close_attempt(flown, "aaa001", Outcome.LANDED, dt=60, references=["quiet-bridge", "quiet-bridge", None])
        again = w.open("aaa002", "UAL1", runway="28R", dt=100, path="quiet-bridge", path_label="Quiet Bridge")
        w.close_attempt(again, "aaa002", Outcome.LANDED, dt=160, references=["quiet-bridge", None, "quiet-bridge"])
        straight = w.open("aaa003", "UAL2", runway="28L", dt=200)
        w.close_attempt(straight, "aaa003", Outcome.LANDED, dt=260, references=[None, None])
    finally:
        w.close()
    labels = reference_labels(example_cfg.airports)
    by_id = {a["approach_id"]: a for a in attempts_of(db, labels=labels)}
    ils = {"name": None, "label": "ILS"}
    assert by_id[flown]["path"] == "quiet-bridge" and by_id[flown]["path_label"] == "Quiet Bridge"
    assert by_id[flown]["references"] == [{"name": "quiet-bridge", "label": "Quiet Bridge"}, ils]
    assert by_id[again]["references"] == [{"name": "quiet-bridge", "label": "Quiet Bridge"}, ils]    # distinct, by first t
    assert by_id[straight]["path"] is None and by_id[straight]["path_label"] is None
    assert by_id[straight]["references"] == [ils]


def test_approach_filter(tmp_path):
    db = empty_db(tmp_path / "filter.sqlite")
    w = LiveWriter(db)
    try:
        on_path = w.open("aaa001", "VIR19Z", runway="28R", path="quiet-bridge", path_label="Quiet Bridge")
        w.close_attempt(on_path, "aaa001", Outcome.LANDED, dt=60, references=["quiet-bridge"])
        straight = w.open("aaa002", "UAL1", runway="28R", dt=100)
        w.close_attempt(straight, "aaa002", Outcome.LANDED, dt=160, references=[None])
    finally:
        w.close()

    def found(**params):
        return {a["arrival_id"] for a in every_page(db, **params)}

    assert found(approach="path") == {on_path} and found(approach="straight") == {straight}
    assert found(approach="all") == found() == {on_path, straight}
    assert found(approach="path", runway="KSFO/28L") == set()                  # it filters with the others, not instead
    with pytest.raises(ValueError, match=re.escape("approach: unknown value 'x'")):
        ArrivalFilter.parse({"approach": "x"})
    assert ArrivalFilter.parse({"approach": " Path "}).approach == "path" and ArrivalFilter.parse({}).approach == "all"


def test_where_must_be_told_whether_the_database_has_a_path_column():
    """No default: a caller that assumed the column would fail on a database not yet migrated to 0008."""
    filt = ArrivalFilter.parse({"approach": "path"})
    with pytest.raises(TypeError):
        filt.where()                                                  # type: ignore[call-arg]
    assert "a.path IS NOT NULL" in filt.where(has_path=True)[0] and "a.path" not in filt.where(has_path=False)[0]
    straight = ArrivalFilter.parse({"approach": "straight"})
    assert "a.path IS NULL" in straight.where(has_path=True)[0] and "a.path" not in straight.where(has_path=False)[0]


def test_an_attempt_without_points_falls_back(tmp_path, example_cfg):
    """A path attempt closed before any point inside 10 nm stored none: its references are its path, never an empty
    list, and a centreline attempt's are the centreline."""
    db = empty_db(tmp_path / "bare.sqlite")
    w = LiveWriter(db)
    try:
        on_path = w.open("aaa001", "VIR19Z", runway="28R", path="quiet-bridge", path_label="Quiet Bridge")
        w.close_attempt(on_path, "aaa001", Outcome.LOST, dt=60)
        straight = w.open("aaa002", "UAL1", runway="28R", dt=100)
        w.close_attempt(straight, "aaa002", Outcome.LOST, dt=160)
    finally:
        w.close()
    by_id = {a["approach_id"]: a for a in attempts_of(db, labels=reference_labels(example_cfg.airports))}
    assert by_id[on_path]["references"] == [{"name": "quiet-bridge", "label": "Quiet Bridge"}]
    assert by_id[straight]["references"] == [{"name": None, "label": "ILS"}]


def test_a_path_with_no_stored_point_on_it_still_leads_the_references(tmp_path, example_cfg):
    """approaches.path is the last path flown, but only points within 10 nm are stored: a switch beyond that leaves
    points that are all on the centreline. The label must still read Quiet Bridge -> ILS."""
    db = empty_db(tmp_path / "switched.sqlite")
    w = LiveWriter(db)
    try:
        aid = w.open("aaa001", "VIR19Z", runway="28R", path="quiet-bridge", path_label="Quiet Bridge")
        w.close_attempt(aid, "aaa001", Outcome.LANDED, dt=60, references=[None, None, None])
    finally:
        w.close()
    [attempt] = attempts_of(db, labels=reference_labels(example_cfg.airports))
    assert attempt["references"] == [{"name": "quiet-bridge", "label": "Quiet Bridge"}, {"name": None, "label": "ILS"}]


def test_an_unconfigured_path_label_falls_back_to_its_name(tmp_path):
    """A path renamed or removed from ksfo.toml, or no labels at all: the stored name is the label, and a centreline the
    configuration does not know is straight-in."""
    db = empty_db(tmp_path / "gone.sqlite")
    w = LiveWriter(db)
    try:
        aid = w.open("aaa001", "VIR19Z", runway="28R", path="quiet-bridge", path_label="Quiet Bridge")
        w.close_attempt(aid, "aaa001", Outcome.LANDED, dt=60, references=["quiet-bridge", None])
    finally:
        w.close()
    for labels, centreline in (({("KSFO", "28R", None): "ILS"}, "ILS"), ({}, "straight-in"), (None, "straight-in")):
        [attempt] = attempts_of(db, labels=labels)
        assert attempt["path_label"] == "quiet-bridge"
        assert attempt["references"] == [{"name": "quiet-bridge", "label": "quiet-bridge"},
                                         {"name": None, "label": centreline}]


def test_the_app_labels_the_arrivals_from_the_configuration(tmp_path, example_cfg):
    """The labels are the configured paths' and runways': the event payloads' are not read for the log."""
    db = empty_db(tmp_path / "app.sqlite")
    w = LiveWriter(db)
    try:
        aid = w.open("aaa001", "VIR19Z", runway="28R", path="quiet-bridge")
        w.close_attempt(aid, "aaa001", Outcome.LANDED, dt=60, references=["quiet-bridge", None])
    finally:
        w.close()
    [arrival] = TestClient(create_app(example_cfg, db)).get("/api/arrivals").json()["arrivals"]
    [attempt] = arrival["attempts"]
    assert attempt["path_label"] == "Quiet Bridge"
    assert [r["label"] for r in attempt["references"]] == ["Quiet Bridge", "ILS"]


def test_a_database_before_migration_8_still_answers(tmp_path, replay_db, example_cfg):
    """The web process is deployed before the collector restarts and migrates: no approaches.path and no
    approach_points.reference. The arrivals, the live cards and the drawer answer, with no path; the path filter finds
    nothing, as no approach has flown one, and the straight-in filter everything."""
    db = copy_db(replay_db, tmp_path / "old.sqlite")
    w = LiveWriter(db)
    try:
        open_id = w.open("aaa001", "UAL1", runway="28R", along_nm=5.0, hat_ft=1500)
        w.update(open_id, "aaa001", dt=5, along_nm=4.5, hat_ft=1400, runway="28R")
    finally:
        w.close()
    before_migration_8(db)
    client = TestClient(create_app(example_cfg, db))
    everything = client.get("/api/arrivals", params={"limit": 200})
    assert everything.status_code == 200
    attempts = [a for arrival in everything.json()["arrivals"] for a in arrival["attempts"]]
    assert attempts and all(a["path"] is None and a["path_label"] is None for a in attempts)
    assert all([r["name"] for r in a["references"]] == [None] for a in attempts)           # the centreline, per runway
    assert {r["label"] for a in attempts for r in a["references"]} <= {"ILS", "straight-in"}
    straight = client.get("/api/arrivals", params={"limit": 200, "approach": "straight"})
    assert straight.status_code == 200 and straight.json()["arrivals"] == everything.json()["arrivals"]
    on_path = client.get("/api/arrivals", params={"limit": 200, "approach": "path"})
    assert on_path.status_code == 200 and on_path.json()["arrivals"] == []
    live = client.get("/api/live")
    assert live.status_code == 200 and [c["hex"] for c in live.json()["cards"]] == ["aaa001"]
    assert live.json()["cards"][0]["path"] is None and live.json()["cards"][0]["path_label"] is None
    detail = client.get(f"/api/approaches/{attempts[0]['approach_id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["approach"]["path"] is None and body["reference"]["path"] is None
    assert body["points"] and {p["reference"] for p in body["points"]} == {None}

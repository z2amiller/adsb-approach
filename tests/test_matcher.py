import math
import statistics
from dataclasses import replace
from pathlib import Path

import pytest

from adsb_approach.config import TypeClasses, load_config
from adsb_approach.events import EventBus
from adsb_approach.fleet import DAY_S, coverage_row, coverage_rows, reflag
from adsb_approach.geometry import NM_FT, destination, distance_ft
from adsb_approach.matcher import Matcher, external_flags
from adsb_approach.model import EMERGENCY_MASK, Airport, Flags, Weather, flag_names
from adsb_approach.scorer import ScoredPoint, coverage_record, scorer_for
from adsb_approach.store.db import Store
from adsb_approach.tracker import Tracker
from adsb_approach.typedb import TypeDb
from adsb_approach.weather import WeatherTable
from tests.conftest import (ROOT, T0, climb_segment, flown_track, ground_point, make_ksfo, make_sfo_28l, make_sfo_28r,
                            synthetic_track)
from tests.fleetdb import norms_for


class Harness:
    def __init__(self, tmp_path: Path, airport: Airport | None = None):
        self.cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
        self.cfg.airports = {"KSFO": make_ksfo() if airport is None else airport}
        self.store = Store(tmp_path / "m.sqlite")
        self.store.migrate()
        self.run_id = self.store.new_run("replay", "test")
        self.events: list[tuple[str, dict]] = []
        bus = EventBus()
        bus.subscribe(lambda kind, t, aid, fid, payload: self.events.append((kind, payload)))
        self.weather = WeatherTable()
        self.weather.add(Weather("KSFO", T0 - 600, 1013.25, 15.0, 5.0, None, None, None, ""))
        self.tracker = Tracker(TypeDb.load(ROOT / "tests" / "fixtures" / "typedb-sample.csv"),
                               TypeClasses.load(ROOT / "config" / "type_classes.toml"),
                               on_new_flight=lambda f: self.store.insert_flight(f.hex, f.callsign, f.reg, f.type_code, f.traffic_class, f.first_seen))
        scorer = scorer_for(self.cfg)      # the very Scorer the collector builds, so this harness cannot drift from it
        self.matcher = Matcher(self.cfg, scorer, self.weather, self.store, bus, self.run_id)

    def feed(self, positions):
        for p in positions:
            self.matcher.process(p, self.tracker.update(p))

    def rows(self):
        return self.store.approaches(run_id=self.run_id)

    def kinds(self):
        return [k for k, _ in self.events]


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def test_clean_landing_with_ground_point(h):
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=0.05)
    h.feed(pts)
    h.feed([ground_point(rwy, along_nm=-0.5, t=pts[-1].t + 20)])
    h.matcher.tick(pts[-1].t + 20 + 46)      # one ground report at 40 kt could still be a touch-and-go; coverage then ends
    rows = h.rows()
    assert len(rows) == 1 and rows[0]["outcome"] == "landed" and rows[0]["runway"] == "28R"
    assert rows[0]["flags"] == 0 and rows[0]["lowest_hat_ft"] == 0.0 and rows[0]["arrival_id"] == rows[0]["id"]
    gates = h.store.gates(rows[0]["id"])
    assert all(g["observed"] for g in gates) and abs(gates[0]["gp_dev_ft"]) < 2
    assert h.kinds()[0] == "approach.started" and h.kinds()[-1] == "approach.completed"
    assert rows[0]["established_at"] == pytest.approx(T0 + 30, abs=2)          # the last positions spanning 30 s
    assert h.store.points(rows[0]["id"])[0]["along_nm"] == pytest.approx(8.0, abs=0.02)   # candidate points back-filled


def test_only_the_nearest_parallel_runway_establishes(h):
    h.feed(synthetic_track(make_sfo_28l(), start_nm=8, end_nm=4.5))
    h.matcher.finish()
    rows = h.rows()
    assert [r["runway"] for r in rows] == ["28L"] and rows[0]["outcome"] == "lost"


def test_lost_on_final_is_presumed_landed_and_lost_high_is_not(h):
    rwy = make_sfo_28r()
    low = synthetic_track(rwy, start_nm=8, end_nm=2.7)
    h.feed(low)
    h.matcher.tick(low[-1].t + 46)
    rows = h.rows()
    assert rows[0]["outcome"] == "lost_on_final" and Flags.PRESUMED_LANDED in Flags(rows[0]["flags"])
    assert Flags.GATE500_UNOBSERVED in Flags(rows[0]["flags"]) and Flags.GATE1000_UNOBSERVED not in Flags(rows[0]["flags"])
    assert rows[0]["ended_at"] == low[-1].t and rows[0]["closest_along_nm"] == pytest.approx(2.7, abs=0.05)
    high = synthetic_track(rwy, hexid="abc123", callsign="UAL1", start_nm=8, end_nm=5, t0=T0 + 1000)
    h.feed(high)
    h.matcher.tick(high[-1].t + 46)
    r2 = h.rows()[1]
    assert r2["outcome"] == "lost" and Flags.PRESUMED_LANDED not in Flags(r2["flags"])


def test_go_around_with_corroboration(h):
    rwy = make_sfo_28r()
    down = synthetic_track(rwy, start_nm=8, end_nm=1.5, nav_altitude_mcp=3000)
    up = climb_segment(rwy, start_along_nm=1.45, end_along_nm=-2.0, start_alt_ft=down[-1].alt_baro, vr_fpm=1500,
                       t0=down[-1].t + 1, nav_altitude_mcp=5000, nav_modes=frozenset({"autopilot"}))
    h.feed(down + up)
    rows = h.rows()
    assert len(rows) == 1 and rows[0]["outcome"] == "go_around"
    f = Flags(rows[0]["flags"])
    assert Flags.MCP_JUMP in f and Flags.APPROACH_MODE_DROPPED in f and Flags.PRESUMED_LANDED not in f
    assert rows[0]["ga_initiated_hat_ft"] == pytest.approx(530, abs=15) and rows[0]["ga_initiated_along_nm"] == pytest.approx(1.5, abs=0.1)
    assert "approach.go_around" in h.kinds()
    assert h.store.gates(rows[0]["id"])[0]["observed"] == 1


def test_touch_and_go(h):
    rwy = make_sfo_28r()
    down = synthetic_track(rwy, start_nm=6, end_nm=0.02)                       # ends ~ 60 ft above threshold
    roll = climb_segment(rwy, start_along_nm=-0.1, end_along_nm=-0.6, start_alt_ft=rwy.threshold_elev_ft + 20, vr_fpm=0, gs_kt=120, t0=down[-1].t + 1)
    up = climb_segment(rwy, start_along_nm=-0.65, end_along_nm=-3.0, start_alt_ft=rwy.threshold_elev_ft + 25, vr_fpm=1200, gs_kt=130, t0=roll[-1].t + 1)
    h.feed(down + roll + up)
    assert h.rows()[0]["outcome"] == "touch_and_go"


def test_approach_updated_carries_the_flights_callsign_and_type_code(h):
    rwy = make_sfo_28r()
    h.feed(synthetic_track(rwy, start_nm=8, end_nm=2.0))
    known = next(payload for kind, payload in h.events if kind == "approach.updated")
    assert (known["callsign"], known["type_code"]) == ("UAL2177", "B39M")

    h.feed(synthetic_track(rwy, hexid="a00001", callsign=None, start_nm=8, end_nm=2.0))
    unknown = next(payload for kind, payload in h.events if kind == "approach.updated" and payload["hex"] == "a00001")
    assert (unknown["callsign"], unknown["type_code"]) == (None, None)


def test_runway_change_sidestep(h):
    rwy_28l, rwy_28r = make_sfo_28l(), make_sfo_28r()
    on_l = synthetic_track(rwy_28l, start_nm=8, end_nm=3.0)
    on_r = synthetic_track(rwy_28r, start_nm=2.9, end_nm=0.8, t0=on_l[-1].t + 3)
    h.feed(on_l + on_r)
    h.matcher.tick(on_r[-1].t + 46)
    rows = h.rows()
    assert [r_["runway"] for r_ in rows] == ["28L", "28R"]
    assert rows[0]["outcome"] == "runway_change" and rows[0]["next_approach_id"] == rows[1]["id"]
    assert rows[1]["prev_approach_id"] == rows[0]["id"] and rows[1]["arrival_id"] == rows[0]["id"] and rows[1]["attempt_no"] == 2
    assert Flags.SIDESTEP in Flags(rows[1]["flags"]) and rows[1]["outcome"] == "lost_on_final"
    assert "approach.runway_change" in h.kinds()


def test_approach_started_carries_traffic_class(h):
    """Both places _establish and _runway_change publish approach.started."""
    rwy_28l, rwy_28r = make_sfo_28l(), make_sfo_28r()
    on_l = synthetic_track(rwy_28l, category="A7", callsign="JOLLY93", start_nm=8, end_nm=3.0)
    on_r = synthetic_track(rwy_28r, category="A7", callsign="JOLLY93", start_nm=2.9, end_nm=0.8, t0=on_l[-1].t + 3)
    h.feed(on_l + on_r)
    h.matcher.tick(on_r[-1].t + 46)
    started = [payload for kind, payload in h.events if kind == "approach.started"]
    assert len(started) == 2 and all(p["traffic_class"] == "rotorcraft" for p in started)


def test_a_runway_change_rescores_each_position_with_the_metar_of_its_own_time(h):
    # The change rescores its last few positions against the new runway once the last of them arrives. A METAR observed
    # between the first of them and the last is newer than the first: it gave them standard pressure (QNH_ASSUMED and
    # TEMP_UNCORRECTED) and heights 185 ft low, although the METAR of their own time was 10 minutes old. A replay loads
    # every METAR up front, so this happens whenever an observation time falls among the change's positions.
    rwy_28l, rwy_28r = make_sfo_28l(), make_sfo_28r()
    h.weather.add(Weather("KSFO", T0 - 600, 1020.0, 15.0, 5.0, None, None, None, ""))      # replaces the harness METAR
    on_l = synthetic_track(rwy_28l, start_nm=8, end_nm=3.0, qnh_hpa=1020.0)
    on_r = synthetic_track(rwy_28r, start_nm=2.9, end_nm=0.8, t0=on_l[-1].t + 3, qnh_hpa=1020.0)
    h.weather.add(Weather("KSFO", on_r[0].t + 1.5, 1020.0, 15.0, 5.0, None, None, None, ""))
    h.feed(on_l + on_r)
    h.matcher.tick(on_r[-1].t + 46)
    change = h.rows()[1]
    assert change["runway"] == "28R" and not Flags(change["flags"]) & (Flags.QNH_ASSUMED | Flags.TEMP_UNCORRECTED)
    assert max(abs(p["gp_dev_ft"]) for p in h.store.points(change["id"])) < 5


def test_abandoned_when_veering_off(h):
    rwy = make_sfo_28r()
    on = synthetic_track(rwy, start_nm=8, end_nm=5.0)
    off = synthetic_track(rwy, start_nm=4.9, end_nm=3.5, cross_ft=9000, track_offset_deg=70, t0=on[-1].t + 1)
    h.feed(on + off)
    assert h.rows()[0]["outcome"] == "abandoned"


def test_reappearance_reclassifies_to_inferred_go_around(h):
    rwy = make_sfo_28r()
    down = synthetic_track(rwy, start_nm=8, end_nm=2.7)
    h.feed(down)
    h.matcher.tick(down[-1].t + 50)
    assert h.rows()[0]["outcome"] == "lost_on_final"
    back = climb_segment(rwy, start_along_nm=-0.7, end_along_nm=-3.0, start_alt_ft=1300, vr_fpm=1500, t0=down[-1].t + 80)
    h.feed(back)
    row = h.rows()[0]
    assert row["outcome"] == "go_around" and Flags.INFERRED_GO_AROUND in Flags(row["flags"]) and Flags.PRESUMED_LANDED not in Flags(row["flags"])
    assert "approach.reclassified" in h.kinds()


def test_landing_confirmed_by_later_departure_and_arrival_chaining(h):
    rwy = make_sfo_28r()
    first = synthetic_track(rwy, start_nm=8, end_nm=1.5, nav_altitude_mcp=3000)
    climb = climb_segment(rwy, start_along_nm=1.45, end_along_nm=-2.0, start_alt_ft=first[-1].alt_baro, vr_fpm=1500, t0=first[-1].t + 1)
    second = synthetic_track(rwy, start_nm=8, end_nm=2.7, t0=climb[-1].t + 600)
    h.feed(first + climb + second)
    h.matcher.tick(second[-1].t + 50)
    rows = h.rows()
    assert [r["outcome"] for r in rows] == ["go_around", "lost_on_final"]
    assert rows[1]["arrival_id"] == rows[0]["id"] and rows[1]["attempt_no"] == 2 and rows[1]["prev_approach_id"] == rows[0]["id"]
    dep = climb_segment(rwy, start_along_nm=-1.0, end_along_nm=-2.5, start_alt_ft=800, vr_fpm=2500, t0=second[-1].t + 40 * 60)
    h.feed(dep)
    row = h.rows()[1]
    assert row["landed_confirmed_by"] == "departure" and Flags.LANDING_CONFIRMED in Flags(row["flags"])
    assert "approach.confirmed" in h.kinds()


def test_a_restart_still_chains_the_next_attempt_onto_the_arrival_left_in_the_store(tmp_path):
    """A restart starts a fresh Matcher with an empty in-memory _arrivals: unlike test_separate_arrivals_when_hours_apart
    (an in-memory link that correctly ages out), this is the same aircraft coming back inside the window, only with
    no process memory of its go-around. The store fallback must find it and chain the same way the in-memory path
    does within one run (test_landing_confirmed_by_later_departure_and_arrival_chaining)."""
    rwy = make_sfo_28r()
    h1 = Harness(tmp_path)
    first = synthetic_track(rwy, start_nm=8, end_nm=1.5, nav_altitude_mcp=3000)
    climb = climb_segment(rwy, start_along_nm=1.45, end_along_nm=-2.0, start_alt_ft=first[-1].alt_baro, vr_fpm=1500,
                          t0=first[-1].t + 1)
    h1.feed(first + climb)
    h1.matcher.tick(climb[-1].t + 50)
    [went_around] = h1.rows()
    assert went_around["outcome"] == "go_around"

    h2 = Harness(tmp_path)     # a restart: a new run, a new Store handle, a fresh Matcher and Tracker on the same file
    second = synthetic_track(rwy, start_nm=8, end_nm=2.7, t0=climb[-1].t + 600)      # 10 min later: inside the window
    h2.feed(second)
    h2.matcher.tick(second[-1].t + 50)
    [landed] = h2.rows()
    assert (landed["arrival_id"], landed["attempt_no"], landed["prev_approach_id"]) == (went_around["id"], 2, went_around["id"])
    next_id = h1.store.conn.execute("SELECT next_approach_id FROM approaches WHERE id = ?", (went_around["id"],)).fetchone()[0]
    assert next_id == landed["id"]


def test_late_events_carry_the_flight_and_describe_the_approach(h):
    seen = []
    h.matcher.events.subscribe(lambda kind, t, aid, fid, payload: seen.append((kind, aid, fid, payload)))
    rwy = make_sfo_28r()
    first = synthetic_track(rwy, start_nm=8, end_nm=2.7)
    h.feed(first)
    h.matcher.tick(first[-1].t + 50)
    h.feed(climb_segment(rwy, start_along_nm=-1.0, end_along_nm=-2.5, start_alt_ft=800, vr_fpm=2500, t0=first[-1].t + 40 * 60))
    other = synthetic_track(rwy, hexid="abc123", callsign="UAL1", start_nm=8, end_nm=2.7, t0=T0 + 3 * 3600)
    h.feed(other)
    h.matcher.tick(other[-1].t + 50)
    h.feed(climb_segment(rwy, hexid="abc123", callsign="UAL1", start_along_nm=-0.7, end_along_nm=-3.0, start_alt_ft=1300,
                         vr_fpm=1500, t0=other[-1].t + 80))
    landed, went_around = h.rows()
    late = {kind: (aid, fid, payload) for kind, aid, fid, payload in seen if kind in ("approach.confirmed", "approach.reclassified")}
    assert late["approach.confirmed"] == (landed["id"], landed["flight_id"], {
        "hex": "a44540", "airport": "KSFO", "runway": "28R", "outcome": "lost_on_final",
        "flags": flag_names(landed["flags"]), "landed_confirmed_by": "departure",
        "arrival_id": landed["arrival_id"], "attempt_no": landed["attempt_no"]})
    assert late["approach.reclassified"] == (went_around["id"], went_around["flight_id"], {
        "from": "lost_on_final", "outcome": "go_around", "flags": flag_names(went_around["flags"]),
        "landed_confirmed_by": None, "hex": "abc123", "airport": "KSFO", "runway": "28R",
        "arrival_id": went_around["arrival_id"], "attempt_no": went_around["attempt_no"]})
    assert landed["flight_id"] is not None and went_around["flight_id"] is not None


def test_flight_id_after_a_callsign_change_matches_the_row_and_every_event(h):
    # The candidate opens under one flight session; the callsign changes (a new session) before it establishes. The
    # row and every event about it must carry the session that was current at establishment, not the one at open.
    seen = []
    h.matcher.events.subscribe(lambda kind, t, aid, fid, payload: seen.append((kind, fid)))
    rwy = make_sfo_28r()
    opening = synthetic_track(rwy, callsign="UAL1", start_nm=8, end_nm=7.5)             # short: stays a candidate
    rest = synthetic_track(rwy, callsign="UAL2", start_nm=7.45, end_nm=2.7, t0=opening[-1].t + 1)
    h.feed(opening + rest)
    h.matcher.tick(rest[-1].t + 50)
    [row] = h.rows()
    fids = {fid for kind, fid in seen if kind.startswith("approach.")}
    assert row["flight_id"] is not None and fids == {row["flight_id"]}


def test_runway_change_attempt_carries_the_current_flight_id(h):
    # A callsign change lands between the two attempts a runway change produces; the new one must carry the flight
    # that was current when it opened, not the old attempt's.
    seen = []
    h.matcher.events.subscribe(lambda kind, t, aid, fid, payload: seen.append((kind, aid, fid)))
    rwy_28l, rwy_28r = make_sfo_28l(), make_sfo_28r()
    on_l = synthetic_track(rwy_28l, callsign="UAL1", start_nm=8, end_nm=3.0)
    on_r = synthetic_track(rwy_28r, callsign="UAL2", start_nm=2.9, end_nm=0.8, t0=on_l[-1].t + 3)
    h.feed(on_l + on_r)
    h.matcher.tick(on_r[-1].t + 46)
    rows = h.rows()
    assert [row["runway"] for row in rows] == ["28L", "28R"]
    started_28r_fid = next(fid for kind, aid, fid in seen if kind == "approach.started" and aid == rows[1]["id"])
    assert started_28r_fid == rows[1]["flight_id"] != rows[0]["flight_id"]


def test_a_tisb_position_in_history_is_not_used_by_the_climb_check(h):
    # A TIS-B position between the closing ADS-B point and a later one still enters the tracker's history. Its own
    # altitude is coarse and sometimes stale, so it must not stand in for "the previous position" when there is no
    # vertical rate to check directly - here a low reading that would otherwise look like a 500 ft climb.
    rwy = make_sfo_28r()
    low = synthetic_track(rwy, start_nm=8, end_nm=1.0)          # within landed_confirm_departure_nm of the airport
    h.feed(low)
    h.matcher.tick(low[-1].t + 46)
    assert h.rows()[0]["landed_confirmed_by"] is None
    baseline_alt = low[-1].alt_baro
    tisb_spike = replace(low[-1], t=low[-1].t + 390, alt_baro=baseline_alt - 500, baro_rate=None, geom_rate=None,
                         source="tisb")
    departure = replace(low[-1], t=low[-1].t + 400, alt_baro=baseline_alt, baro_rate=None, geom_rate=None)
    h.feed([tisb_spike, departure])
    assert h.rows()[0]["landed_confirmed_by"] is None       # the spike must not look like a climb from the real altitude


def test_separate_arrivals_when_hours_apart(h):
    rwy = make_sfo_28r()
    a = synthetic_track(rwy, start_nm=8, end_nm=2.7)
    b = synthetic_track(rwy, start_nm=8, end_nm=2.7, t0=T0 + 4 * 3600)
    h.feed(a)
    h.matcher.tick(a[-1].t + 50)
    h.feed(b)
    h.matcher.tick(b[-1].t + 50)
    rows = h.rows()
    assert len(rows) == 2 and rows[1]["arrival_id"] == rows[1]["id"] and rows[1]["attempt_no"] == 1
    assert rows[0]["flight_id"] != rows[1]["flight_id"]          # 4 h gap = new flight session


def test_finish_closes_open_attempts(h):
    rwy = make_sfo_28r()
    h.feed(synthetic_track(rwy, start_nm=8, end_nm=3.0))
    assert h.rows()[0]["outcome"] is None
    h.matcher.finish()
    assert h.rows()[0]["outcome"] == "lost_on_final"


def test_an_approach_scored_on_standard_pressure_is_flagged(h):
    rwy = make_sfo_28r()
    fresh = synthetic_track(rwy, start_nm=8, end_nm=2.7)                          # the METAR is 10 minutes old
    stale = synthetic_track(rwy, hexid="abc123", callsign="UAL1", start_nm=8, end_nm=2.7, t0=T0 + 4 * 3600)
    h.feed(fresh)
    h.matcher.tick(fresh[-1].t + 46)
    h.feed(stale)
    h.matcher.tick(stale[-1].t + 46)
    fresh_flags, stale_flags = (Flags(r["flags"]) for r in h.rows())
    assert not fresh_flags & (Flags.QNH_ASSUMED | Flags.TEMP_UNCORRECTED)
    assert Flags.QNH_ASSUMED in stale_flags and Flags.TEMP_UNCORRECTED in stale_flags


def test_a_stale_metar_is_not_linked(h):
    # The row's weather_id should name the METAR that was actually applied. WeatherTable.at() returns the harness's
    # only observation regardless of age, but this approach establishes well past qnh_max_age_min of it.
    rwy = make_sfo_28r()
    stale_age_s = h.cfg.service.qnh_max_age_min * 60.0
    late = synthetic_track(rwy, start_nm=8, end_nm=2.7, t0=T0 + stale_age_s + 3600)
    h.feed(late)
    h.matcher.tick(late[-1].t + 46)
    assert h.rows()[0]["weather_id"] is None


def test_tisb_and_adsr_tracks_are_never_candidates(h):
    # non-ICAO addresses (TIS-B / ADS-R, hex prefixed "~") carry coarse positions: never open an attempt
    h.feed(synthetic_track(make_sfo_28r(), hexid="~a1b2c3", callsign=None, start_nm=8, end_nm=0.05))
    assert h.rows() == [] and "approach.started" not in h.kinds()


def test_radar_derived_tisb_positions_are_not_scored(h):
    # readsb marks positions relayed from ground radar (TIS-B) by their source; they are coarse even under an ICAO address
    from dataclasses import replace
    rwy = make_sfo_28r()
    adsb = synthetic_track(rwy, start_nm=8, end_nm=2.7)
    radar = [replace(p, source="tisb_icao") for p in synthetic_track(rwy, start_nm=2.6, end_nm=1.2, t0=adsb[-1].t + 2)]
    h.feed(adsb + radar)
    h.matcher.tick(radar[-1].t + 46)
    [row] = h.rows()
    assert row["outcome"] == "lost_on_final" and row["ended_at"] == adsb[-1].t
    assert min(p["along_nm"] for p in h.store.points(row["id"])) == pytest.approx(2.7, abs=0.05)
    h.feed([replace(p, hex="abc123", callsign="UAL1", source="tisb")
            for p in synthetic_track(rwy, start_nm=8, end_nm=0.5, t0=T0 + 3600)])
    assert len(h.rows()) == 1


def test_intercept_overshoot_towards_the_parallel_is_not_a_runway_change(h):
    # UAL852 (a1b535) on 2026-09-12: joins 28L from the south, overshoots almost to 28R's centreline (751 ft right of
    # 28L), then settles on 28L. Over its first five points the median |cross| is smaller for 28R (353 ft vs 398 ft)
    # although it is converging on 28L; establishing on 28R opened an attempt that a runway change closed 3 points later.
    rwy_28l = make_sfo_28l()
    gs_kt, step_s = 180.0, 8.0
    step_nm = gs_kt * step_s / 3600.0
    intercept = [(296, 19.0), (698, 4.6), (622, -3.5), (398, -5.1), (240, -4.2), (128, -2.0), (97, -0.3), (116, 0.7)]
    pts = []
    for i, (cross_ft, track_offset_deg) in enumerate(intercept):      # one position per call: start_nm == end_nm
        along_nm = 11.8 - i * step_nm
        pts += synthetic_track(rwy_28l, start_nm=along_nm, end_nm=along_nm, cross_ft=cross_ft,
                               track_offset_deg=track_offset_deg, gs_kt=gs_kt, t0=T0 + i * step_s)
    pts += synthetic_track(rwy_28l, start_nm=11.8 - len(intercept) * step_nm, end_nm=3.0, gs_kt=gs_kt, step_s=step_s,
                           t0=T0 + len(intercept) * step_s)
    h.feed(pts)
    h.matcher.tick(pts[-1].t + 46)
    assert [(r["runway"], r["outcome"]) for r in h.rows()] == [("28L", "lost_on_final")]
    assert "approach.runway_change" not in h.kinds()


def test_a_feed_wide_gap_closes_quiet_attempts_before_the_next_position(h):
    # Nothing arrives for 120 s (readsb restart, network drop). Attempts close on the stream clock before a position is
    # matched, so the aircraft that happens to be processed first afterwards cannot carry its attempt across the gap.
    rwy = make_sfo_28r()
    a1 = synthetic_track(rwy, start_nm=8, end_nm=5.0)
    b1 = synthetic_track(rwy, hexid="abc123", callsign="UAL1", start_nm=9, end_nm=6.0)
    resume = a1[-1].t + 120
    a2 = synthetic_track(rwy, start_nm=3.0, end_nm=2.5, t0=resume)
    b2 = synthetic_track(rwy, hexid="abc123", callsign="UAL1", start_nm=4.0, end_nm=3.5, t0=resume + 0.5)
    h.feed(sorted(a1 + b1, key=lambda p: p.t) + sorted(a2 + b2, key=lambda p: p.t))
    rows = {r["hex"]: r for r in h.rows()}
    assert {k: r["outcome"] for k, r in rows.items()} == {"a44540": "lost", "abc123": "lost"}
    assert rows["a44540"]["ended_at"] == a1[-1].t and rows["abc123"]["ended_at"] == b1[-1].t


def test_arrival_links_are_forgotten_once_the_link_window_has_passed(h):
    # the live service runs for months: the per-flight arrival index must not keep every flight it has ever closed
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=2.7)
    h.feed(pts)
    h.matcher.tick(pts[-1].t + 50)
    assert len(h.matcher._arrivals) == 1
    h.matcher.tick(pts[-1].t + h.cfg.detection.arrival_link_window_min * 60 + 60)
    assert h.matcher._arrivals == {}


def test_a_later_attempt_supersedes_the_post_close_rules_of_the_previous_one(h):
    # Coverage drops at 5 nm (attempt 1 closes lost); the aircraft reappears lower on the same final (attempt 2) and goes
    # around. Only attempt 2 is a go-around: attempt 2's climb must not also reclassify attempt 1 as an inferred one.
    rwy = make_sfo_28r()
    first = synthetic_track(rwy, start_nm=8, end_nm=5.0)
    h.feed(first)
    h.matcher.tick(first[-1].t + 46)
    second = synthetic_track(rwy, start_nm=2.9, end_nm=1.0, t0=first[-1].t + 50)
    climb = climb_segment(rwy, start_along_nm=0.95, end_along_nm=-4.0, start_alt_ft=second[-1].alt_baro, vr_fpm=1500,
                          t0=second[-1].t + 1)
    h.feed(second + climb)
    assert [(r["outcome"], r["attempt_no"]) for r in h.rows()] == [("lost", 1), ("go_around", 2)]
    assert "approach.reclassified" not in h.kinds()


def test_a_superseded_attempt_is_not_confirmed_by_the_next_attempts_landing(h):
    rwy = make_sfo_28r()
    first = synthetic_track(rwy, start_nm=8, end_nm=5.0)
    h.feed(first)
    h.matcher.tick(first[-1].t + 46)
    second = synthetic_track(rwy, start_nm=2.9, end_nm=0.05, t0=first[-1].t + 50)
    h.feed(second + [ground_point(rwy, along_nm=-0.5, t=second[-1].t + 20)])
    h.matcher.tick(second[-1].t + 20 + 46)
    assert [(r["outcome"], r["landed_confirmed_by"]) for r in h.rows()] == [("lost", None), ("landed", None)]


def test_touch_and_go_with_ground_contact(h):
    # Ground contact inside the footprint followed by the go-around climb rule is a touch-and-go, not a landing
    rwy = make_sfo_28r()
    down = synthetic_track(rwy, start_nm=6, end_nm=0.02)
    roll = [ground_point(rwy, along_nm=-0.1 - 0.03 * i, t=down[-1].t + 1 + i, gs=120.0) for i in range(8)]
    up = climb_segment(rwy, start_along_nm=-0.4, end_along_nm=-3.0, start_alt_ft=rwy.threshold_elev_ft + 25, vr_fpm=1200,
                       gs_kt=130, t0=roll[-1].t + 1)
    h.feed(down + roll + up)
    assert [r["outcome"] for r in h.rows()] == ["touch_and_go"]


def test_ground_rollout_is_landed_on_reaching_taxi_speed_or_leaving_the_runway(h):
    rwy = make_sfo_28r()
    down = synthetic_track(rwy, start_nm=8, end_nm=0.05)
    roll = [ground_point(rwy, along_nm=-0.2 - 0.03 * i, t=down[-1].t + 5 + i, gs=gs)
            for i, gs in enumerate((120.0, 90.0, 60.0, 40.0, 20.0))]
    h.feed(down + roll[:4])
    assert h.rows()[0]["outcome"] is None                                   # still fast enough for a touch-and-go
    h.feed(roll[4:])
    assert (h.rows()[0]["outcome"], h.rows()[0]["ended_at"]) == ("landed", roll[4].t)
    down2 = synthetic_track(rwy, hexid="abc123", callsign="UAL1", start_nm=8, end_nm=0.05, t0=T0 + 3600)
    kw = dict(hexid="abc123", callsign="UAL1", gs=45.0)
    exit_ = [ground_point(rwy, along_nm=-1.2, t=down2[-1].t + 30, **kw),
             ground_point(rwy, along_nm=-1.25, cross_ft=400.0, t=down2[-1].t + 33, **kw)]    # high-speed exit at 45 kt
    h.feed(down2 + exit_)
    assert (h.rows()[1]["outcome"], h.rows()[1]["ended_at"]) == ("landed", exit_[1].t)


def test_airborne_reported_rollout_that_turns_off_is_landed_not_abandoned(h):
    # light-GA installations without an air/ground input can report "airborne" at field elevation after touchdown;
    # slowing on the runway and turning onto a taxiway is a landing, not an abandoned approach
    rwy = make_sfo_28r()
    ga = dict(hexid="a0b1c2", callsign="N0123AB", category="A1")
    down = synthetic_track(rwy, gs_kt=80, start_nm=6, end_nm=0.02, **ga)
    roll = [synthetic_track(rwy, start_nm=-0.05 - 0.01 * i, end_nm=-0.05 - 0.01 * i, offset_ft=-rwy.tch_ft, gs_kt=gs,
                            t0=down[-1].t + 1 + i, **ga)[0] for i, gs in enumerate((60.0, 45.0, 35.0, 25.0, 15.0))]
    turn = [synthetic_track(rwy, start_nm=-0.1, end_nm=-0.1, cross_ft=60.0 * (i + 1), track_offset_deg=90, offset_ft=-rwy.tch_ft,
                            gs_kt=12, t0=roll[-1].t + 1 + i, **ga)[0] for i in range(4)]
    h.feed(down + roll + turn)
    assert [r["outcome"] for r in h.rows()] == ["landed"]


@pytest.mark.parametrize(("cross_ft", "outcome"), [(0.0, "touch_and_go"), (600.0, "go_around")])
def test_climb_after_a_ground_confirmation_withdraws_it(h, cross_ft, outcome):
    # Lost on final, a ground report (confirmed by ground), then climbing within the reappearance window: the climb
    # wins. Ground contact on the runway followed by a climb is a touch-and-go; a ground report off the runway was not
    # this approach's landing. Either way the row and the events must stop claiming a confirmed landing.
    rwy = make_sfo_28r()
    down = synthetic_track(rwy, start_nm=8, end_nm=2.7)
    h.feed(down)
    h.matcher.tick(down[-1].t + 50)
    h.feed([ground_point(rwy, along_nm=-0.5, cross_ft=cross_ft, t=down[-1].t + 70)])
    assert h.rows()[0]["landed_confirmed_by"] == "ground"
    h.feed(climb_segment(rwy, start_along_nm=-1.0, end_along_nm=-3.0, start_alt_ft=1300, vr_fpm=1500, t0=down[-1].t + 120))
    row = h.rows()[0]
    flags = Flags(row["flags"])
    assert (row["outcome"], row["landed_confirmed_by"]) == (outcome, None)
    assert Flags.INFERRED_GO_AROUND in flags and Flags.LANDING_CONFIRMED not in flags and Flags.PRESUMED_LANDED not in flags
    payload = next(p for k, p in h.events if k == "approach.reclassified")
    assert (payload["from"], payload["outcome"], payload["landed_confirmed_by"]) == ("lost_on_final", outcome, None)


@pytest.mark.parametrize("step_s", [0.5, 8.0])
def test_establishment_window_is_measured_in_seconds(h, step_s):
    # 2 positions/s from the live port or 8 s tar1090 history: the same stretch of flight establishes the attempt
    h.feed(synthetic_track(make_sfo_28r(), start_nm=8, end_nm=3.0, step_s=step_s))
    h.matcher.finish()
    assert h.rows()[0]["established_at"] - T0 == pytest.approx(31.0, abs=1.5)


@pytest.mark.parametrize("step_s", [0.5, 8.0])
@pytest.mark.parametrize(("start_cross_ft", "offset_deg", "legs"), [
    (-3000.0, 18.0, [(15.8, 18.0), (40.0, 0.0), (110.0, 0.0)]),     # EJA219's geometry
    (-2500.0, 8.0, [(60.3, 8.0), (17.8, 0.0), (88.0, 0.0)]),        # shallow: within 10 deg of the course, still crossing
], ids=["18deg", "8deg"])
def test_intercept_across_the_parallel_establishes_only_on_the_runway_it_rolls_out_on(h, step_s, start_cross_ft,
                                                                                       offset_deg, legs):
    # EJA219 (a1dc60) on 2026-09-12: an 18 deg intercept from south of 28L crossed 28L's centreline still turning and
    # rolled out on 28R. At 2 positions/s its last five points (2.5 s) lay in 28L's corridor, so it established on 28L
    # and changed runway 8 s later. Live or at the 8 s history rate it is one attempt, on 28R. The shallow intercept
    # spends 47 s in 28L's corridor nearest 28L with its track within 10 deg; its 47 ft/s cross-track rate holds it.
    pts = flown_track(make_sfo_28l(), start_nm=12.5, start_cross_ft=start_cross_ft, start_offset_deg=offset_deg,
                      legs=legs, gs_kt=200.0, step_s=step_s)
    h.feed(pts)
    h.matcher.tick(pts[-1].t + 46)
    assert [(r["runway"], r["outcome"]) for r in h.rows()] == [("28R", "lost_on_final")]
    assert "approach.runway_change" not in h.kinds()


@pytest.mark.parametrize("step_s", [0.5, 8.0])
@pytest.mark.parametrize(("offset_deg", "peak_ft"), [(4.5, 520.0), (7.0, 700.0)])
def test_excursion_to_the_parallel_and_back_is_not_a_runway_change(h, offset_deg, peak_ft, step_s):
    # N00216 (001104) at KNUQ on 2026-09-12 went 32L -> 32R -> 32L: at 2 positions/s, 1.5 s near the parallel was a
    # runway change. Established on 28L, this aircraft drifts to within 300 ft of 28R's centreline and back: aligned but
    # near 28R for only 7 s (4.5 deg), or near it for 16 s but never within 5 deg of its course (7 deg).
    gs_kt = 150.0
    out_s = peak_ft / (gs_kt * NM_FT / 3600.0 * math.sin(math.radians(offset_deg)))
    pts = flown_track(make_sfo_28l(), start_nm=11.0, gs_kt=gs_kt, step_s=step_s, hexid="001104", callsign="N00216",
                      legs=[(70.0, 0.0), (0.0, offset_deg), (out_s, offset_deg), (0.0, -offset_deg), (out_s, -offset_deg),
                            (0.0, 0.0), (75.0, 0.0)])     # ends within 4 nm at 8 s too
    h.feed(pts)
    h.matcher.tick(pts[-1].t + 46)
    assert [(r["runway"], r["outcome"]) for r in h.rows()] == [("28L", "lost_on_final")]
    assert "approach.runway_change" not in h.kinds()


@pytest.mark.parametrize("step_s", [0.5, 8.0])
def test_sidestep_at_two_nm_is_a_runway_change_at_live_and_history_rates(h, step_s):
    # A visual sidestep from 28L to 28R begun at 2 nm: 15 deg right, then rolled out on 28R's centreline at 1.5 deg/s
    gs_kt = 140.0
    v = gs_kt * NM_FT / 3600.0
    turns_ft = (v / math.radians(3.0) + v / math.radians(1.5)) * (1 - math.cos(math.radians(15.0)))
    straight_s = (751.0 - turns_ft) / (v * math.sin(math.radians(15.0)))
    pts = flown_track(make_sfo_28l(), start_nm=8.0, gs_kt=gs_kt, step_s=step_s, hexid="ac2706", callsign="UAL2432",
                      legs=[(6.0 * NM_FT / v, 0.0), (5.0, 15.0), (straight_s, 15.0), (10.0, 0.0), (24.0, 0.0)])  # to 0.3 nm
    h.feed(pts)
    h.matcher.tick(pts[-1].t + 46)
    rows = h.rows()
    assert [(r["runway"], r["outcome"]) for r in rows] == [("28L", "runway_change"), ("28R", "lost_on_final")]
    assert Flags.SIDESTEP in Flags(rows[1]["flags"]) and rows[0]["next_approach_id"] == rows[1]["id"]
    if step_s == 0.5:        # the continuation starts where the aircraft came near 28R, before it was aligned with it
        first = h.store.points(rows[1]["id"])[0]
        assert abs(first["cross_ft"]) < 300 and abs(first["track"] - make_sfo_28r().course_true_deg) > 5.0


@pytest.mark.parametrize("step_s", [0.5, 8.0])
def test_crossing_traffic_is_not_established(h, step_s):
    # N00271 (001146) at KNUQ on 2026-09-12 crossed both centrelines at 10-14 deg, level at 3,000 ft, and was recorded
    # as an abandoned approach. Slow traffic crossing 28L at 11 deg drifts across at 19 ft/s, inside the convergence
    # bound, and stays nearest 28L for 110 s: only the track tolerance keeps it a candidate.
    pts = flown_track(make_sfo_28l(), start_nm=6.0, start_cross_ft=-3000.0, start_offset_deg=11.0, legs=[(200.0, 11.0)],
                      gs_kt=60.0, step_s=step_s, hexid="001146", callsign="N00271", category="A1")
    h.feed(pts)
    h.matcher.finish()
    assert h.rows() == [] and "approach.started" not in h.kinds()


@pytest.mark.parametrize("step_s", [0.5, 8.0])
def test_turning_away_is_abandoned_once_off_course_for_abandon_min_s(h, step_s):
    # established on 28R, then a 70 deg turn away at 5 nm: abandoned once every position has been off course for
    # abandon_min_s, which is measured in seconds at the live port's 2 positions/s and at the 8 s history rate alike
    rwy = make_sfo_28r()
    on = synthetic_track(rwy, start_nm=8, end_nm=5.0, step_s=step_s)
    away = flown_track(rwy, start_nm=4.98, legs=[(0.0, 70.0), (60.0, 70.0)], gs_kt=150.0, step_s=step_s,
                       t0=on[-1].t + step_s, hexid="a44540", callsign="UAL2177")
    h.feed(on + away)
    rows = h.rows()
    assert [(r["runway"], r["outcome"]) for r in rows] == [("28R", "abandoned")]
    hold = h.cfg.detection.abandon_min_s
    assert hold <= rows[0]["ended_at"] - away[0].t < hold + step_s


def test_a_few_seconds_of_bad_track_reports_do_not_abandon_the_approach(h):
    # At 2 positions/s three consecutive track reports more than 45 deg off the course (1.5 s) used to abandon the
    # approach. Five seconds of them on an otherwise steady final is not an abandonment.
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=2.7, step_s=0.5)
    bad = range(len(pts) // 2, len(pts) // 2 + 10)
    pts = [replace(p, track=(p.track + 60.0) % 360) if i in bad else p for i, p in enumerate(pts)]
    h.feed(pts)
    h.matcher.tick(pts[-1].t + 46)
    assert [(r["runway"], r["outcome"]) for r in h.rows()] == [("28R", "lost_on_final")]


@pytest.mark.parametrize("step_s", [0.5, 8.0])
def test_go_around_turning_away_as_it_climbs_is_a_go_around(h, step_s):
    # The abandon hold is as long as the go-around climb, so a missed approach that turns more than 45 deg away while
    # it climbs is a go-around at any feed rate. At 2 positions/s the 3-position abandon rule fired 4 s into the climb.
    rwy = make_sfo_28r()
    down = synthetic_track(rwy, start_nm=8, end_nm=1.5, step_s=step_s)
    turn = flown_track(rwy, start_nm=1.45, legs=[(5.0, 60.0), (45.0, 60.0)], gs_kt=150.0, step_s=step_s,
                       t0=down[-1].t + step_s, hexid="a44540", callsign="UAL2177")
    up = [replace(p, alt_baro=down[-1].alt_baro + int(1500 * (p.t - turn[0].t) / 60), baro_rate=1500) for p in turn]
    h.feed(down + up)
    assert [(r["runway"], r["outcome"]) for r in h.rows()] == [("28R", "go_around")]


@pytest.mark.parametrize(("step_s", "n", "established"), [
    (7.5, 5, True),       # five positions spanning exactly established_window_s
    (7.475, 5, False),    # five positions spanning 29.9 s
    (10.0, 4, False),     # 30 s in four positions: fewer than established_min_points
    (0.5, 61, True),      # the live port: 61 positions spanning exactly 30 s
    (0.5, 60, False),
])
def test_establishment_window_boundaries(h, step_s, n, established):
    pts = synthetic_track(make_sfo_28r(), start_nm=8, end_nm=3.0, step_s=step_s)[:n]
    h.feed([replace(p, t=T0 + k * step_s) for k, p in enumerate(pts)])        # exact timestamps
    assert len(h.rows()) == (1 if established else 0)


@pytest.mark.parametrize("step_s", [0.5, 8.0])
def test_overshooting_the_parallel_centreline_is_still_a_runway_change(h, step_s):
    # A sidestep from 28L that crosses 28R's centreline at 15 deg and rolls out 380 ft right of it. The run begins within
    # runway_change_near_ft of 28R and continues within runway_change_far_ft, so the overshoot does not end it.
    gs_kt = 140.0
    v = gs_kt * NM_FT / 3600.0
    turns_ft = (v / math.radians(3.0) + v / math.radians(1.5)) * (1 - math.cos(math.radians(15.0)))
    straight_s = (751.0 + 380.0 - turns_ft) / (v * math.sin(math.radians(15.0)))
    pts = flown_track(make_sfo_28l(), start_nm=8.0, gs_kt=gs_kt, step_s=step_s, hexid="ac2706", callsign="UAL2432",
                      legs=[(4.0 * NM_FT / v, 0.0), (5.0, 15.0), (straight_s, 15.0), (10.0, 0.0), (40.0, 0.0)])
    h.feed(pts)
    h.matcher.tick(pts[-1].t + 46)
    rows = h.rows()
    assert [(r["runway"], r["outcome"]) for r in rows] == [("28L", "runway_change"), ("28R", "lost_on_final")]
    if step_s == 0.5:        # the continuation starts short of 28R's centreline, before the overshoot
        assert -300.0 < h.store.points(rows[1]["id"])[0]["cross_ft"] < 0.0


@pytest.mark.parametrize("step_s", [0.5, 8.0])
def test_go_around_that_turns_away_as_it_starts_to_climb_is_a_go_around(h, step_s):
    # The climb and the turn start on the same position, so the go-around and abandon holds end on the same position
    # too: the go-around rule is tested first
    rwy = make_sfo_28r()
    down = synthetic_track(rwy, start_nm=8, end_nm=1.5, step_s=step_s)
    turn = flown_track(rwy, start_nm=1.45, legs=[(0.0, 60.0), (45.0, 60.0)], gs_kt=150.0, step_s=step_s,
                       t0=down[-1].t + step_s, hexid="a44540", callsign="UAL2177")
    up = [replace(p, alt_baro=down[-1].alt_baro + int(1500 * (p.t - turn[0].t) / 60), baro_rate=1500) for p in turn]
    h.feed(down + up)
    assert [(r["runway"], r["outcome"]) for r in h.rows()] == [("28R", "go_around")]


def test_parallel_runways_are_resolved_on_the_whole_window(h):
    # Drifting at 3 deg from 28R's side of the midline onto 28L, converged on both. At 2 positions/s the last five
    # positions (2.5 s) are nearer 28L soon after the midline, but the attempt opens only once every position of the
    # 30 s window is nearer 28L.
    ksfo = h.cfg.airports["KSFO"]
    rwy_28l, rwy_28r = ksfo.runways["28L"], ksfo.runways["28R"]
    pts = flown_track(rwy_28l, start_nm=8.0, start_cross_ft=560.0, start_offset_deg=-3.0,
                      legs=[(39.7, -3.0), (5.0, 0.0), (80.0, 0.0)])
    h.feed(pts)
    h.matcher.tick(pts[-1].t + 46)
    rows = h.rows()
    nearer_l = next(p.t for p in pts if abs(h.matcher.scorer.score(rwy_28l, ksfo, p, None).cross_ft)
                    < abs(h.matcher.scorer.score(rwy_28r, ksfo, p, None).cross_ft))
    assert [x["runway"] for x in rows] == ["28L"]
    assert rows[0]["established_at"] == pytest.approx(nearer_l + h.cfg.detection.established_window_s, abs=0.5)


def test_alignment_with_the_parallel_must_hold_for_runway_change_min_s_without_a_break(h):
    # Near 28R for 20 s with the track swinging between 3 and 7 deg every 2 s: never within
    # runway_change_track_tol_deg for runway_change_min_s at a stretch, so it is not a runway change
    swing = [(0.0, 3.0), (2.0, 3.0), (0.0, 7.0), (2.0, 7.0)] * 5
    pts = flown_track(make_sfo_28l(), start_nm=11.0, gs_kt=150.0, hexid="001104", callsign="N00216",
                      legs=[(70.0, 0.0), (0.0, 6.0), (18.2, 6.0), *swing, (0.0, -7.0), (29.0, -7.0), (0.0, 0.0), (60.0, 0.0)])
    h.feed(pts)
    h.matcher.tick(pts[-1].t + 46)
    assert [(r["runway"], r["outcome"]) for r in h.rows()] == [("28L", "lost_on_final")]
    assert "approach.runway_change" not in h.kinds()


def test_runway_change_fires_runway_change_min_s_after_lining_up_with_the_parallel(h):
    # Exact 2 positions/s timestamps: on 28R's centreline and aligned from its first position, the change fires on the
    # position exactly runway_change_min_s later, and the continuation starts at that first position
    on_l = synthetic_track(make_sfo_28l(), start_nm=8, end_nm=3.0, step_s=0.5)
    on_r = synthetic_track(make_sfo_28r(), start_nm=2.9, end_nm=0.8, step_s=0.5)
    pts = [replace(p, t=T0 + k * 0.5) for k, p in enumerate(on_l + on_r)]
    h.feed(pts)
    h.matcher.tick(pts[-1].t + 46)
    rows = h.rows()
    assert [(r["runway"], r["outcome"]) for r in rows] == [("28L", "runway_change"), ("28R", "lost_on_final")]
    first_r = pts[len(on_l)].t
    assert (rows[0]["ended_at"] - first_r, rows[1]["established_at"]) == (h.cfg.detection.runway_change_min_s, first_r)
    # the new attempt's approach.started carries the height and distance of the position that opened it, against 28R,
    # not the newest position (the two differ here: the change fires runway_change_min_s, and several nm, later)
    opening = pts[len(on_l)]
    wx = h.weather.at(h.cfg.airports["KSFO"].metar, opening.t)
    expected = h.matcher.scorer.score(make_sfo_28r(), h.cfg.airports["KSFO"], opening, wx)
    started = next(payload for kind, payload in h.events if kind == "approach.started" and payload.get("sidestep"))
    assert started["hat_ft"] == round(expected.hat_ft)
    assert started["along_nm"] == round(expected.along_nm, 2)


def shared_rules_verdict(h, rwy, positions):
    """The landing and go-around rules applied to scored points alone, as the backfill applies them: the verdict and
    the time of the point that reached it, or (None, None)."""
    from adsb_approach.attempt import Attempt, State
    from adsb_approach.matcher import landing_or_go_around
    airport = h.cfg.airports["KSFO"]
    att = Attempt(hex=positions[0].hex, runway=rwy, airport=airport, state=State.ESTABLISHED, points=[], raw=[],
                  last_t=positions[0].t, flight_key=0)
    for pos in positions:
        sp = h.matcher.scorer.score(rwy, airport, pos, h.weather.at("KSFO", pos.t))
        att.add_scored(sp)
        verdict = landing_or_go_around(att, sp, h.cfg.detection)
        if verdict is not None:
            return verdict, sp.t
    return None, None


def test_the_shared_rules_reach_the_matchers_outcomes_from_scored_points_alone(h):
    rwy = make_sfo_28r()
    down = synthetic_track(rwy, start_nm=8, end_nm=1.5, nav_altitude_mcp=3000)
    up = climb_segment(rwy, start_along_nm=1.45, end_along_nm=-2.0, start_alt_ft=down[-1].alt_baro, vr_fpm=1500,
                       t0=down[-1].t + 1, nav_altitude_mcp=5000, nav_modes=frozenset({"autopilot"}))
    low = synthetic_track(rwy, hexid="abc123", callsign="UAL1", start_nm=6, end_nm=0.02, t0=T0 + 2000)
    roll = climb_segment(rwy, hexid="abc123", callsign="UAL1", start_along_nm=-0.1, end_along_nm=-0.6,
                         start_alt_ft=rwy.threshold_elev_ft + 20, vr_fpm=0, gs_kt=120, t0=low[-1].t + 1)
    climb = climb_segment(rwy, hexid="abc123", callsign="UAL1", start_along_nm=-0.65, end_along_nm=-3.0,
                          start_alt_ft=rwy.threshold_elev_ft + 25, vr_fpm=1200, gs_kt=130, t0=roll[-1].t + 1)
    h.feed(down + up + low + roll + climb)
    go_around, touch_and_go = h.rows()
    for row, positions in ((go_around, down + up), (touch_and_go, low + roll + climb)):
        verdict, at = shared_rules_verdict(h, rwy, positions)
        assert verdict is not None and (verdict.outcome.value, at) == (row["outcome"], row["ended_at"])
        assert verdict.ga is not None and verdict.ga["initiated_hat_ft"] == pytest.approx(row["ga_initiated_hat_ft"])
        assert Flags(row["flags"]) & verdict.extra_flags == verdict.extra_flags
    assert (go_around["outcome"], touch_and_go["outcome"]) == ("go_around", "touch_and_go")
    landing = synthetic_track(rwy, hexid="abc124", start_nm=3, end_nm=0.05, t0=T0 + 4000)
    rollout = ground_point(rwy, hexid="abc124", along_nm=-0.6, t=landing[-1].t + 15, gs=20.0)
    verdict, at = shared_rules_verdict(h, rwy, landing + [rollout])
    assert verdict is not None and verdict.outcome.value == "landed" and verdict.ga is None and at == rollout.t


def test_a_closed_approach_records_its_norm_and_its_gates_gain_deltas(h):
    """The deltas are measured at the close, before the flags the speed delta decides."""
    rwy = make_sfo_28r()
    h.matcher.scorer.set_norms(norms_for(key="B39M", gs=130.0))
    pts = synthetic_track(rwy, start_nm=8, end_nm=0.05, gs_kt=150.0)
    h.feed(pts)
    h.feed([ground_point(rwy, along_nm=-0.5, t=pts[-1].t + 20)])
    h.matcher.tick(pts[-1].t + 66)
    row = h.rows()[0]
    assert (row["outcome"], row["fleet_key"]) == ("landed", "type:B39M")
    flags = Flags(row["flags"])
    assert Flags.FAST in flags and Flags.SLOW not in flags
    assert Flags.UNSTABLE_1000 in flags and Flags.UNSTABLE_500 in flags
    gates = h.store.gates(row["id"])
    assert [g["fleet_gs_delta_kt"] for g in gates] == [20.0, 20.0]
    assert all(abs(g["fleet_gp_delta_ft"]) < 2.0 for g in gates)


def test_a_close_against_the_all_aircraft_norm_records_the_delta_but_not_fast(h):
    """Only a type or class norm may set FAST or SLOW (deliberately); a * norm still measures the delta and is still
    named and shown, on the live path exactly as at a splice or a reflag."""
    rwy = make_sfo_28r()
    h.matcher.scorer.set_norms(norms_for(key_kind="*", key="*", gs=130.0))
    pts = synthetic_track(rwy, start_nm=8, end_nm=0.05, gs_kt=150.0)
    h.feed(pts)
    h.feed([ground_point(rwy, along_nm=-0.5, t=pts[-1].t + 20)])
    h.matcher.tick(pts[-1].t + 66)
    row = h.rows()[0]
    assert row["outcome"] == "landed" and row["fleet_key"] == "*"
    assert not Flags(row["flags"]) & (Flags.FAST | Flags.SLOW)
    gates = h.store.gates(row["id"])
    assert [g["fleet_gs_delta_kt"] for g in gates] == [20.0, 20.0]        # the delta is still measured and stored


def test_a_slow_approach_is_flagged_slow_and_one_without_norms_has_neither(h):
    rwy = make_sfo_28r()
    h.matcher.scorer.set_norms(norms_for(key="B39M", gs=165.0))
    pts = synthetic_track(rwy, start_nm=8, end_nm=0.05, gs_kt=150.0)
    h.feed(pts)
    h.matcher.tick(pts[-1].t + 46)
    slow = h.rows()[0]
    assert Flags.SLOW in Flags(slow["flags"]) and Flags.FAST not in Flags(slow["flags"])
    h.matcher.scorer.set_norms(None)
    later = synthetic_track(rwy, hexid="abc123", callsign="UAL1", start_nm=8, end_nm=0.05, t0=T0 + 3000, gs_kt=150.0)
    h.feed(later)
    h.matcher.tick(later[-1].t + 46)
    plain = h.rows()[1]
    assert plain["fleet_key"] is None and not Flags(plain["flags"]) & (Flags.FAST | Flags.SLOW)
    assert [g["fleet_gs_delta_kt"] for g in h.store.gates(plain["id"])] == [None, None]


# --- emergency squawks: the four stamping paths ---------------------------------------------------------------------
# A flight's latched flags reach every attempt it closes by four paths: (1) at open, written onto the new row;
# (2) while open, ORed onto the open row at once; (3) at close, ORed into the flags the scorer computed; (4) just after
# close -- a 30 s run can straddle a close -- onto the flight's most recent attempt the matcher still holds as closed.
#
# Warm-up and "far" positions below sit 20-40 nm out on a runway's extended centreline: beyond AIRPORT_RADIUS_NM (15),
# so the matcher's per-airport loop skips them outright while the tracker still feeds them to the flight's watch --
# a squawk can be held long enough to latch well before the real approach, or well after it has closed, without ever
# risking a spurious candidate or establishment of its own.

def test_a_flag_latched_before_establishment_is_stamped_and_published_at_open(h):
    rwy = make_sfo_28r()
    warmup = [replace(p, squawk="7700") for p in synthetic_track(rwy, start_nm=25, end_nm=20, t0=T0)]
    approach = synthetic_track(rwy, start_nm=8, end_nm=0.05, t0=warmup[-1].t + 5)
    h.feed(warmup + approach)
    h.feed([ground_point(rwy, along_nm=-0.5, t=approach[-1].t + 20)])
    h.matcher.tick(approach[-1].t + 20 + 46)
    rows = h.rows()
    assert len(rows) == 1 and rows[0]["outcome"] == "landed"
    assert Flags.EMERGENCY_GENERAL in Flags(rows[0]["flags"])
    started = next(payload for kind, payload in h.events if kind == "approach.started")
    assert started["emergency"] == ["EMERGENCY_GENERAL"]


def test_a_runway_changes_new_attempt_is_stamped_and_published_at_open_too(h):
    """approach.started is published in two places -- a normal open and a runway change's new attempt -- and both need
    "emergency"."""
    rwy_28l, rwy_28r = make_sfo_28l(), make_sfo_28r()
    warmup = [replace(p, squawk="7700") for p in synthetic_track(rwy_28l, start_nm=25, end_nm=20, t0=T0)]
    on_l = synthetic_track(rwy_28l, start_nm=8, end_nm=3.0, t0=warmup[-1].t + 5)
    on_r = synthetic_track(rwy_28r, start_nm=2.9, end_nm=0.8, t0=on_l[-1].t + 3)
    h.feed(warmup + on_l + on_r)
    h.matcher.tick(on_r[-1].t + 46)
    rows = h.rows()
    assert [row["runway"] for row in rows] == ["28L", "28R"]
    assert all(Flags.EMERGENCY_GENERAL in Flags(row["flags"]) for row in rows)
    starts = [payload for kind, payload in h.events if kind == "approach.started"]
    assert len(starts) == 2 and all(p["emergency"] == ["EMERGENCY_GENERAL"] for p in starts)
    assert starts[1].get("sidestep") is True


def test_approach_started_carries_an_empty_emergency_list_when_none_has_latched(h):
    h.feed(synthetic_track(make_sfo_28r(), start_nm=8, end_nm=2.0))
    started = next(payload for kind, payload in h.events if kind == "approach.started")
    assert started["emergency"] == []


def test_a_flag_latched_while_established_is_stamped_and_published_before_the_close(h):
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=0.05)
    t0 = pts[0].t
    before = [p for p in pts if p.t < t0 + 40]                                    # established well within this
    during = [replace(p, squawk="7700") for p in pts if t0 + 40 <= p.t < t0 + 100]  # the run starts and latches here
    after = [replace(p, squawk="7700") for p in pts if p.t >= t0 + 100]
    assert before and during and after
    h.feed(before + during)
    row = h.rows()[0]
    assert row["outcome"] is None                                    # still open
    assert Flags.EMERGENCY_GENERAL in Flags(row["flags"])             # stamped already, ahead of any close
    emergencies = [payload for kind, payload in h.events if kind == "approach.emergency"]
    assert len(emergencies) == 1
    payload = emergencies[0]
    assert payload["in_progress"] is True and payload["emergency"] == ["EMERGENCY_GENERAL"]
    assert (payload["arrival_id"], payload["attempt_no"]) == (row["arrival_id"], 1)
    assert payload["established_at"] == row["established_at"]
    h.feed(after)
    h.feed([ground_point(rwy, along_nm=-0.5, t=pts[-1].t + 20)])
    h.matcher.tick(pts[-1].t + 20 + 46)
    final = h.rows()[0]
    assert final["outcome"] == "landed" and Flags.EMERGENCY_GENERAL in Flags(final["flags"])
    assert len([p for k, p in h.events if k == "approach.emergency"]) == 1     # not re-published later


def test_minimum_fuel_alone_publishes_no_approach_emergency_event(h):
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=0.05)
    t0 = pts[0].t
    fed = [replace(p, emergency="minfuel") if p.t >= t0 + 40 else p for p in pts]
    h.feed(fed)
    h.feed([ground_point(rwy, along_nm=-0.5, t=fed[-1].t + 20)])
    h.matcher.tick(fed[-1].t + 20 + 46)
    assert "approach.emergency" not in h.kinds()
    row = h.rows()[0]
    assert Flags.MINIMUM_FUEL in Flags(row["flags"]) and not (Flags(row["flags"]) & EMERGENCY_MASK)


def test_at_close_the_flags_written_include_whatever_was_latched_by_then(h):
    """A white-box check of _close's own flags computation: att.emergency_flags simulates a flag _stamp_emergency
    already recorded on the very last position processed, ahead of the idle timeout that closes it below -- without
    needing to engineer the watch's 30 s timer to land on an exact tick."""
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=2.7)
    h.feed(pts)
    att = h.matcher._attempts["a44540"][rwy.key]
    att.emergency_flags = Flags.EMERGENCY_UNLAWFUL
    h.matcher.tick(pts[-1].t + 46)
    row = h.rows()[0]
    assert row["outcome"] == "lost_on_final"
    assert Flags.PRESUMED_LANDED in Flags(row["flags"]) and Flags.EMERGENCY_UNLAWFUL in Flags(row["flags"])


def test_a_latch_on_a_position_no_attempt_scores_still_reaches_a_close_made_by_tick(h):
    """Path 3 ORs the flight's latched flags at the close. Touched down, the aircraft is then reported only by TIS-B,
    which is never scored, squawking 7700 long enough to latch; the lost timeout then closes the attempt from tick(),
    with no position of its own to stamp from."""
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=0.05)
    t1 = pts[-1].t
    h.feed(pts)
    h.feed([ground_point(rwy, along_nm=-0.5, t=t1 + 5, gs=40.0)])
    h.feed([replace(ground_point(rwy, along_nm=-0.6, t=t1 + 6 + i, gs=40.0), squawk="7700", source="tisb_icao")
            for i in range(35)])
    assert Flags.EMERGENCY_GENERAL in h.tracker.get("a44540").flight.emergency.latched
    h.matcher.tick(t1 + 5 + 46)
    row = h.rows()[0]
    assert row["outcome"] == "landed" and Flags.EMERGENCY_GENERAL in Flags(row["flags"])


def test_a_latch_on_a_position_no_attempt_scores_still_reaches_the_close_at_the_end_of_a_replay(h):
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=3.0)
    h.feed(pts)
    h.feed([replace(p, squawk="7700", source="tisb_icao")
            for p in synthetic_track(rwy, start_nm=2.95, end_nm=1.4, t0=pts[-1].t + 1)])
    h.matcher.finish()
    row = h.rows()[0]
    assert row["outcome"] == "lost_on_final" and Flags.EMERGENCY_GENERAL in Flags(row["flags"])

def test_a_latch_just_after_close_stamps_the_closed_approach_with_in_progress_false(h):
    """A run straddling the close (path 4): 7500 starts 10 s before the attempt goes quiet, short of latching by the
    time it does, and reaches 30 s from a later, far-out encounter of the same flight -- well outside the airport
    radius, so nothing there opens an attempt of its own for path 4 to be confused with."""
    rwy = make_sfo_28r()
    low = synthetic_track(rwy, start_nm=8, end_nm=2.7)
    fed = [replace(p, squawk="7500") if p.t >= low[-1].t - 10 else p for p in low]
    h.feed(fed)
    h.matcher.tick(low[-1].t + 46)
    aid = h.rows()[0]["id"]
    assert h.rows()[0]["outcome"] == "lost_on_final" and not Flags(h.rows()[0]["flags"]) & EMERGENCY_MASK
    far = [replace(p, squawk="7500") for p in synthetic_track(rwy, start_nm=40, end_nm=35, t0=low[-1].t + 25)]
    h.feed(far)
    row = h.store.approach(aid)
    assert Flags.EMERGENCY_UNLAWFUL in Flags(row["flags"]) and row["outcome"] == "lost_on_final"
    payload = next(p for k, p in h.events if k == "approach.emergency")
    assert payload["in_progress"] is False and payload["emergency"] == ["EMERGENCY_UNLAWFUL"]
    assert (payload["arrival_id"], payload["attempt_no"]) == (row["arrival_id"], row["attempt_no"])
    assert payload["callsign"] is None and payload["along_nm"] is None and payload["established_at"] is None


def test_a_latch_just_after_close_survives_a_later_landing_confirmation(h):
    """The trap the store's own sticky rule also guards against: set_confirmation writes back a copy of flags the
    matcher captured at close, so path 4's update to info.flags is what keeps this from erasing the latch. Straddles the
    close the same way as the test above, so the rule that a run must have started at or before ended_at still lets it
    through."""
    rwy = make_sfo_28r()
    low = synthetic_track(rwy, start_nm=8, end_nm=2.7)
    fed = [replace(p, squawk="7500") if p.t >= low[-1].t - 10 else p for p in low]
    h.feed(fed)
    h.matcher.tick(low[-1].t + 46)
    aid = h.rows()[0]["id"]
    far = [replace(p, squawk="7500") for p in synthetic_track(rwy, start_nm=40, end_nm=35, t0=low[-1].t + 25)]
    h.feed(far)
    assert Flags.EMERGENCY_UNLAWFUL in Flags(h.store.approach(aid)["flags"])
    ground = ground_point(rwy, along_nm=-0.2, t=far[-1].t + 5)
    h.feed([ground])
    row = h.store.approach(aid)
    assert row["landed_confirmed_by"] == "ground"
    assert Flags.EMERGENCY_UNLAWFUL in Flags(row["flags"]) and Flags.LANDING_CONFIRMED in Flags(row["flags"])
    confirmed = next(p for k, p in h.events if k == "approach.confirmed")
    assert "EMERGENCY_UNLAWFUL" in confirmed["flags"]


@pytest.mark.parametrize("later_s, callsign", [(40 * 60, "UAL999"), (3 * 3600, "UAL2177")],
                         ids=["new callsign on the turnaround", "same callsign after the session gap"])
def test_a_later_flight_of_the_same_airframe_never_stamps_the_earlier_flights_closed_approach(h, later_s, callsign):
    """A lost approach's ClosedInfo outlives its flight by up to landed_confirm_departure_h (8 h), so the airframe's
    next departure can confirm the landing. Path 4 is the flight's own most recent attempt: the next flight is a new
    Flight with a fresh watch, and an emergency it declares, 60 nm away, is not this arrival's."""
    rwy = make_sfo_28r()
    low = synthetic_track(rwy, start_nm=8, end_nm=2.7)
    h.feed(low)
    h.matcher.tick(low[-1].t + 46)
    aid = h.rows()[0]["id"]
    assert h.rows()[0]["outcome"] == "lost_on_final"
    first = h.tracker.get("a44540").flight
    later = [replace(p, squawk="7700", callsign=callsign)
             for p in synthetic_track(rwy, start_nm=65, end_nm=60, t0=low[-1].t + later_s)]
    h.feed(later)
    flight = h.tracker.get("a44540").flight
    assert flight is not first and Flags.EMERGENCY_GENERAL in flight.emergency.latched
    assert not Flags(h.store.approach(aid)["flags"]) & EMERGENCY_MASK
    assert "approach.emergency" not in h.kinds()


def test_a_departure_of_the_same_flight_never_stamps_its_earlier_landing(h):
    """"Flight" is the tracker's session, not a physical journey: an aircraft whose callsign is its N-number keeps
    one Flight, and one EmergencyWatch, across a landing and a departure less than session_gap_s later, so
    flight_id alone (the previous fix) cannot tell this apart from a run that straddled the close. 7700 raised well
    into the departure, 40 minutes after the landing went quiet, must not reach backwards onto it."""
    rwy = make_sfo_28r()
    low = synthetic_track(rwy, start_nm=8, end_nm=2.7)
    h.feed(low)
    h.matcher.tick(low[-1].t + 46)
    aid = h.rows()[0]["id"]
    assert h.rows()[0]["outcome"] == "lost_on_final"
    first = h.tracker.get("a44540").flight
    departure = [replace(p, squawk="7700") for p in synthetic_track(rwy, start_nm=65, end_nm=60, t0=low[-1].t + 40 * 60)]
    h.feed(departure)
    flight = h.tracker.get("a44540").flight
    assert flight is first and Flags.EMERGENCY_GENERAL in flight.emergency.latched   # same session, same watch
    assert not Flags(h.store.approach(aid)["flags"]) & EMERGENCY_MASK
    assert "approach.emergency" not in h.kinds()


def test_a_run_straddling_a_landing_reaches_the_landed_approach(h):
    """7700 set on the rollout, 10 s before the landing closes the attempt, and held: it latches 20 s after the close. A
    landing straddles a run as easily as a loss does (path 4), and holding the landed attempt for it must not let the
    confirmation rules, which are a lost attempt's, touch it."""
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=0.05)
    t1 = pts[-1].t
    h.feed(pts)
    h.feed([replace(ground_point(rwy, along_nm=-0.5, t=t1 + 20, gs=40.0), squawk="7700")])   # touched, still rolling
    h.feed([replace(ground_point(rwy, along_nm=-0.8, t=t1 + 30, gs=25.0), squawk="7700")])   # landed here
    assert h.rows()[0]["outcome"] == "landed" and not Flags(h.rows()[0]["flags"]) & EMERGENCY_MASK
    h.feed([replace(ground_point(rwy, along_nm=-0.9, t=t1 + dt, gs=10.0), squawk="7700") for dt in (40, 50, 60)])
    row = h.rows()[0]
    assert row["outcome"] == "landed" and Flags.EMERGENCY_GENERAL in Flags(row["flags"])
    assert row["landed_confirmed_by"] is None and Flags.LANDING_CONFIRMED not in Flags(row["flags"])
    payload = next(p for k, p in h.events if k == "approach.emergency")
    assert payload["in_progress"] is False and payload["emergency"] == ["EMERGENCY_GENERAL"]


def test_a_run_straddling_a_go_around_reaches_the_go_around_approach(h):
    rwy = make_sfo_28r()
    approach = synthetic_track(rwy, start_nm=8, end_nm=0.5)
    climb = [replace(p, squawk="7700") for p in climb_segment(rwy, start_along_nm=0.45, end_along_nm=-3.0,
                                                              start_alt_ft=approach[-1].alt_baro, vr_fpm=1500,
                                                              t0=approach[-1].t + 1)]
    h.feed(approach + climb)
    row = h.rows()[0]
    assert row["outcome"] == "go_around" and row["ended_at"] < climb[0].t + 30     # closed before the run latched
    assert Flags.EMERGENCY_GENERAL in Flags(row["flags"])


# ------------------------------------------------------------ adsb.lol points
EXTERNAL = Flags.EXTERNAL_TAIL | Flags.EXTERNAL_GAPS | Flags.EXTERNAL_OUTCOME
EXTERNAL_ENDING = Flags.EXTERNAL_TAIL | Flags.EXTERNAL_OUTCOME


def from_adsb_lol(positions):
    return [replace(p, origin="adsb.lol") for p in positions]


def test_an_approach_whose_ending_came_from_adsb_lol_is_flagged_tail_and_outcome_and_confirmed_by_it(h):
    # the receiver loses the aircraft at 2.5 nm; adsb.lol follows it for over lost_timeout_s down to the roll-out
    rwy = make_sfo_28r()
    local = synthetic_track(rwy, start_nm=8, end_nm=2.5)
    lol = from_adsb_lol(synthetic_track(rwy, start_nm=2.45, end_nm=0.05, t0=local[-1].t + 1))
    roll = from_adsb_lol([ground_point(rwy, along_nm=-0.2 - 0.03 * i, t=lol[-1].t + 5 + i, gs=gs)
                          for i, gs in enumerate((28.0, 22.0, 15.0))])
    assert roll[0].t - local[-1].t > h.cfg.detection.lost_timeout_s
    h.feed(local + lol + roll)
    row, = h.rows()
    flags = Flags(row["flags"])
    assert row["outcome"] == "landed" and row["ended_at"] == roll[0].t
    assert EXTERNAL_ENDING in flags and Flags.EXTERNAL_GAPS not in flags
    assert row["landed_confirmed_by"] == "adsb.lol"
    points = h.store.points(row["id"])
    assert (points[0]["origin"], points[-1]["origin"]) == ("local", "adsb.lol")


def test_a_short_adsb_lol_stretch_at_the_end_is_a_gap_and_decides_nothing(h):
    # the receiver hears the aircraft to the runway and goes quiet for 5 s; adsb.lol's point is the one that lands it
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=0.05)
    touchdown = from_adsb_lol([ground_point(rwy, along_nm=-0.2, t=pts[-1].t + 5, gs=20.0)])
    h.feed(pts + touchdown)
    row, = h.rows()
    flags = Flags(row["flags"])
    assert row["outcome"] == "landed" and row["ended_at"] == touchdown[0].t and Flags.EXTERNAL_GAPS in flags
    assert flags & (Flags.EXTERNAL_TAIL | Flags.EXTERNAL_OUTCOME) == Flags.NONE
    assert row["landed_confirmed_by"] is None


def test_adsb_lol_points_between_the_receiver_s_own_set_gaps_only(h):
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=0.05)
    gap = from_adsb_lol(pts[50:55])
    h.feed(pts[:50] + gap + pts[55:] + [ground_point(rwy, along_nm=-0.2, t=pts[-1].t + 5, gs=20.0)])
    row, = h.rows()
    flags = Flags(row["flags"])
    assert row["outcome"] == "landed" and Flags.EXTERNAL_GAPS in flags
    assert flags & (Flags.EXTERNAL_TAIL | Flags.EXTERNAL_OUTCOME) == Flags.NONE
    assert row["landed_confirmed_by"] is None
    assert [p["origin"] for p in h.store.points(row["id"])].count("adsb.lol") == 5


def test_a_receiver_only_approach_gets_no_external_flags(h):
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=0.05)
    h.feed(pts + [ground_point(rwy, along_nm=-0.2, t=pts[-1].t + 5, gs=20.0)])
    row, = h.rows()
    assert row["outcome"] == "landed" and Flags(row["flags"]) & EXTERNAL == Flags.NONE
    assert row["landed_confirmed_by"] is None
    assert {p["origin"] for p in h.store.points(row["id"])} == {"local"}


def test_a_go_around_decided_on_an_adsb_lol_point_is_its_outcome(h):
    # the receiver loses the aircraft at 3 nm; adsb.lol follows it down to 1.5 nm and up for over lost_timeout_s
    rwy = make_sfo_28r()
    local = synthetic_track(rwy, start_nm=8, end_nm=3.0, nav_altitude_mcp=3000)
    down = from_adsb_lol(synthetic_track(rwy, start_nm=2.95, end_nm=1.5, t0=local[-1].t + 1, nav_altitude_mcp=3000))
    up = from_adsb_lol(climb_segment(rwy, start_along_nm=1.45, end_along_nm=-2.0, start_alt_ft=down[-1].alt_baro,
                                     vr_fpm=1500, t0=down[-1].t + 1))
    h.feed(local + down + up)
    row, = h.rows()
    flags = Flags(row["flags"])
    assert row["outcome"] == "go_around" and row["ended_at"] - local[-1].t > h.cfg.detection.lost_timeout_s
    assert EXTERNAL_ENDING in flags and Flags.EXTERNAL_GAPS not in flags
    assert row["landed_confirmed_by"] is None


def test_a_go_around_decided_after_a_short_adsb_lol_stretch_gets_gaps_and_no_outcome(h):
    rwy = make_sfo_28r()
    down = synthetic_track(rwy, start_nm=8, end_nm=1.5, nav_altitude_mcp=3000)
    up = from_adsb_lol(climb_segment(rwy, start_along_nm=1.45, end_along_nm=-2.0, start_alt_ft=down[-1].alt_baro,
                                     vr_fpm=1500, t0=down[-1].t + 1))
    h.feed(down + up)
    row, = h.rows()
    flags = Flags(row["flags"])
    assert row["outcome"] == "go_around" and row["ended_at"] - down[-1].t < h.cfg.detection.lost_timeout_s
    assert Flags.EXTERNAL_GAPS in flags and flags & (Flags.EXTERNAL_TAIL | Flags.EXTERNAL_OUTCOME) == Flags.NONE


@pytest.mark.parametrize("lost_nm", [2.4, 2.0])
def test_a_go_around_the_receiver_lost_above_its_bottom_is_adsb_lols_outcome(h, lost_nm):
    # The receiver loses the aircraft at 600-870 ft; only adsb.lol sees the bottom near 500 ft and the climb. The
    # go-around closes 15 s into the climb, so the receiver has been silent for less than lost_timeout_s at the close
    # (37 s and 27 s here), and the silence rule alone would call the whole stretch gaps.
    rwy = make_sfo_28r()
    local = synthetic_track(rwy, start_nm=8, end_nm=lost_nm, nav_altitude_mcp=3000)
    down = from_adsb_lol(synthetic_track(rwy, start_nm=lost_nm - 0.05, end_nm=1.5, t0=local[-1].t + 1,
                                         nav_altitude_mcp=3000))
    up = from_adsb_lol(climb_segment(rwy, start_along_nm=1.45, end_along_nm=-2.0, start_alt_ft=down[-1].alt_baro,
                                     vr_fpm=1500, t0=down[-1].t + 1))
    h.feed(local + down + up)
    row, = h.rows()
    flags = Flags(row["flags"])
    assert row["outcome"] == "go_around" and row["ended_at"] - local[-1].t < h.cfg.detection.lost_timeout_s
    assert EXTERNAL_ENDING in flags and Flags.EXTERNAL_GAPS not in flags
    assert row["landed_confirmed_by"] is None


def test_the_live_events_carry_the_origin_of_the_position_they_report(h):
    # the drawer of an attempt still open draws its points from these events
    rwy = make_sfo_28r()
    local = synthetic_track(rwy, start_nm=8, end_nm=5.0)
    lol = from_adsb_lol(synthetic_track(rwy, start_nm=4.95, end_nm=3.0, t0=local[-1].t + 1))
    h.feed(local + lol)
    (started,) = [payload for kind, payload in h.events if kind == "approach.started"]
    updated = [payload for kind, payload in h.events if kind == "approach.updated"]
    assert started["origin"] == "local"
    assert {p["origin"] for p in updated if p["along_nm"] > 5.0} == {"local"}
    assert {p["origin"] for p in updated if p["along_nm"] < 4.9} == {"adsb.lol"}


@pytest.mark.parametrize("origin", ["local", "adsb.lol"])
def test_an_attempt_established_on_an_adsb_lol_position_starts_with_that_origin(h, origin):
    h.feed([replace(p, origin=origin) for p in synthetic_track(make_sfo_28r(), start_nm=8, end_nm=6.0)])
    (started,) = [payload for kind, payload in h.events if kind == "approach.started"]
    assert started["origin"] == origin


@pytest.mark.parametrize("origin", ["local", "adsb.lol"])
def test_a_runway_changes_new_attempt_starts_with_the_origin_of_its_first_point(h, origin):
    rwy_28l, rwy_28r = make_sfo_28l(), make_sfo_28r()
    on_l = synthetic_track(rwy_28l, start_nm=8, end_nm=3.0)
    on_r = [replace(p, origin=origin) for p in synthetic_track(rwy_28r, start_nm=2.9, end_nm=0.8, t0=on_l[-1].t + 3)]
    h.feed(on_l + on_r)
    started = [payload for kind, payload in h.events if kind == "approach.started"]
    assert [(p["runway"], p["origin"]) for p in started] == [("28L", "local"), ("28R", origin)]


def scored(h, positions):
    ksfo = h.cfg.airports["KSFO"]
    return [h.matcher.scorer.score(make_sfo_28r(), ksfo, p, None) for p in positions]


@pytest.mark.parametrize("origins, step_s, expected", [
    ("", 1.0, Flags.NONE),
    ("llll", 1.0, Flags.NONE),
    ("llaa", 1.0, Flags.EXTERNAL_GAPS),             # a short trailing stretch is a hole the receiver left, not a tail
    ("llaa", 30.0, Flags.EXTERNAL_TAIL),            # 60 s of silence after its last point
    ("lala", 1.0, Flags.EXTERNAL_GAPS),
    ("lala", 50.0, Flags.EXTERNAL_TAIL | Flags.EXTERNAL_GAPS),
    ("lall", 1.0, Flags.EXTERNAL_GAPS),
    ("aall", 1.0, Flags.NONE),                      # adsb.lol before the receiver's first point is neither
    ("aall", 50.0, Flags.NONE),
    ("aaaa", 1.0, Flags.EXTERNAL_TAIL),             # the receiver never heard it
    ("laalla", 1.0, Flags.EXTERNAL_GAPS),
    ("laalla", 50.0, Flags.EXTERNAL_TAIL | Flags.EXTERNAL_GAPS),
])
def test_external_flags_read_the_origins_and_times_of_the_points(h, origins, step_s, expected):
    pts = synthetic_track(make_sfo_28r(), start_nm=8, end_nm=7.0)
    pts = [replace(p, t=T0 + i * step_s, origin="local" if c == "l" else "adsb.lol")
           for i, (p, c) in enumerate(zip(pts, origins))]
    assert external_flags(scored(h, pts), h.cfg.detection.lost_timeout_s) == expected


@pytest.mark.parametrize("stretch_over_s, expected", [(-1.0, Flags.EXTERNAL_GAPS), (0.0, Flags.EXTERNAL_GAPS),
                                                      (0.5, Flags.EXTERNAL_TAIL)])
def test_a_tail_needs_the_receiver_silent_for_longer_than_lost_timeout_s(h, stretch_over_s, expected):
    lost = h.cfg.detection.lost_timeout_s
    pts = synthetic_track(make_sfo_28r(), start_nm=8, end_nm=7.0)[:4]
    # the receiver's last point is at T0 + 1
    times = (T0, T0 + 1, T0 + 1 + lost / 2, T0 + 1 + lost + stretch_over_s)
    pts = [replace(p, t=t, origin="local" if i < 2 else "adsb.lol") for i, (p, t) in enumerate(zip(pts, times))]
    assert external_flags(scored(h, pts), lost) == expected


@pytest.mark.parametrize("bottom_after_s, expected", [(0.5, Flags.EXTERNAL_TAIL), (0.0, Flags.EXTERNAL_GAPS),
                                                      (-0.5, Flags.EXTERNAL_GAPS)])
def test_a_tail_also_counts_when_the_receiver_stopped_before_a_go_arounds_bottom(h, bottom_after_s, expected):
    lost = h.cfg.detection.lost_timeout_s
    pts = synthetic_track(make_sfo_28r(), start_nm=8, end_nm=7.0)[:4]
    times = (T0, T0 + 1, T0 + 5, T0 + 9)                         # the receiver is silent for 8 s at the end only
    pts = [replace(p, t=t, origin="local" if i < 2 else "adsb.lol") for i, (p, t) in enumerate(zip(pts, times))]
    assert external_flags(scored(h, pts), lost) == Flags.EXTERNAL_GAPS
    assert external_flags(scored(h, pts), lost, bottom_t=T0 + 1 + bottom_after_s) == expected


@pytest.mark.parametrize("last_origin, receiver_until_nm, outcome_flag, confirmed_by", [
    ("local", 0.05, Flags.NONE, None),
    ("adsb.lol", 0.05, Flags.NONE, None),                               # quiet for 5 s only
    ("adsb.lol", 2.5, Flags.EXTERNAL_OUTCOME, "adsb.lol"),             # followed down by adsb.lol alone
])
def test_a_lost_touched_approach_lands_on_the_origin_of_its_last_point(h, last_origin, receiver_until_nm, outcome_flag,
                                                                       confirmed_by):
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=receiver_until_nm)
    if receiver_until_nm > 1.0:
        pts += from_adsb_lol(synthetic_track(rwy, start_nm=receiver_until_nm - 0.05, end_nm=0.05, t0=pts[-1].t + 1))
    # fast, so not yet landed
    last = replace(ground_point(rwy, along_nm=-0.2, t=pts[-1].t + 5, gs=40.0), origin=last_origin)
    h.feed(pts + [last])
    assert h.rows()[0]["outcome"] is None
    h.matcher.tick(last.t + 46)
    row, = h.rows()
    assert row["outcome"] == "landed" and Flags(row["flags"]) & Flags.EXTERNAL_OUTCOME == outcome_flag
    assert row["landed_confirmed_by"] == confirmed_by


@pytest.mark.parametrize("receiver_until_nm, expected", [(5.0, Flags.EXTERNAL_GAPS), (6.5, EXTERNAL_ENDING)])
def test_an_abandon_is_adsb_lols_outcome_only_after_a_long_silence_of_the_receiver(h, receiver_until_nm, expected):
    rwy = make_sfo_28r()
    on = synthetic_track(rwy, start_nm=8, end_nm=receiver_until_nm)
    if receiver_until_nm > 5.0:
        on += from_adsb_lol(synthetic_track(rwy, start_nm=receiver_until_nm - 0.05, end_nm=5.0, t0=on[-1].t + 1))
    off = from_adsb_lol(synthetic_track(rwy, start_nm=4.9, end_nm=3.5, cross_ft=9000, track_offset_deg=70,
                                        t0=on[-1].t + 1))
    h.feed(on + off)
    row, = h.rows()
    assert row["outcome"] == "abandoned" and Flags(row["flags"]) & EXTERNAL == expected
    assert row["landed_confirmed_by"] is None


def test_a_runway_change_and_a_loss_are_never_adsb_lols_outcome(h):
    rwy_28l, rwy_28r = make_sfo_28l(), make_sfo_28r()
    on_l = synthetic_track(rwy_28l, start_nm=8, end_nm=3.0)
    on_r = from_adsb_lol(synthetic_track(rwy_28r, start_nm=2.9, end_nm=0.8, t0=on_l[-1].t + 3))
    h.feed(on_l + on_r)
    h.matcher.tick(on_r[-1].t + 46)
    rows = h.rows()
    assert [(x["runway"], x["outcome"]) for x in rows] == [("28L", "runway_change"), ("28R", "lost_on_final")]
    for row in rows:
        assert Flags(row["flags"]) & Flags.EXTERNAL_OUTCOME == Flags.NONE and row["landed_confirmed_by"] is None
    # 28L's last points are the few seconds of adsb.lol it held before the change; 28R never held a receiver point
    assert Flags(rows[0]["flags"]) & EXTERNAL == Flags.EXTERNAL_GAPS
    assert Flags(rows[1]["flags"]) & EXTERNAL == Flags.EXTERNAL_TAIL


# ------------------------------------------------------------ a go-around's climb-out
def go_around_stream(rwy, *, lowest_nm, hexid="a44540", t0=T0):
    """An approach that bottoms out at lowest_nm and climbs away along the course: (down, up)."""
    down = synthetic_track(rwy, hexid=hexid, start_nm=8, end_nm=lowest_nm, nav_altitude_mcp=3000, t0=t0)
    up = climb_segment(rwy, hexid=hexid, start_along_nm=lowest_nm - 0.05, end_along_nm=-3.0,
                       start_alt_ft=down[-1].alt_baro, vr_fpm=1500, t0=down[-1].t + 1)
    return down, up


def feed_until_closed(h, positions):
    """Feed positions one at a time through the one that closes the next approach; the positions after it."""
    closed = h.kinds().count("approach.completed")
    for i, p in enumerate(positions):
        h.feed([p])
        if h.kinds().count("approach.completed") > closed:
            return positions[i + 1:]
    raise AssertionError("the approach never closed")


def reaching(positions, start_t, target_ft):
    """The first of positions by which target_ft has been flown since start_t, counted without the matcher."""
    chain = [p for p in positions if p.t >= start_t]
    flown = 0.0
    for prev, p in zip(chain, chain[1:]):
        flown += distance_ft(prev.lat, prev.lon, p.lat, p.lon)
        if flown >= target_ft:
            return p
    raise AssertionError("never flew that far")


def closed_state(h, approach_id):
    """Everything a close decides: the row (outcome, flags, summary, go-around fields), the gates and the bins."""
    return (dict(h.store.approach(approach_id)), [dict(g) for g in h.store.gates(approach_id)],
            [dict(b) for b in h.store.bins(approach_id)])


def extended_events(h):
    return [payload for kind, payload in h.events if kind == "approach.extended"]


def test_a_go_around_keeps_its_climb_out_to_1_nm_unscored(h):
    rwy = make_sfo_28r()
    down, up = go_around_stream(rwy, lowest_nm=0.45)
    after = feed_until_closed(h, down + up)
    row, = h.rows()
    aid = row["id"]
    assert row["outcome"] == "go_around" and row["ga_initiated_along_nm"] < 1.0
    at_close, n_close = closed_state(h, aid), len(h.store.points(aid))
    h.feed(after)
    last = reaching(down + up, row["ga_initiated_at"], NM_FT)
    added = [p["t"] for p in h.store.points(aid)][n_close:]
    assert added and added == [p.t for p in after if p.t <= last.t]       # up to the position that flew the 1 nm
    assert {p["origin"] for p in h.store.points(aid)} == {"local"}
    assert closed_state(h, aid) == at_close                                # gates, bins, summary, flags, outcome
    assert extended_events(h) == [{
        "hex": "a44540", "airport": "KSFO", "runway": "28R", "arrival_id": aid, "attempt_no": 1,
        "points_added": len(added), "gap_points_added": 0, "flags": flag_names(Flags(row["flags"]))}]


def test_a_go_around_s_climb_out_uses_d_when_it_is_over_1_nm(h):
    rwy = make_sfo_28r()
    down, up = go_around_stream(rwy, lowest_nm=2.5)
    after = feed_until_closed(h, down + up)
    row, = h.rows()
    aid = row["id"]
    assert row["ga_initiated_along_nm"] == pytest.approx(2.5, abs=0.05)
    n_close = len(h.store.points(aid))
    h.feed(after)
    last = reaching(down + up, row["ga_initiated_at"], row["ga_initiated_along_nm"] * NM_FT)
    assert last.t > reaching(down + up, row["ga_initiated_at"], NM_FT).t               # farther than the 1 nm floor
    points = h.store.points(aid)
    assert len(points) > n_close and points[-1]["t"] == last.t
    assert [extended["points_added"] for extended in extended_events(h)] == [len(points) - n_close]


def test_a_climb_out_takes_adsb_lol_positions_too(h):
    rwy = make_sfo_28r()
    down, up = go_around_stream(rwy, lowest_nm=0.45)
    after = feed_until_closed(h, down + up)
    row, = h.rows()
    aid = row["id"]
    at_close, n_close = closed_state(h, aid), len(h.store.points(aid))
    h.feed(from_adsb_lol(after))
    points = h.store.points(aid)
    assert points[-1]["t"] == reaching(down + up, row["ga_initiated_at"], NM_FT).t
    assert len(points) > n_close and {p["origin"] for p in points[n_close:]} == {"adsb.lol"}
    assert closed_state(h, aid) == at_close                      # the flags stay those the close decided
    assert len(extended_events(h)) == 1


def test_a_climb_out_stops_when_the_next_attempt_is_established(h):
    rwy_28l, rwy_28r = make_sfo_28l(), make_sfo_28r()
    down, up = go_around_stream(rwy_28r, lowest_nm=6.0)         # D = 6 nm: 6 nm of flying would end it
    after = feed_until_closed(h, down + up)
    first, = h.rows()
    again = synthetic_track(rwy_28l, start_nm=8, end_nm=0.5, t0=after[0].t)
    for i, p in enumerate(again):
        h.feed([p])
        if h.kinds().count("approach.started") == 2:
            break
    else:
        raise AssertionError("the second attempt was never established")
    n_established = len(h.store.points(first["id"]))
    h.feed(again[i + 1:])
    assert len(h.store.points(first["id"])) == n_established
    assert extended_events(h) == []                             # 6 nm were not flown before it, and none after


@pytest.mark.parametrize("watch_max_min", [1, 20])
def test_a_climb_out_stops_after_watch_max_min(h, watch_max_min):
    h.cfg.external.watch_max_min = watch_max_min
    rwy = make_sfo_28r()
    down, up = go_around_stream(rwy, lowest_nm=6.0)             # D = 6 nm, which takes over 2 minutes to fly
    after = feed_until_closed(h, down + up)
    row, = h.rows()
    aid = row["id"]
    reached = reaching(down + up, row["ga_initiated_at"], 6.0 * NM_FT)
    assert reached.t - row["ended_at"] > 61                     # so the watch time, not the distance, is what stops it
    h.feed(after)
    stored_until = min(reached.t, row["ended_at"] + watch_max_min * 60)
    assert h.store.points(aid)[-1]["t"] == stored_until
    assert after[60].t == row["ended_at"] + 61
    assert (after[60].t in [p["t"] for p in h.store.points(aid)]) == (watch_max_min == 20)


def test_a_landing_stores_nothing_after_its_close(h):
    rwy = make_sfo_28r()
    pts = synthetic_track(rwy, start_nm=8, end_nm=0.05)
    roll = [ground_point(rwy, along_nm=-0.2 - 0.03 * i, t=pts[-1].t + 5 + i, gs=gs)
            for i, gs in enumerate((28.0, 22.0, 15.0, 10.0, 5.0))]
    after = feed_until_closed(h, pts + roll)
    row, = h.rows()
    assert row["outcome"] == "landed" and after
    n_close = len(h.store.points(row["id"]))
    h.feed(after)
    assert len(h.store.points(row["id"])) == n_close and extended_events(h) == []


def test_a_position_half_a_second_past_the_watch_time_is_not_stored(h):
    h.cfg.external.watch_max_min = 1
    down, up = go_around_stream(make_sfo_28r(), lowest_nm=6.0)
    after = feed_until_closed(h, down + up)
    row, = h.rows()
    h.feed(after[:60])                                    # the last at ended_at + 60 s, where the watch ends
    n = len(h.store.points(row["id"]))
    h.feed([replace(after[59], t=after[59].t + 0.5)])     # inside the second since the last tick
    assert len(h.store.points(row["id"])) == n


def test_a_climb_out_nobody_feeds_is_dropped_by_the_clock(h):
    h.cfg.external.watch_max_min = 1
    down, up = go_around_stream(make_sfo_28r(), lowest_nm=6.0)
    feed_until_closed(h, down + up)
    row, = h.rows()
    assert "a44540" in h.matcher._climb_outs
    h.matcher.tick(row["ended_at"] + 60)
    assert "a44540" in h.matcher._climb_outs
    h.matcher.tick(row["ended_at"] + 61)
    assert h.matcher._climb_outs == {}


@pytest.mark.parametrize("over_s, kept", [(-1.0, True), (0.0, True), (1.0, False)])
def test_a_climb_out_ends_at_a_gap_longer_than_lost_timeout_s(h, over_s, kept):
    # with the merge off, or adsb.lol silent, a reappearance minutes later is not the climb-out: it must not join it,
    # nor meet the target in one jump
    down, up = go_around_stream(make_sfo_28r(), lowest_nm=6.0)
    after = feed_until_closed(h, down + up)
    row, = h.rows()
    h.feed(after[:5])
    n = len(h.store.points(row["id"]))
    shift = h.cfg.detection.lost_timeout_s + over_s - 1.0       # puts after[5] lost_timeout_s + over_s past after[4]
    h.feed([replace(p, t=p.t + shift) for p in after[5:10]])
    assert (len(h.store.points(row["id"])) > n) is kept
    assert ("a44540" in h.matcher._climb_outs) is kept


def test_a_position_that_is_not_newer_than_the_last_one_taken_is_not_counted_again(h):
    down, up = go_around_stream(make_sfo_28r(), lowest_nm=0.45)
    after = feed_until_closed(h, down + up)
    row, = h.rows()
    aid = row["id"]
    n_close = len(h.store.points(aid))
    h.feed(after[:1])
    h.feed([after[0], replace(after[0], t=after[0].t - 1)])
    h.feed(after[1:])
    points = h.store.points(aid)
    assert [extended["points_added"] for extended in extended_events(h)] == [len(points) - n_close]


def test_a_climb_out_counts_the_positions_it_does_not_store(h):
    # a go-around that turns back out along the extended centreline passes score_max_along_nm before it has flown D
    rwy = make_sfo_28r()
    down = synthetic_track(rwy, start_nm=12, end_nm=9.0, offset_ft=-1500)
    step_ft = 160.0 * NM_FT / 3600.0
    template = climb_segment(rwy, start_along_nm=9.0, end_along_nm=9.0, start_alt_ft=down[-1].alt_baro, vr_fpm=1500,
                             t0=down[-1].t + 1)[0]
    back = []
    for i in range(260):
        lat, lon = destination(rwy.threshold_lat, rwy.threshold_lon, (rwy.course_true_deg + 180) % 360,
                               9.0 * NM_FT + i * step_ft)
        back.append(replace(template, t=template.t + i, lat=lat, lon=lon, alt_baro=template.alt_baro + 25 * i,
                            track=(rwy.course_true_deg + 180) % 360))
    after = feed_until_closed(h, down + back)
    row, = h.rows()
    aid = row["id"]
    assert row["outcome"] == "go_around" and row["ga_initiated_along_nm"] == pytest.approx(9.0, abs=0.05)
    n_close = len(h.store.points(aid))
    h.feed(after)
    points = h.store.points(aid)[n_close:]
    reached = reaching(down + back, row["ga_initiated_at"], row["ga_initiated_along_nm"] * NM_FT)
    assert points and max(p["along_nm"] for p in points) <= h.cfg.detection.score_max_along_nm
    assert points[-1]["t"] < reached.t                           # the position that flew the 9 nm lies past 10 nm
    assert [extended["points_added"] for extended in extended_events(h)] == [len(points)]


def test_a_go_around_whose_close_already_covers_the_distance_gets_no_climb_out(h):
    rwy = make_sfo_28r()
    down = synthetic_track(rwy, start_nm=8, end_nm=0.45)
    up = climb_segment(rwy, start_along_nm=0.4, end_along_nm=-3.0, start_alt_ft=down[-1].alt_baro, vr_fpm=1500,
                       gs_kt=300.0, t0=down[-1].t + 1)             # 15 s of this climb is over 1 nm
    after = feed_until_closed(h, down + up)
    row, = h.rows()
    aid = row["id"]
    assert row["outcome"] == "go_around" and reaching(down + up, row["ga_initiated_at"], NM_FT).t <= row["ended_at"]
    assert h.matcher._climb_outs == {}
    n_close = len(h.store.points(aid))
    h.feed(after)
    assert len(h.store.points(aid)) == n_close and extended_events(h) == []


def test_a_touch_and_go_keeps_its_climb_out_too(h):
    rwy = make_sfo_28r()
    down = synthetic_track(rwy, start_nm=6, end_nm=0.02)
    roll = [ground_point(rwy, along_nm=-0.1 - 0.03 * i, t=down[-1].t + 1 + i, gs=120.0) for i in range(8)]
    up = climb_segment(rwy, start_along_nm=-0.4, end_along_nm=-3.0, start_alt_ft=rwy.threshold_elev_ft + 25,
                       vr_fpm=1200, gs_kt=130, t0=roll[-1].t + 1)
    after = feed_until_closed(h, down + roll + up)
    row, = h.rows()
    aid = row["id"]
    assert row["outcome"] == "touch_and_go" and row["ga_initiated_along_nm"] < 1.0
    at_close, n_close = closed_state(h, aid), len(h.store.points(aid))
    h.feed(after)
    points = h.store.points(aid)
    assert len(points) > n_close and points[-1]["t"] == reaching(down + roll + up, row["ga_initiated_at"], NM_FT).t
    assert closed_state(h, aid) == at_close
    assert [extended["points_added"] for extended in extended_events(h)] == [len(points) - n_close]


def test_a_coarse_position_is_not_part_of_a_climb_out(h):
    down, up = go_around_stream(make_sfo_28r(), lowest_nm=6.0)
    after = feed_until_closed(h, down + up)
    row, = h.rows()
    n_close = len(h.store.points(row["id"]))
    h.feed([replace(after[0], source="tisb_icao"), replace(after[1], hex="~a44540")])
    assert len(h.store.points(row["id"])) == n_close
    h.feed(after[2:3])
    assert len(h.store.points(row["id"])) == n_close + 1


def test_a_rebuild_never_rescores_a_climb_out(h):
    """The fleet rebuild's coverage and reflag read an approach's stored points back and score them again; the climb-out
    is for the charts only, so neither may count it: the bins and the coverage record stay those of the points the close
    scored. Two go-arounds, since a day's quartiles need two approaches."""
    rwy = make_sfo_28r()
    records, rows, bins_at_close = [], [], {}
    for k, hexid in enumerate(("a44540", "abc123")):
        down, up = go_around_stream(rwy, lowest_nm=2.5, hexid=hexid, t0=T0 + 1000.0 * k)
        after = feed_until_closed(h, down + up)
        row = h.rows()[-1]
        aid = row["id"]
        records.append(coverage_record(h.matcher.scorer, rwy, [ScoredPoint.from_row(p) for p in h.store.points(aid)]))
        bins_at_close[aid] = [dict(b) for b in h.store.bins(aid)]
        n_close = len(h.store.points(aid))
        h.feed(after)
        climb_out = h.store.points(aid)[n_close:]
        assert climb_out and min(p["along_nm"] for p in climb_out) < records[-1]["closest_along_nm"] - 1.0
        rows.append(h.store.approach(aid))

    coverage, _ = coverage_rows(h.store, h.cfg, scorer_for(h.cfg), since=T0 - DAY_S, include_replays=True, stored={})
    day, = coverage
    assert {k: v for k, v in day.items() if k not in ("airport", "runway", "day")} == coverage_row(
        records, h.cfg.detection.gates_ft, backfill_n=0)
    assert day["n"] == 2
    assert day["closest_nm_p50"] == pytest.approx(statistics.median(r["closest_along_nm"] for r in rows))
    assert day["lowest_hat_p50"] == pytest.approx(statistics.median(r["lowest_hat_ft"] for r in rows))

    reflag(h.store, h.cfg, since=T0 - DAY_S, include_replays=True, norms=norms_for())
    assert {aid: [dict(b) for b in h.store.bins(aid)] for aid in bins_at_close} == bins_at_close

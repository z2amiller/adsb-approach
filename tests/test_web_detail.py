import math
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from adsb_approach.geometry import NM_FT
from adsb_approach.model import Outcome
from adsb_approach.web.db import snapshot
from adsb_approach.web.detail import approach_detail, tar1090_base, tar1090_link, tar1090_live_link
from tests.lookups import extend_approach, record_lookup, schedule_lookup
from tests.webdb import LiveWriter, before_migration_8, copy_db

TAR1090 = "http://tar1090.example/tar1090/"
NOW = 1789300000.0     # well after the 2026-09-12 replay fixtures; irrelevant except to the go-around/last_seen tests
NO_PATH = {"path": None, "path_label": None, "path_source": None}      # a reference of an approach that flew no path
ILS_CENTRELINE = {"centreline_label": "ILS"}                            # what the log calls a runway's centreline: KSFO 28L has an ILS


def ual2177_go_around(db) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT id FROM approaches WHERE hex = 'a44540' AND attempt_no = 1").fetchone()[0]
    finally:
        conn.close()


def ual2177_second_attempt(db) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT id FROM approaches WHERE hex = 'a44540' AND attempt_no = 2").fetchone()[0]
    finally:
        conn.close()


def line_points(tch_ft: float, glide_path_deg: float, along_nms: list[float], t0: float = NOW - 3600.0) -> list[dict]:
    """Points exactly on the (tch_ft, glide_path_deg) reference line -- gp_dev_ft 0, so hat_ft - gp_dev_ft is that
    line's own height at along_nm, the identity reference_from_points (web/detail.py) inverts to recover it."""
    slope = NM_FT * math.tan(math.radians(glide_path_deg))
    return [{"t": t0 + i, "along_nm": nm, "hat_ft": tch_ft + nm * slope, "gp_dev_ft": 0.0}
            for i, nm in enumerate(along_nms)]


def close_approach_with_points(db, *, points: list[dict], runway: str = "28L", airport: str = "KSFO",
                               reference_source: str = "cifp:test", hexid: str = "dead01",
                               t0: float = NOW - 3600.0, path: str | None = None) -> int:
    """A closed, landed approach on `db` whose `approach_points` are exactly `points`: through Store.open_approach and
    close_approach directly, not LiveWriter (whose close_attempt always writes none, tests/webdb.py), so a test can
    set stored along_nm/hat_ft/gp_dev_ft to imply whatever reference it needs, independent of rwy's current
    configuration -- and reference_source to whatever the approach was scored against, independent of the runway's."""
    from adsb_approach.model import Flags
    from adsb_approach.store.db import Store
    store = Store(db)
    try:
        run_id = store.new_run("live", "test")
        aid = store.open_approach(run_id=run_id, flight_id=None, arrival_id=None, attempt_no=1, hex=hexid,
                                  callsign="TEST1", type_code="B738", traffic_class="airline", approach_cat="C",
                                  airport=airport, runway=runway, established_at=t0,
                                  first_along_nm=points[0]["along_nm"] if points else 6.0, weather_id=None,
                                  reference_source=reference_source, prev_approach_id=None, path=path)
        store.close_approach(aid, outcome=Outcome.LANDED, flags=Flags.NONE, ended_at=t0 + 60.0, summary={}, gates=[],
                             points=points, bins=[], ga=None)
    finally:
        store.close()
    return aid


def test_the_detail_of_ual2177s_go_around(replay_db, example_cfg):
    aid = ual2177_go_around(replay_db)
    with snapshot(replay_db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
        stored = conn.execute("SELECT COUNT(*) FROM approach_points WHERE approach_id = ?", (aid,)).fetchone()[0]
    assert d is not None
    a = d["approach"]
    assert (a["hex"], a["airport"], a["runway"], a["outcome"]) == ("a44540", "KSFO", "28L", "go_around")
    assert "INFERRED_GO_AROUND" in a["flags"] and a["flags_mask"] & (1 << 16)
    assert [(s["attempt_no"], s["outcome"]) for s in d["arrival"]] == [(1, "go_around"), (2, "lost_on_final")]
    by_attempt = {s["attempt_no"]: s for s in d["arrival"]}
    assert by_attempt[1]["flags"] == a["flags"] and "INFERRED_GO_AROUND" in by_attempt[1]["flags"]  # this approach itself
    assert by_attempt[2]["flags"] != by_attempt[1]["flags"]           # each attempt carries its own, not a shared copy
    assert len(d["points"]) == stored > 10
    assert [p["t"] for p in d["points"]] == sorted(p["t"] for p in d["points"])
    gates = {g["gate_ft"]: g for g in d["gates"]}
    assert gates[1000]["observed"] is True and 3.0 < gates[1000]["along_nm"] < 3.6 and -90 < gates[1000]["gp_dev_ft"] < -10
    assert d["weather"] is not None and "KSFO" in d["weather"]["raw"]
    rwy = example_cfg.airports["KSFO"].runways["28L"]
    # tch_ft/glide_path_deg are refit from this approach's own points, not read from rwy directly; exact, but the round
    # trip through tan/atan need not be bit-identical to rwy's own values.
    assert d["reference"] == {"glide_path_deg": pytest.approx(rwy.glide_path_deg), "tch_ft": pytest.approx(rwy.tch_ft),
                              "threshold_elev_ft": rwy.threshold_elev_ft, "ft_per_nm": NM_FT,
                              "reference_source": rwy.reference_source, **NO_PATH, **ILS_CENTRELINE}
    assert d["tar1090_url"] == TAR1090 + "?icao=a44540&showTrace=2026-09-12&startTime=15:14:25&endTime=15:31:18"


def test_the_reference_path_is_the_line_every_stored_deviation_is_measured_from(replay_db, example_cfg):
    with snapshot(replay_db) as conn:
        d = approach_detail(conn, ual2177_go_around(replay_db), example_cfg, TAR1090, NOW)
    ref = d["reference"]
    checked = 0
    for p in d["points"]:
        if p["along_nm"] >= 0 and p["gp_dev_ft"] is not None:
            line = ref["tch_ft"] + p["along_nm"] * ref["ft_per_nm"] * math.tan(math.radians(ref["glide_path_deg"]))
            assert p["hat_ft"] - p["gp_dev_ft"] == pytest.approx(line, abs=0.5)
            checked += 1
    assert checked > 10


def test_a_stored_reference_different_from_the_runways_current_one_wins(tmp_path, replay_db, example_cfg):
    """A runway's tch_ft, glide_path_deg or reference_source can move on (a CIFP cycle, a fallback to REFERENCE_ASSUMED)
    after an approach was already scored against the old one. The drawer must draw the line the approach's own stored
    deviations were actually measured against, not whatever the runway is configured to now -- this is the bug, and this
    test must fail before the fix."""
    db = copy_db(replay_db, tmp_path / "drift.sqlite")
    rwy = example_cfg.airports["KSFO"].runways["28L"]
    old_tch_ft, old_glide_path_deg = rwy.tch_ft + 27.0, rwy.glide_path_deg + 0.6
    points = line_points(old_tch_ft, old_glide_path_deg, [0.5, 1.5, 2.5, 3.5, 4.5, 5.5])
    aid = close_approach_with_points(db, points=points, reference_source="cifp:OLD_CYCLE")
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d["reference"] == {"glide_path_deg": pytest.approx(old_glide_path_deg), "tch_ft": pytest.approx(old_tch_ft),
                              "threshold_elev_ft": rwy.threshold_elev_ft, "ft_per_nm": NM_FT,
                              "reference_source": "cifp:OLD_CYCLE", **NO_PATH, **ILS_CENTRELINE}
    assert d["reference"]["tch_ft"] != rwy.tch_ft and d["reference"]["glide_path_deg"] != rwy.glide_path_deg


def test_the_derived_references_source_is_the_approachs_own_not_the_runways(tmp_path, replay_db, example_cfg):
    db = copy_db(replay_db, tmp_path / "drift-source.sqlite")
    rwy = example_cfg.airports["KSFO"].runways["28L"]
    points = line_points(rwy.tch_ft + 10.0, rwy.glide_path_deg, [1.0, 2.0, 3.0, 4.0])
    aid = close_approach_with_points(db, points=points, reference_source="cifp:SOME_OTHER_CYCLE")
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d["reference"]["reference_source"] == "cifp:SOME_OTHER_CYCLE" != rwy.reference_source


def test_fewer_than_two_usable_points_falls_back_to_the_runways_configuration(tmp_path, replay_db, example_cfg):
    """A very short or unscored track: too little of the approach's own line survives to refit, so the drawer falls
    back to the runway's configuration now, the same line it would have drawn before this fix."""
    db = copy_db(replay_db, tmp_path / "short.sqlite")
    rwy = example_cfg.airports["KSFO"].runways["28L"]
    points = line_points(rwy.tch_ft + 40.0, rwy.glide_path_deg, [2.0])       # one usable point: too few to fit a line
    aid = close_approach_with_points(db, points=points, reference_source="cifp:OLD_CYCLE")
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d["reference"] == {"glide_path_deg": rwy.glide_path_deg, "tch_ft": rwy.tch_ft,
                              "threshold_elev_ft": rwy.threshold_elev_ft, "ft_per_nm": NM_FT,
                              "reference_source": rwy.reference_source, **NO_PATH, **ILS_CENTRELINE}


def test_an_open_attempts_points_keep_the_origin_their_events_carry(tmp_path, replay_db, example_cfg):
    """The collector's events carry the origin of the position they report since the live merge, so the drawer draws
    adsb.lol's points as adsb.lol's before the close; an event written before that has no key and is the receiver's."""
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        aid = w.open("aaa001", "UAL1", along_nm=6.0, hat_ft=1900, origin="local")
        w.update(aid, "aaa001", dt=5, along_nm=4.5, hat_ft=1400, origin="adsb.lol")
        w.update(aid, "aaa001", dt=10, along_nm=4.0, hat_ft=1300)
        w.update(aid, "aaa001", dt=15, along_nm=3.5, hat_ft=1200, origin="adsb.lol")
        with snapshot(db) as conn:
            d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    finally:
        w.close()
    assert d["in_progress"] is True
    assert [(p["along_nm"], p["origin"]) for p in d["points"]] == [
        (6.0, "local"), (4.5, "adsb.lol"), (4.0, "local"), (3.5, "adsb.lol")]


def test_an_in_progress_attempts_reference_is_the_runways_configuration(tmp_path, replay_db, example_cfg):
    """live_points (an in-progress attempt's own points) carries no gp_dev_ft at all, so nothing can be refit -- and
    today's configuration is the one it is being scored against right now anyway, so the fallback is also correct,
    not just available."""
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        aid = w.open("aaa001", "UAL1", along_nm=6.0, hat_ft=1900)
        w.update(aid, "aaa001", dt=5, along_nm=4.5, hat_ft=1400)
        with snapshot(db) as conn:
            d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    finally:
        w.close()
    rwy = example_cfg.airports["KSFO"].runways["28L"]
    assert d["in_progress"] is True
    assert d["reference"] == {"glide_path_deg": rwy.glide_path_deg, "tch_ft": rwy.tch_ft,
                              "threshold_elev_ft": rwy.threshold_elev_ft, "ft_per_nm": NM_FT,
                              "reference_source": rwy.reference_source, **NO_PATH, **ILS_CENTRELINE}


def test_an_unconfigured_runways_closed_approach_still_derives_its_reference_from_its_points(tmp_path, replay_db, example_cfg):
    """rwy is None only says the runway is not configured *now*; the approach's own points still pin down what it was
    scored against, so a runway later removed from config need not lose its closed approaches' charts entirely."""
    db = copy_db(replay_db, tmp_path / "ghost.sqlite")
    points = line_points(80.0, 3.4, [1.0, 2.0, 3.0, 4.0, 5.0])
    aid = close_approach_with_points(db, points=points, airport="ZZZZ", runway="99X", reference_source="cifp:ghost")
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d["reference"] == {"glide_path_deg": pytest.approx(3.4), "tch_ft": pytest.approx(80.0),
                              "threshold_elev_ft": None, "ft_per_nm": NM_FT, "reference_source": "cifp:ghost", **NO_PATH,
                              "centreline_label": "straight-in"}


def test_an_unconfigured_runway_with_too_few_points_has_no_reference(tmp_path, replay_db, example_cfg):
    """With no rwy to fall back to and too little to refit, reference stays None -- unchanged from before this fix."""
    db = copy_db(replay_db, tmp_path / "ghost-short.sqlite")
    points = line_points(80.0, 3.4, [2.0])
    aid = close_approach_with_points(db, points=points, airport="ZZZZ", runway="99X", reference_source="cifp:ghost")
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d["reference"] is None


def test_an_unclean_fit_falls_back_to_the_runways_configuration(tmp_path, replay_db, example_cfg):
    """Should be impossible: matcher.py opens a new approach on a runway change, so one approach's points are never
    scored against more than one reference. If they disagree anyway, the fit cannot be trusted, so this backs off to
    the runway's configuration the same as too few points do, rather than draw a line that may match neither."""
    db = copy_db(replay_db, tmp_path / "dirty.sqlite")
    rwy = example_cfg.airports["KSFO"].runways["28L"]
    points = (line_points(rwy.tch_ft, rwy.glide_path_deg, [1.0, 3.0, 5.0], t0=NOW - 3600.0)
             + line_points(rwy.tch_ft, rwy.glide_path_deg + 3.0, [1.5, 3.5, 5.5], t0=NOW - 3500.0))
    aid = close_approach_with_points(db, points=points, reference_source="cifp:OLD_CYCLE")
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d["reference"] == {"glide_path_deg": rwy.glide_path_deg, "tch_ft": rwy.tch_ft,
                              "threshold_elev_ft": rwy.threshold_elev_ft, "ft_per_nm": NM_FT,
                              "reference_source": rwy.reference_source, **NO_PATH, **ILS_CENTRELINE}


def qb_points(rwy, references: list[str | None]) -> list[dict]:
    """Points on the 28R glide path, one per entry of `references` (what each was measured against), 4 nm out and in."""
    line = line_points(rwy.tch_ft, rwy.glide_path_deg, [4.0 - i * (3.0 / max(len(references) - 1, 1)) for i in range(len(references))])
    return [{**p, "reference": ref} for p, ref in zip(line, references)]


def without_the_path(cfg):
    """A copy of the configuration as it would be if ksfo.toml dropped the Quiet Bridge path."""
    airport = cfg.airports["KSFO"]
    rwy28r = replace(airport.runways["28R"], paths=())
    return replace(cfg, airports={**cfg.airports, "KSFO": replace(airport, runways={**airport.runways, "28R": rwy28r})})


def test_detail_names_the_path_and_its_source(tmp_path, replay_db, example_cfg):
    db = copy_db(replay_db, tmp_path / "qb.sqlite")
    rwy = example_cfg.airports["KSFO"].runways["28R"]
    [configured] = rwy.paths
    aid = close_approach_with_points(db, points=qb_points(rwy, ["quiet-bridge"] * 4), runway="28R", path="quiet-bridge")
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d["approach"]["path"] == "quiet-bridge"
    assert d["reference"]["path"] == "quiet-bridge" and d["reference"]["path_label"] == "Quiet Bridge"
    assert d["reference"]["centreline_label"] == "ILS"                    # what the path hands over to, as the log names it
    assert d["reference"]["path_source"] == configured.source and configured.source.startswith("SFO VOR R-095")
    assert d["reference"]["glide_path_deg"] == pytest.approx(rwy.glide_path_deg)          # the path shares the runway's glide path
    assert {p["reference"] for p in d["points"]} == {"quiet-bridge"}


def test_each_point_names_the_reference_it_was_measured_against(tmp_path, replay_db, example_cfg):
    """After a switch the approach's points name the reference they were measured against, which is where the drawer
    puts its switch markers; the glide path is the runway's whichever they are."""
    db = copy_db(replay_db, tmp_path / "qb-ils.sqlite")
    rwy = example_cfg.airports["KSFO"].runways["28R"]
    aid = close_approach_with_points(db, points=qb_points(rwy, ["quiet-bridge", "quiet-bridge", None, None]), runway="28R",
                                     path="quiet-bridge")
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert [p["reference"] for p in d["points"]] == ["quiet-bridge", "quiet-bridge", None, None]
    assert d["reference"]["path"] == "quiet-bridge"                       # approaches.path is the last path flown
    assert d["reference"]["tch_ft"] == pytest.approx(rwy.tch_ft)


def test_detail_with_an_unconfigured_path_still_opens(tmp_path, replay_db, example_cfg):
    """A path renamed or removed from ksfo.toml after the approach was stored: its name is all there is to show."""
    db = copy_db(replay_db, tmp_path / "gone.sqlite")
    rwy = example_cfg.airports["KSFO"].runways["28R"]
    aid = close_approach_with_points(db, points=qb_points(rwy, ["quiet-bridge"] * 4), runway="28R", path="quiet-bridge")
    cfg = without_the_path(example_cfg)
    assert cfg.airports["KSFO"].runways["28R"].paths == () and example_cfg.airports["KSFO"].runways["28R"].paths
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, cfg, TAR1090, NOW)
    assert d is not None and d["approach"]["path"] == "quiet-bridge"
    assert d["reference"]["path"] == "quiet-bridge"
    assert d["reference"]["path_label"] == "quiet-bridge"                    # the stored name is the label...
    assert d["reference"]["path_source"] is None                              # ...and there is no source to give
    assert d["reference"]["centreline_label"] == "ILS"                        # the runway is still configured
    assert d["reference"]["glide_path_deg"] == pytest.approx(rwy.glide_path_deg)


@pytest.mark.parametrize("ident, expected", [("28L", "ILS"), ("10R", "straight-in")])
def test_detail_names_the_centreline_as_the_log_does(tmp_path, replay_db, example_cfg, ident, expected):
    """The log says "ILS" for a runway with an ILS and "straight-in" for one without; the drawer says the same."""
    db = copy_db(replay_db, tmp_path / f"centreline-{ident}.sqlite")
    rwy = example_cfg.airports["KSFO"].runways[ident]
    assert bool(rwy.ils) == (expected == "ILS")
    points = line_points(rwy.tch_ft, rwy.glide_path_deg, [1.0, 2.0, 3.0, 4.0])
    aid = close_approach_with_points(db, points=points, runway=ident)
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d["reference"]["centreline_label"] == expected
    assert d["reference"]["path"] is None


def test_an_open_path_attempts_detail_names_its_path(tmp_path, replay_db, example_cfg):
    """An open attempt's points come from its events, which carry no reference: no markers until it closes. The path it
    is on is the row's, written when it opened or switched."""
    db = copy_db(replay_db, tmp_path / "open-qb.sqlite")
    w = LiveWriter(db)
    try:
        aid = w.open("aaa001", "VIR19Z", runway="28R", along_nm=6.0, hat_ft=1900)
        w.store.set_path(aid, "quiet-bridge")
        w.update(aid, "aaa001", dt=5, along_nm=4.5, hat_ft=1400, runway="28R")
        with snapshot(db) as conn:
            d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    finally:
        w.close()
    assert d["in_progress"] is True and d["reference"]["path"] == "quiet-bridge"
    assert d["reference"]["path_label"] == "Quiet Bridge"
    assert d["points"] and all(p.get("reference") is None for p in d["points"])


def test_a_database_before_migration_8_still_opens_a_detail(tmp_path, replay_db, example_cfg):
    """The web process can be deployed before the collector restarts and migrates: no approaches.path and no
    approach_points.reference. The drawer opens, with no path and every point on the centreline."""
    db = before_migration_8(copy_db(replay_db, tmp_path / "old.sqlite"))
    aid = ual2177_go_around(db)
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d is not None and d["approach"]["path"] is None and d["reference"]["path"] is None
    assert d["reference"]["path_label"] is None and d["reference"]["path_source"] is None
    assert len(d["points"]) > 10 and {p["reference"] for p in d["points"]} == {None}


def test_an_unknown_approach_is_none(replay_db, example_cfg):
    with snapshot(replay_db) as conn:
        assert approach_detail(conn, 10**9, example_cfg, TAR1090, NOW) is None


@pytest.mark.parametrize("configured, page_host, expected", [
    ("http://127.0.0.1/tar1090/", "192.0.2.7", "http://192.0.2.7/tar1090/"),
    ("http://localhost:8080/tracks/", "receiver.example", "http://receiver.example/tracks/"),
    ("http://[::1]/tar1090/", "2001:db8::7", "http://[2001:db8::7]/tar1090/"),
    ("http://receiver.example/tar1090/", "192.0.2.7", "http://receiver.example/tar1090/"),
    ("http://127.0.0.1/tar1090/", None, "http://127.0.0.1/tar1090/"),
])
def test_tar1090_is_on_the_pages_own_host_unless_configured_elsewhere(configured, page_host, expected):
    assert tar1090_base(configured, page_host) == expected


def test_a_trace_window_past_midnight_stops_at_the_end_of_the_day():
    # established 23:58:00Z on 2026-09-12, ended 00:03:00Z on the 13th
    link = tar1090_link("http://receiver.example/tar1090/", "abc123", 1789257480.0, 1789257780.0)
    assert link == "http://receiver.example/tar1090/?icao=abc123&showTrace=2026-09-12&startTime=23:48:00&endTime=23:59:59"


def test_a_trace_window_that_starts_the_day_before_shows_the_approachs_own_day():
    # established 00:05:00Z on 2026-09-13, ended 00:12:00Z: only the 10 minutes before it fall on the 12th
    link = tar1090_link("http://receiver.example/tar1090/", "abc123", 1789257900.0, 1789258320.0)
    assert link == "http://receiver.example/tar1090/?icao=abc123&showTrace=2026-09-13&startTime=00:00:00&endTime=00:17:00"


def test_an_open_attempts_detail_has_points_from_its_events_no_gates_and_the_live_link(tmp_path, replay_db, example_cfg):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        aid = w.open("aaa001", "UAL1", along_nm=6.0, hat_ft=1900)
        w.update(aid, "aaa001", dt=5, along_nm=4.5, hat_ft=1400)
        with snapshot(db) as conn:
            d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    finally:
        w.close()
    assert d["in_progress"] is True
    assert d["gates"] == []
    assert [(p["t"], p["along_nm"], p["hat_ft"], p["cross_ft"], p["gs"], p["gs_corr"]) for p in d["points"]] == [
        (w.t0, 6.0, 1900.0, None, None, None), (w.t0 + 5, 4.5, 1400.0, 15, 140.0, 150.0)]
    assert d["tar1090_url"] == TAR1090 + "?icao=aaa001"


def test_an_open_attempts_points_are_read_through_the_events_time_index(tmp_path, replay_db, example_cfg):
    """Every drawer open or follow-up refresh on a live approach asks for its detail, and events grow by every
    approach.updated until they are pruned: the query must search events_t from the attempt's establishment, not
    scan the whole table (tests/test_web_live.py's test_cards_read_the_events_through_their_time_index checks
    live_cards the same way)."""
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        aid = w.open("aaa001", "UAL1", along_nm=6.0, hat_ft=1900)
        w.update(aid, "aaa001", dt=1, along_nm=5.9, hat_ft=1880)
    finally:
        w.close()
    statements: list[str] = []
    with snapshot(db) as conn:
        conn.set_trace_callback(statements.append)
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
        conn.set_trace_callback(None)
        [sql] = [s for s in statements if "FROM events" in s and "approach.updated" in s]
        plan = [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", [0] * sql.count("?"))]
    assert len(d["points"]) == 2
    assert not any(step.startswith("SCAN events") for step in plan), plan


def test_a_later_attempts_trace_starts_at_the_arrivals_first_attempt(replay_db, example_cfg):
    """UAL2177's second attempt (the "lost on final 28L" after its go-around) shows the missed approach and the
    circuit back to final: its trace starts 10 minutes before the first attempt's own establishment, not its own."""
    aid = ual2177_second_attempt(replay_db)
    with snapshot(replay_db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d["tar1090_url"] == TAR1090 + "?icao=a44540&showTrace=2026-09-12&startTime=15:14:25&endTime=15:44:38"


def test_the_first_attempts_trace_is_unchanged_by_the_later_attempt_rule(replay_db, example_cfg):
    aid = ual2177_go_around(replay_db)
    with snapshot(replay_db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d["tar1090_url"] == TAR1090 + "?icao=a44540&showTrace=2026-09-12&startTime=15:14:25&endTime=15:31:18"


def test_tar1090_link_can_keep_to_a_later_day():
    # established 23:50:00Z on 2026-10-22, ended 00:20:00Z on the 23rd; a flight search keeps to the 23rd, so the
    # 10 minutes before it that fall on the 22nd start at midnight and the end is the real one
    established = datetime(2026, 10, 22, 23, 50, tzinfo=timezone.utc).timestamp()
    link = tar1090_link("http://receiver.example/tar1090/", "abc123", established, established + 1800.0,
                        day=date(2026, 10, 23))
    assert link == "http://receiver.example/tar1090/?icao=abc123&showTrace=2026-10-23&startTime=00:00:00&endTime=00:25:00"


def test_a_later_attempts_trace_clips_to_midnight_when_the_arrivals_first_attempt_was_the_day_before():
    # this attempt established 00:05:00Z on 2026-09-13 (as in the single-attempt midnight test above); its arrival's
    # first attempt was established an hour earlier, on 2026-09-12
    link = tar1090_link("http://receiver.example/tar1090/", "abc123", 1789257900.0, 1789258320.0,
                        first_established_at=1789254300.0)
    assert link == "http://receiver.example/tar1090/?icao=abc123&showTrace=2026-09-13&startTime=00:00:00&endTime=00:17:00"


def test_after_close_attempt_the_detail_has_the_stored_trace_and_in_progress_is_false(tmp_path, replay_db, example_cfg):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        aid = w.open("aaa001", "UAL1", along_nm=6.0, hat_ft=1900)
        w.close_attempt(aid, "aaa001", Outcome.LANDED, dt=9)
        with snapshot(db) as conn:
            d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    finally:
        w.close()
    assert d["in_progress"] is False
    assert d["tar1090_url"].startswith(TAR1090 + "?icao=aaa001&showTrace=")
    assert "startTime=" in d["tar1090_url"] and "endTime=" in d["tar1090_url"]


def test_a_go_arounds_still_latest_attempt_seen_recently_gets_the_live_link(tmp_path, replay_db, example_cfg):
    """After a go-around the aircraft is usually still flying the circuit: while its flight's last_seen is recent,
    the drawer's link should follow it live, the same as an attempt still in progress."""
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        fid = w.flight_row("aaa001", "UAL1", last_seen=w.t0 + 570)      # seen 30 s before "now" (t0 + 600)
        aid = w.open("aaa001", "UAL1", along_nm=1.0, hat_ft=150, flight_id=fid)
        w.close_attempt(aid, "aaa001", Outcome.GO_AROUND, dt=60)
        with snapshot(db) as conn:
            d = approach_detail(conn, aid, example_cfg, TAR1090, w.t0 + 600)
    finally:
        w.close()
    assert d["in_progress"] is False
    assert d["tar1090_url"] == tar1090_live_link(TAR1090, "aaa001") == TAR1090 + "?icao=aaa001"


def test_seen_looks_across_every_flight_row_for_the_hex_not_only_the_attempts_own(tmp_path, replay_db, example_cfg):
    """A collector restart gives the same aircraft a new flight row: the go-around's own attempt still links to the
    pre-restart one, whose last_seen is now frozen, but the aircraft is still in view under the new row. "Seen" is
    the newest last_seen among every flight row for the hex, not only the one the attempt's own flight_id names."""
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        stale_fid = w.flight_row("aaa001", "UAL1", last_seen=w.t0 - 3600)     # the pre-restart row: long frozen
        aid = w.open("aaa001", "UAL1", along_nm=1.0, hat_ft=150, flight_id=stale_fid)
        w.close_attempt(aid, "aaa001", Outcome.GO_AROUND, dt=60)
        w.flight_row("aaa001", "UAL1", last_seen=w.t0 + 590)                  # the post-restart row: seen 10 s ago
        with snapshot(db) as conn:
            d = approach_detail(conn, aid, example_cfg, TAR1090, w.t0 + 600)
    finally:
        w.close()
    assert d["tar1090_url"] == tar1090_live_link(TAR1090, "aaa001") == TAR1090 + "?icao=aaa001"


def test_a_go_around_seen_10_min_ago_gets_a_trace_ending_5_min_after_last_seen(tmp_path, replay_db, example_cfg):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        fid = w.flight_row("aaa001", "UAL1", last_seen=w.t0 + 60)       # last touched shortly after the go-around
        aid = w.open("aaa001", "UAL1", along_nm=1.0, hat_ft=150, flight_id=fid)
        w.close_attempt(aid, "aaa001", Outcome.GO_AROUND, dt=10)
        now = w.t0 + 60 + 600                                          # 10 minutes after last_seen
        with snapshot(db) as conn:
            d = approach_detail(conn, aid, example_cfg, TAR1090, now)
    finally:
        w.close()
    assert d["tar1090_url"] == tar1090_link(TAR1090, "aaa001", w.t0, w.t0 + 60, w.t0)


def test_once_a_second_attempt_exists_the_first_attempts_link_is_the_plain_trace_again(tmp_path, replay_db, example_cfg):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        fid = w.flight_row("aaa001", "UAL1", last_seen=w.t0 + 590)      # would qualify for the live link on its own
        aid = w.open("aaa001", "UAL1", along_nm=1.0, hat_ft=150, flight_id=fid)
        w.close_attempt(aid, "aaa001", Outcome.GO_AROUND, dt=60)
        w.open("aaa001", "UAL1", along_nm=6.0, hat_ft=1900, dt=70, arrival_id=aid, attempt_no=2,
              prev_approach_id=aid, flight_id=fid)
        with snapshot(db) as conn:
            d = approach_detail(conn, aid, example_cfg, TAR1090, w.t0 + 600)
    finally:
        w.close()
    assert d["tar1090_url"] == tar1090_link(TAR1090, "aaa001", w.t0, w.t0 + 60, w.t0)


def test_a_landed_attempt_ignores_a_recent_last_seen(tmp_path, replay_db, example_cfg):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        fid = w.flight_row("aaa001", "UAL1", last_seen=w.t0 + 595)      # seen 5 s ago: would qualify if this were a go-around
        aid = w.open("aaa001", "UAL1", along_nm=1.0, hat_ft=150, flight_id=fid)
        w.close_attempt(aid, "aaa001", Outcome.LANDED, dt=60)
        with snapshot(db) as conn:
            d = approach_detail(conn, aid, example_cfg, TAR1090, w.t0 + 600)
    finally:
        w.close()
    assert d["tar1090_url"] == tar1090_link(TAR1090, "aaa001", w.t0, w.t0 + 60, w.t0)


def test_a_last_seen_late_in_the_day_clips_to_the_end_of_day(tmp_path, replay_db, example_cfg):
    t0 = 1789257300.0    # 2026-09-12 23:55:00Z
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db, t0=t0)
    try:
        fid = w.flight_row("aaa001", "UAL1", last_seen=t0 + 240)        # 23:59:00Z: +5 min crosses into the next day
        aid = w.open("aaa001", "UAL1", along_nm=1.0, hat_ft=150, flight_id=fid)
        w.close_attempt(aid, "aaa001", Outcome.GO_AROUND, dt=60)        # ended 23:56:00Z, well before last_seen
        now = t0 + 240 + 130                                            # 130 s after last_seen: past the live window
        with snapshot(db) as conn:
            d = approach_detail(conn, aid, example_cfg, TAR1090, now)
    finally:
        w.close()
    assert d["tar1090_url"] == tar1090_link(TAR1090, "aaa001", t0, t0 + 240, t0)
    assert d["tar1090_url"].endswith("endTime=23:59:59")


# --- external_globe_url: the drawer's second link, adsb.lol's own globe over the same timeframe as tar1090_url -----
# example_cfg.external.globe_url is "https://globe.adsb.lol/", the shipped default, unless a test overrides it below.
GLOBE = "https://globe.adsb.lol/"


def test_a_closed_approachs_globe_link_is_the_tar1090_link_with_only_the_base_changed(replay_db, example_cfg):
    aid = ual2177_go_around(replay_db)
    with snapshot(replay_db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d["tar1090_url"] == TAR1090 + "?icao=a44540&showTrace=2026-09-12&startTime=15:14:25&endTime=15:31:18"
    assert d["external_globe_url"] == GLOBE + "?icao=a44540&showTrace=2026-09-12&startTime=15:14:25&endTime=15:31:18"
    assert d["external_globe_url"] == d["tar1090_url"].replace(TAR1090, GLOBE)


def test_an_in_progress_attempts_globe_link_is_also_the_live_view(tmp_path, replay_db, example_cfg):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        aid = w.open("aaa001", "UAL1", along_nm=6.0, hat_ft=1900)
        with snapshot(db) as conn:
            d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    finally:
        w.close()
    assert d["in_progress"] is True
    assert d["tar1090_url"] == TAR1090 + "?icao=aaa001"
    assert d["external_globe_url"] == GLOBE + "?icao=aaa001"


def test_a_go_arounds_globe_link_is_live_too_while_recently_seen(tmp_path, replay_db, example_cfg):
    """Mirrors test_a_go_arounds_still_latest_attempt_seen_recently_gets_the_live_link: the live-vs-trace choice is
    made once, so both links follow it together."""
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        fid = w.flight_row("aaa001", "UAL1", last_seen=w.t0 + 570)      # seen 30 s before "now" (t0 + 600)
        aid = w.open("aaa001", "UAL1", along_nm=1.0, hat_ft=150, flight_id=fid)
        w.close_attempt(aid, "aaa001", Outcome.GO_AROUND, dt=60)
        with snapshot(db) as conn:
            d = approach_detail(conn, aid, example_cfg, TAR1090, w.t0 + 600)
    finally:
        w.close()
    assert d["tar1090_url"] == TAR1090 + "?icao=aaa001"
    assert d["external_globe_url"] == GLOBE + "?icao=aaa001"


def test_a_go_arounds_globe_link_gets_the_same_extended_end_once_no_longer_seen(tmp_path, replay_db, example_cfg):
    """Mirrors test_a_go_around_seen_10_min_ago_gets_a_trace_ending_5_min_after_last_seen: past LIVE_SEEN_WITHIN_S both
    links fall back to the timed trace, and its end is extended to last_seen the same way for each -- the one decision
    (render, in approach_detail) is applied to both bases, so they cannot drift apart."""
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    # A fixed midday start: a trace link ends at 23:59:59 of its first day, so a t0 within a minute of midnight UTC
    # would clamp the attempt's own end and last_seen to the same time and hide the extension this test is about.
    w = LiveWriter(db, t0=1790856000.0)                                  # 2026-10-01 12:00:00Z
    try:
        fid = w.flight_row("aaa001", "UAL1", last_seen=w.t0 + 60)       # last touched shortly after the go-around
        aid = w.open("aaa001", "UAL1", along_nm=1.0, hat_ft=150, flight_id=fid)
        w.close_attempt(aid, "aaa001", Outcome.GO_AROUND, dt=10)
        now = w.t0 + 60 + 600                                          # 10 minutes after last_seen
        with snapshot(db) as conn:
            d = approach_detail(conn, aid, example_cfg, TAR1090, now)
    finally:
        w.close()
    assert d["tar1090_url"] == tar1090_link(TAR1090, "aaa001", w.t0, w.t0 + 60, w.t0)
    assert d["external_globe_url"] == tar1090_link(GLOBE, "aaa001", w.t0, w.t0 + 60, w.t0)
    # Both moved past the attempt's own end (dt=10) to last_seen (w.t0 + 60): proof the extension, not just the base,
    # carried over to the second link.
    unextended_end = tar1090_link(TAR1090, "aaa001", w.t0, w.t0 + 10, w.t0).split("endTime=")[1]
    tar1090_end, globe_end = d["tar1090_url"].split("endTime=")[1], d["external_globe_url"].split("endTime=")[1]
    assert tar1090_end == globe_end != unextended_end


def test_a_later_attempts_globe_link_also_starts_at_the_arrivals_first_attempt(replay_db, example_cfg):
    """Mirrors test_a_later_attempts_trace_starts_at_the_arrivals_first_attempt."""
    aid = ual2177_second_attempt(replay_db)
    with snapshot(replay_db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d["tar1090_url"] == TAR1090 + "?icao=a44540&showTrace=2026-09-12&startTime=15:14:25&endTime=15:44:38"
    assert d["external_globe_url"] == GLOBE + "?icao=a44540&showTrace=2026-09-12&startTime=15:14:25&endTime=15:44:38"


def test_an_empty_globe_url_turns_the_second_link_off_and_leaves_tar1090_url_alone(replay_db, example_cfg):
    off_cfg = replace(example_cfg, external=replace(example_cfg.external, globe_url=""))
    aid = ual2177_go_around(replay_db)
    with snapshot(replay_db) as conn:
        on = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
        off = approach_detail(conn, aid, off_cfg, TAR1090, NOW)
    assert on["external_globe_url"] == GLOBE + "?icao=a44540&showTrace=2026-09-12&startTime=15:14:25&endTime=15:31:18"
    assert off["external_globe_url"] is None
    assert off["tar1090_url"] == on["tar1090_url"] == TAR1090 + "?icao=a44540&showTrace=2026-09-12&startTime=15:14:25&endTime=15:31:18"


def test_stored_points_say_they_are_local_and_an_approach_without_a_lookup_has_no_external(replay_db, example_cfg):
    with snapshot(replay_db) as conn:
        d = approach_detail(conn, ual2177_second_attempt(replay_db), example_cfg, TAR1090, NOW)
    assert d["points"] and {p["origin"] for p in d["points"]} == {"local"} and d["external"] is None


def test_an_open_attempts_points_are_local(tmp_path, replay_db, example_cfg):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        aid = w.open("aaa001", "UAL1", along_nm=6.0, hat_ft=1900)
        with snapshot(db) as conn:
            d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    finally:
        w.close()
    assert [p["origin"] for p in d["points"]] == ["local"]


def test_a_spliced_approach_shows_its_adsb_lol_points_and_its_lookup(tmp_path, replay_db, example_cfg):
    from adsb_approach.model import Flags
    from adsb_approach.store.db import Store
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    aid = ual2177_second_attempt(db)
    store = Store(db)
    try:
        row, last = store.approach(aid), store.points(aid)[-1]
        point = {**dict(last), "t": last["t"] + 5.0, "along_nm": last["along_nm"] - 0.2, "origin": "adsb.lol"}
        extend_approach(store, aid, outcome=Outcome(row["outcome"]), flags=Flags(row["flags"]) | Flags.EXTERNAL_TAIL,
                        landed_confirmed_by=row["landed_confirmed_by"], summary=None, gates=None, points=[point],
                        ga=None)
        schedule_lookup(store, aid, row["hex"], last["t"] + 180.0, row["outcome"])
        seam = dict(overlap_n=8, offset_s=0.6, offset_spread_s=0.1, offset_ft=-12.0, offset_spread_ft=3.0)
        record_lookup(store, aid, status="done", due_at=last["t"] + 180.0, attempts=1, fetched_at=last["t"] + 181.0,
                      http_status=200, error=None, points_added=1, note="tail", seam=seam)
    finally:
        store.close()
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert [p["origin"] for p in d["points"][-2:]] == ["local", "adsb.lol"]
    assert d["external"] == {"status": "done", "points_added": 1, "due_at": last["t"] + 180.0, "http_status": 200,
                             "error": None, "overlap_n": 8, "offset_s": 0.6, "offset_spread_s": 0.1, "offset_ft": -12.0,
                             "offset_spread_ft": 3.0, "match_n": None, "match_offset_s": None,
                             "applied_offset_s": None, "offset_source": None, "gaps_filled": 0,
                             "gap_points_added": 0} and "EXTERNAL_TAIL" in d["approach"]["flags"]


def test_a_gap_filled_approach_shows_its_offset_and_what_it_filled(tmp_path, replay_db, example_cfg):
    from adsb_approach.model import Flags
    from adsb_approach.store.db import Store
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    aid = ual2177_second_attempt(db)
    store = Store(db)
    try:
        row, points = store.approach(aid), store.points(aid)
        between = {**dict(points[0]), "t": (points[0]["t"] + points[1]["t"]) / 2, "origin": "adsb.lol"}
        extend_approach(store, aid, outcome=Outcome(row["outcome"]), flags=Flags(row["flags"]) | Flags.EXTERNAL_GAPS,
                        landed_confirmed_by=row["landed_confirmed_by"], summary=None, gates=None, points=[between],
                        ga=None)
        schedule_lookup(store, aid, row["hex"], row["ended_at"] + 180.0, row["outcome"])
        offset = dict(match_n=14, match_offset_s=0.099, match_spread_s=0.012, applied_offset_s=0.099,
                      offset_source="matches")
        record_lookup(store, aid, status="done", due_at=row["ended_at"] + 180.0, attempts=1,
                      fetched_at=row["ended_at"] + 181.0, http_status=200, error=None, note="gaps only",
                      offset=offset, gaps_filled=1, gap_points_added=1)
    finally:
        store.close()
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert [p["origin"] for p in d["points"][:3]] == ["local", "adsb.lol", "local"]
    assert d["external"] == {"status": "done", "points_added": 0, "due_at": row["ended_at"] + 180.0,
                             "http_status": 200, "error": None, "overlap_n": None, "offset_s": None,
                             "offset_spread_s": None, "offset_ft": None, "offset_spread_ft": None, "match_n": 14,
                             "match_offset_s": 0.099, "applied_offset_s": 0.099, "offset_source": "matches",
                             "gaps_filled": 1, "gap_points_added": 1}
    assert "EXTERNAL_GAPS" in d["approach"]["flags"] and "EXTERNAL_TAIL" not in d["approach"]["flags"]


def test_the_detail_names_the_norm_carries_its_band_and_gives_the_gates_their_deltas(tmp_path, replay_db, example_cfg):
    from adsb_approach.model import Flags
    from adsb_approach.store.db import Store
    from tests.fleetdb import stats_row
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    aid = ual2177_second_attempt(db)
    store = Store(db)
    try:
        row = store.approach(aid)
        store.replace_norms([stats_row(airport=row["airport"], runway=row["runway"], key_kind="class", key="airline",
                                       bin_nm=b, n=n, key_n=142) for b, n in ((1.0, 12), (1.5, 40))],
                            [], set(), built_at=100.0)
        store.set_fleet(aid, flags=Flags(row["flags"]) | Flags.FAST, fleet_key="class:airline")
        store.set_gate_deltas(aid, [(1000, 6.0, -3.0)])
    finally:
        store.close()
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d["fleet"] == {"key_kind": "class", "key": "airline", "n": 142,   # the key's own count, not either bin's
                          "bin_width_nm": example_cfg.fleet.bin_nm,
                          "bins": [{"bin_nm": 1.0, "n": 12, "gp_p25": -10.0, "gp_p50": 10.0, "gp_p75": 30.0,
                                    "gs_p25": 137.0, "gs_p50": 142.0, "gs_p75": 147.0},
                                   {"bin_nm": 1.5, "n": 40, "gp_p25": -10.0, "gp_p50": 10.0, "gp_p75": 30.0,
                                    "gs_p25": 137.0, "gs_p50": 142.0, "gs_p75": 147.0}]}
    gates = {g["gate_ft"]: g for g in d["gates"]}
    assert (gates[1000]["fleet_gp_delta_ft"], gates[1000]["fleet_gs_delta_kt"]) == (6.0, -3.0)
    assert (gates[500]["fleet_gp_delta_ft"], gates[500]["fleet_gs_delta_kt"]) == (None, None)
    assert "FAST" in d["approach"]["flags"] and d["approach"]["fleet_key"] == "class:airline"


def test_an_approach_with_no_norm_or_one_the_last_rebuild_dropped_has_no_band(tmp_path, replay_db, example_cfg):
    from adsb_approach.store.db import Store
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    aid = ual2177_second_attempt(db)
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d["fleet"] is None and d["approach"]["fleet_key"] is None       # scored before the first rebuild
    assert d["gates"][0]["fleet_gs_delta_kt"] is None
    store = Store(db)
    try:
        store.set_fleet(aid, flags=0, fleet_key="type:B39M")
    finally:
        store.close()
    with snapshot(db) as conn:
        d = approach_detail(conn, aid, example_cfg, TAR1090, NOW)
    assert d["fleet"] is None                                             # a key the latest rebuild no longer writes

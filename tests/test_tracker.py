from dataclasses import replace

from adsb_approach.config import TypeClasses
from adsb_approach.model import Flags
from adsb_approach.tracker import Tracker
from adsb_approach.typedb import TypeDb
from tests.conftest import ROOT, T0, make_sfo_28r, synthetic_track


def make_tracker(**kw) -> Tracker:
    return Tracker(TypeDb.load(ROOT / "tests" / "fixtures" / "typedb-sample.csv"),
                   TypeClasses.load(ROOT / "config" / "type_classes.toml"), **kw)


def test_new_flight_resolves_type_and_class():
    tr = make_tracker()
    pos = synthetic_track(make_sfo_28r(), callsign="UAL2177")[0]
    track = tr.update(pos)
    f = track.flight
    assert (f.hex, f.callsign, f.reg, f.type_code, f.traffic_class, f.approach_cat) == ("a44540", "UAL2177", "N37415", "B39M", "airline", "C")
    assert f.first_seen == pos.t and f.last_seen == pos.t and track.last is pos


def test_callsign_fills_in_and_reclassifies():
    created, updated = [], []
    tr = make_tracker(on_new_flight=lambda f: created.append(f) or 7, on_flight_update=lambda f: updated.append(f))
    pts = synthetic_track(make_sfo_28r(), callsign=None, start_nm=8, end_nm=7.9)
    first = tr.update(pts[0])
    assert first.flight.traffic_class == "other" and first.flight.id == 7
    second = tr.update(synthetic_track(make_sfo_28r(), callsign="UAL2177", start_nm=7.8, end_nm=7.7, t0=T0 + 10)[0])
    assert second.flight is first.flight and second.flight.callsign == "UAL2177" and second.flight.traffic_class == "airline"
    assert len(created) == 1 and updated == [first.flight]


def test_callsign_change_and_gap_start_new_sessions():
    tr = make_tracker()
    a = tr.update(synthetic_track(make_sfo_28r(), callsign="UAL2177", start_nm=8, end_nm=7.9)[0])
    b = tr.update(synthetic_track(make_sfo_28r(), callsign="UAL2178", start_nm=7.8, end_nm=7.7, t0=T0 + 60)[0])
    assert b.flight is not a.flight and b.flight.callsign == "UAL2178"
    c = tr.update(synthetic_track(make_sfo_28r(), callsign="UAL2178", start_nm=7.6, end_nm=7.5, t0=T0 + 60 + 3 * 3600)[0])
    assert c.flight is not b.flight and c.flight.first_seen == T0 + 60 + 3 * 3600


def test_history_is_pruned_and_expiry():
    tr = make_tracker(history_s=100.0)
    pts = synthetic_track(make_sfo_28r(), start_nm=8, end_nm=2, step_s=10.0)   # ~2.4 min of points
    for p in pts:
        track = tr.update(p)
    assert track.history[0].t >= track.last.t - 100.0 and len(track.history) == 11
    assert tr.get("a44540") is track
    assert tr.expire(track.last.t + 7201) == ["a44540"] and tr.get("a44540") is None


def test_unknown_hex_uses_position_fields():
    tr = make_tracker()
    p = synthetic_track(make_sfo_28r(), hexid="ffffff", callsign="N01KE")[0]
    p.reg, p.type_code = "N01KE", "C56X"
    f = tr.update(p).flight
    assert (f.reg, f.type_code, f.traffic_class) == ("N01KE", "C56X", "bizjet")


def test_session_change_persists_the_finished_flight():
    updated = []
    tr = make_tracker(on_new_flight=lambda f: 1, on_flight_update=lambda f: updated.append((f.callsign, f.last_seen)))
    first = synthetic_track(make_sfo_28r(), callsign="UAL2177", start_nm=8, end_nm=7.9)
    for p in first:
        tr.update(p)
    tr.update(synthetic_track(make_sfo_28r(), callsign="UAL2178", start_nm=7.8, end_nm=7.7, t0=first[-1].t + 60)[0])
    assert updated == [("UAL2177", first[-1].t)]


def test_expire_persists_last_seen():
    updated = []
    tr = make_tracker(on_flight_update=lambda f: updated.append(f.last_seen))
    pts = synthetic_track(make_sfo_28r(), start_nm=8, end_nm=7.5)
    for p in pts:
        tr.update(p)
    assert tr.expire(pts[-1].t + 7201) == ["a44540"] and updated == [pts[-1].t]


def test_close_all_persists_every_live_flight():
    updated = []
    tr = make_tracker(on_flight_update=lambda f: updated.append((f.hex, f.last_seen)))
    a = synthetic_track(make_sfo_28r(), hexid="a44540", start_nm=8, end_nm=7.9)
    b = synthetic_track(make_sfo_28r(), hexid="ac2706", callsign="UAL2432", start_nm=6, end_nm=5.9, t0=T0 + 5)
    for p in a + b:
        tr.update(p)
    tr.close_all()
    assert sorted(updated) == sorted([("a44540", a[-1].t), ("ac2706", b[-1].t)]) and tr.tracks() == []


def test_expire_keeps_a_track_that_can_still_continue_its_session():
    tr = make_tracker()
    a = synthetic_track(make_sfo_28r(), start_nm=8, end_nm=7.9)
    for p in a:
        tr.update(p)
    assert tr.expire(a[-1].t + 3700) == []
    back = tr.update(synthetic_track(make_sfo_28r(), start_nm=7.8, end_nm=7.7, t0=a[-1].t + 3700)[0])
    assert back.flight.first_seen == a[0].t and back.flight.last_seen == a[-1].t + 3700
    assert tr.expire(back.last.t + 7201) == ["a44540"]


def test_history_survives_a_session_change():
    tr = make_tracker()
    a = synthetic_track(make_sfo_28r(), callsign="UAL2177", start_nm=8, end_nm=7.9)
    for p in a:
        tr.update(p)
    b = synthetic_track(make_sfo_28r(), callsign="UAL2178", start_nm=7.8, end_nm=7.7, t0=a[-1].t + 10)[0]
    track = tr.update(b)
    assert track.flight.callsign == "UAL2178" and list(track.history) == a + [b]


def test_update_feeds_positions_to_the_flights_emergency_watch():
    tr = make_tracker()
    pts = [replace(p, squawk="7700") for p in synthetic_track(make_sfo_28r(), start_nm=8, end_nm=6.5)]  # 36 s, 1 s apart
    assert pts[-1].t - pts[0].t >= 30.0
    track = None
    for p in pts:
        track = tr.update(p)
    assert Flags.EMERGENCY_GENERAL in track.flight.emergency.latched


def test_a_new_session_after_the_gap_gets_a_fresh_emergency_watch():
    tr = make_tracker()
    pts = [replace(p, squawk="7700") for p in synthetic_track(make_sfo_28r(), start_nm=8, end_nm=6.5)]
    for p in pts:
        track = tr.update(p)
    assert Flags.EMERGENCY_GENERAL in track.flight.emergency.latched
    after_gap = synthetic_track(make_sfo_28r(), start_nm=8, end_nm=7.9, t0=pts[-1].t + 7201)  # past session_gap_s
    new_track = tr.update(after_gap[0])
    assert new_track.flight is not track.flight
    assert new_track.flight.emergency.latched == Flags.NONE


def test_expire_releases_idle_history_but_keeps_the_session():
    tr = make_tracker()
    pts = synthetic_track(make_sfo_28r(), start_nm=8, end_nm=7.5)
    for p in pts:
        tr.update(p)
    assert tr.expire(pts[-1].t + 1000) == [] and len(tr.get("a44540").history) == 0
    back = tr.update(synthetic_track(make_sfo_28r(), start_nm=7.4, end_nm=7.3, t0=pts[-1].t + 1000)[0])
    assert back.flight.first_seen == pts[0].t and list(back.history) == [back.last]

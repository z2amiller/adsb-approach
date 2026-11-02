import math

import pytest

from adsb_approach.altitude import h_isa_ft
from adsb_approach.config import StabilityCfg
from adsb_approach.model import Flags, Weather
from adsb_approach.reference import Reference
from adsb_approach.scorer import Gate, Scorer, key_may_flag, speed_flags
from tests.conftest import QUIET_BRIDGE, T0, make_ksfo, make_sfo_28r, path_track, synthetic_track

ISA_WX = Weather("KSFO", T0 - 600, 1013.25, 15.0, 5.0, 298, 20, None, "METAR KSFO test")   # standard QNH, ISA temp, 20 kt headwind


def scorer() -> Scorer:
    return Scorer(StabilityCfg(), temp_max_age_s=5400.0, qnh_max_age_s=10800.0, gates_ft=[1000, 500], gate_max_bracket_s=30.0,
                  bin_nm=0.5, bin_max_nm=8.0)


def score_all(rwy, points, wx=ISA_WX):
    sc, apt = scorer(), make_ksfo()
    return sc, [sc.score(rwy, apt, p, wx) for p in points]


def test_on_path_point_scores_near_zero():
    rwy = make_sfo_28r()
    pos = synthetic_track(rwy, start_nm=3.0, end_nm=2.99)[0]
    sc, [sp] = score_all(rwy, [pos])
    assert sp.along_nm == pytest.approx(3.0, abs=0.002) and abs(sp.cross_ft) < 3
    assert sp.alt_qnh == pytest.approx(1023.3, abs=0.6) and sp.temp_corrected is True
    # a non-standard altimeter setting is undone when the METAR carries the same QNH
    hi = synthetic_track(rwy, start_nm=3.0, end_nm=2.99, qnh_hpa=1015.2)[0]
    assert hi.alt_baro == 970
    sc, [sh] = score_all(rwy, [hi], Weather("KSFO", T0 - 600, 1015.2, 15.0, 5.0, None, None, None, ""))
    assert sh.alt_qnh == pytest.approx(1023.3, abs=0.6) and sh.gp_dev_ft == pytest.approx(0.0, abs=1.0)
    assert sp.gp_dev_ft == pytest.approx(0.0, abs=1.0) and sp.gp_dev_deg == pytest.approx(0.0, abs=0.005)
    assert sp.loc_dev_ft == pytest.approx(0.0, abs=3) and sp.gs_corr == pytest.approx(170.0)      # 20 kt headwind
    assert sp.sink_fpm == pytest.approx(796, abs=2) and sp.nav_modes == frozenset({"approach"})


def test_warm_day_raises_corrected_altitude():
    rwy = make_sfo_28r()
    pos = synthetic_track(rwy, start_nm=3.0, end_nm=2.99)[0]
    warm = Weather("KSFO", T0 - 600, 1013.25, 21.0, 12.0, None, None, None, "")
    sc, [sp] = score_all(rwy, [pos], warm)
    assert sp.gp_dev_ft == pytest.approx(21.0, abs=2.0)
    stale = Weather("KSFO", T0 - 7200, 1013.25, 21.0, 12.0, None, None, None, "")
    sc, [sp2] = score_all(rwy, [pos], stale)
    assert sp2.temp_corrected is False and sp2.gp_dev_ft == pytest.approx(0.0, abs=1.0)
    sc, [sp3] = score_all(rwy, [pos], None)
    assert sp3.gs_corr == 150.0 and sp3.temp_corrected is False           # no weather: no wind, no temperature


def test_standard_pressure_without_a_usable_altimeter_setting_is_flagged():
    rwy = make_sfo_28r()
    pos = synthetic_track(rwy, start_nm=3.0, end_nm=2.99, qnh_hpa=1020.0)[0]          # altimeters set to 1020 hPa
    sc, [ok] = score_all(rwy, [pos], Weather("KSFO", T0 - 600, 1020.0, 15.0, 5.0, None, None, None, ""))
    assert ok.qnh_assumed is False and ok.gp_dev_ft == pytest.approx(0.0, abs=1.0)
    for wx in (Weather("KSFO", T0 - 4 * 3600, 1020.0, 15.0, 5.0, None, None, None, ""),       # older than 3 h
               Weather("KSFO", T0 - 600, None, 15.0, 5.0, None, None, None, ""),              # no altimeter setting
               None):
        sc, [sp] = score_all(rwy, [pos], wx)
        assert sp.qnh_assumed is True and sp.gp_dev_ft == pytest.approx(h_isa_ft(1020.0), abs=1.5)   # about -185 ft
    ground = synthetic_track(rwy, start_nm=0.1, end_nm=0.09)[0]
    ground.on_ground, ground.alt_baro = True, None
    assert score_all(rwy, [ground], None)[1][0].qnh_assumed is False            # no altitude was corrected
    pts = score_all(rwy, synthetic_track(rwy, start_nm=8, end_nm=0.5), None)[1]
    assert Flags.QNH_ASSUMED in sc.flags(rwy, sc.gates(rwy, pts), sc.summarize(pts), established_hat_ft=None,
                                         sidestep=False, temp_uncorrected=False, qnh_assumed=True)


def test_high_and_off_course_geometry():
    rwy = make_sfo_28r()
    high = synthetic_track(rwy, start_nm=3.0, end_nm=2.99, offset_ft=100)[0]
    sc, [sp] = score_all(rwy, [high])
    assert sp.gp_dev_ft == pytest.approx(100, abs=1.5)
    assert sp.gp_dev_deg == pytest.approx(0.2965, abs=0.01) and sc.gp_dots(sp.gp_dev_deg) == pytest.approx(0.847, abs=0.03)
    right = synthetic_track(rwy, start_nm=3.0, end_nm=2.99, cross_ft=400)[0]
    sc, [sr] = score_all(rwy, [right])
    assert sr.loc_dev_ft == pytest.approx(400, abs=5) and sr.loc_dev_deg == pytest.approx(0.754, abs=0.02)
    assert sc.loc_dots(rwy, sr.loc_dev_deg) == pytest.approx(0.917, abs=0.03)


def test_ground_point():
    rwy = make_sfo_28r()
    p = synthetic_track(rwy, start_nm=0.1, end_nm=0.09)[0]
    p.on_ground, p.alt_baro = True, None
    sc, [sp] = score_all(rwy, [p])
    assert sp.on_ground and sp.hat_ft == 0.0 and sp.alt_corr is None and sp.gp_dev_ft is None


def test_gates_interpolated_and_unobserved_on_gap():
    rwy = make_sfo_28r()
    sc, pts = score_all(rwy, synthetic_track(rwy, start_nm=8, end_nm=0.5, step_s=1.0), None)
    g1000, g500 = sc.gates(rwy, pts)
    assert g1000.observed and g1000.along_nm == pytest.approx(2.968, abs=0.02) and g1000.gp_dev_ft == pytest.approx(0, abs=1.5)
    assert g500.observed and g500.along_nm == pytest.approx(1.397, abs=0.02)
    assert g1000.gs == 150.0 and g1000.ias_est == pytest.approx(147.8, abs=0.5) and g1000.sink_fpm == pytest.approx(796, abs=2)
    assert g1000.nav_modes == "approach" and g1000.t == pytest.approx(T0 + (8 - 2.968) / (150 / 3600), abs=1.0)
    t500 = g500.t
    gapped = [p for p in pts if not (t500 - 20 <= p.t <= t500 + 20)]
    g1000b, g500b = sc.gates(rwy, gapped)
    assert g1000b.observed and not g500b.observed and g500b.t is None


def test_summary_of_clean_approach():
    rwy = make_sfo_28r()
    sc, pts = score_all(rwy, synthetic_track(rwy, start_nm=8, end_nm=0.5, step_s=1.0), None)
    s = sc.summarize(pts)
    assert s.points == 181 and s.duration_s == 180.0
    assert s.descent_angle_deg == pytest.approx(3.0, abs=0.03) and s.gs_at_4nm == 150.0 and s.gs_at_2nm == 150.0
    assert s.first_along_nm == pytest.approx(8.0, abs=0.01) and s.closest_along_nm == pytest.approx(0.5, abs=0.01)
    assert 210 < s.lowest_hat_ft < 230 and abs(s.gp_dev_mean_ft) < 1 and s.gp_dev_max_abs_ft < 2 and s.loc_dev_rms_ft < 3
    assert s.sink_max_fpm == pytest.approx(796, abs=2)


def test_speed_at_a_distance_needs_points_as_close_as_a_gate_does():
    rwy = make_sfo_28r()
    sc, pts = score_all(rwy, synthetic_track(rwy, start_nm=8, end_nm=0.5, step_s=1.0), None)
    gapped = [p for p in pts if not 3.0 < p.along_nm < 5.0]          # 2 nm without positions: 48 s at 150 kt
    s = sc.summarize(gapped)
    assert s.gs_at_4nm is None and s.gs_at_2nm == 150.0


def test_flags_from_gates_and_summary():
    rwy = make_sfo_28r()
    sc, pts = score_all(rwy, synthetic_track(rwy, start_nm=8, end_nm=0.5, step_s=1.0), None)
    gates, summ = sc.gates(rwy, pts), sc.summarize(pts)
    assert sc.flags(rwy, gates, summ, established_hat_ft=2400, sidestep=False, temp_uncorrected=False,
                    qnh_assumed=False) == Flags.NONE
    sc2, high = score_all(rwy, synthetic_track(rwy, start_nm=8, end_nm=0.5, offset_ft=200), None)
    f = sc2.flags(rwy, sc2.gates(rwy, high), sc2.summarize(high), established_hat_ft=800, sidestep=True,
                 temp_uncorrected=True, qnh_assumed=False)
    assert Flags.HIGH in f and Flags.UNSTABLE_1000 in f and Flags.UNSTABLE_500 in f and Flags.LOW not in f
    assert Flags.LATE_ESTABLISHED in f and Flags.SIDESTEP in f and Flags.TEMP_UNCORRECTED in f and Flags.STEEP not in f
    sc3, low = score_all(rwy, synthetic_track(rwy, start_nm=8, end_nm=0.5, offset_ft=-150), None)
    assert Flags.LOW in sc3.flags(rwy, sc3.gates(rwy, low), sc3.summarize(low), established_hat_ft=None, sidestep=False,
                                  temp_uncorrected=False, qnh_assumed=False)
    g1000, g500 = gates
    g1000.fleet_gs_delta_kt, g1000.sink_fpm = 20.0, 1100.0
    g500.observed = False
    # fleet_key must be a type or class key for FAST to fire (a deliberate rule); see test_flags_fast_and_slow_
    # need_a_type_or_class_key below for the * and None cases this guards against.
    f2 = sc.flags(rwy, [g1000, g500], summ, established_hat_ft=None, sidestep=False, temp_uncorrected=False,
                  qnh_assumed=False, fleet_key="type:B738")
    assert Flags.FAST in f2 and Flags.HIGH_SINK in f2 and Flags.GATE500_UNOBSERVED in f2 and Flags.UNSTABLE_1000 in f2
    assumed = make_sfo_28r(reference_source="ourairports+assumed")
    assert Flags.REFERENCE_ASSUMED in sc.flags(assumed, [g1000, g500], summ, established_hat_ft=None, sidestep=False,
                                               temp_uncorrected=False, qnh_assumed=False)


def test_speed_flags_is_the_one_place_the_two_thresholds_are_read():
    """A live close and reflag both decide FAST and SLOW through scorer.speed_flags, so the rule cannot drift between
    them: the same gate and config give the same flags whichever caller asks."""
    cfg = StabilityCfg()
    fast = Gate(gate_ft=1000, observed=True, fleet_gs_delta_kt=cfg.fast_delta_kt + 0.1)
    slow = Gate(gate_ft=500, observed=True, fleet_gs_delta_kt=cfg.slow_delta_kt - 0.1)
    assert speed_flags(fast, cfg, may_flag=True) == Flags.FAST
    assert speed_flags(slow, cfg, may_flag=True) == Flags.SLOW
    assert speed_flags(fast, cfg, may_flag=False) == Flags.NONE          # the all-aircraft norm flags nothing
    assert speed_flags(Gate(gate_ft=1000, observed=True), cfg, may_flag=True) == Flags.NONE      # no delta, no flag
    on_the_line = Gate(gate_ft=1000, observed=True, fleet_gs_delta_kt=cfg.fast_delta_kt)
    assert speed_flags(on_the_line, cfg, may_flag=True) == Flags.NONE    # the threshold itself is not past it


def test_flags_fast_and_slow_need_a_type_or_class_key():
    """The all-aircraft norm's spread is about twice a single type's, so only a type or class fleet_key may set FAST or
    SLOW, though the delta itself is measured and stored whatever the key."""
    assert key_may_flag("type:B738") and key_may_flag("class:airline")
    assert not key_may_flag("*") and not key_may_flag(None)
    rwy = make_sfo_28r()
    sc, pts = score_all(rwy, synthetic_track(rwy, start_nm=8, end_nm=0.5, step_s=1.0), None)
    gates, summ = sc.gates(rwy, pts), sc.summarize(pts)
    g1000, g500 = gates
    g1000.fleet_gs_delta_kt, g500.fleet_gs_delta_kt = 20.0, -20.0
    for fleet_key in (None, "*"):
        f = sc.flags(rwy, gates, summ, established_hat_ft=None, sidestep=False, temp_uncorrected=False,
                     qnh_assumed=False, fleet_key=fleet_key)
        assert Flags.FAST not in f and Flags.SLOW not in f and Flags.UNSTABLE_1000 not in f and Flags.UNSTABLE_500 not in f
    f = sc.flags(rwy, gates, summ, established_hat_ft=None, sidestep=False, temp_uncorrected=False, qnh_assumed=False,
                fleet_key="class:airline")
    assert Flags.FAST in f and Flags.SLOW in f and Flags.UNSTABLE_1000 in f and Flags.UNSTABLE_500 in f


def test_qnh_assumed_is_a_required_keyword():
    # A forgotten qnh_assumed used to default to False silently; it must now be named at every call site.
    rwy = make_sfo_28r()
    sc, pts = score_all(rwy, synthetic_track(rwy, start_nm=8, end_nm=0.5, step_s=1.0), None)
    gates, summ = sc.gates(rwy, pts), sc.summarize(pts)
    with pytest.raises(TypeError):
        sc.flags(rwy, gates, summ, established_hat_ft=None, sidestep=False, temp_uncorrected=False)


def test_bins():
    rwy = make_sfo_28r()
    # start off a bin edge: a point exactly on one lands either side after the lat/lon round trip
    sc, pts = score_all(rwy, synthetic_track(rwy, start_nm=7.98, end_nm=0.5, step_s=1.0), None)
    bins = sc.bins(pts)
    assert [b["bin_nm"] for b in bins] == [0.5 * i for i in range(1, 16)]      # 0.5 … 7.5 (8.0 excluded, <0.5 none)
    three = next(b for b in bins if b["bin_nm"] == 3.0)
    assert 11 <= three["n"] <= 13 and abs(three["gp_dev_mean_ft"]) < 1 and three["gs_corr_mean"] == 150.0


def test_a_scored_point_reads_back_from_its_stored_row(tmp_path):
    from dataclasses import replace

    from adsb_approach.model import Outcome
    from adsb_approach.scorer import ScoredPoint
    from adsb_approach.store.db import Store
    from tests.conftest import ground_point

    rwy = make_sfo_28r()
    apt = make_ksfo(rwy)
    scorer = Scorer(StabilityCfg(), temp_max_age_s=5400.0, qnh_max_age_s=10800.0, gates_ft=[1000, 500],
                    gate_max_bracket_s=30.0, bin_nm=0.5, bin_max_nm=8.0)
    wx = Weather("KSFO", T0 - 600, 1015.2, 21.0, 11.0, 280, 12, None, "METAR KSFO")
    corrected = scorer.score(rwy, apt, synthetic_track(rwy, start_nm=3.0, end_nm=3.0)[0], wx)
    standard = replace(scorer.score(rwy, apt, synthetic_track(rwy, start_nm=1.0, end_nm=1.0, t0=T0 + 50)[0], None),
                       origin="adsb.lol")
    ground = replace(scorer.score(rwy, apt, ground_point(rwy, along_nm=-0.4, t=T0 + 90), wx), origin="adsb.lol")
    assert (corrected.qnh_assumed, standard.qnh_assumed, standard.temp_corrected, ground.on_ground) == (False, True, False, True)
    store = Store(tmp_path / "points.sqlite")
    try:
        store.migrate()
        aid = store.open_approach(run_id=store.new_run("replay", "test"), flight_id=None, arrival_id=None, attempt_no=1,
                                  hex="a44540", callsign=None, type_code=None, traffic_class="other", approach_cat=None,
                                  airport="KSFO", runway="28R", established_at=T0, first_along_nm=3.0, weather_id=None,
                                  reference_source="cifp:test", prev_approach_id=None)
        store.close_approach(aid, outcome=Outcome.LANDED, flags=Flags.NONE, ended_at=T0 + 90, summary={}, gates=[],
                             points=[p.as_row() for p in (corrected, standard, ground)], bins=[], ga=None)
        back = [ScoredPoint.from_row(row) for row in store.points(aid)]
    finally:
        store.close()
    assert [(p.origin, p.source) for p in back] == [("local", "adsb_icao"), ("adsb.lol", "adsb_icao"), ("adsb.lol", "adsb_icao")]
    for original, stored in zip((corrected, standard, ground), back):
        assert stored.along_ft == pytest.approx(original.along_ft)
        assert replace(stored, along_ft=original.along_ft, track_dev_deg=original.track_dev_deg) == original   # track_dev_deg is not stored


# --- scoring against a reference (procedure paths) ------------------------------------------------------------------

def quiet_bridge_runway():
    return make_sfo_28r(paths=(QUIET_BRIDGE,))


def test_centreline_scoring_is_unchanged():
    rwy = make_sfo_28r()
    sc, apt = scorer(), make_ksfo(rwy)
    for offset in (0.0, 4.0):
        for pos in synthetic_track(rwy, track_offset_deg=offset):
            plain, with_ref = sc.score(rwy, apt, pos, ISA_WX), sc.score(rwy, apt, pos, ISA_WX, ref=Reference(rwy))
            assert plain.as_row() == with_ref.as_row() and plain.reference is None and with_ref.reference is None
            assert plain.track_dev_deg == pytest.approx(offset, abs=1e-9)
            assert with_ref.track_dev_deg == plain.track_dev_deg


def test_path_scoring_measures_from_the_path():
    rwy = quiet_bridge_runway()
    ref = Reference(rwy, QUIET_BRIDGE)
    sc, apt = scorer(), make_ksfo(rwy)
    pos = path_track(ref, start_dtg_nm=12.0, end_dtg_nm=11.9, cross_ft=500.0)[0]
    sp = sc.score(rwy, apt, pos, ISA_WX, ref=ref)
    frame = ref.frame(pos.lat, pos.lon)
    assert sp.cross_ft == pytest.approx(500.0, abs=1.0) and sp.loc_dev_ft == sp.cross_ft
    assert sp.along_ft == frame.along_ft and sp.along_nm == pytest.approx(12.0, abs=0.001)
    assert sp.reference == "quiet-bridge" and sp.track_dev_deg == pytest.approx(0.0, abs=1e-6)
    assert (sp.lat, sp.lon, sp.track) == (pos.lat, pos.lon, pos.track)                  # observations stay the aircraft's
    assert sp.gp_dev_ft == pytest.approx(0.0, abs=2.0)                                  # on the glide path for its distance to go
    assert sp.loc_dev_deg == pytest.approx(math.degrees(math.atan2(500.0, sc.geom(rwy).loc_to_thr_ft + frame.along_ft)), abs=1e-6)
    centre = sc.score(rwy, apt, pos, ISA_WX)                                            # the same position against the centreline
    assert centre.cross_ft > 8000 and centre.reference is None and abs(centre.track_dev_deg) > 5
    assert sc.score(rwy, apt, pos, ISA_WX, ref=Reference(rwy)).as_row() == centre.as_row()


@pytest.mark.parametrize("wind_dir, headwind", [(112, -20.0), (292, 20.0)], ids=["tailwind-from-112", "headwind-from-292"])
def test_headwind_uses_the_local_course(wind_dir, headwind):
    rwy = quiet_bridge_runway()
    ref = Reference(rwy, QUIET_BRIDGE)
    sc, apt = scorer(), make_ksfo(rwy)
    wx = Weather("KSFO", T0 - 600, 1013.25, 15.0, 5.0, wind_dir, 20, None, "METAR KSFO test")
    pos = path_track(ref, start_dtg_nm=12.0, end_dtg_nm=11.9)[0]                        # on the first leg, 292.0 true
    on_path = sc.score(rwy, apt, pos, wx, ref=ref)
    on_centreline = sc.score(rwy, apt, pos, wx)
    assert on_path.gs_corr - pos.gs == pytest.approx(headwind, abs=1e-3)               # the wind runs down the leg
    assert on_centreline.gs_corr - pos.gs == pytest.approx(headwind * math.cos(math.radians(6.0)), abs=1e-3)
    assert abs(on_centreline.gs_corr - pos.gs) < abs(on_path.gs_corr - pos.gs)


def test_a_reference_of_another_runway_is_refused():
    rwy, other = make_sfo_28r(), make_sfo_28r(ident="28L")
    with pytest.raises(AssertionError):
        scorer().score(rwy, make_ksfo(rwy, other), synthetic_track(rwy)[0], ISA_WX, ref=Reference(other))


def test_a_scored_point_carries_its_position_s_origin():
    from dataclasses import replace
    rwy = make_sfo_28r()
    pos = synthetic_track(rwy, start_nm=3.0, end_nm=2.99)[0]
    _, [local] = score_all(rwy, [pos])
    _, [polled] = score_all(rwy, [replace(pos, origin="adsb.lol")])
    assert local.origin == "local"
    assert polled.origin == "adsb.lol" and polled.as_row()["origin"] == "adsb.lol"

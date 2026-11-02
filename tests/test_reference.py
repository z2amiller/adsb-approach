"""Reference: a runway's centreline or one of its procedure paths, and the frame a position has against it."""
import math
from dataclasses import replace

import pytest

from adsb_approach.geometry import NM_FT, destination, runway_frame, track_diff_deg
from adsb_approach.reference import Frame, Reference, reference_for, reference_labels, references, validate_path
from tests.conftest import QUIET_BRIDGE, make_ksfo, make_sfo_28r, path_track


def rwy_with_path(**over):
    return make_sfo_28r(paths=(QUIET_BRIDGE,), **over)


def at_enu(rwy, e: float, n: float) -> tuple[float, float]:
    """The (lat, lon) at east/north feet from the threshold, the inverse of geometry.enu_ft."""
    return destination(rwy.threshold_lat, rwy.threshold_lon, math.degrees(math.atan2(e, n)) % 360.0, math.hypot(e, n))


def on_leg(ref, i: int, along_leg_ft: float, right_ft: float = 0.0) -> tuple[float, float]:
    """The (lat, lon) `along_leg_ft` from leg i's start, `right_ft` to the right of its direction of flight."""
    leg = ref.legs[i]
    (ae, an), (te, tn) = leg.a, leg.t
    return at_enu(ref.runway, ae + along_leg_ft * te + right_ft * tn, an + along_leg_ft * tn - right_ft * te)


def test_centreline_frame_is_runway_frame():
    r = make_sfo_28r()
    ref = Reference(r)
    for dist_nm, side_ft in ((5.0, 0.0), (2.3, 400.0), (9.0, -1200.0)):
        lat, lon = destination(r.threshold_lat, r.threshold_lon, (r.course_true_deg + 180) % 360, dist_nm * NM_FT)
        lat, lon = destination(lat, lon, (r.course_true_deg + 90) % 360, side_ft)
        along, cross = runway_frame(r, lat, lon)
        f = ref.frame(lat, lon)
        assert isinstance(f, Frame)
        assert f.along_ft == pytest.approx(along, abs=1e-9) and f.cross_ft == pytest.approx(cross, abs=1e-9)
        assert f.course_deg == r.course_true_deg


def test_quiet_bridge_frame_on_and_off_the_path():
    r = rwy_with_path()
    ref = Reference(r, QUIET_BRIDGE)
    assert [round(leg.length / NM_FT, 2) for leg in ref.legs] == [15.08, 3.94, 1.1]
    for lat, lon in QUIET_BRIDGE.points:
        assert abs(ref.frame(lat, lon).cross_ft) < 5
    mid = ref.legs[0].length / 2
    assert ref.frame(*on_leg(ref, 0, mid, right_ft=1000.0)).cross_ft == pytest.approx(1000.0, abs=5)
    assert ref.frame(*on_leg(ref, 0, mid, right_ft=-1000.0)).cross_ft == pytest.approx(-1000.0, abs=5)
    assert ref.frame(*on_leg(ref, 0, mid)).course_deg == pytest.approx(292.0, abs=0.2)
    assert ref.frame(*on_leg(ref, 1, ref.legs[1].length / 2)).course_deg == pytest.approx(290.0, abs=0.2)
    turn = ref.frame(*QUIET_BRIDGE.points[1])
    assert turn.along_ft / NM_FT == pytest.approx((ref.legs[1].length + ref.legs[2].length) / NM_FT, abs=0.001)
    assert turn.along_ft / NM_FT == pytest.approx(5.04, abs=0.01)
    assert ref.frame(*QUIET_BRIDGE.points[2]).along_ft / NM_FT == pytest.approx(1.1, abs=0.001)


def test_first_leg_extends_outwards_and_last_leg_past_the_threshold():
    r = rwy_with_path()
    ref = Reference(r, QUIET_BRIDGE)
    first = ref.frame(*QUIET_BRIDGE.points[0])
    far = ref.frame(*on_leg(ref, 0, -10 * NM_FT))                  # 10 nm before the first vertex, on the first leg's line
    assert abs(far.cross_ft) < 5 and far.course_deg == pytest.approx(first.course_deg, abs=1e-9)
    assert far.along_ft == pytest.approx(first.along_ft + 10 * NM_FT, abs=5)
    lat, lon = destination(r.threshold_lat, r.threshold_lon, r.course_true_deg, 0.5 * NM_FT)     # past the threshold
    past = ref.frame(lat, lon)
    assert past.along_ft == pytest.approx(-0.5 * NM_FT, abs=5) and abs(past.cross_ft) < 5


def corner_point(ref, i: int, distance_ft: float, *, outside: bool) -> tuple[float, float]:
    """The point `distance_ft` from the vertex that ends leg i, on the bisector of the two legs that meet there."""
    (te, tn), (ue, un) = ref.legs[i].t, ref.legs[i + 1].t
    de, dn = (te - ue, tn - un) if outside else (ue - te, un - tn)          # the change of heading points inside the turn
    scale = distance_ft / math.hypot(de, dn)
    ve = ref.legs[i + 1].a[0]
    vn = ref.legs[i + 1].a[1]
    return at_enu(ref.runway, ve + de * scale, vn + dn * scale)


def corner_ref(turn_deg: float) -> Reference:
    """A path with one corner of turn_deg (positive: a left turn) four miles out on the 28R centreline: the first leg
    comes in 8 nm from off the centreline, the second runs down it to the join and the threshold."""
    r = make_sfo_28r()
    back = (r.course_true_deg + 180) % 360
    join = destination(r.threshold_lat, r.threshold_lon, back, 1.1 * NM_FT)
    vertex = destination(r.threshold_lat, r.threshold_lon, back, 4.1 * NM_FT)
    far = destination(*vertex, (back + turn_deg) % 360, 8 * NM_FT)
    return Reference(r, replace(QUIET_BRIDGE, name="corner", points=(far, vertex, join)))


@pytest.mark.parametrize("turn_deg", [40.0, -40.0], ids=["left-40", "right-40"])
def test_outside_a_corner_measures_to_the_vertex(turn_deg):
    ref = corner_ref(turn_deg)
    assert ref.legs[0].course != pytest.approx(ref.legs[1].course, abs=30)
    at_vertex = ref.frame(*ref.path.points[1])
    f = ref.frame(*corner_point(ref, 0, 2000.0, outside=True))       # 2,000 ft from the vertex, outside the turn
    assert f.cross_ft == pytest.approx(math.copysign(2000.0, turn_deg), abs=1.0)      # right of a left turn, left of a right
    assert f.along_ft == pytest.approx(at_vertex.along_ft, abs=1.0)                  # the vertex's distance to go


@pytest.mark.parametrize("turn_deg", [40.0, -40.0], ids=["left-40", "right-40"])
def test_the_frame_is_continuous_flying_past_the_outside_of_a_corner(turn_deg):
    """Flown 2,000 ft outside the second leg, from before the vertex to after it, every 100 ft: the frame moves by about
    a step, not by the 1,000 ft or so that measuring to the earlier leg's extended line made it jump."""
    ref = corner_ref(turn_deg)
    ve, vn = ref.legs[1].a
    (te, tn), side = ref.legs[1].t, (1.0 if turn_deg > 0 else -1.0)              # the outside of a left turn is on the right
    frames = [ref.frame(*at_enu(ref.runway, ve + 2000.0 * side * tn + s * te, vn - 2000.0 * side * te + s * tn))
              for s in range(-6000, 6001, 100)]
    for a, b in zip(frames, frames[1:]):
        assert abs(b.along_ft - a.along_ft) < 150.0 and abs(b.cross_ft - a.cross_ft) < 150.0


@pytest.mark.parametrize("outside", [True, False])
def test_a_corner_tie_takes_the_earlier_leg(outside):
    r = rwy_with_path()
    ref = Reference(r, QUIET_BRIDGE)
    lat, lon = corner_point(ref, 0, 2000.0, outside=outside)               # equidistant from both legs
    courses = {ref.frame(lat, lon).course_deg for _ in range(20)}
    assert courses == {ref.legs[0].course}
    vertex = ref.frame(*QUIET_BRIDGE.points[1])                              # and so is the vertex itself
    assert vertex.course_deg == ref.legs[0].course


def test_a_leg_far_off_the_runway_course():
    r = make_sfo_28r()
    join = destination(r.threshold_lat, r.threshold_lon, (r.course_true_deg + 180) % 360, 1.1 * NM_FT)
    far = destination(*join, (r.course_true_deg + 180 + 40) % 360, 8 * NM_FT)      # flown towards the join, 40 deg to the right
    ref = Reference(r, replace(QUIET_BRIDGE, points=(far, join)))
    f = ref.frame(*on_leg(ref, 0, ref.legs[0].length / 2, right_ft=300.0))
    assert f.course_deg == ref.legs[0].course
    assert track_diff_deg(f.course_deg, r.course_true_deg) == pytest.approx(40.0, abs=0.5)
    assert f.cross_ft == pytest.approx(300.0, abs=5)


def test_labels():
    assert Reference(make_sfo_28r()).label == "ILS"
    assert Reference(make_sfo_28r(ils=None)).label == "straight-in"
    r = rwy_with_path()
    centre, bridge = Reference(r), Reference(r, QUIET_BRIDGE)
    assert bridge.label == "Quiet Bridge"
    assert (centre.key, bridge.key) == ("KSFO/28R", "KSFO/28R@quiet-bridge")
    assert (centre.name, bridge.name) == (None, "quiet-bridge")
    assert centre == Reference(r) and centre != bridge and len({centre, Reference(r), bridge}) == 2


def test_a_reference_is_immutable():
    ref = Reference(rwy_with_path(), QUIET_BRIDGE)
    with pytest.raises(AttributeError):
        ref.path = None
    with pytest.raises(AttributeError):
        ref.extra = 1


def test_references_lists_the_centreline_then_the_paths_in_file_order():
    second = replace(QUIET_BRIDGE, name="second", label="Second")
    r = make_sfo_28r(paths=(QUIET_BRIDGE, second))
    assert [x.key for x in references(r)] == ["KSFO/28R", "KSFO/28R@quiet-bridge", "KSFO/28R@second"]
    assert [x.key for x in references(make_sfo_28r())] == ["KSFO/28R"]
    assert all(x.runway is r for x in references(r))


def test_reference_for_unknown_name_is_none():
    r = rwy_with_path()
    assert reference_for(r, "rnp-y") is None
    assert reference_for(r, None) == Reference(r)
    assert reference_for(r, "quiet-bridge") == Reference(r, QUIET_BRIDGE)


def test_reference_labels_are_keyed_by_airport_runway_and_path():
    r = rwy_with_path()
    labels = reference_labels({"KSFO": make_ksfo(r, make_sfo_28r(ident="28L", ils=None))})
    assert labels == {("KSFO", "28R", None): "ILS", ("KSFO", "28R", "quiet-bridge"): "Quiet Bridge",
                      ("KSFO", "28L", None): "straight-in"}


def test_validate_path_names_the_rule():
    r = make_sfo_28r()
    validate_path(r, QUIET_BRIDGE)
    with pytest.raises(ValueError, match="at least 2 points"):
        validate_path(r, replace(QUIET_BRIDGE, points=QUIET_BRIDGE.points[:1]))
    with pytest.raises(ValueError, match="name"):
        validate_path(r, replace(QUIET_BRIDGE, name="Quiet Bridge"))


def test_path_track_flies_the_legs_at_the_asked_distance_to_go():
    ref = Reference(rwy_with_path(), QUIET_BRIDGE)
    track = path_track(ref, start_dtg_nm=22.0, end_dtg_nm=-0.2, gs_kt=180.0)         # from before the first vertex to past the threshold
    assert track[0].t < track[1].t and {p.hex for p in track} == {"407699"} and all(p.alt_baro > 0 for p in track[:-60])
    step_ft = 180.0 * NM_FT / 3600.0
    for i, p in enumerate(track):
        f = ref.frame(p.lat, p.lon)
        assert f.along_ft == pytest.approx(22.0 * NM_FT - i * step_ft, abs=1.0) and abs(f.cross_ft) < 1.0
        assert p.track == f.course_deg
    assert track[0].track == pytest.approx(292.0, abs=0.2) and track[-1].track == ref.legs[-1].course
    assert len(track) == int(22.2 * NM_FT / step_ft + 1e-6) + 1


def test_path_track_keeps_its_offset_to_the_right_of_the_path():
    ref = Reference(rwy_with_path(), QUIET_BRIDGE)
    for cross_ft in (500.0, -300.0):
        for p in path_track(ref, start_dtg_nm=12.0, end_dtg_nm=8.0, cross_ft=cross_ft):
            assert ref.frame(p.lat, p.lon).cross_ft == pytest.approx(cross_ft, abs=1.0)
    centre = Reference(make_sfo_28r())                                            # the centreline: one straight leg
    for p in path_track(centre, start_dtg_nm=6.0, end_dtg_nm=2.0, cross_ft=200.0):
        f = centre.frame(p.lat, p.lon)
        assert f.cross_ft == pytest.approx(200.0, abs=1.0) and p.track == f.course_deg

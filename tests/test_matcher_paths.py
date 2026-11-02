"""The matcher on procedure paths: candidates and establishment per reference, switching reference within a runway, and
handing a path attempt over to the parallel. Every track is synthetic."""
from __future__ import annotations

import math
from dataclasses import replace
from itertools import groupby

import pytest

from adsb_approach.geometry import NM_FT, destination, runway_frame, track_diff_deg
from adsb_approach.matcher import Matcher
from adsb_approach.model import Flags, ProcedurePath
from adsb_approach.poller import WatchList
from adsb_approach.reference import Reference, reference_for
from tests import test_matcher as base
from tests.conftest import (QUIET_BRIDGE, T0, flown_track, ground_point, make_ksfo, make_sfo_28l, make_sfo_28r, path_track,
                            synthetic_track)
from tests.test_matcher import Harness

GS_KT = 180.0                                    # path_track's default ground speed


ORDERS = {"28L-first": ("28L", "28R"), "28R-first": ("28R", "28L")}      # config/airports/ksfo.toml lists 28L first


def sfo_with(*paths: ProcedurePath, order: tuple[str, ...] = ORDERS["28L-first"]):
    """KSFO whose 28R carries `paths`, its runways in `order` (the matcher visits them in that order), and its 28R runway."""
    r28r = make_sfo_28r(paths=paths)
    runways = {"28L": make_sfo_28l(), "28R": r28r}
    return make_ksfo(*(runways[ident] for ident in order)), r28r


@pytest.fixture(params=list(ORDERS))
def order(request) -> tuple[str, ...]:
    """The order the airport lists its runways in: where a pass of the matcher meets 28L relative to 28R decides which of
    two attempts has taken a position when the other is looked at. Every test below runs in both."""
    return ORDERS[request.param]


@pytest.fixture
def h(tmp_path, order):
    """The plain airport: what today's matcher sees."""
    (tmp_path / "plain").mkdir()
    return Harness(tmp_path / "plain", airport=sfo_with(order=order)[0])


@pytest.fixture
def hq(tmp_path, order):
    """The same airport with the Quiet Bridge path on 28R."""
    (tmp_path / "paths").mkdir()
    return Harness(tmp_path / "paths", airport=sfo_with(QUIET_BRIDGE, order=order)[0])


@pytest.fixture
def qb(hq) -> Reference:
    ref = reference_for(hq.cfg.airports["KSFO"].runways["28R"], "quiet-bridge")
    assert ref is not None
    return ref


def dtg_at(t: float, start_dtg_nm: float, t0: float = T0, gs_kt: float = GS_KT) -> float:
    """Distance still to fly at time t for a path_track that starts at start_dtg_nm at t0."""
    return start_dtg_nm - (t - t0) * gs_kt / 3600.0


def finish(h: Harness, positions) -> list:
    """Feed `positions`, let the matcher's lost timeout pass, and return the rows."""
    h.feed(positions)
    h.matcher.tick(positions[-1].t + 46)
    return h.rows()


def gate(h: Harness, row, gate_ft: int):
    return next(g for g in h.store.gates(row["id"]) if g["gate_ft"] == gate_ft)


def payloads(h: Harness, kind: str) -> list[dict]:
    return [p for k, p in h.events if k == kind]


def listen_for(h: Harness, kind: str) -> list[float]:
    """The times at which the matcher publishes `kind`."""
    times: list[float] = []
    h.matcher.events.subscribe(lambda k, t, aid, fid, payload: times.append(t) if k == kind else None)
    return times


# ------------------------------------------------------------------ candidates and establishment per reference
def test_quiet_bridge_flight_establishes_on_the_path(hq, qb):
    pts = path_track(qb, start_dtg_nm=16, end_dtg_nm=2.2)
    [row] = finish(hq, pts)
    assert (row["runway"], row["path"], row["outcome"]) == ("28R", "quiet-bridge", "lost_on_final")
    assert row["first_along_nm"] > 11
    assert 9.5 < dtg_at(row["established_at"], 16) < 11        # the candidate opens at 12 nm, and 30 s at 180 kt is 1.5 nm
    g1000 = gate(hq, row, 1000)
    assert g1000["observed"] == 1 and abs(g1000["loc_dev_ft"]) < 50
    bad = Flags.OFF_COURSE | Flags.UNSTABLE_1000 | Flags.LATE_ESTABLISHED
    assert not Flags(row["flags"]) & bad
    [started] = payloads(hq, "approach.started")
    assert (started["path"], started["path_label"]) == ("quiet-bridge", "Quiet Bridge")


def test_ils_flight_is_unchanged(h, hq):
    pts = synthetic_track(make_sfo_28r(), start_nm=12, end_nm=2.5)
    [plain], [with_paths] = finish(h, pts), finish(hq, pts)
    for column in ("established_at", "first_along_nm", "flags", "outcome", "runway"):
        assert with_paths[column] == plain[column], column
    assert with_paths["path"] is None
    [started] = payloads(hq, "approach.started")
    assert started["path"] is None and started["path_label"] == "ILS"
    assert payloads(hq, "approach.completed")[0]["path"] is None


def test_points_record_their_reference(hq, qb):
    [row] = finish(hq, path_track(qb, start_dtg_nm=16, end_dtg_nm=2.2))
    points = hq.store.points(row["id"])
    assert points and {p["reference"] for p in points} == {"quiet-bridge"}


def test_an_open_path_attempt_already_names_its_path(hq, qb):
    # a reader of the database mid-approach sees the path, not only after the close
    hq.feed(path_track(qb, start_dtg_nm=16, end_dtg_nm=8))
    [row] = hq.rows()
    assert row["outcome"] is None and row["path"] == "quiet-bridge"


def zigzag_airport(order):
    """28R with a path whose first leg runs 40 deg off the runway course (258), then 12 deg off (286), joining the
    centreline 1.5 nm out; the corner lies 9.0 nm out along the path."""
    r28r = make_sfo_28r()
    join = destination(r28r.threshold_lat, r28r.threshold_lon, (r28r.course_true_deg + 180) % 360, 1.5 * NM_FT)
    corner = destination(*join, 286.0 - 180.0, 7.5 * NM_FT)
    far = destination(*corner, 258.0 - 180.0, 8.0 * NM_FT)
    zig = ProcedurePath("zigzag", "Zigzag", "test", (far, corner, join))
    airport, rwy = sfo_with(zig, order=order)
    return airport, rwy, zig


def test_a_zigzag_leg_far_off_the_runway_course_establishes(tmp_path, order):
    airport, rwy, zig = zigzag_airport(order)
    zh = Harness(tmp_path, airport=airport)
    ref = reference_for(rwy, "zigzag")
    assert ref is not None and [round(leg.course) for leg in ref.legs] == [258, 286, 298]
    [row] = finish(zh, path_track(ref, start_dtg_nm=16, end_dtg_nm=2.2))
    assert (row["runway"], row["path"]) == ("28R", "zigzag")
    assert dtg_at(row["established_at"], 16) > 9.0           # established on the first leg, before the corner
    assert row["first_along_nm"] > 11


@pytest.mark.parametrize("step_s", [0.5, 8.0])
def test_crossing_traffic_is_still_rejected_with_paths(hq, step_s):
    base.test_crossing_traffic_is_not_established(hq, step_s)


# ------------------------------------------------------------------ switching reference within a runway
# RNP Y to 28R from CIFP 2609: GUTTS, DONNG, JOSUF and FABLA, which is the join, 2.12 nm out and 2 ft off the centreline.
RNP_Y = ProcedurePath("rnp-y", "RNP Y", "test", (
    (37.546622, -122.155939), (37.586717, -122.251400), (37.592128, -122.285550), (37.597397, -122.318728)))


def early_join_track(qb: Reference, straight_s: float = 52.0) -> list:
    """Quiet Bridge from 16 to 7 nm to go, then a 30 degree turn left that rolls out on the 28R centreline about 5.1 nm
    out (the path lies 4,600 ft right of the centreline at 7 nm), and on down it for straight_s seconds (52: to 2.5 nm)."""
    rwy = qb.runway
    head = path_track(qb, start_dtg_nm=16, end_dtg_nm=7.0)
    last = head[-1]
    along, cross = runway_frame(rwy, last.lat, last.lon)
    step_ft = GS_KT * NM_FT / 3600.0
    turn = flown_track(rwy, start_nm=(along - step_ft) / NM_FT, start_cross_ft=cross, start_offset_deg=last.track - rwy.course_true_deg,
                       step_s=1.0, gs_kt=GS_KT, t0=last.t + 1, hexid=last.hex, callsign=last.callsign,
                       legs=[(10.0, -30.0), (19.0, -30.0), (10.0, 0.0), (straight_s, 0.0)])
    end_along, end_cross = runway_frame(rwy, turn[-1].lat, turn[-1].lon)
    # the scenario is the one described: on the centreline, as far down it as the seconds flown since the roll-out at 5.13 nm
    assert abs(end_cross) < 100 and abs(end_along / NM_FT - (5.13 - straight_s * GS_KT / 3600.0)) < 0.2
    return head + turn


def reference_events(h: Harness) -> list[dict]:
    return payloads(h, "approach.reference")


def test_an_early_join_to_the_localizer_switches_in_one_attempt(hq, qb):
    [row] = finish(hq, early_join_track(qb))
    assert (row["outcome"], row["path"]) == ("lost_on_final", "quiet-bridge")            # the last path flown
    refs = [k for k, _ in groupby(p["reference"] for p in hq.store.points(row["id"]))]
    assert refs == ["quiet-bridge", None]
    [event] = reference_events(hq)
    assert (event["from"], event["to"], event["from_label"], event["to_label"]) == ("quiet-bridge", None, "Quiet Bridge", "ILS")
    assert (event["path"], event["path_label"]) == (None, "ILS")                         # the reference it is on now
    assert 5.0 < event["along_nm"] < 7.0
    assert abs(gate(hq, row, 1000)["loc_dev_ft"]) < 60                                   # measured from the centreline
    assert len(hq.rows()) == 1


def test_overlapping_paths_switch_to_the_one_flown(tmp_path, order):
    airport, rwy = sfo_with(QUIET_BRIDGE, RNP_Y, order=order)
    oh = Harness(tmp_path, airport=airport)
    rnp, qbr = reference_for(rwy, "rnp-y"), reference_for(rwy, "quiet-bridge")
    assert rnp is not None and qbr is not None
    head = path_track(rnp, start_dtg_nm=16, end_dtg_nm=9.0)             # RNP Y's first leg, which Quiet Bridge crosses at 9 nm
    tail = path_track(qbr, start_dtg_nm=8.87, end_dtg_nm=2.2, t0=head[-1].t + 1)
    [row] = finish(oh, head + tail)
    assert (row["runway"], row["outcome"], row["path"]) == ("28R", "lost_on_final", "quiet-bridge")
    [started] = payloads(oh, "approach.started")
    assert started["path"] == "rnp-y" and 9.5 < dtg_at(row["established_at"], 16) < 11
    refs = [k for k, _ in groupby(p["reference"] for p in oh.store.points(row["id"]))]
    assert refs == ["rnp-y", "quiet-bridge"]
    [event] = reference_events(oh)
    assert (event["from"], event["to"], event["from_label"], event["to_label"]) == ("rnp-y", "quiet-bridge", "RNP Y", "Quiet Bridge")


@pytest.mark.parametrize("seconds, switches", [(6, False), (12, True)])
def test_a_switch_needs_ten_seconds_aligned(hq, qb, seconds, switches):
    # a flight on Quiet Bridge 6 nm out, 4,000 ft right of the centreline, is placed on the centreline for a few
    # seconds and back: aligned with it for runway_change_min_s (10 s) is a join, for less it is not
    rwy = qb.runway
    head = path_track(qb, start_dtg_nm=16, end_dtg_nm=6.0)
    last = head[-1]
    on_line = synthetic_track(rwy, start_nm=runway_frame(rwy, last.lat, last.lon)[0] / NM_FT - 0.05,
                              end_nm=runway_frame(rwy, last.lat, last.lon)[0] / NM_FT - 0.05 * seconds, step_s=1.0, gs_kt=GS_KT,
                              hexid=last.hex, callsign=last.callsign, t0=last.t + 1)
    assert len(on_line) == seconds
    back = path_track(qb, start_dtg_nm=6.0 - 0.05 * (seconds + 1), end_dtg_nm=3.0, t0=last.t + seconds + 1)
    finish(hq, head + on_line + back)
    assert bool(reference_events(hq)) is switches
    if switches:
        assert (reference_events(hq)[0]["from"], reference_events(hq)[0]["to"]) == ("quiet-bridge", None)


def test_joining_at_the_join_needs_no_switch(hq, qb):
    [row] = finish(hq, path_track(qb, start_dtg_nm=16, end_dtg_nm=0.5))        # through the join, 1.1 nm out
    assert row["path"] == "quiet-bridge" and reference_events(hq) == []
    assert {p["reference"] for p in hq.store.points(row["id"])} == {"quiet-bridge"}


def scripted_track(qb: Reference, *, head_end_dtg_nm: float, profile: list[tuple[float, float]], frame_rwy=None) -> list:
    """Quiet Bridge down to head_end_dtg_nm, then one position per second placed by hand in the centreline's frame of
    `frame_rwy` (28R): each (cross_ft right of the centreline, track offset from its course in degrees) of `profile`,
    going on down it."""
    rwy = qb.runway if frame_rwy is None else frame_rwy
    head = path_track(qb, start_dtg_nm=16, end_dtg_nm=head_end_dtg_nm)
    last = head[-1]
    along = runway_frame(rwy, last.lat, last.lon)[0] / NM_FT
    out = list(head)
    for k, (cross_ft, offset_deg) in enumerate(profile, start=1):
        at = along - 0.05 * k                                   # 180 kt for a second
        out += synthetic_track(rwy, start_nm=at, end_nm=at, cross_ft=cross_ft, track_offset_deg=offset_deg, gs_kt=GS_KT,
                               hexid=last.hex, callsign=last.callsign, t0=last.t + k)
    return out


APPROACH_TO_LINE = [(2300 - 250 * k, -30.0) for k in range(8)]              # 2300 ... 550 ft right of the centreline


@pytest.mark.parametrize("offset_deg, switches", [(-3.0, True), (-8.0, False)])
def test_crossing_the_centreline_is_not_a_join_unless_aligned(hq, qb, offset_deg, switches):
    # 13 s inside the 300 ft band either way; only the flight heading within 5 degrees of the centreline's course joins it
    rate_ft_s = -GS_KT * NM_FT / 3600.0 * math.sin(math.radians(offset_deg))
    crossing = [(290 - rate_ft_s * k, offset_deg) for k in range(13)]
    finish(hq, scripted_track(qb, head_end_dtg_nm=4.0, profile=APPROACH_TO_LINE + crossing))
    assert bool(reference_events(hq)) is switches


@pytest.mark.parametrize("peak_ft, switches", [(440.0, True), (480.0, False)])
def test_the_run_near_the_centreline_goes_on_to_450_ft(hq, qb, peak_ft, switches):
    # within 300 ft begins the run and within 450 ft continues it, as for a runway change: a flight aligned with the
    # centreline that drifts out to 440 ft is on the run still, and one that drifts past 450 ft starts again
    drift = [(min(290.0 + 40.0 * k, peak_ft), 0.0) for k in range(16)]
    finish(hq, scripted_track(qb, head_end_dtg_nm=4.0, profile=APPROACH_TO_LINE + drift))
    assert bool(reference_events(hq)) is switches


def test_a_switch_rescores_only_the_tail_and_clears_what_it_was_counting(hq, qb):
    hq.feed(path_track(qb, start_dtg_nm=16, end_dtg_nm=8))
    [att] = hq.matcher.live()
    centreline = Reference(qb.runway)
    att.off_course_since, att.switch_near_since[centreline.key] = att.last_t - 5, att.last_t - 4
    att.switch_aligned_since[centreline.key] = att.last_t - 3
    att.min_alt_point = att.points[-2]
    n, since = len(att.points), att.points[-3].t
    hq.matcher._switch_reference(att, centreline, since)
    assert att.reference == centreline and att.key == qb.key                      # filed under the reference it began on
    assert len(att.points) == n and [p.reference for p in att.points[-4:]] == ["quiet-bridge", None, None, None]
    assert att.off_course_since is None and not att.switch_near_since and not att.switch_aligned_since
    assert att.min_alt_point is att.points[-2]                                    # the lowest point is the one re-measured


# ------------------------------------------------------------------ from a path to the parallel
def cross_to_28l(qb: Reference, *, gs_kt: float, head_end_dtg_nm: float, legs: list[tuple[float, float]],
                 path_cross_ft: float = 0.0) -> list:
    """Quiet Bridge (flown path_cross_ft right of it) down to head_end_dtg_nm to go, then a track flown in 28L's frame from
    where it left off, `legs` being flown_track's (seconds, track offset from 28L's course) pairs."""
    rwy_28l = make_sfo_28l()
    head = path_track(qb, start_dtg_nm=16, end_dtg_nm=head_end_dtg_nm, gs_kt=gs_kt, cross_ft=path_cross_ft)
    last = head[-1]
    along, cross = runway_frame(rwy_28l, last.lat, last.lon)
    step_ft = gs_kt * NM_FT / 3600.0
    return head + flown_track(rwy_28l, start_nm=(along - step_ft) / NM_FT, start_cross_ft=cross, step_s=1.0,
                              gs_kt=gs_kt, start_offset_deg=track_diff_deg(last.track, rwy_28l.course_true_deg),
                              t0=last.t + 1, hexid=last.hex, callsign=last.callsign, legs=legs)


def along_where_it_left_the_path(qb: Reference, positions: list, rwy) -> float:
    """The distance to go on `rwy` of the last position within 450 ft of the path before the flight is clear of it."""
    for i, p in enumerate(positions):
        if abs(qb.frame(p.lat, p.lon).cross_ft) > 450.0:
            before = positions[i - 1]
            return runway_frame(rwy, before.lat, before.lon)[0] / NM_FT
    raise AssertionError("the flight never left the path")


def hand_over_rows(h: Harness, positions: list) -> tuple:
    """The two rows of a hand-over from a Quiet Bridge approach to 28L, checked as the chain a runway change makes."""
    rows = finish(h, positions)
    assert [(r["runway"], r["outcome"]) for r in rows][:2] == [("28R", "runway_change"), ("28L", rows[1]["outcome"])]
    first, second = rows[0], rows[1]
    assert first["path"] == "quiet-bridge" and second["path"] is None and len(rows) == 2
    assert Flags.SIDESTEP in Flags(second["flags"]) and second["attempt_no"] == 2
    assert second["arrival_id"] == first["arrival_id"] == first["id"]
    assert (second["prev_approach_id"], first["next_approach_id"]) == (first["id"], second["id"])
    # the crossing is stored once: the path attempt ends at its last position before the parallel's attempt begins, and no
    # point of its own is at or after that one (a position is a second apart here)
    assert first["ended_at"] == second["established_at"] - 1.0
    assert max(p["t"] for p in h.store.points(first["id"])) < min(p["t"] for p in h.store.points(second["id"]))
    return first, second


# 90 kt, a 22 degree turn left from Quiet Bridge at 7 nm that crosses the 28R centreline and settles on a track 7 degrees
# left of 28L's course: it never lines up with 28L within the 5 degrees the runway-change rule asks, and it drifts at
# 18 ft/s, inside the 20 ft/s an established attempt may have. It is nearer 28L than 28R's centreline from 375 ft right of
# 28L, 30 s later it has a window nearer 28L than every reference of 28R, and the 28L candidate establishes.
SLOW_CROSSING = [(8, -22.0), (70, -22.0), (10, -7.0), (50, -7.0), (20, 0.0)]


def test_crossing_to_28l_by_the_candidate_route(hq, qb):
    # The hand-over is the candidate route's: the runway-change rule cannot fire on a track that is never within 5 degrees.
    fired = listen_for(hq, "approach.runway_change")
    pts = cross_to_28l(qb, gs_kt=90.0, head_end_dtg_nm=7.0, legs=SLOW_CROSSING)
    first, second = hand_over_rows(hq, pts)
    rwy_28l = make_sfo_28l()
    where_it_fired = next(p for p in pts if p.t == fired[0])
    assert abs(track_diff_deg(where_it_fired.track,
                              rwy_28l.course_true_deg)) > 5.0             # outside the 10 s rule's alignment
    assert second["first_along_nm"] == pytest.approx(along_where_it_left_the_path(qb, pts, rwy_28l), abs=0.1)
    assert second["first_along_nm"] < 7.5                                                    # not the candidate's 8.2 nm
    assert not Flags(second["flags"]) & Flags.OFF_COURSE                  # the stretch flown on the path is not charged to 28L


def test_the_candidate_route_keeps_the_position_that_triggers_it(hq, qb):
    # the position that completes the 28L candidate's window is the new attempt's, whichever of the two attempts had taken
    # it when the candidate established (with 28L listed first, the path attempt has not yet)
    pts = cross_to_28l(qb, gs_kt=90.0, head_end_dtg_nm=7.0, legs=SLOW_CROSSING)
    rows = finish(hq, pts)
    times = [p["t"] for p in hq.store.points(rows[1]["id"])]
    assert times and all(b - a == 1.0 for a, b in zip(times, times[1:]))
    assert times[-1] == pts[-1].t                                         # and nothing after it either


def test_crossing_to_28l_onto_the_centreline(hq, qb):
    # the same hand-over by the runway-change rule: 180 kt, a 25 degree turn left that rolls out on 28L about 4.6 nm out
    pts = cross_to_28l(qb, gs_kt=GS_KT, head_end_dtg_nm=7.0, legs=[(8, -25.0), (33, -25.0), (8, 0.0), (60, 0.0)])
    _, second = hand_over_rows(hq, pts)
    assert second["first_along_nm"] == pytest.approx(along_where_it_left_the_path(qb, pts, make_sfo_28l()), abs=0.1)
    assert not Flags(second["flags"]) & Flags.OFF_COURSE


def test_a_late_sidestep_by_the_runway_change_rule_starts_at_the_same_point(hq, qb):
    # on the path to 3.5 nm, then 2,800 ft to 28L in 17 s: aligned with it for 10 s long before a candidate has had 30 s
    fired = listen_for(hq, "approach.runway_change")
    pts = cross_to_28l(qb, gs_kt=GS_KT, head_end_dtg_nm=3.5, legs=[(5, -30.0), (12, -30.0), (5, 0.0), (30, 0.0)])
    first, second = hand_over_rows(hq, pts)
    left = along_where_it_left_the_path(qb, pts, make_sfo_28l())
    assert second["first_along_nm"] == pytest.approx(left, abs=0.1) and 3.0 < left < 3.5   # the rule's own run began near 2.6
    where_it_fired = next(p for p in pts if p.t == fired[0])
    assert abs(runway_frame(make_sfo_28l(), where_it_fired.lat, where_it_fired.lon)[1]) < 300.0       # inside the rule's band


def test_an_early_crossing_is_a_runway_change_not_abandoned(hq, qb):
    # Leaving the path at 10 nm with a 35 degree turn, then closing on 28L at 8 degrees, takes the flight more than 1 nm
    # from the path for 15 s before it has been aligned with 28L for 10 s: without the exemption for a flight still
    # aligned with a reference of the group, the path attempt is abandoned.
    pts = cross_to_28l(qb, gs_kt=GS_KT, head_end_dtg_nm=10.0,
                       legs=[(6, -35.0), (36, -35.0), (6, -8.0), (20, -8.0), (8, 0.0), (60, 0.0)])
    assert max(abs(qb.frame(p.lat, p.lon).cross_ft) for p in pts) > 6076.0              # more than 1 nm from the path
    rows = finish(hq, pts)
    assert [(r["runway"], r["outcome"]) for r in rows][:2] == [("28R", "runway_change"), ("28L", rows[1]["outcome"])]
    assert "abandoned" not in {r["outcome"] for r in rows}
    assert Flags.SIDESTEP in Flags(rows[1]["flags"]) and rows[1]["prev_approach_id"] == rows[0]["id"]
    assert rows[0]["ended_at"] == rows[1]["established_at"] - 1.0                      # the crossing is stored once


def settling_on_the_centreline_track(qb: Reference) -> list:
    """Quiet Bridge to 10 nm to go, then a 12 degree convergence on 28R's centreline until it is 1,000 ft off at 5.5 nm, then
    2.5 degrees (13 ft/s, inside the drift an established attempt may have) down to the centreline, which it reaches about
    2.5 nm out. From 5.5 nm on the flight is inside 0.3 nm of 28L too, and nearer 28R's centreline than 28L."""
    rwy = qb.runway
    head = path_track(qb, start_dtg_nm=16, end_dtg_nm=10.0)
    last = head[-1]
    along, cross = runway_frame(rwy, last.lat, last.lon)
    step_ft = GS_KT * NM_FT / 3600.0
    return head + flown_track(rwy, start_nm=(along - step_ft) / NM_FT, start_cross_ft=cross, step_s=1.0, gs_kt=GS_KT,
                              start_offset_deg=track_diff_deg(last.track, rwy.course_true_deg), t0=last.t + 1,
                              hexid=last.hex, callsign=last.callsign,
                              legs=[(5, -12.0), (80, -12.0), (5, -2.5), (50, -2.5), (20, 0.0)])


def test_a_flight_that_settles_on_the_centreline_is_not_taken_by_the_parallel(hq, qb, monkeypatch):
    # Established on Quiet Bridge far out, the flight leaves it for the 28R centreline and is for 30 s inside 0.3 nm of 28L,
    # converging gently, with 28L nearer than the path (2,000 ft and more away) though 28R's centreline is nearer than
    # either. Measured against the path alone the 28L candidate wins; against every reference of 28R it does not, and the
    # flight is 28R's: the switch rule takes it onto the centreline, and nothing is handed over to 28L.
    established = []
    original = Matcher._establish

    def spy(self, att, track, per_hex, wx):
        established.append(att.runway.ident)
        original(self, att, track, per_hex, wx)

    monkeypatch.setattr(Matcher, "_establish", spy)
    rows = finish(hq, settling_on_the_centreline_track(qb))
    assert "28L" in established                                           # the 28L candidate did establish, and was refused
    assert [(r["runway"], r["path"], r["outcome"]) for r in rows] == [("28R", "quiet-bridge", "lost_on_final")]
    assert [(e["from"], e["to"]) for e in reference_events(hq)] == [("quiet-bridge", None)]
    points = hq.store.points(rows[0]["id"])
    assert [k for k, _ in groupby(p["reference"] for p in points)] == ["quiet-bridge", None]
    assert "approach.runway_change" not in hq.kinds()


def test_a_28r_landing_keeps_its_path_attempt(hq, qb, monkeypatch):
    # Past the join the flight is 750 ft from 28L, inside 0.3 nm, and not converging on it, so a 28L candidate establishes
    # once it has 30 s of that: a slow flight (100 kt) has, before the threshold. It is nearer the path at every
    # position, so the path attempt stands and the landing is one 28R row.
    established = []
    original = Matcher._establish

    def spy(self, att, track, per_hex, wx):
        established.append(att.runway.ident)
        original(self, att, track, per_hex, wx)

    monkeypatch.setattr(Matcher, "_establish", spy)
    rows = finish(hq, path_track(qb, start_dtg_nm=16, end_dtg_nm=0.2, gs_kt=100.0))
    assert [(r["runway"], r["path"]) for r in rows] == [("28R", "quiet-bridge")]
    assert "approach.runway_change" not in hq.kinds()
    assert "28L" in established                                            # the 28L candidate did establish, and was refused


def test_left_path_t_is_the_last_position_within_450_ft(hq, qb):
    hq.feed(path_track(qb, start_dtg_nm=16, end_dtg_nm=8))
    [att] = hq.matcher.live()
    assert hq.matcher._left_path_t(att, -1.0) == att.last_t
    for p in att.points[-4:]:
        p.cross_ft = 451.0
    assert hq.matcher._left_path_t(att, -1.0) == att.points[-5].t
    att.points[-5].cross_ft = -450.0
    assert hq.matcher._left_path_t(att, -1.0) == att.points[-5].t           # 450 ft is still on the path
    for p in att.points:
        p.cross_ft = 1000.0
    assert hq.matcher._left_path_t(att, -1.0) == -1.0                       # never on it: the route's own start


SIDESTEPS = [pytest.param(base.test_runway_change_sidestep, (), id="sidestep"),
             pytest.param(base.test_intercept_overshoot_towards_the_parallel_is_not_a_runway_change, (), id="overshoot"),
             pytest.param(base.test_sidestep_at_two_nm_is_a_runway_change_at_live_and_history_rates, (0.5,), id="two-nm-live"),
             pytest.param(base.test_sidestep_at_two_nm_is_a_runway_change_at_live_and_history_rates, (8.0,), id="two-nm-history")]


@pytest.mark.parametrize("scenario, args", SIDESTEPS)
def test_a_centreline_sidestep_is_unchanged(h, hq, scenario, args):
    scenario(h, *args)                                    # the very scenario and assertions that run in test_matcher
    scenario(hq, *args)
    columns = ("runway", "attempt_no", "outcome", "flags", "established_at", "ended_at", "first_along_nm", "arrival_id",
               "prev_approach_id", "next_approach_id", "path")
    assert [tuple(r[c] for c in columns) for r in hq.rows()] == [tuple(r[c] for c in columns) for r in h.rows()]
    assert all(r["path"] is None for r in hq.rows())


def test_turning_away_from_a_path_is_still_abandoned_on_time(hq, qb):
    # a 70 degree turn right at 5 nm: more than 45 degrees from the path, and from the centreline, which is within a nautical
    # mile of the flight for the first seconds: neither reference excuses it, so it is abandoned abandon_min_s later
    rwy = qb.runway
    head = path_track(qb, start_dtg_nm=16, end_dtg_nm=5.0, gs_kt=150.0)
    last = head[-1]
    along, cross = runway_frame(rwy, last.lat, last.lon)
    away = flown_track(rwy, start_nm=along / NM_FT - 0.02, start_cross_ft=cross, start_offset_deg=70.0, legs=[(60.0, 70.0)],
                       gs_kt=150.0, step_s=1.0, t0=last.t + 1, hexid=last.hex, callsign=last.callsign)
    [row] = finish(hq, head + away)
    assert row["outcome"] == "abandoned" and row["path"] == "quiet-bridge"
    hold = hq.cfg.detection.abandon_min_s
    assert last.t + hold <= row["ended_at"] <= last.t + 1 + hold + 2


@pytest.mark.parametrize("outlier_ft, hands_over", [(None, True), (1100.0, False)])
def test_the_parallel_must_be_nearer_at_every_position_to_take_over(hq, qb, outlier_ft, hands_over):
    # The path lies 2,400 ft right of 28L at 3 nm and 1,700 ft at 2.1 nm. A flight 350 ft right of 28L for 30 s there
    # establishes on 28L and hands over, unless one position in the window lies nearer the path than 28L: here 1,100 ft right
    # of 28L, about 700 ft from the path. The median still favours 28L; that one position vetoes the hand-over.
    rwy_28l = make_sfo_28l()
    profile = [(350.0, 0.0)] * 40
    if outlier_ft is not None:
        profile[15] = (outlier_ft, 0.0)
    pts = scripted_track(qb, head_end_dtg_nm=3.0, profile=profile, frame_rwy=rwy_28l)
    rows = finish(hq, pts)
    assert [r["runway"] for r in rows][:1] == ["28R"]
    assert (len(rows) == 2 and rows[0]["outcome"] == "runway_change") is hands_over
    if not hands_over:
        assert "approach.runway_change" not in hq.kinds()


def test_a_centreline_attempt_is_abandoned_off_its_own_centreline(hq):
    # The exemption is for a path attempt crossing to the parallel. A flight established on the 28L centreline that swerves
    # right 6,400 ft (1.05 nm) and flies on parallel to it is still abandoned, though it is within a nautical mile of 28R
    # and aligned with it.
    rwy_28l = make_sfo_28l()
    on_28l = synthetic_track(rwy_28l, start_nm=9, end_nm=6.0, step_s=1.0, gs_kt=GS_KT)
    swerve = flown_track(rwy_28l, start_nm=5.95, legs=[(10, 30.0), (32, 30.0), (10, 0.0), (80, 0.0)], gs_kt=GS_KT,
                         step_s=1.0, t0=on_28l[-1].t + 1, hexid=on_28l[0].hex, callsign=on_28l[0].callsign)
    assert max(runway_frame(rwy_28l, p.lat, p.lon)[1] for p in swerve) > 6076.0
    rows = finish(hq, on_28l + swerve)
    assert [(r["runway"], r["outcome"], r["path"]) for r in rows] == [("28L", "abandoned", None)]


# ------------------------------------------------------------------ a path attempt's parallel candidates
GAP_S = 50.0                      # more than lost_timeout_s (45) and less than a candidate's timeout (60)


def go_quiet(h: Harness, from_t: float, until_t: float) -> None:
    """The matcher's clock passing second by second while no position arrives (the live service ticks every second, so an
    attempt that stays quiet is closed when it has been for lost_timeout_s)."""
    for k in range(1, int(until_t - from_t)):
        h.matcher.tick(from_t + k)


@pytest.mark.parametrize("gs_kt, gap_at_nm, end_nm, gap_s, ticks", [(100.0, 2.0, 0.1, GAP_S, True), (110.0, 2.2, 0.1, GAP_S, True),
                                                                    (100.0, 2.4, 0.05, 65.0, False)])
def test_a_gap_on_final_leaves_no_false_28l_attempt(hq, qb, gs_kt, gap_at_nm, end_nm, gap_s, ticks):
    # A 28L candidate stands beside every Quiet Bridge flight from 8 nm in. The aircraft goes quiet for 50 s: the path attempt
    # is closed as lost after 45 s, but the candidate, which times out after 60 s, would still be there, and its old window
    # with the new position in it (a gap loosens the drift bound) would establish it with nothing to hold it back. After
    # 65 s without a tick (a feed-wide gap: the first position's own tick sees it all at once) both are past their
    # timeouts in one pass, the attempt first, and the candidate it took with it must not be timed out a second time.
    head = path_track(qb, start_dtg_nm=16, end_dtg_nm=gap_at_nm, gs_kt=gs_kt)
    tail = path_track(qb, start_dtg_nm=gap_at_nm - gap_s * gs_kt / 3600.0, end_dtg_nm=end_nm, gs_kt=gs_kt, t0=head[-1].t + gap_s)
    hq.feed(head)
    assert {a.runway.ident for a in hq.matcher._attempts[head[0].hex].values()} == {"28R", "28L"}      # the premise
    if ticks:
        go_quiet(hq, head[-1].t, tail[0].t)
    hq.feed(tail)
    hq.matcher.tick(tail[-1].t + 46)
    assert [(r["runway"], r["path"]) for r in hq.rows()] == [("28R", "quiet-bridge")]
    assert "approach.runway_change" not in hq.kinds()


def test_closing_a_path_attempt_drops_the_candidates_beside_it(hq, qb):
    pts = path_track(qb, start_dtg_nm=16, end_dtg_nm=4.0)
    hq.feed(pts)
    assert sorted(hq.matcher._attempts[pts[0].hex]) == ["KSFO/28L", "KSFO/28R@quiet-bridge"]
    hq.matcher.tick(pts[-1].t + 46)                                     # the path attempt is lost; the candidate would not be
    assert pts[0].hex not in hq.matcher._attempts


def test_a_hand_over_leaves_the_new_attempt_alone(hq, qb):
    # the candidates the path attempt had beside it go with it, but not the attempt that takes over
    pts = cross_to_28l(qb, gs_kt=GS_KT, head_end_dtg_nm=7.0, legs=[(8, -25.0), (33, -25.0), (8, 0.0), (60, 0.0)])
    hq.feed(pts)
    assert [a.runway.ident for a in hq.matcher.live()] == ["28L"]


def test_switching_to_the_centreline_drops_the_candidates_beside_the_path_attempt(hq, qb):
    pts = early_join_track(qb)
    for i, p in enumerate(pts):
        hq.feed([p])
        if reference_events(hq):
            break
    else:
        raise AssertionError("it never switched")
    assert [(a.runway.ident, a.state.value) for a in hq.matcher._attempts[p.hex].values()] == [("28R", "established")]
    assert hq.matcher.live()[0].reference.path is None                  # on the centreline now, which blocks the parallel's


def test_the_switch_to_the_centreline_then_lost_leaves_no_false_28l_attempt(hq, qb):
    # The 28L candidate that opened while the flight was rolling out on the centreline is the path attempt's to drop when
    # it joins the centreline; if it were not, a gap after the switch would let it establish alone.
    pts = early_join_track(qb, straight_s=90.0)
    for i, p in enumerate(pts):
        hq.feed([p])
        if reference_events(hq):
            break
    quiet_from = pts[i + 12].t
    hq.feed(pts[i + 1:i + 13])                                           # twelve more seconds on the centreline
    resume = [q for q in pts[i + 13:] if q.t >= quiet_from + GAP_S]
    go_quiet(hq, quiet_from, resume[0].t)
    hq.feed(resume)
    hq.matcher.tick(resume[-1].t + 46)
    assert [r["runway"] for r in hq.rows()] == ["28R"] and hq.rows()[0]["path"] == "quiet-bridge"
    assert "approach.runway_change" not in hq.kinds()


# ------------------------------------------------------------------ where the hand-over starts when the path was never near
def test_with_no_point_near_the_path_the_rule_route_starts_at_its_own_near_run(hq, qb):
    # 600 ft left of the path all the way (established on it all the same, being inside 0.3 nm), so no point of the attempt
    # ever lies within 450 ft of it: the hand-over starts where the runway-change rule's run near 28L began, not at the
    # attempt's first position.
    pts = cross_to_28l(qb, gs_kt=GS_KT, head_end_dtg_nm=7.0, legs=[(8, -25.0), (28, -25.0), (8, 0.0), (60, 0.0)],
                       path_cross_ft=-600.0)                                  # 5 s of 25 degrees less: it starts 600 ft nearer
    rwy_28l = make_sfo_28l()
    near = next(p for p in pts if abs(runway_frame(rwy_28l, p.lat, p.lon)[1]) < 300.0)
    first, second = hand_over_rows(hq, pts)
    assert second["first_along_nm"] == pytest.approx(runway_frame(rwy_28l, near.lat, near.lon)[0] / NM_FT, abs=0.1)
    assert second["established_at"] == near.t and second["first_along_nm"] < 6.0       # not the 11 nm of the first point


def test_with_no_point_near_the_path_the_candidate_route_starts_at_its_window(hq, qb):
    # the same flight 600 ft left of the path, handed over by the candidate route: the new attempt starts where the 28L
    # candidate's establishment window began, one window (30 s) before the position that completed it
    fired = listen_for(hq, "approach.runway_change")
    pts = cross_to_28l(qb, gs_kt=90.0, head_end_dtg_nm=7.0, legs=SLOW_CROSSING, path_cross_ft=-600.0)
    first, second = hand_over_rows(hq, pts)
    [t_fired] = fired
    window = hq.cfg.detection.established_window_s
    assert window <= t_fired - second["established_at"] < window + 2.0
    assert second["first_along_nm"] < 6.0                                  # not the 11 nm of the attempt's first position


# ------------------------------------------------------------------ smaller points
def test_a_switch_between_paths_writes_the_new_path_to_the_open_row(tmp_path, order):
    airport, rwy = sfo_with(QUIET_BRIDGE, RNP_Y, order=order)
    oh = Harness(tmp_path, airport=airport)
    rnp, qbr = reference_for(rwy, "rnp-y"), reference_for(rwy, "quiet-bridge")
    head = path_track(rnp, start_dtg_nm=16, end_dtg_nm=9.0)
    oh.feed(head)
    assert [(r["outcome"], r["path"]) for r in oh.rows()] == [(None, "rnp-y")]
    oh.feed(path_track(qbr, start_dtg_nm=8.87, end_dtg_nm=5.0, t0=head[-1].t + 1))
    assert reference_events(oh) and [(r["outcome"], r["path"]) for r in oh.rows()] == [(None, "quiet-bridge")]


def test_a_switch_to_the_centreline_keeps_the_last_path_on_the_open_row(hq, qb):
    for p in early_join_track(qb):
        hq.feed([p])
        if reference_events(hq):
            break
    assert [(r["outcome"], r["path"]) for r in hq.rows()] == [(None, "quiet-bridge")]


def test_a_reference_blocked_for_candidates_is_not_scored_for_them(hq, qb, monkeypatch):
    # once the flight is established on Quiet Bridge, 28R's own centreline can open no candidate: it is scored for the
    # switch rule only, once a position, not a second time for a candidate that would be refused
    pts = path_track(qb, start_dtg_nm=16, end_dtg_nm=6.0)
    hq.feed(pts[:-5])
    scored: list[str] = []
    original = hq.matcher.scorer.score

    def spy(rwy, airport, pos, wx, *, ref=None):
        scored.append(rwy.key if ref is None else ref.key)
        return original(rwy, airport, pos, wx, ref=ref)

    monkeypatch.setattr(hq.matcher.scorer, "score", spy)
    hq.feed(pts[-5:-4])
    assert scored.count("KSFO/28R") == 1 and scored.count("KSFO/28R@quiet-bridge") == 1


# ------------------------------------------------------------------ a hand-over never starts before the path attempt did
def drifting_off_the_path_track(qb: Reference, leave_dtg_nm: float) -> list:
    """Quiet Bridge 400 ft left of it through the establishment window (to 10.5 nm), then 470 to 600 ft left of it, never
    within 450 ft again, down to 7 nm; then 25 degrees across to 28L, rolling out on it about 6 nm out. The only points
    within 450 ft of the path are in the window (those before leave_dtg_nm)."""
    step_nm = GS_KT / 3600.0
    head: list = []
    start = 16.0
    for end, cross in [(leave_dtg_nm, -400.0), (leave_dtg_nm - 0.05, -470.0), (leave_dtg_nm - 0.10, -520.0),
                       (leave_dtg_nm - 0.15, -570.0), (7.0, -600.0)]:
        head += path_track(qb, start_dtg_nm=start if not head else start - step_nm, end_dtg_nm=end, cross_ft=cross,
                           t0=head[-1].t + 1 if head else T0)
        start = end
    rwy_28l = make_sfo_28l()
    last = head[-1]
    along, cross = runway_frame(rwy_28l, last.lat, last.lon)
    return head + flown_track(rwy_28l, start_nm=along / NM_FT - step_nm, start_cross_ft=cross, step_s=1.0, gs_kt=GS_KT,
                              start_offset_deg=track_diff_deg(last.track, rwy_28l.course_true_deg), t0=last.t + 1,
                              hexid=last.hex, callsign=last.callsign, legs=[(8, -25.0), (28, -25.0), (8, 0.0), (60, 0.0)])


@pytest.mark.parametrize("leave_dtg_nm", [10.6, 11.0])
def test_a_hand_over_never_starts_before_the_path_attempt_was_established(hq, qb, leave_dtg_nm):
    # Established 400 ft left of the path at 10.5 nm, the flight is never within 450 ft of it afterwards except inside the
    # window it was established on. The hand-over is the runway-change rule's, and the point it falls back to from there
    # is no earlier than the establishment: the path row is not over before it began, and 28L's attempt 2 is not
    # established before attempt 1.
    pts = drifting_off_the_path_track(qb, leave_dtg_nm)
    first, second = finish(hq, pts)[:2]
    assert (first["runway"], first["outcome"], first["path"], second["runway"], second["attempt_no"]) == (
        "28R", "runway_change", "quiet-bridge", "28L", 2)
    assert first["established_at"] <= first["ended_at"] < second["established_at"]
    assert second["established_at"] > first["established_at"] and second["prev_approach_id"] == first["id"]


def test_the_hand_over_clamp_keeps_the_path_attempts_establishment_position(hq, qb):
    # whatever start a route asks for, even one before the attempt existed, the path attempt ends with the position it was
    # established on and the parallel's attempt begins with the next
    pts = path_track(qb, start_dtg_nm=16, end_dtg_nm=9.0)
    hq.feed(pts)
    [att] = hq.matcher.live()
    established_at = att.established_at
    hq.matcher._runway_change(att, Reference(make_sfo_28l()), att.points[0].t - 100.0, hq.tracker._tracks[pts[0].hex],
                              hq.matcher._attempts[pts[0].hex])
    first, second = hq.rows()
    assert first["ended_at"] == established_at == first["established_at"]
    assert second["established_at"] == established_at + 1.0


# ------------------------------------------------------------------ with the live merge
# adsb.lol's positions join the stream and are scored like the receiver's, so on a path approach they are measured from
# the attempt's reference too, and so is a go-around's climb-out, though it is stored after the close.
CROSS_TO_28L_ON_THE_CENTRELINE = [(8, -25.0), (33, -25.0), (8, 0.0), (60, 0.0)]


def climbing_on_along(qb: Reference, last, *, to_dtg_nm: float, gs_kt: float = 160.0, vr_fpm: int = 1500) -> list:
    """From `last`, straight on along Quiet Bridge (and past the threshold, along its last leg) to to_dtg_nm, climbing at
    vr_fpm, as a go-around flies it: the approach's own altitude target given up for the missed approach's."""
    dtg = qb.frame(last.lat, last.lon).along_ft / NM_FT - gs_kt / 3600.0
    return [replace(p, alt_baro=last.alt_baro + round(vr_fpm * (i + 1) / 60.0), baro_rate=vr_fpm, nav_altitude_mcp=5000,
                    nav_modes=frozenset({"autopilot"}))
            for i, p in enumerate(path_track(qb, start_dtg_nm=dtg, end_dtg_nm=to_dtg_nm, gs_kt=gs_kt, t0=last.t + 1,
                                             hexid=last.hex, callsign=last.callsign))]


@pytest.mark.parametrize("origin", ["local", "adsb.lol"])
def test_a_path_go_around_keeps_its_climb_out_measured_from_the_path(hq, qb, origin):
    # Down Quiet Bridge to its lowest 2.5 nm out, on the turn-in leg some hundreds of feet right of the centreline, then a
    # climb straight on along the path. The go-around closes 15 s into the climb and its climb-out goes on for D = 2.5 nm
    # from the bottom, through the join at 1.1 nm: from either origin, its points are measured from the path, as the
    # approach's were, and name it, so the approach still reads "Quiet Bridge" with no switch to the ILS.
    rwy = qb.runway
    down = path_track(qb, start_dtg_nm=16, end_dtg_nm=2.5)
    up = climbing_on_along(qb, down[-1], to_dtg_nm=-2.0)
    after = base.feed_until_closed(hq, down + up)
    [row] = hq.rows()
    assert (row["runway"], row["outcome"], row["path"]) == ("28R", "go_around", "quiet-bridge")
    assert row["ga_initiated_along_nm"] == pytest.approx(2.5, abs=0.05)
    n_close = len(hq.store.points(row["id"]))
    hq.feed([replace(p, origin=origin) for p in after])
    points = hq.store.points(row["id"])
    climb_out = [p for p in points if p["charts_only"]]
    assert climb_out and len(points) == n_close + len(climb_out) and {p["origin"] for p in climb_out} == {origin}
    assert climb_out[-1]["t"] == base.reaching(down + up, row["ga_initiated_at"], row["ga_initiated_along_nm"] * NM_FT).t
    assert {p["reference"] for p in points} == {"quiet-bridge"}
    flown = {p.t: p for p in after}
    for p in climb_out:
        frame = qb.frame(flown[p["t"]].lat, flown[p["t"]].lon)
        assert p["cross_ft"] == pytest.approx(frame.cross_ft, abs=0.01) and abs(p["cross_ft"]) < 50.0
        assert p["along_nm"] == pytest.approx(frame.along_ft / NM_FT, abs=1e-4)
    # the premise: from the centreline, the climb-out's stretch on the turn-in leg lies hundreds of feet off
    assert max(abs(runway_frame(rwy, flown[p["t"]].lat, flown[p["t"]].lon)[1]) for p in climb_out) > 300.0
    [extended] = payloads(hq, "approach.extended")
    assert (extended["runway"], extended["points_added"]) == ("28R", len(climb_out))


def test_a_path_approach_adsb_lol_follows_to_the_landing_is_measured_from_the_path(hq, qb):
    # The receiver loses a Quiet Bridge flight at 2.5 nm, on the turn-in leg; adsb.lol follows it down the path to the
    # roll-out. Its points are measured from the path like the receiver's, and the ending is adsb.lol's.
    rwy = qb.runway
    local = path_track(qb, start_dtg_nm=16, end_dtg_nm=2.5)
    lol = base.from_adsb_lol(path_track(qb, start_dtg_nm=2.45, end_dtg_nm=0.05, t0=local[-1].t + 1))
    roll = base.from_adsb_lol([ground_point(rwy, along_nm=-0.2 - 0.03 * i, t=lol[-1].t + 5 + i, gs=gs, hexid=local[0].hex,
                                            callsign=local[0].callsign) for i, gs in enumerate((28.0, 22.0, 15.0))])
    assert roll[0].t - local[-1].t > hq.cfg.detection.lost_timeout_s
    hq.feed(local + lol + roll)
    [row] = hq.rows()
    flags = Flags(row["flags"])
    assert (row["outcome"], row["path"], row["ended_at"]) == ("landed", "quiet-bridge", roll[0].t)
    assert base.EXTERNAL_ENDING in flags and Flags.EXTERNAL_GAPS not in flags and row["landed_confirmed_by"] == "adsb.lol"
    points = hq.store.points(row["id"])
    assert {p["reference"] for p in points} == {"quiet-bridge"}
    theirs = [p for p in points if p["origin"] == "adsb.lol"]
    assert theirs and max(abs(p["cross_ft"]) for p in theirs) < 50.0
    flown = {p.t: p for p in lol}
    assert max(abs(runway_frame(rwy, flown[p["t"]].lat, flown[p["t"]].lon)[1]) for p in theirs if p["t"] in flown) > 300.0


def test_a_switch_measures_adsb_lol_points_again_and_keeps_their_origin(hq, qb):
    # the receiver hears the flight on Quiet Bridge only; from its turn towards the localizer on, adsb.lol's positions carry
    # it, and the switch measures the ones from the run's start again against the centreline, each keeping its origin
    pts = early_join_track(qb)
    on_path_until = T0 + (16.0 - 7.0) * 3600.0 / GS_KT                   # early_join_track turns off the path 7 nm out
    pts = [p if p.t <= on_path_until else replace(p, origin="adsb.lol") for p in pts]
    [row] = finish(hq, pts)
    runs = [k for k, _ in groupby((p["reference"], p["origin"]) for p in hq.store.points(row["id"]))]
    assert runs == [("quiet-bridge", "local"), ("quiet-bridge", "adsb.lol"), (None, "adsb.lol")]
    assert [(e["from"], e["to"]) for e in reference_events(hq)] == [("quiet-bridge", None)]


@pytest.mark.parametrize("receiver_until_nm, expected", [(9.5, Flags.EXTERNAL_TAIL), (8.0, Flags.EXTERNAL_GAPS)])
def test_a_hand_over_s_external_flags_are_those_of_the_points_the_path_row_keeps(hq, qb, receiver_until_nm, expected):
    # The receiver loses the flight on the path, and adsb.lol carries it across to 28L. The path row keeps only its
    # points before the hand-over starts, where the flight left the path about 7 nm out, and ends with the last of them,
    # so its flags are read from those: a tail when the receiver had been silent for longer than lost_timeout_s by then
    # (heard to 9.5 nm), gaps when not (heard to 8 nm). A runway change is never adsb.lol's outcome.
    pts = cross_to_28l(qb, gs_kt=GS_KT, head_end_dtg_nm=7.0, legs=CROSS_TO_28L_ON_THE_CENTRELINE)
    heard_until = T0 + (16.0 - receiver_until_nm) * 3600.0 / GS_KT
    pts = [p if p.t <= heard_until else replace(p, origin="adsb.lol") for p in pts]
    first, second = hand_over_rows(hq, pts)
    kept = hq.store.points(first["id"])
    last_local = max(p["t"] for p in kept if p["origin"] == "local")
    assert kept[-1]["origin"] == "adsb.lol" and kept[-1]["t"] == first["ended_at"]
    assert (kept[-1]["t"] - last_local > hq.cfg.detection.lost_timeout_s) is (expected is Flags.EXTERNAL_TAIL)
    assert Flags(first["flags"]) & base.EXTERNAL == expected and first["landed_confirmed_by"] is None
    assert Flags(second["flags"]) & base.EXTERNAL == Flags.EXTERNAL_TAIL           # 28L never held a receiver point


@pytest.mark.parametrize("scenario", ["switch", "candidate-route", "rule-route"])
def test_the_watch_list_holds_a_path_approach_through_a_switch_and_a_hand_over(hq, qb, scenario):
    # The poller asks adsb.lol about the aircraft of every open approach, from its approach.started. A path approach
    # starts like any other; a switch opens no attempt, so it changes nothing; a hand-over closes the path attempt and
    # starts the parallel's in one breath, so the aircraft never leaves the list.
    tracks = {"switch": lambda: early_join_track(qb),
              "candidate-route": lambda: cross_to_28l(qb, gs_kt=90.0, head_end_dtg_nm=7.0, legs=SLOW_CROSSING),
              "rule-route": lambda: cross_to_28l(qb, gs_kt=GS_KT, head_end_dtg_nm=7.0, legs=CROSS_TO_28L_ON_THE_CENTRELINE)}
    pts = tracks[scenario]()
    watch = WatchList(hq.cfg.external.watch_max_min * 60.0)
    hq.matcher.events.subscribe(watch.on_event)
    watched = []
    for p in pts:
        hq.feed([p])
        watched.append(p.hex in watch.hexes(p.t))
    started = payloads(hq, "approach.started")
    assert len(started) == (1 if scenario == "switch" else 2) and started[0]["path"] == "quiet-bridge"
    assert bool(reference_events(hq)) is (scenario == "switch")
    assert True in watched and all(watched[watched.index(True):])        # from the path attempt's start to the track's end
    hq.matcher.tick(pts[-1].t + 46)                                       # lost, and nobody to watch any more
    assert watch.hexes(pts[-1].t + 46) == []

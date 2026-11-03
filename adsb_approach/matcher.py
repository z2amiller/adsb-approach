"""Candidate → established → outcome state machine per (aircraft, runway end)."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from .attempt import Attempt, State
from .config import Config, DetectionCfg
from .events import EventBus
from .geometry import NM_FT, distance_ft, track_diff_deg
from .model import EMERGENCY_MASK, Airport, Flags, Outcome, Position, RunwayEnd, Weather, flag_names
from .reference import Reference, references
from .scorer import Gate, ScoredPoint, Scorer, Summary
from .store.db import Store
from .tracker import Track
from .weather import WeatherTable

AIRPORT_RADIUS_NM = 15.0
CANDIDATE_TIMEOUT_S = 60.0
CANDIDATE_MAX_POINTS = 400
TOUCHDOWN_HAT_FT = 100.0          # ground contact, or below this height inside the runway footprint: touched down
ROLLOUT_GS_KT = 30.0              # touched down and at or below this ground speed: taxiing, too slow for a touch-and-go


def coarse(pos: Position) -> bool:
    """Too coarse to use: radar-derived TIS-B positions (readsb sources tisb, tisb_icao, tisb_other, tisb_trackfile), even
    for an ICAO address, and every non-ICAO address (hex prefixed ~: TIS-B track files, ADS-R and ADS-B others)."""
    return pos.source.startswith("tisb") or pos.hex.startswith("~")


@dataclass
class ArrivalInfo:
    arrival_id: int
    last_approach_id: int
    ended_at: float
    attempt_no: int
    airport: str


@dataclass
class ClosedInfo:
    approach_id: int
    flight_id: int | None
    airport: Airport
    outcome: Outcome
    flags: Flags
    ended_at: float
    min_alt_baro: int | None
    arrival_id: int
    attempt_no: int
    confirmed: bool = False
    reclassified: bool = False
    runway: str = ""
    ground_in_footprint: bool = False     # the ground report that confirmed the landing lay on this approach's runway


@dataclass
class ClimbOut:
    """A go-around's or touch-and-go's record, still open after its close: the hex's later positions, from either
    origin, are stored on the closed approach for the charts until `target_ft` has been flown since the go-around began
    (the retired trace backfill's rule: max(D, 1 nm), D being the go-around's distance from the threshold), `until`, or
    a gap of more than lost_timeout_s in the hex's positions. `reference` is the one the attempt was measured against
    when it closed, which its climb-out is measured against too (a path approach's climb-out is not measured from the
    centreline). `last_*` is the latest position taken, `flags` those the close wrote, and `added` how many rows the
    climb-out has stored so far."""
    approach_id: int
    flight_id: int | None
    reference: Reference
    airport: Airport
    arrival_id: int
    attempt_no: int
    flags: Flags
    target_ft: float
    flown_ft: float
    last_lat: float
    last_lon: float
    last_t: float
    until: float
    added: int = 0


def _flown_ft(points: list[ScoredPoint], start_t: float) -> float:
    """Great-circle distance along the points from start_t on."""
    flown = [p for p in points if p.t >= start_t]
    return sum(distance_ft(a.lat, a.lon, b.lat, b.lon) for a, b in zip(flown, flown[1:]))


def in_footprint(rwy: RunwayEnd, sp: ScoredPoint) -> bool:
    """The landing footprint: 3,000 ft short of the landing threshold to the far end of the pavement, within 300 ft of
    the centreline."""
    return -rwy.landing_length_ft <= sp.along_ft <= 3000.0 and abs(sp.cross_ft) <= 300.0


def external_flags(points: list[ScoredPoint], lost_timeout_s: float, bottom_t: float | None = None) -> Flags:
    """Where an approach's points came from, as flags; a point whose origin isn't "local" is adsb.lol's. Receiver gaps
    of 2 s or more cover about half of tracked time and a poll can fill any of them, so an adsb.lol point inside the
    track says little: only a stretch the receiver could not have followed counts as a tail.
    - EXTERNAL_TAIL: no point is local, or the last point is adsb.lol's and the receiver's last point is earlier and
      either lies more than lost_timeout_s before it (the time after which, without adsb.lol, the matcher would have
      closed the attempt as lost: the coverage column's "the receiver couldn't follow them down") or lies before
      `bottom_t`, the time a go-around or touch-and-go reached its lowest altitude. The second clause is there because
      such an attempt closes only a few seconds into the climb, often under lost_timeout_s after the receiver's last
      point, yet it was adsb.lol alone that saw the bottom and the climb.
    - EXTERNAL_GAPS: an adsb.lol point lies after the receiver's first point and is not part of a tail: between two of
      its points, or in a trailing stretch that is not a tail.
    Points before the receiver's first are neither."""
    local = [i for i, p in enumerate(points) if p.origin == "local"]
    if not local:
        return Flags.EXTERNAL_TAIL if points else Flags.NONE
    first, last = local[0], local[-1]
    tail = last < len(points) - 1 and (points[-1].t - points[last].t > lost_timeout_s
                                       or (bottom_t is not None and points[last].t < bottom_t))
    flags = Flags.EXTERNAL_TAIL if tail else Flags.NONE
    if any(p.origin != "local" for p in points[first:last if tail else len(points)]):
        flags |= Flags.EXTERNAL_GAPS
    return flags


@dataclass
class Verdict:
    """An outcome reached at a point by landing_or_go_around, with a go-around's or touch-and-go's fields and
    corroborating flags."""
    outcome: Outcome
    ga: dict | None = None
    extra_flags: Flags = Flags.NONE


def _arm_and_note_descent(att: Attempt, sp: ScoredPoint, d: DetectionCfg) -> None:
    """Arm go-around detection once the aircraft is at or below go_around_armed_below_ft, and keep the selected
    altitude of the descent (the one a later jump in it is measured against) while it is not climbing and not yet
    armed."""
    vr = sp.vertical_rate
    if sp.nav_altitude_mcp is not None and (vr is None or vr <= 0) and not att.armed:
        att.descent_mcp = sp.nav_altitude_mcp
    if sp.hat_ft is not None and sp.hat_ft <= d.go_around_armed_below_ft:
        att.armed = True


def _touchdown_is_landing(att: Attempt, sp: ScoredPoint, d: DetectionCfg) -> bool:
    """Record a touchdown at this point and say whether it has become a landing.
    Touchdown (ground contact, or hat < 100 ft inside the footprint; ground positions score hat 0) is not yet a
    landing: if the go-around climb rule follows, it is a touch-and-go. It is a landing once the aircraft, still
    down, slows to taxi speed or leaves the footprint (turned off); failing that, the lost timeout lands it."""
    inside = in_footprint(att.runway, sp)
    hat = math.inf if sp.hat_ft is None else sp.hat_ft
    down = hat < TOUCHDOWN_HAT_FT
    if down and inside:
        att.touched = True
        att.touched_low = att.touched_low or hat < d.touch_and_go_max_height_ft
    return att.touched and down and (not inside or (sp.gs is not None and sp.gs <= ROLLOUT_GS_KT))


def _note_climb_start(att: Attempt, sp: ScoredPoint, d: DetectionCfg) -> None:
    """Keep att.climb_start_t: when the current unbroken climb at go_around_min_vr_fpm or better began, None once a
    point is not climbing."""
    vr = sp.vertical_rate
    if vr is not None and vr >= d.go_around_min_vr_fpm:
        if att.climb_start_t is None:
            att.climb_start_t = sp.t
    else:
        att.climb_start_t = None


def _climbed_away(att: Attempt, sp: ScoredPoint, d: DetectionCfg) -> bool:
    """The go-around test: armed, climbing for go_around_min_s, go_around_min_climb_ft above the lowest corrected
    altitude flown, and inside the scored stretch (from a nautical mile past the far end of the pavement out to
    score_max_along_nm before the threshold)."""
    return (att.armed and att.climb_start_t is not None and sp.t - att.climb_start_t >= d.go_around_min_s
            and sp.alt_corr is not None and sp.alt_corr - att.min_alt_corr >= d.go_around_min_climb_ft
            and -(att.runway.landing_length_ft + NM_FT) <= sp.along_ft <= d.score_max_along_nm * NM_FT)


def _go_around_verdict(att: Attempt, sp: ScoredPoint) -> Verdict:
    """A touch-and-go (it touched down below touch_and_go_max_height_ft) or a go-around, with where it began (the
    attempt's lowest point) and the flags that corroborate it: a jump in the selected altitude since the descent, and
    the approach mode dropped."""
    extra = Flags.NONE
    if att.descent_mcp is not None and sp.nav_altitude_mcp is not None and sp.nav_altitude_mcp >= att.descent_mcp + 1000:
        extra |= Flags.MCP_JUMP
    if att.approach_mode_seen and sp.nav_modes and "approach" not in sp.nav_modes:
        extra |= Flags.APPROACH_MODE_DROPPED
    mp = att.min_alt_point
    ga = dict(initiated_at=att.min_alt_t, initiated_along_nm=None if mp is None else mp.along_nm,
              initiated_hat_ft=att.min_hat_ft if att.min_hat_ft != math.inf else None,
              initiated_gs=None if mp is None else mp.gs)
    return Verdict(Outcome.TOUCH_AND_GO if att.touched_low else Outcome.GO_AROUND, ga, extra)


def landing_or_go_around(att: Attempt, sp: ScoredPoint, d: DetectionCfg) -> Verdict | None:
    """The first two outcome rules at a point already added to the attempt, first match wins: landed, then go-around or
    touch-and-go. They update the attempt's arming, touchdown and climb state and read only scored points."""
    _arm_and_note_descent(att, sp, d)
    if _touchdown_is_landing(att, sp, d):
        return Verdict(Outcome.LANDED)
    # go-around: sustained climb after the minimum
    _note_climb_start(att, sp, d)
    if _climbed_away(att, sp, d):
        return _go_around_verdict(att, sp)
    return None


class Matcher:
    def __init__(self, cfg: Config, scorer: Scorer, weather: WeatherTable, store: Store, events: EventBus,
                 run_id: int) -> None:
        self.cfg = cfg
        self.d = cfg.detection
        self.scorer = scorer
        self.weather = weather
        self.store = store
        self.events = events
        self.run_id = run_id
        self.clock = 0.0
        self._attempts: dict[str, dict[str, Attempt]] = {}         # per hex, filed under Attempt.key
        self._refs_by_icao: dict[str, tuple[Reference, ...]] = {}
        self._arrivals: dict[int, ArrivalInfo] = {}
        self._closed: dict[str, ClosedInfo] = {}
        self._climb_outs: dict[str, ClimbOut] = {}
        self._last_tick = -math.inf

    # ------------------------------------------------------------------ public
    def process(self, pos: Position, track: Track) -> None:
        """One position of an airframe: stamp what its flight has latched, close what went quiet, then take the position
        into the attempts of every airport within AIRPORT_RADIUS_NM, after the climb-out and the closed-approach
        rules."""
        # Stamp what the flight has latched (_stamp_emergency) for every position of this airframe, a coarse one or one
        # outside every airport's radius included: the watch latches on any of them (Tracker.update runs first), and
        # _close reads att.emergency_flags, so it must be current before tick() below can close an attempt that no
        # position of its own will reach.
        self._stamp_established_emergencies(pos, track)
        if pos.t - self._last_tick >= 1.0:
            self.tick(pos.t)          # first, so a position after a feed-wide gap cannot extend an attempt that went quiet
        self.clock = max(self.clock, pos.t)
        if coarse(pos):
            return                    # never scored, and never taken as a go-around or a landing either
        self._feed_climb_out(pos)
        self._after_close_checks(pos, track)
        for airport in self.cfg.enabled_airports():
            if distance_ft(airport.lat, airport.lon, pos.lat, pos.lon) > AIRPORT_RADIUS_NM * NM_FT:
                continue
            self._process_airport(airport, pos, track)

    def _process_airport(self, airport: Airport, pos: Position, track: Track) -> None:
        """The position against every reference of an airport it is within the radius of; the hex's entry in
        _attempts goes once it holds no attempt."""
        wx = self.weather.at(airport.metar, pos.t)
        per_hex = self._attempts.setdefault(pos.hex, {})
        for ref in self._refs(airport):
            self._process_reference(airport, ref, pos, track, per_hex, wx)
        if not per_hex:
            self._attempts.pop(pos.hex, None)

    def _process_reference(self, airport: Airport, ref: Reference, pos: Position, track: Track,
                           per_hex: dict[str, Attempt], wx: Weather | None) -> None:
        """The position against one reference: it opens a candidate on it, or goes to the attempt filed under it, an
        established one's outcome rules or a candidate's establishment test."""
        att = per_hex.get(ref.key)
        if att is None:
            if not self._may_open_on(pos, ref, per_hex):
                return
        elif att.raw and att.raw[-1] is pos:
            return                      # already added to this attempt (opened by a runway change this pass)
        # An attempt is measured against the reference it has now, which is not always the one it was filed under.
        sp = self.scorer.score(ref.runway, airport, pos, wx, ref=ref if att is None else att.reference)
        if att is None:
            if self._candidate_ok(sp, pos):
                self._open_candidate(airport, ref, sp, pos, track, per_hex)
        elif att.state is State.ESTABLISHED:
            self._update_established(att, sp, pos, track, per_hex, wx)
        else:
            self._advance_candidate(att, sp, pos, track, per_hex, wx)

    def tick(self, now: float) -> None:
        """Close attempts that stopped receiving positions; forget closed state that no rule can use any more."""
        self._last_tick = now
        self.clock = max(self.clock, now)
        for hexid, per_hex in list(self._attempts.items()):
            for key, att in list(per_hex.items()):
                if per_hex.get(key) is not att:
                    continue                      # closing an attempt earlier in this pass dropped it (its parallel's candidates)
                idle = now - att.last_t
                if att.state is State.CANDIDATE and idle > CANDIDATE_TIMEOUT_S:
                    del per_hex[key]
                elif att.state is State.ESTABLISHED and idle > self.d.lost_timeout_s:
                    self._close_lost(att, per_hex)
            if not per_hex:
                del self._attempts[hexid]
        horizon = self.d.landed_confirm_departure_h * 3600.0
        for hexid, info in list(self._closed.items()):
            if now - info.ended_at > horizon:
                del self._closed[hexid]
        for hexid, climb_out in list(self._climb_outs.items()):
            if now > climb_out.until:
                del self._climb_outs[hexid]
        link_window = self.d.arrival_link_window_min * 60.0
        for flight_key, arrival in list(self._arrivals.items()):
            if now - arrival.ended_at > link_window:
                del self._arrivals[flight_key]

    def finish(self) -> None:
        """End of stream: close every established attempt as lost, drop candidates."""
        for per_hex in list(self._attempts.values()):
            for att in list(per_hex.values()):
                if att.state is State.ESTABLISHED:
                    self._close_lost(att, per_hex)
        self._attempts.clear()

    def live(self) -> list[Attempt]:
        return [a for per in self._attempts.values() for a in per.values() if a.state is State.ESTABLISHED]

    # ------------------------------------------------------------ candidates
    def _may_open_on(self, pos: Position, ref: Reference, per_hex: dict[str, Attempt]) -> bool:
        """Cheap pre-filter for a reference without an attempt: whether the position is airborne, flying the
        reference's course and not blocked, so that it is worth scoring. A path's local course is that of the leg
        nearest the position, so it costs its frame and nothing of the altitude work."""
        if pos.on_ground or pos.track is None:
            return False
        course = ref.runway.course_true_deg if ref.path is None else ref.frame(pos.lat, pos.lon).course_deg
        if abs(track_diff_deg(pos.track, course)) > self.d.candidate_track_tol_deg:
            return False
        return not self._blocks_candidate(per_hex, ref)       # blocked: nothing to open here, so nothing to score

    @staticmethod
    def _open_candidate(airport: Airport, ref: Reference, sp: ScoredPoint, pos: Position, track: Track,
                        per_hex: dict[str, Attempt]) -> None:
        """A new candidate on `ref`, filed under its key, starting with this position."""
        flight_key = track.flight.id if track.flight.id is not None else id(track.flight)
        per_hex[ref.key] = Attempt(hex=pos.hex, runway=ref.runway, airport=airport, state=State.CANDIDATE,
                                   points=[], raw=[], last_t=pos.t, flight_key=flight_key,
                                   flight_id=track.flight.id, reference=ref)
        per_hex[ref.key].add(sp, pos)

    def _advance_candidate(self, att: Attempt, sp: ScoredPoint, pos: Position, track: Track,
                           per_hex: dict[str, Attempt], wx: Weather | None) -> None:
        """A candidate takes the position if it still fits (keeping its newest CANDIDATE_MAX_POINTS) and is established
        once its window is steady; one the position no longer fits is dropped."""
        if self._candidate_ok(sp, pos):
            att.add(sp, pos)
            if len(att.points) > CANDIDATE_MAX_POINTS:
                del att.points[0]
                del att.raw[0]
            if self._established_ok(att):
                self._establish(att, track, per_hex, wx)
        else:
            del per_hex[att.key]

    def _refs(self, airport: Airport) -> tuple[Reference, ...]:
        """The references of the airport's enabled runways, each runway's centreline then its paths, in file order:
        what candidates and attempts are filed under. Built on first use, since the configuration is fixed once the
        matcher runs."""
        refs = self._refs_by_icao.get(airport.icao)
        if refs is None:
            refs = tuple(ref for rwy in airport.runways.values() if rwy.enabled for ref in references(rwy))
            self._refs_by_icao[airport.icao] = refs
        return refs

    def _candidate_ok(self, sp: ScoredPoint, pos: Position) -> bool:
        """`sp` is the position against the reference the candidate is on, and its track deviation is from that
        reference's local course."""
        if pos.on_ground or pos.track is None or sp.hat_ft is None or sp.track_dev_deg is None:
            return False
        vr = pos.vertical_rate
        return (0.0 <= sp.along_nm <= self.d.candidate_max_along_nm
                and abs(sp.cross_ft) <= self.d.candidate_max_cross_nm * NM_FT
                and abs(sp.track_dev_deg) <= self.d.candidate_track_tol_deg
                and sp.hat_ft <= self.d.candidate_max_height_ft
                and (vr is None or vr <= self.d.candidate_max_climb_fpm))

    def _window_start(self, points: list[ScoredPoint]) -> int | None:
        """Index of the first point of the establishment window: the most recent points spanning at least
        established_window_s and numbering at least established_min_points; None while the candidate is shorter.
        Measured in seconds, so 2 positions/s from the live port and 8 s history judge the same stretch of flight."""
        i = len(points) - max(self.d.established_min_points, 2)
        t_last = points[-1].t if points else 0.0
        while i >= 0 and t_last - points[i].t < self.d.established_window_s:
            i -= 1
        return i if i >= 0 else None

    def _established_ok(self, att: Attempt) -> bool:
        """Every point of the window within established_cross_nm and established_track_tol_deg of the course (of the
        attempt's reference: the points' cross and track deviation are from it), closing on the threshold, and
        converged: the net cross-track change across the window is at most established_max_cross_rate_fps for each
        second it spans, so an intercept still crossing towards a parallel stays a candidate."""
        i = self._window_start(att.points)
        if i is None:
            return False
        d = self.d
        recent = att.points[i:]
        return (all(abs(p.cross_ft) <= d.established_cross_nm * NM_FT and p.track_dev_deg is not None
                    and abs(p.track_dev_deg) <= d.established_track_tol_deg for p in recent)
                and recent[-1].along_ft < recent[0].along_ft
                and abs(recent[-1].cross_ft - recent[0].cross_ft)
                <= d.established_max_cross_rate_fps * (recent[-1].t - recent[0].t))

    @staticmethod
    def _blocks_candidate(per_hex: dict[str, Attempt], ref: Reference) -> bool:
        """What stops a candidate opening on `ref`: an established attempt on the same runway, whatever it is measured
        against (a runway with paths but no parallel group must not open a second attempt beside the one it has), or one
        on a parallel runway's centreline. An attempt established on a path does not block the parallel's references:
        the flight may be about to cross to it, and _establish decides whether the parallel takes over."""
        rwy = ref.runway
        return any(a.state is State.ESTABLISHED and a.airport.icao == rwy.airport
                   and (a.runway.ident == rwy.ident
                        or (a.runway.ident in rwy.parallel_group and a.reference.path is None)) for a in per_hex.values())

    def _establish(self, att: Attempt, track: Track, per_hex: dict[str, Attempt], wx: Weather | None) -> None:
        """A candidate whose window is steady becomes an established attempt, unless it is blocked, a parallel member or
        a path attempt on the group has the better claim to the flight, or the members' comparison is not yet settled:
        then it stays a candidate (or goes) and a later position decides."""
        # A candidate that opened while a path attempt stood on the group (which blocks nothing but its own runway) is
        # blocked once the attempt is on a centreline, as it would have been had it been there from the start.
        if self._blocks_candidate(per_hex, att.reference):
            per_hex.pop(att.key, None)
            return
        newest = att.raw[-1]                          # the position this pass is on, which a winner may not have taken yet
        start = self._window_start(att.points)
        assert start is not None                      # _established_ok(att) held
        recent = att.raw[start:]
        contest = self._contest_parallel_group(att, per_hex, wx, recent)
        if contest is None:
            return
        att, cross = contest
        # A flight established on a path in the parallel group is not given up to a parallel's candidate on its say-so:
        # near the join Quiet Bridge lies within 0.3 nm of 28L, and that candidate establishes on every landing. It takes
        # over only if it wins the comparison the members just made over the same window. When the parallel wins, the
        # path attempt closes as a runway change that starts where the flight left the path.
        cur = self._path_attempt_on_group(att, per_hex)
        if cur is not None:
            ours = cross.get(att.key) or [abs(p.cross_ft) for p in att.points[start:]]       # the same window as above
            if self._beats_path_attempt(cur, ours, recent):
                per_hex.pop(att.key, None)
                # where the flight left the path; if it was never within runway_change_far_ft of it,
                # where this window began
                self._runway_change(cur, att.reference, self._left_path_t(cur, recent[0].t), track, per_hex,
                                    latest=newest)
            return                                    # otherwise the path is still the nearer: it stays a candidate
        self._open_established(att, track, wx)

    def _contest_parallel_group(self, att: Attempt, per_hex: dict[str, Attempt], wx: Weather | None,
                                recent: list[Position]) -> tuple[Attempt, dict[str, list[float]]] | None:
        """The attempt to establish, with each member's |cross| at the positions `recent` (empty when `att` has no rival
        in the group); None while the comparison does not yet settle it.

        Parallel group: the member closest to its centreline wins; the others are dropped. Every member is judged on
        the same recent positions (a member updated later in this pass still lacks the newest one), and nothing is
        established until each of those positions is nearest the winner: an intercept that crosses or overshoots
        towards the parallel's centreline stays a candidate on both instead of opening an attempt on the wrong one.
        A member is any reference of the runway or of its parallels: a path competes with its own centreline, and with
        the parallel's references, on the same footing."""
        members = [a for a in per_hex.values() if a.state is State.CANDIDATE and a.airport is att.airport
                   and (a.runway.ident == att.runway.ident or a.runway.ident in att.runway.parallel_group)]
        cross: dict[str, list[float]] = {}
        if len(members) > 1:
            cross = {a.key: [abs(self.scorer.score(a.runway, a.airport, p, wx, ref=a.reference).cross_ft) for p in recent]
                     for a in members}
            winner = min(members, key=lambda a: statistics.median(cross[a.key]))
            if any(min(c[i] for c in cross.values()) < cross[winner.key][i] for i in range(len(recent))):
                return None
            if winner is not att:
                if not self._established_ok(winner):
                    return None
                att = winner
            for a in members:
                if a is not att:
                    per_hex.pop(a.key, None)
        return att, cross

    @staticmethod
    def _path_attempt_on_group(att: Attempt, per_hex: dict[str, Attempt]) -> Attempt | None:
        """The established attempt on a path of another runway of `att`'s parallel group, if the hex has one."""
        return next((a for a in per_hex.values() if a.state is State.ESTABLISHED and a.airport is att.airport
                     and a.runway.ident != att.runway.ident and a.runway.ident in att.runway.parallel_group
                     and a.reference.path is not None), None)

    def _beats_path_attempt(self, cur: Attempt, ours: list[float], recent: list[Position]) -> bool:
        """Whether the parallel's candidate, whose |cross| at the positions `recent` is `ours`, wins the comparison the
        members just made over the same window, against the nearest of every reference of the path attempt `cur`'s
        runway (its path, its centreline, any other path): a smaller median |cross| and, at every position, a smaller
        |cross|. The centreline counts because a flight that has left the path and settles on it is on that runway (the
        parallel's candidate would otherwise take it whenever it beat the path alone, which the path's distance makes
        easy), and its way onto the centreline is the switch rule's."""
        own_refs = [r for r in self._refs(cur.airport) if r.runway.ident == cur.runway.ident]
        theirs = [min(abs(r.frame(p.lat, p.lon).cross_ft) for r in own_refs) for p in recent]
        # (the second test implies the first; both are kept to match the rule as the docstring words it)
        return statistics.median(ours) < statistics.median(theirs) and all(o < t for o, t in zip(ours, theirs))

    def _open_established(self, att: Attempt, track: Track, wx: Weather | None) -> None:
        """The candidate is established: take over from the previous attempt's closed state, open its row and publish
        approach.started."""
        pos, sp = att.raw[-1], att.points[-1]
        att.state = State.ESTABLISHED
        att.established_at = pos.t
        att.established_hat_ft = sp.hat_ft
        self._closed.pop(att.hex, None)       # a new attempt supersedes the previous one's reappearance and confirmation
        self._climb_outs.pop(att.hex, None)   # and ends the previous go-around's climb-out
        self._chain_arrival(att, pos.t)
        metar_fresh = wx is not None and 0 <= pos.t - wx.observed_at <= self.cfg.service.qnh_max_age_min * 60.0
        att.weather_id = self.store.insert_weather(wx) if metar_fresh and wx is not None else None
        f = track.flight
        att.flight_id = f.id      # the session current at establishment, which may differ from the one at open
        att.type_code, att.traffic_class = f.type_code, f.traffic_class
        att.approach_id = self.store.open_approach(
            run_id=self.run_id, flight_id=f.id, arrival_id=att.arrival_id, attempt_no=att.attempt_no, hex=att.hex,
            callsign=f.callsign, type_code=f.type_code, traffic_class=f.traffic_class, approach_cat=f.approach_cat,
            airport=att.airport.icao, runway=att.runway.ident, established_at=pos.t,
            first_along_nm=max(p.along_nm for p in att.points), weather_id=att.weather_id,
            reference_source=att.runway.reference_source, prev_approach_id=att.prev_approach_id, path=att.reference.name)
        if att.arrival_id is None:
            att.arrival_id = att.approach_id
        # Whatever the flight has latched anywhere earlier in its life lands on this attempt's row too, not just what it
        # is doing right now: an aircraft that declared an emergency on descent still marks its approach.
        att.emergency_flags = f.emergency.latched
        if att.emergency_flags:
            self.store.add_flags(att.approach_id, att.emergency_flags)
        self._publish("approach.started", pos.t, att, {"callsign": f.callsign, "type_code": f.type_code,
                       "traffic_class": f.traffic_class,
                       "along_nm": round(sp.along_nm, 2), "hat_ft": None if sp.hat_ft is None else round(sp.hat_ft),
                       "attempt_no": att.attempt_no, "arrival_id": att.arrival_id, "origin": sp.origin,
                       "emergency": flag_names(att.emergency_flags & EMERGENCY_MASK)})

    def _chain_arrival(self, att: Attempt, t: float) -> None:
        """Link a newly-established attempt to its arrival's previous attempt, if any: in-memory first (the usual
        case, cheap and exact), falling back to the store when this process has no in-memory predecessor for the
        flight — a restart starts a fresh Matcher with an empty _arrivals, so the aircraft's own last attempt before
        the restart would otherwise be missed and this attempt would wrongly start a new arrival (observed on a
        running collector). The store fallback uses the same window and finds the same predecessor a surviving
        in-memory entry would have named; open_approach links the found row's next_approach_id forward, the same as the
        in-memory path's own later open_approach call does."""
        info = self._arrivals.get(att.flight_key)
        if info is not None and info.airport == att.airport.icao and t - info.ended_at <= self.d.arrival_link_window_min * 60.0:
            att.arrival_id = info.arrival_id
            att.attempt_no = info.attempt_no + 1
            att.prev_approach_id = info.last_approach_id
            return
        row = self.store.latest_ended_approach(att.hex, att.airport.icao, t - self.d.arrival_link_window_min * 60.0)
        if row is not None:
            att.arrival_id = row["arrival_id"]
            att.attempt_no = row["attempt_no"] + 1
            att.prev_approach_id = row["id"]

    # ------------------------------------------------------------ established
    @staticmethod
    def _in_footprint(rwy: RunwayEnd, sp: ScoredPoint) -> bool:
        return in_footprint(rwy, sp)

    def _update_established(self, att: Attempt, sp: ScoredPoint, pos: Position, track: Track,
                            per_hex: dict[str, Attempt], wx: Weather | None) -> None:
        """The outcome rules for one position, first match wins: landed, go-around or touch-and-go
        (landing_or_go_around), runway change, abandoned; otherwise approach.updated at most once per second. Between
        the runway change and abandonment sits the switch to another reference of the same runway, which ends nothing:
        the attempt goes on measured from the other reference.

        The emergency stamp (_stamp_emergency) has already run for this position, at the top of process, ahead of every
        outcome rule: a run can latch on the very position that closes the attempt below, and _close reads
        att.emergency_flags rather than redoing the latch lookup."""
        att.add(sp, pos)
        verdict = landing_or_go_around(att, sp, self.d)
        if verdict is not None:
            self._close(att, per_hex, verdict.outcome, pos.t, ga=verdict.ga, extra_flags=verdict.extra_flags,
                        decided_by=sp)
            return
        if self._change_runway_if_settled(att, sp, pos, track, per_hex, wx):
            return
        sp = self._switch_reference_if_settled(att, sp, pos, per_hex, wx)
        if self._abandoned(att, sp, pos, wx):
            self._close(att, per_hex, Outcome.ABANDONED, pos.t, decided_by=sp)
            return
        self._publish_update(att, sp, pos, track)

    def _settled_since(self, other: Reference, osp: ScoredPoint, sp: ScoredPoint, pos: Position,
                       near_runs: dict[str, float], aligned_runs: dict[str, float]) -> float | None:
        """Where the run that moves the attempt onto `other` began, once this position completes it; None until then.
        The run is the same for the runway change and the switch to another reference of the same runway: near `other`
        (`osp` is the position measured from it) while far from the attempt's own reference (`sp`), and then aligned
        with `other`'s course for runway_change_min_s. `near_runs` and `aligned_runs` hold, by reference key, the first
        position of the current run near it / near and aligned with it; a position that breaks a run clears it."""
        d = self.d
        near_ft = d.runway_change_far_ft if other.key in near_runs else d.runway_change_near_ft
        if not (abs(osp.cross_ft) < near_ft and abs(sp.cross_ft) > d.runway_change_far_ft):
            near_runs.pop(other.key, None)
            aligned_runs.pop(other.key, None)
            return None
        near_since = near_runs.setdefault(other.key, pos.t)
        if osp.track_dev_deg is None or abs(osp.track_dev_deg) > d.runway_change_track_tol_deg:
            aligned_runs.pop(other.key, None)
            return None
        if pos.t - aligned_runs.setdefault(other.key, pos.t) >= d.runway_change_min_s:
            return near_since
        return None

    def _change_runway_if_settled(self, att: Attempt, sp: ScoredPoint, pos: Position, track: Track,
                                  per_hex: dict[str, Attempt], wx: Weather | None) -> bool:
        """Runway change to a parallel member: every position near its centreline (within runway_change_near_ft to begin
        the run, runway_change_far_ft to continue it, so overshooting the new centreline does not restart it) and far
        from this one, and for the last runway_change_min_s also aligned with its course (a turn away across the
        parallel is not a change). The new attempt starts where the run began. True if the attempt changed runway."""
        rwy = att.runway
        for other in self._refs(att.airport):
            if other.runway.ident == rwy.ident or other.runway.ident not in rwy.parallel_group:
                continue
            osp = self.scorer.score(other.runway, att.airport, pos, wx, ref=other)
            near_since = self._settled_since(other, osp, sp, pos, att.change_near_since, att.change_aligned_since)
            if near_since is not None:
                # a flight on a path hands over where it left the path, not where it came near the parallel, so the
                # stretch flown on the path is never charged to the parallel as lateral deviation; if it was never within
                # runway_change_far_ft of the path, where it came near the parallel after all
                self._runway_change(att, other, near_since if att.reference.path is None
                                    else self._left_path_t(att, near_since), track, per_hex)
                return True
        return False

    def _switch_reference_if_settled(self, att: Attempt, sp: ScoredPoint, pos: Position, per_hex: dict[str, Attempt],
                                     wx: Weather | None) -> ScoredPoint:
        """Switch to another reference of this runway (a path, or the centreline): the runway-change rule's hysteresis
        against the references of the same runway, so an attempt that settles on another reference stays one attempt
        and measures from that one. Returns the position as measured from the reference the attempt is on afterwards:
        `sp` itself unless it switched."""
        rwy = att.runway
        for other in self._refs(att.airport):
            if other.runway.ident != rwy.ident or other.key == att.reference.key:
                continue
            osp = self.scorer.score(rwy, att.airport, pos, wx, ref=other)
            near_since = self._settled_since(other, osp, sp, pos, att.switch_near_since, att.switch_aligned_since)
            if near_since is not None:
                self._switch_reference(att, other, near_since)
                if other.path is None:                  # a centreline attempt blocks the parallel's candidates
                    self._drop_parallel_candidates(att, per_hex)
                return att.points[-1]                   # this position, measured from the reference it is on now
        return sp

    def _abandoned(self, att: Attempt, sp: ScoredPoint, pos: Position, wx: Weather | None) -> bool:
        """Abandoned: every position for abandon_min_s off course (beyond abandon_cross_nm, or tracking more than
        abandon_track_tol_deg away from the course) without the go-around rule firing first. Notes where the current
        run of off-course positions began, and says whether it has now lasted abandon_min_s."""
        d = self.d
        off = abs(sp.cross_ft) > d.abandon_cross_nm * NM_FT or (
            sp.track_dev_deg is not None and abs(sp.track_dev_deg) > d.abandon_track_tol_deg)
        if off and att.reference.path is not None and self._on_a_group_reference(att, pos, wx):
            off = False                                 # crossing to the parallel is not leaving the approach
        if not off:
            att.off_course_since = None
        elif att.off_course_since is None:
            att.off_course_since = pos.t
        return att.off_course_since is not None and pos.t - att.off_course_since >= d.abandon_min_s

    def _publish_update(self, att: Attempt, sp: ScoredPoint, pos: Position, track: Track) -> None:
        """approach.updated for the position, at most once per second."""
        if pos.t - att.last_update_event_t >= 1.0:
            att.last_update_event_t = pos.t
            f = track.flight
            self._publish("approach.updated", pos.t, att, {
                "callsign": f.callsign, "type_code": f.type_code,
                "along_nm": round(sp.along_nm, 2), "hat_ft": None if sp.hat_ft is None else round(sp.hat_ft),
                "gp_dev_ft": None if sp.gp_dev_ft is None else round(sp.gp_dev_ft),
                "gp_dots": self._round(self.scorer.gp_dots(sp.gp_dev_deg)), "loc_dev_ft": round(sp.loc_dev_ft),
                "loc_dots": self._round(self.scorer.loc_dots(att.runway, sp.loc_dev_deg)), "gs": sp.gs,
                "gs_corr": self._round(sp.gs_corr), "sink_fpm": sp.sink_fpm, "origin": sp.origin})

    def _left_path_t(self, att: Attempt, default: float) -> float:
        """When the flight left the path: the time of the last point within runway_change_far_ft of the attempt's
        reference, scanning back from the end; `default`, the start of the route that is handing over, if there is none."""
        far = self.d.runway_change_far_ft
        return next((p.t for p in reversed(att.points) if abs(p.cross_ft) <= far), default)

    @staticmethod
    def _drop_parallel_candidates(att: Attempt, per_hex: dict[str, Attempt]) -> None:
        """Forget the candidates on the other runways of `att`'s parallel group. They stood beside a path attempt only
        because it does not block them; once it has closed or joined a centreline nothing holds them back from
        establishing alone on a window that is stale (a quiet spell loosens its drift bound), and the parallel is blocked
        as it is for a centreline attempt."""
        for key, a in list(per_hex.items()):
            if (a.state is State.CANDIDATE and a.airport is att.airport and a.runway.ident != att.runway.ident
                    and a.runway.ident in att.runway.parallel_group):
                del per_hex[key]

    def _on_a_group_reference(self, att: Attempt, pos: Position, wx: Weather | None) -> bool:
        """Whether the position is still on some other reference of the attempt's runway or its parallels: within
        abandon_cross_nm of it and within abandon_track_tol_deg of its local course. Scored only when asked, which is
        when a path attempt is off its own reference."""
        d, rwy = self.d, att.runway
        for ref in self._refs(att.airport):
            if ref.key == att.reference.key or not (ref.runway.ident == rwy.ident or ref.runway.ident in rwy.parallel_group):
                continue
            osp = self.scorer.score(ref.runway, att.airport, pos, wx, ref=ref)
            if (abs(osp.cross_ft) <= d.abandon_cross_nm * NM_FT
                    and (osp.track_dev_deg is None or abs(osp.track_dev_deg) <= d.abandon_track_tol_deg)):
                return True
        return False

    def _switch_reference(self, att: Attempt, other: Reference, since: float) -> None:
        """The attempt settles on `other`, another reference of its runway: from the first position at or after `since`
        (where the run near it began) the positions are measured against `other`, with the METAR of each position's own
        time, and later ones are too. Earlier points keep the values they were stored with. The attempt is the same one:
        no event closes, and its number, arrival and outcome chain stand. A path it settles on is the open row's path from
        now on; a centreline leaves it with the last path flown."""
        old = att.reference
        i = len(att.raw)
        while i > 0 and att.raw[i - 1].t >= since:
            i -= 1
        tail = [self.scorer.score(att.runway, att.airport, p, self.weather.at(att.airport.metar, p.t), ref=other)
                for p in att.raw[i:]]
        if att.min_alt_point is not None:                       # the lowest point may be one of those measured again
            att.min_alt_point = next((n for o, n in zip(att.points[i:], tail) if o is att.min_alt_point), att.min_alt_point)
        att.points[i:] = tail
        att.reference = other
        if other.path is not None:
            assert att.approach_id is not None
            self.store.set_path(att.approach_id, other.name)
        att.switch_near_since.clear()
        att.switch_aligned_since.clear()
        att.off_course_since = None
        self._publish("approach.reference", att.last_t, att, {
            "from": old.name, "to": other.name, "from_label": old.label, "to_label": other.label,
            "along_nm": round(tail[0].along_nm, 2)})

    def _stamp_established_emergencies(self, pos: Position, track: Track) -> None:
        """Stamp every established attempt of the position's hex with what its flight has newly latched."""
        for att in list(self._attempts.get(pos.hex, {}).values()):
            if att.state is State.ESTABLISHED:
                self._stamp_emergency(att, att.last, pos.t, track)

    def _stamp_emergency(self, att: Attempt, sp: ScoredPoint, t: float, track: Track) -> None:
        """Stamp an open attempt: whatever the flight has newly latched since this attempt last stamped it is ORed onto
        the open row at once, and for the red flags approach.emergency is published at once too, so a hidden or filtered
        live card turns red now rather than waiting for the close."""
        new = track.flight.emergency.latched & ~att.emergency_flags
        if not new:
            return
        assert att.approach_id is not None
        att.emergency_flags |= new
        self.store.add_flags(att.approach_id, new)
        if new & EMERGENCY_MASK:
            f = track.flight
            self._publish("approach.emergency", t, att, {
                "callsign": f.callsign, "type_code": f.type_code, "traffic_class": f.traffic_class,
                "along_nm": round(sp.along_nm, 2), "hat_ft": None if sp.hat_ft is None else round(sp.hat_ft),
                "attempt_no": att.attempt_no, "arrival_id": att.arrival_id, "established_at": att.established_at,
                "in_progress": True, "emergency": flag_names(att.emergency_flags & EMERGENCY_MASK)})

    def _runway_change(self, att: Attempt, other: Reference, since: float, track: Track, per_hex: dict[str, Attempt],
                       latest: Position | None = None) -> None:
        """Close `att` as a runway change to the reference `other` of a parallel runway; the new attempt starts at the
        first position at or after `since`. `latest` is the position being processed when `att` has not taken it yet
        (the 28L candidate that establishes before the path attempt is visited in a pass): the new attempt takes it too.

        A centreline attempt keeps all it has and ends with the last position. A path attempt keeps only the points
        before `since` and ends with the last of them, so that the stretch flown on the way across is stored once, as the
        parallel's; and `since` is no earlier than the position after the one it was established on, so that it never
        ends before it began (whatever a route falls back to when the flight was never near the path)."""
        from_path = att.reference.path is not None
        if from_path and att.established_at is not None:
            since = max(since, next((p.t for p in att.points if p.t > att.established_at), math.inf))
        i = len(att.raw)
        while i > 0 and att.raw[i - 1].t >= since:          # the positions since it came near the other runway
            i -= 1
        raw = att.raw[i:]
        if latest is not None and (not raw or raw[-1] is not latest):
            raw = raw + [latest]
        f = track.flight
        new = Attempt(hex=att.hex, runway=other.runway, airport=att.airport, state=State.ESTABLISHED, points=[], raw=[],
                      last_t=raw[-1].t, flight_key=att.flight_key, flight_id=f.id, sidestep=True,
                      arrival_id=att.arrival_id, attempt_no=att.attempt_no + 1, prev_approach_id=att.approach_id,
                      weather_id=att.weather_id, type_code=f.type_code, traffic_class=f.traffic_class, reference=other)
        for p in raw:
            new.add(self.scorer.score(other.runway, att.airport, p, self.weather.at(att.airport.metar, p.t), ref=other), p)
        new.established_at = raw[0].t
        new.established_hat_ft = new.points[0].hat_ft
        new.armed = att.armed
        new.approach_id = self.store.open_approach(
            run_id=self.run_id, flight_id=f.id, arrival_id=new.arrival_id, attempt_no=new.attempt_no, hex=new.hex,
            callsign=f.callsign, type_code=f.type_code, traffic_class=f.traffic_class, approach_cat=f.approach_cat,
            airport=att.airport.icao, runway=other.runway.ident, established_at=new.established_at,
            first_along_nm=max(p.along_nm for p in new.points), weather_id=new.weather_id,
            reference_source=other.runway.reference_source, prev_approach_id=att.approach_id, path=other.name)
        # Stamped at open too (approach.started below carries the red ones): a runway change opens a new row the same
        # as a fresh establishment does, so it gets whatever the flight has latched so far the same way.
        new.emergency_flags = f.emergency.latched
        if new.emergency_flags:
            self.store.add_flags(new.approach_id, new.emergency_flags)
        assert att.approach_id is not None
        self._publish("approach.runway_change", raw[-1].t, att, {"to_approach_id": new.approach_id,
                       "from_runway": att.runway.ident, "to_runway": other.runway.ident})
        if from_path:
            del att.points[i:], att.raw[i:]
            att.last_t = att.points[-1].t
        self._close(att, per_hex, Outcome.RUNWAY_CHANGE, att.points[-1].t if from_path else raw[-1].t)
        per_hex[new.key] = new
        self._publish("approach.started", new.established_at, new, {"callsign": f.callsign, "type_code": f.type_code,
                       "traffic_class": f.traffic_class,
                       "along_nm": round(new.points[0].along_nm, 2),
                       "hat_ft": None if new.established_hat_ft is None else round(new.established_hat_ft),
                       "attempt_no": new.attempt_no, "arrival_id": new.arrival_id, "sidestep": True,
                       "origin": new.points[0].origin, "emergency": flag_names(new.emergency_flags & EMERGENCY_MASK)})

    # ------------------------------------------------------------------ close
    def _close_lost(self, att: Attempt, per_hex: dict[str, Attempt]) -> None:
        last = att.last
        vr = last.vertical_rate
        if att.touched:
            self._close(att, per_hex, Outcome.LANDED, last.t, decided_by=last)
        elif (last.hat_ft is not None and last.hat_ft <= self.d.presumed_landed_max_height_ft
              and last.along_nm <= self.d.presumed_landed_max_along_nm and (vr is None or vr <= 300)):
            self._close(att, per_hex, Outcome.LOST_ON_FINAL, last.t, extra_flags=Flags.PRESUMED_LANDED)
        else:
            self._close(att, per_hex, Outcome.LOST, last.t)

    def _close(self, att: Attempt, per_hex: dict[str, Attempt], outcome: Outcome, ended_at: float, *,
               ga: dict | None = None, extra_flags: Flags = Flags.NONE, decided_by: ScoredPoint | None = None) -> None:
        """Close an attempt. `decided_by` is the point the outcome rests on, given for a verdict, an abandon and a lost
        attempt's touched landing. When it is adsb.lol's and the close carries EXTERNAL_TAIL (the receiver was silent
        for longer than lost_timeout_s, or lost the aircraft before a go-around's bottom, not just in a hole adsb.lol
        filled), the outcome is flagged EXTERNAL_OUTCOME and a landing is confirmed by adsb.lol. A runway change and a
        loss pass none."""
        assert att.approach_id is not None and att.arrival_id is not None
        rwy = att.runway
        gates = self.scorer.gates(rwy, att.points)
        # The fleet deltas are measured before the flags, which read them: FAST and SLOW come from the speed delta.
        fleet_key = self.scorer.fleet_deltas(att.airport.icao, rwy.ident, gates, type_code=att.type_code,
                                             traffic_class=att.traffic_class)
        summary = self.scorer.summarize(att.points)
        # att.emergency_flags is current as of the airframe's latest position (process stamps every established attempt
        # of the hex first, ahead of tick() and of every outcome rule), so it already carries a flag latched on the very
        # position that closes this attempt, and the scorer knows nothing of it either way.
        flags = self.scorer.flags(rwy, gates, summary, established_hat_ft=att.established_hat_ft, sidestep=att.sidestep,
                                  temp_uncorrected=att.temp_uncorrected, qnh_assumed=att.qnh_assumed,
                                  fleet_key=fleet_key) | extra_flags | att.emergency_flags
        flags |= external_flags(att.points, self.d.lost_timeout_s, None if ga is None else ga.get("initiated_at"))
        confirmed_by = None
        if decided_by is not None and decided_by.origin != "local" and Flags.EXTERNAL_TAIL in flags:
            flags |= Flags.EXTERNAL_OUTCOME
            confirmed_by = "adsb.lol" if outcome is Outcome.LANDED else None
        stored = [p for p in att.points if p.along_nm <= self.d.score_max_along_nm]
        last_path = next((p.reference for p in reversed(att.points) if p.reference is not None), None)
        self.store.close_approach(att.approach_id, outcome=outcome, flags=flags, ended_at=ended_at, summary=summary.as_row(),
                                  gates=[g.as_row() for g in gates], points=[p.as_row() for p in stored],
                                  bins=self.scorer.bins(att.points), ga=ga, fleet_key=fleet_key,
                                  landed_confirmed_by=confirmed_by, path=last_path)
        self._publish_close(att, outcome, ended_at, flags, summary, gates, ga)
        self._hold_closed(att, per_hex, outcome, ended_at, flags, ga)

    def _publish_close(self, att: Attempt, outcome: Outcome, ended_at: float, flags: Flags, summary: Summary,
                       gates: list[Gate], ga: dict | None) -> None:
        """approach.completed for the closed attempt, after approach.go_around for a go-around or touch-and-go."""
        payload = {"outcome": outcome.value, "flags": flag_names(flags), "runway": att.runway.ident,
                   "airport": att.airport.icao, "attempt_no": att.attempt_no, "arrival_id": att.arrival_id,
                   "lowest_hat_ft": summary.lowest_hat_ft, "closest_along_nm": summary.closest_along_nm,
                   "gate1000": next((g.as_row() for g in gates if g.gate_ft == 1000), None)}
        if outcome in (Outcome.GO_AROUND, Outcome.TOUCH_AND_GO):
            self._publish("approach.go_around", ended_at, att, {**payload, **(ga or {})})
        self._publish("approach.completed", ended_at, att, payload)

    def _hold_closed(self, att: Attempt, per_hex: dict[str, Attempt], outcome: Outcome, ended_at: float, flags: Flags,
                     ga: dict | None) -> None:
        """What the matcher keeps of an attempt it has closed: its place in the arrival chain, its ClosedInfo for the
        reappearance and confirmation rules, the removal of the attempt itself (a path attempt takes the candidates on
        its parallel runways with it, `_drop_parallel_candidates`) and, for a go-around or touch-and-go, the
        climb-out."""
        assert att.approach_id is not None and att.arrival_id is not None
        self._arrivals[att.flight_key] = ArrivalInfo(att.arrival_id, att.approach_id, ended_at, att.attempt_no, att.airport.icao)
        # Every close is held, not only a lost one: a 30 s emergency run straddles a landing or a go-around as easily as
        # a loss, and the after-close stamp (_stamp_closed_emergency) needs the attempt it straddled. Only a lost
        # attempt goes on to the reappearance and confirmation rules; any other is held already settled, so they leave
        # it alone.
        lost = outcome in (Outcome.LOST, Outcome.LOST_ON_FINAL)
        self._closed[att.hex] = ClosedInfo(att.approach_id, att.flight_id, att.airport, outcome, flags, ended_at, att.min_alt_baro,
                                           arrival_id=att.arrival_id, attempt_no=att.attempt_no, runway=att.runway.ident,
                                           confirmed=not lost, reclassified=not lost)
        per_hex.pop(att.key, None)
        if att.reference.path is not None:
            self._drop_parallel_candidates(att, per_hex)
        if ga is not None and ga["initiated_at"] is not None:        # a go-around or a touch-and-go
            self._open_climb_out(att, ga, flags, ended_at)

    def _open_climb_out(self, att: Attempt, ga: dict, flags: Flags, ended_at: float) -> None:
        """The rule of the retired trace backfill's `_flown_end`: the climb-out runs until the aircraft has flown max(D,
        1 nm) since the go-around began, D being `initiated_along_nm`, counted from the attempt's own points; none is
        opened when the close itself already covers that."""
        target_ft = max(ga["initiated_along_nm"] or 0.0, 0.0, 1.0) * NM_FT
        flown_ft = _flown_ft(att.points, ga["initiated_at"])
        if flown_ft >= target_ft:
            return
        last = att.points[-1]
        assert att.approach_id is not None and att.arrival_id is not None
        self._climb_outs[att.hex] = ClimbOut(
            approach_id=att.approach_id, flight_id=att.flight_id, reference=att.reference, airport=att.airport,
            arrival_id=att.arrival_id, attempt_no=att.attempt_no, flags=flags, target_ft=target_ft, flown_ft=flown_ft,
            last_lat=last.lat, last_lon=last.lon, last_t=last.t,
            until=ended_at + self.cfg.external.watch_max_min * 60.0)

    def _feed_climb_out(self, pos: Position) -> None:
        """Store a position of a hex with an open climb-out on its closed approach, scored for the chart only (it
        changes no gate, flag, summary bin or outcome) against the reference the attempt closed on, a path approach's
        path included, so the climb-out's points carry on in the frame of the points before them and name the same
        reference. Every position flown counts towards the distance, one beyond score_max_along_nm included, though that
        one is not stored. The climb-out ends once the distance is flown, publishing approach.extended once if it stored
        anything, and without storing anything when the aircraft was last taken more than lost_timeout_s before: with
        the merge off, or adsb.lol silent, the aircraft that reappears minutes later is no longer on this climb-out, and
        the distance across the gap is not flown."""
        co = self._climb_outs.get(pos.hex)
        if co is None or pos.t <= co.last_t:
            return
        if pos.t > co.until or pos.t - co.last_t > self.d.lost_timeout_s:
            del self._climb_outs[pos.hex]
            return
        ref = co.reference
        sp = self.scorer.score(ref.runway, co.airport, pos, self.weather.at(co.airport.metar, pos.t), ref=ref)
        co.flown_ft += distance_ft(co.last_lat, co.last_lon, pos.lat, pos.lon)
        co.last_lat, co.last_lon, co.last_t = pos.lat, pos.lon, pos.t
        if sp.along_nm <= self.d.score_max_along_nm:
            self.store.add_points(co.approach_id, [sp.as_row()], charts_only=True)
            co.added += 1
        if co.flown_ft < co.target_ft:
            return
        del self._climb_outs[pos.hex]
        if co.added:
            self.events.publish("approach.extended", pos.t, co.approach_id, co.flight_id, {
                "hex": pos.hex, "airport": co.airport.icao, "runway": ref.runway.ident, "arrival_id": co.arrival_id,
                "attempt_no": co.attempt_no, "points_added": co.added, "gap_points_added": 0,
                "flags": flag_names(co.flags)})

    # ------------------------------------------------- reappearance, confirmation
    def _after_close_checks(self, pos: Position, track: Track) -> None:
        """The two rules for a hex whose attempt has closed: a lost one that reappears go_around_min_climb_ft above the
        lowest altitude it flew is reclassified as a go-around or touch-and-go; one still unsettled is confirmed as a
        landing by a ground report or a departure."""
        info = self._closed.get(pos.hex)
        if info is None:
            return
        self._stamp_closed_emergency(pos, track, info)
        if info.reclassified and info.confirmed:
            return                    # settled: neither rule below can change it, so skip their per-position work
        dt = pos.t - info.ended_at
        dist_nm = distance_ft(info.airport.lat, info.airport.lon, pos.lat, pos.lon) / NM_FT
        if self._reappeared_above_its_lowest(info, pos, dt, dist_nm):
            self._reclassify_as_go_around(info, pos)
            return
        if info.confirmed or info.reclassified:
            return
        by = self._landing_confirmed_by(info, pos, track, dt, dist_nm)
        if by is not None:
            self._confirm_landing(info, pos, by)

    def _reappeared_above_its_lowest(self, info: ClosedInfo, pos: Position, dt: float, dist_nm: float) -> bool:
        """A lost attempt's aircraft reappearing within reappear_go_around_s of the close, airborne near the airport and
        go_around_min_climb_ft above the lowest altitude the attempt flew."""
        d = self.d
        return (not info.reclassified and info.outcome in (Outcome.LOST, Outcome.LOST_ON_FINAL)
                and dt <= d.reappear_go_around_s and not pos.on_ground and dist_nm <= d.reappear_max_nm
                and pos.alt_baro is not None and info.min_alt_baro is not None
                and pos.alt_baro >= info.min_alt_baro + d.go_around_min_climb_ft)

    def _reclassify_as_go_around(self, info: ClosedInfo, pos: Position) -> None:
        """Reclassify the closed attempt as a touch-and-go (ground contact on its runway was seen) or a go-around, with
        approach.reclassified."""
        # The climb outranks an earlier ground confirmation and withdraws it: ground contact on this approach's runway
        # followed by a climb is a touch-and-go; a ground report anywhere else was not its landing.
        prev_outcome = info.outcome
        outcome = Outcome.TOUCH_AND_GO if info.ground_in_footprint else Outcome.GO_AROUND
        info.flags = (info.flags & ~(Flags.PRESUMED_LANDED | Flags.LANDING_CONFIRMED)) | Flags.INFERRED_GO_AROUND
        info.outcome = outcome
        info.reclassified = True
        info.confirmed = False
        self.store.reclassify(info.approach_id, outcome, info.flags)
        self.events.publish("approach.reclassified", pos.t, info.approach_id, info.flight_id,
                            {"from": prev_outcome.value, "outcome": outcome.value, "flags": flag_names(info.flags),
                             "landed_confirmed_by": None, "hex": pos.hex, "airport": info.airport.icao,
                             "runway": info.runway, "arrival_id": info.arrival_id, "attempt_no": info.attempt_no})

    def _landing_confirmed_by(self, info: ClosedInfo, pos: Position, track: Track, dt: float,
                              dist_nm: float) -> str | None:
        """What confirms the landing, if anything: "ground", a ground report near the airport soon after the close, or
        "departure", the aircraft airborne and climbing near the airport after the go-around window."""
        d = self.d
        if pos.on_ground and dt <= d.landed_confirm_ground_min * 60.0 and dist_nm <= d.landed_confirm_departure_nm:
            return "ground"
        if (d.reappear_go_around_s < dt <= d.landed_confirm_departure_h * 3600.0 and not pos.on_ground
                and dist_nm <= d.landed_confirm_departure_nm and self._climbing(pos, track)):
            return "departure"
        return None

    def _confirm_landing(self, info: ClosedInfo, pos: Position, by: str) -> None:
        """Confirm the closed attempt's landing, with approach.confirmed. A ground report also notes on `info` whether
        it lay in the approach's runway footprint, which makes a later climb a touch-and-go."""
        if by == "ground":
            rwy = info.airport.runways.get(info.runway)
            info.ground_in_footprint = (rwy is not None
                                        and self._in_footprint(rwy, self.scorer.score(rwy, info.airport, pos, None)))
        info.confirmed = True
        info.flags |= Flags.LANDING_CONFIRMED
        self.store.set_confirmation(info.approach_id, by, info.flags)
        self.events.publish("approach.confirmed", pos.t, info.approach_id, info.flight_id,
                            {"hex": pos.hex, "airport": info.airport.icao, "runway": info.runway,
                             "outcome": info.outcome.value, "flags": flag_names(info.flags), "landed_confirmed_by": by,
                             "arrival_id": info.arrival_id, "attempt_no": info.attempt_no})

    def _stamp_closed_emergency(self, pos: Position, track: Track, info: ClosedInfo) -> None:
        """Stamp a closed attempt: a 30 s run can straddle a close, so the flight's most recently closed attempt for
        this hex -- the one _establish pops this ClosedInfo for the moment a new one supersedes it -- gets whatever
        latches after it too. Both the stored row (add_flags) and this in-memory copy (info.flags) are updated, before
        reclassify or set_confirmation below can write back a stale copy of it; the store's own sticky rule is the
        second line of defence for the same trap.

        Only while this hex is still flying the attempt's own flight: a ClosedInfo outlives it by up to
        landed_confirm_departure_h, so that the airframe's next departure can confirm the landing. But "flight" is
        the tracker's session, not a physical journey: an aircraft whose callsign is its N-number keeps one Flight,
        and one EmergencyWatch, across a landing and a departure less than session_gap_s later, so flight_id alone
        does not tell a run that straddled this close from one that only starts once the aircraft is airborne
        again. This stamp exists for the straddling case only, so a newly latched flag is kept here solely when its run
        started at or before this attempt's own ended_at; watch.latched_at is where the run's start survives for this
        comparison, since latched itself remembers only that a flag is set, not since when."""
        if info.flight_id != track.flight.id:
            return
        watch = track.flight.emergency
        candidate = watch.latched & ~info.flags
        new = Flags.NONE
        for f in Flags:
            if f in candidate and watch.latched_at.get(f, math.inf) <= info.ended_at:
                new |= f
        if not new:
            return
        self.store.add_flags(info.approach_id, new)
        info.flags |= new
        if new & EMERGENCY_MASK:
            self._publish_closed("approach.emergency", pos.t, info, pos.hex, {
                "callsign": None, "type_code": None, "traffic_class": None, "along_nm": None, "hat_ft": None,
                "attempt_no": info.attempt_no, "arrival_id": info.arrival_id, "established_at": None,
                "in_progress": False, "emergency": flag_names(info.flags & EMERGENCY_MASK)})

    # ------------------------------------------------------------------ util
    @staticmethod
    def _climbing(pos: Position, track: Track) -> bool:
        """Vertical rate above +300 fpm, or +200 ft within 60 s against the last non-coarse position when no rate is
        broadcast: a TIS-B position between them is cruder and sometimes stale, and must not stand in for it."""
        vr = pos.vertical_rate
        if vr is not None:
            return vr > 300
        if pos.alt_baro is None:
            return False
        prev = next((p for p in list(track.history)[-2::-1] if not coarse(p)), None)
        return prev is not None and prev.alt_baro is not None and 0 < pos.t - prev.t <= 60.0 and pos.alt_baro - prev.alt_baro >= 200

    def _publish(self, kind: str, t: float, att: Attempt, payload: dict) -> None:
        self.events.publish(kind, t, att.approach_id, att.flight_id, {"hex": att.hex, "runway": att.runway.ident,
                            "airport": att.airport.icao, "path": att.reference.name, "path_label": att.reference.label,
                            **payload})

    def _publish_closed(self, kind: str, t: float, info: ClosedInfo, hexid: str, payload: dict) -> None:
        """_publish's counterpart for an attempt the matcher only still holds as a ClosedInfo: ClosedInfo carries no hex
        of its own, so the caller passes the one it looked it up by."""
        self.events.publish(kind, t, info.approach_id, info.flight_id, {"hex": hexid, "runway": info.runway,
                            "airport": info.airport.icao, **payload})

    @staticmethod
    def _round(v: float | None) -> float | None:
        return None if v is None else round(v, 2)

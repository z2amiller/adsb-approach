"""Per-point scoring, gates, summary, stability flags, distance bins."""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .altitude import P0_HPA, headwind_kt, qnh_altitude_ft, sigma, temperature_scaled_ft
from .config import Config, StabilityCfg
from .geometry import NM_FT, gpip_along_ft, loc_width_deg, localizer_to_threshold_ft, runway_frame, track_diff_deg
from .model import Airport, Flags, Position, RunwayEnd, Weather
from .reference import Frame, Reference

if TYPE_CHECKING:                     # fleet.py imports this module, so its snapshot types are annotations only
    from .fleet import FleetNorms, Norm

GS_DOT_DEG = 0.35


@dataclass(slots=True)
class RunwayGeom:
    tan_gpa: float
    gpip_along_ft: float
    loc_to_thr_ft: float
    loc_width_deg: float


def runway_geom(rwy: RunwayEnd) -> RunwayGeom:
    return RunwayGeom(tan_gpa=math.tan(math.radians(rwy.glide_path_deg)), gpip_along_ft=gpip_along_ft(rwy),
                      loc_to_thr_ft=localizer_to_threshold_ft(rwy), loc_width_deg=loc_width_deg(rwy))


@dataclass(slots=True)
class ScoredPoint:
    t: float
    lat: float
    lon: float
    alt_baro: int | None
    alt_geom: int | None
    alt_qnh: float | None
    alt_corr: float | None
    temp_corrected: bool
    qnh_assumed: bool
    gs: float | None
    track: float | None
    baro_rate: int | None
    geom_rate: int | None
    on_ground: bool
    along_ft: float
    cross_ft: float
    hat_ft: float | None
    gp_dev_ft: float | None
    gp_dev_deg: float | None
    loc_dev_ft: float
    loc_dev_deg: float | None
    gs_corr: float | None
    sink_fpm: float | None
    nav_altitude_mcp: int | None
    nav_qnh: float | None
    nav_modes: frozenset[str]
    source: str                   # readsb's position type: adsb_icao, mlat, ...
    origin: str = "local"         # where the point came from: 'local', the local receiver, or 'adsb.lol'
    track_dev_deg: float | None = None    # track less the reference's local course, signed; None without a track. Not stored
    reference: str | None = None  # the procedure path the point was measured against; None is the runway's centreline

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ScoredPoint:
        """A stored approach_points row as the point it was stored from, so the scorer can run again from stored points.
        Points stored before migration 0002 have no on_ground or qnh_assumed, and read as False; a row from a database
        before migration 0008 has no `reference` and reads as the centreline. track_dev_deg is not stored, so it is None."""
        modes = row["nav_modes"]
        return cls(
            t=row["t"], lat=row["lat"], lon=row["lon"], alt_baro=row["alt_baro"], alt_geom=row["alt_geom"],
            alt_qnh=row["alt_qnh"], alt_corr=row["alt_corr"], temp_corrected=bool(row["temp_corrected"]),
            qnh_assumed=bool(row["qnh_assumed"]), gs=row["gs"], track=row["track"], baro_rate=row["baro_rate"],
            geom_rate=row["geom_rate"], on_ground=bool(row["on_ground"]), along_ft=row["along_nm"] * NM_FT,
            cross_ft=row["cross_ft"], hat_ft=row["hat_ft"], gp_dev_ft=row["gp_dev_ft"], gp_dev_deg=row["gp_dev_deg"],
            loc_dev_ft=row["loc_dev_ft"], loc_dev_deg=row["loc_dev_deg"], gs_corr=row["gs_corr"], sink_fpm=row["sink_fpm"],
            nav_altitude_mcp=row["nav_altitude_mcp"], nav_qnh=row["nav_qnh"],
            nav_modes=frozenset(modes.split(",")) if modes else frozenset(), source=row["source"] or "unknown",
            origin=row["origin"], reference=row["reference"] if "reference" in row.keys() else None)

    @property
    def along_nm(self) -> float:
        return self.along_ft / NM_FT

    @property
    def vertical_rate(self) -> int | None:
        return self.baro_rate if self.baro_rate is not None else self.geom_rate

    def as_row(self) -> dict:
        """The approach_points row (store.db POINT_COLUMNS): its `source` is the readsb position type, and its `origin`
        this point's origin."""
        return dict(t=self.t, lat=self.lat, lon=self.lon, alt_baro=self.alt_baro, alt_geom=self.alt_geom, alt_qnh=self.alt_qnh,
                    alt_corr=self.alt_corr, temp_corrected=self.temp_corrected, qnh_assumed=self.qnh_assumed,
                    on_ground=self.on_ground, gs=self.gs, track=self.track,
                    baro_rate=self.baro_rate, geom_rate=self.geom_rate, along_nm=self.along_nm, cross_ft=self.cross_ft,
                    hat_ft=self.hat_ft, gp_dev_ft=self.gp_dev_ft, gp_dev_deg=self.gp_dev_deg, loc_dev_ft=self.loc_dev_ft,
                    loc_dev_deg=self.loc_dev_deg, gs_corr=self.gs_corr, sink_fpm=self.sink_fpm,
                    nav_altitude_mcp=self.nav_altitude_mcp, nav_qnh=self.nav_qnh,
                    nav_modes=",".join(sorted(self.nav_modes)) or None, source=self.source, origin=self.origin,
                    reference=self.reference)


def correct_altitude(pos: Position, airport: Airport, wx: Weather | None, temp_max_age_s: float,
                     qnh_max_age_s: float) -> tuple[float | None, float | None, bool, bool]:
    """(alt_qnh, alt_corr, temp_corrected, qnh_assumed); (None, None, False, False) on the ground or without a baro
    altitude. Without a METAR altimeter setting observed within qnh_max_age_s the height rests on standard pressure,
    about 27 ft per hPa off, and qnh_assumed says so."""
    if pos.on_ground or pos.alt_baro is None:
        return None, None, False, False
    qnh, qnh_assumed = P0_HPA, True
    if wx is not None and wx.altim_hpa and 0 <= pos.t - wx.observed_at <= qnh_max_age_s:
        qnh, qnh_assumed = wx.altim_hpa, False
    alt_qnh = qnh_altitude_ft(pos.alt_baro, qnh)
    if wx is not None and wx.temp_c is not None and 0 <= pos.t - wx.observed_at <= temp_max_age_s:
        return alt_qnh, temperature_scaled_ft(alt_qnh, airport.elevation_ft, wx.temp_c), True, qnh_assumed
    return alt_qnh, alt_qnh, False, qnh_assumed


def score_point(rwy: RunwayEnd, geom: RunwayGeom, airport: Airport, pos: Position, wx: Weather | None,
                temp_max_age_s: float, qnh_max_age_s: float, frame: Frame | None = None,
                reference: str | None = None) -> ScoredPoint:
    """`frame` is the position's frame against a procedure path (reference.Reference.frame); without one the runway's
    centreline is the reference. Along, cross, the localizer deviation and the glide-path height are measured in it and
    the headwind along its local course; lat, lon and track stay the aircraft's own. `reference` names the path."""
    if frame is None:
        along, cross = runway_frame(rwy, pos.lat, pos.lon)
        course = rwy.course_true_deg
    else:
        along, cross, course = frame.along_ft, frame.cross_ft, frame.course_deg
    alt_qnh, alt_corr, temp_corrected, qnh_assumed = correct_altitude(pos, airport, wx, temp_max_age_s, qnh_max_age_s)
    hat: float | None
    gp_dev_ft: float | None
    gp_dev_deg: float | None
    if pos.on_ground:
        hat, gp_dev_ft, gp_dev_deg = 0.0, None, None
    elif alt_corr is None:
        hat, gp_dev_ft, gp_dev_deg = None, None, None
    else:
        hat = alt_corr - rwy.threshold_elev_ft
        h_ref = rwy.tch_ft + max(along, 0.0) * geom.tan_gpa
        gp_dev_ft = hat - h_ref
        dist_from_gpip = along - geom.gpip_along_ft
        gp_dev_deg = (math.degrees(math.atan2(hat, dist_from_gpip)) - rwy.glide_path_deg) if dist_from_gpip > 0 else None
    loc_dist = geom.loc_to_thr_ft + along
    loc_dev_deg = math.degrees(math.atan2(cross, loc_dist)) if loc_dist > 0 else None
    headwind = 0.0
    if wx is not None and wx.wind_dir is not None and wx.wind_kt is not None:
        headwind = headwind_kt(wx.wind_dir, wx.wind_kt, course)
    gs_corr = None if pos.gs is None else pos.gs + headwind
    vr = pos.vertical_rate
    return ScoredPoint(
        t=pos.t, lat=pos.lat, lon=pos.lon, alt_baro=pos.alt_baro, alt_geom=pos.alt_geom, alt_qnh=alt_qnh, alt_corr=alt_corr,
        temp_corrected=temp_corrected, qnh_assumed=qnh_assumed, gs=pos.gs, track=pos.track, baro_rate=pos.baro_rate,
        geom_rate=pos.geom_rate,
        on_ground=pos.on_ground, along_ft=along, cross_ft=cross, hat_ft=hat, gp_dev_ft=gp_dev_ft, gp_dev_deg=gp_dev_deg,
        loc_dev_ft=cross, loc_dev_deg=loc_dev_deg, gs_corr=gs_corr, sink_fpm=None if vr is None else float(-vr),
        nav_altitude_mcp=pos.nav_altitude_mcp, nav_qnh=pos.nav_qnh, nav_modes=pos.nav_modes, source=pos.source,
        origin=pos.origin, track_dev_deg=None if pos.track is None else track_diff_deg(pos.track, course),
        reference=reference,
    )


@dataclass(slots=True)
class Gate:
    gate_ft: int
    observed: bool
    t: float | None = None
    along_nm: float | None = None
    gp_dev_ft: float | None = None
    gp_dev_deg: float | None = None
    gp_dots: float | None = None
    loc_dev_ft: float | None = None
    loc_dev_deg: float | None = None
    loc_dots: float | None = None
    gs: float | None = None
    gs_corr: float | None = None
    ias_est: float | None = None
    sink_fpm: float | None = None
    nav_modes: str | None = None
    fleet_gp_delta_ft: float | None = None
    fleet_gs_delta_kt: float | None = None

    def as_row(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}   # type: ignore[attr-defined]


def _lerp(a: float | None, b: float | None, f: float) -> float | None:
    return None if a is None or b is None else a + (b - a) * f


def interpolate_gate(points: list[ScoredPoint], gate_ft: int, max_bracket_s: float, loc_width: float) -> Gate:
    """Gate at the first descending crossing of gate_ft; unobserved when there is none or the bracket is too wide."""
    prev: ScoredPoint | None = None
    for p in points:
        if p.on_ground or p.hat_ft is None:
            prev = None
            continue
        if prev is not None and prev.hat_ft is not None and prev.hat_ft >= gate_ft > p.hat_ft:
            if p.t - prev.t > max_bracket_s:
                return Gate(gate_ft, False)
            f = (prev.hat_ft - gate_ft) / (prev.hat_ft - p.hat_ft)
            gp_deg = _lerp(prev.gp_dev_deg, p.gp_dev_deg, f)
            loc_deg = _lerp(prev.loc_dev_deg, p.loc_dev_deg, f)
            gs_corr = _lerp(prev.gs_corr, p.gs_corr, f)
            alt_corr = _lerp(prev.alt_corr, p.alt_corr, f)
            along = _lerp(prev.along_ft, p.along_ft, f)
            return Gate(
                gate_ft=gate_ft, observed=True, t=prev.t + (p.t - prev.t) * f,
                along_nm=None if along is None else along / NM_FT,
                gp_dev_ft=_lerp(prev.gp_dev_ft, p.gp_dev_ft, f), gp_dev_deg=gp_deg,
                gp_dots=None if gp_deg is None else gp_deg / GS_DOT_DEG,
                loc_dev_ft=_lerp(prev.loc_dev_ft, p.loc_dev_ft, f), loc_dev_deg=loc_deg,
                loc_dots=None if loc_deg is None else loc_deg / (loc_width / 4.0),
                gs=_lerp(prev.gs, p.gs, f), gs_corr=gs_corr,
                ias_est=None if gs_corr is None or alt_corr is None else gs_corr * math.sqrt(sigma(alt_corr)),
                sink_fpm=_lerp(prev.sink_fpm, p.sink_fpm, f),
                nav_modes=",".join(sorted(prev.nav_modes)) or None,
            )
        prev = p
    return Gate(gate_ft, False)


@dataclass(slots=True)
class Summary:
    duration_s: float = 0.0
    first_along_nm: float | None = None
    closest_along_nm: float | None = None
    lowest_hat_ft: float | None = None
    points: int = 0
    descent_angle_deg: float | None = None
    gp_dev_mean_ft: float | None = None
    gp_dev_rms_ft: float | None = None
    gp_dev_max_abs_ft: float | None = None
    loc_dev_rms_ft: float | None = None
    loc_dev_max_abs_ft: float | None = None
    gs_at_4nm: float | None = None
    gs_at_2nm: float | None = None
    sink_max_fpm: float | None = None

    def as_row(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}   # type: ignore[attr-defined]


def _gs_at(points: list[ScoredPoint], along_nm: float, max_bracket_s: float) -> float | None:
    """Ground speed where the track first crosses along_nm inbound, interpolated; None, like an unobserved gate, when the
    two points around the crossing are more than max_bracket_s apart."""
    prev: ScoredPoint | None = None
    for p in points:
        if prev is not None and prev.along_nm >= along_nm > p.along_nm and prev.gs is not None and p.gs is not None:
            if p.t - prev.t > max_bracket_s:
                return None
            f = (prev.along_nm - along_nm) / (prev.along_nm - p.along_nm)
            return prev.gs + (p.gs - prev.gs) * f
        prev = p
    return None


def _lowest_hat_ft(points: list[ScoredPoint], airborne: list[ScoredPoint]) -> float | None:
    """The lowest height above the threshold the track reached: 0 for an approach with any point on the ground, else
    the lowest airborne point's, and None when no point has a height."""
    if any(p.on_ground for p in points):
        return 0.0
    if airborne:
        return min(p.hat_ft for p in airborne)          # type: ignore[type-var]
    return None


def _fill_deviation_stats(s: Summary, window: list[ScoredPoint]) -> None:
    """The deviation statistics of the points in the stats window, set on `s`: the glide-path deviation's mean, RMS and
    largest magnitude over the points that have one, the localizer deviation's RMS and largest magnitude over all of
    them. Each stays None when no point contributes."""
    gp = [p.gp_dev_ft for p in window if p.gp_dev_ft is not None]
    loc = [p.loc_dev_ft for p in window]
    if gp:
        s.gp_dev_mean_ft = sum(gp) / len(gp)
        s.gp_dev_rms_ft = math.sqrt(sum(v * v for v in gp) / len(gp))
        s.gp_dev_max_abs_ft = max(abs(v) for v in gp)
    if loc:
        s.loc_dev_rms_ft = math.sqrt(sum(v * v for v in loc) / len(loc))
        s.loc_dev_max_abs_ft = max(abs(v) for v in loc)


def _descent_angle_deg(window: list[ScoredPoint]) -> float | None:
    """The angle the aircraft actually descended at, from a least-squares line of height above the threshold against
    distance along, over the stats-window points before the threshold. None unless there are at least three such
    points spanning at least half a mile, enough for the line to mean anything."""
    fit = [p for p in window if p.along_ft > 0]
    if len(fit) >= 3 and (max(p.along_ft for p in fit) - min(p.along_ft for p in fit)) >= 0.5 * NM_FT:
        n = len(fit)
        mx = sum(p.along_ft for p in fit) / n
        my = sum(p.hat_ft for p in fit) / n                         # type: ignore[misc]
        var = sum((p.along_ft - mx) ** 2 for p in fit)
        cov = sum((p.along_ft - mx) * (p.hat_ft - my) for p in fit)  # type: ignore[operator]
        if var > 0:
            return math.degrees(math.atan(cov / var))
    return None


def _sink_max_fpm(airborne: list[ScoredPoint]) -> float | None:
    """The fastest sink rate below 1500 ft above the threshold; None when no point down there has a sink rate."""
    sinks = [p.sink_fpm for p in airborne if p.sink_fpm is not None and p.hat_ft is not None and p.hat_ft < 1500]
    return max(sinks) if sinks else None


def summarize(points: list[ScoredPoint], stats_max_along_nm: float = 6.0, max_bracket_s: float = math.inf) -> Summary:
    """The summary of an approach's scored points; the all-default Summary for none. From every point: the duration,
    the point count, the farthest and closest distance along, and the lowest height above the threshold (0 when any
    point is on the ground). From the airborne points from the threshold out to stats_max_along_nm on the approach
    side: the deviation statistics and the descent angle. From all the airborne points: the ground speeds at 4 and
    2 nm, each None when the points around its crossing are more than max_bracket_s apart, and the fastest sink rate
    below 1500 ft above the threshold."""
    if not points:
        return Summary()
    airborne = [p for p in points if not p.on_ground and p.hat_ft is not None]
    s = Summary(duration_s=points[-1].t - points[0].t, points=len(points),
                first_along_nm=max(p.along_nm for p in points), closest_along_nm=min(p.along_nm for p in points))
    s.lowest_hat_ft = _lowest_hat_ft(points, airborne)
    window = [p for p in airborne if 0.0 <= p.along_nm <= stats_max_along_nm]
    _fill_deviation_stats(s, window)
    s.descent_angle_deg = _descent_angle_deg(window)
    s.gs_at_4nm = _gs_at(airborne, 4.0, max_bracket_s)
    s.gs_at_2nm = _gs_at(airborne, 2.0, max_bracket_s)
    s.sink_max_fpm = _sink_max_fpm(airborne)
    return s


def key_may_flag(fleet_key: str | None) -> bool:
    """Whether a fleet key is specific enough to set FAST or SLOW: true only for a `type:` or `class:` key, never for
    the all-aircraft `*` or for no key at all, since the all-aircraft spread is about twice a single type's and the
    flag would be noise. fleet.key_text guarantees those are the only two prefixed forms a key takes, so a prefix test
    is enough; scorer does not import fleet, which imports scorer."""
    return fleet_key is not None and (fleet_key.startswith("type:") or fleet_key.startswith("class:"))


def speed_flags(gate: "Gate", cfg: StabilityCfg, *, may_flag: bool) -> Flags:
    """FAST and SLOW for one gate, from its speed delta against the norm. The only place the two thresholds are read: a
    live close applies it through stability_flags, a splice through the same path, and reflag over stored gates
    directly, so a change to the rule reaches all three. `may_flag` is key_may_flag of the norm that measured the delta;
    a gate with no delta yields nothing, which is what an unobserved gate carries."""
    if not may_flag or gate.fleet_gs_delta_kt is None:
        return Flags.NONE
    f = Flags.NONE
    if gate.fleet_gs_delta_kt > cfg.fast_delta_kt:
        f |= Flags.FAST
    if gate.fleet_gs_delta_kt < cfg.slow_delta_kt:
        f |= Flags.SLOW
    return f


# Every flag gate_flags can set for one gate: the two GATE*_UNOBSERVED marks, the four "bad" conditions, and the two
# UNSTABLE marks that pair with them. reflag clears this whole mask before it calls gate_flags again, so no bit the
# helper used to set can survive as a stale leftover. Flags.STEEP, .SHALLOW and everything below them in the enum are
# never in here: they read the whole approach (or nothing about the flight at all), not one gate, and are gate_flags'
# to leave alone, not to set.
GATE_FLAG_MASK = (Flags.GATE1000_UNOBSERVED | Flags.GATE500_UNOBSERVED | Flags.HIGH | Flags.LOW | Flags.OFF_COURSE |
                  Flags.FAST | Flags.SLOW | Flags.HIGH_SINK | Flags.UNSTABLE_1000 | Flags.UNSTABLE_500)


def gate_flags(gate: "Gate", cfg: StabilityCfg, *, may_flag: bool) -> Flags:
    """One gate's own verdict, everything stability_flags used to compute inline for a single gate: an unobserved
    gate carries only its GATE1000_UNOBSERVED or GATE500_UNOBSERVED; an observed one carries whichever of HIGH, LOW,
    OFF_COURSE, HIGH_SINK and, through speed_flags, FAST or SLOW its own numbers earn, paired with UNSTABLE_1000 or
    UNSTABLE_500 whenever any of them fire -- never on its own. The only place that per-gate verdict is decided:
    stability_flags applies it down a live gate list, and reflag applies it again to gates rebuilt from stored rows,
    so the two paths cannot drift, the way speed_flags already keeps its own two thresholds from drifting between
    them. GATE_FLAG_MASK names every bit this function can return."""
    unobserved = {1000: Flags.GATE1000_UNOBSERVED, 500: Flags.GATE500_UNOBSERVED}.get(gate.gate_ft, Flags.NONE)
    if not gate.observed:
        return unobserved
    bad = Flags.NONE
    if gate.gp_dots is not None and gate.gp_dots > cfg.gp_dots_max:
        bad |= Flags.HIGH
    if gate.gp_dots is not None and gate.gp_dots < -cfg.gp_dots_max:
        bad |= Flags.LOW
    if gate.loc_dots is not None and abs(gate.loc_dots) > cfg.loc_dots_max:
        bad |= Flags.OFF_COURSE
    bad |= speed_flags(gate, cfg, may_flag=may_flag)
    if gate.sink_fpm is not None and gate.sink_fpm > cfg.sink_max_fpm:
        bad |= Flags.HIGH_SINK
    if not bad:
        return Flags.NONE
    unstable = {1000: Flags.UNSTABLE_1000, 500: Flags.UNSTABLE_500}.get(gate.gate_ft, Flags.NONE)
    return bad | unstable


def stability_flags(rwy: RunwayEnd, gates: list[Gate], summary: Summary, cfg: StabilityCfg, *,
                    established_hat_ft: float | None, sidestep: bool, temp_uncorrected: bool,
                    qnh_assumed: bool, fleet_key: str | None = None) -> Flags:
    f = Flags.NONE
    may_flag = key_may_flag(fleet_key)
    for g in gates:
        f |= gate_flags(g, cfg, may_flag=may_flag)
    if summary.descent_angle_deg is not None:
        if summary.descent_angle_deg > rwy.glide_path_deg + cfg.steep_delta_deg:
            f |= Flags.STEEP
        elif summary.descent_angle_deg < rwy.glide_path_deg - cfg.steep_delta_deg:
            f |= Flags.SHALLOW
    if established_hat_ft is not None and established_hat_ft < cfg.late_established_hat_ft:
        f |= Flags.LATE_ESTABLISHED
    if sidestep:
        f |= Flags.SIDESTEP
    if rwy.reference_assumed:
        f |= Flags.REFERENCE_ASSUMED
    if temp_uncorrected:
        f |= Flags.TEMP_UNCORRECTED
    if qnh_assumed:
        f |= Flags.QNH_ASSUMED
    return f


def bin_of(along_nm: float, bin_nm: float) -> float:
    """The distance bin a position or a gate falls in: the bin's lower edge, rounded so 0.5 nm bins read 0.0, 0.5, 1.0
    and not 1.0000000000000002. bin_means and the fleet norms bin alike, so a gate lands in the bin its own approach's
    points built."""
    return round(math.floor(along_nm / bin_nm) * bin_nm, 3)


def bin_means(points: list[ScoredPoint], bin_nm: float, max_nm: float) -> list[dict]:
    groups: dict[float, list[ScoredPoint]] = {}
    for p in points:
        if p.on_ground or p.hat_ft is None or not (0.0 <= p.along_nm < max_nm):
            continue
        groups.setdefault(bin_of(p.along_nm, bin_nm), []).append(p)

    def mean(vals: list[float | None]) -> float | None:
        vs = [v for v in vals if v is not None]
        return sum(vs) / len(vs) if vs else None

    return [dict(bin_nm=k, n=len(ps), gp_dev_mean_ft=mean([p.gp_dev_ft for p in ps]),
                 loc_dev_mean_ft=mean([p.loc_dev_ft for p in ps]), gs_corr_mean=mean([p.gs_corr for p in ps]),
                 sink_mean_fpm=mean([p.sink_fpm for p in ps])) for k, ps in sorted(groups.items())]


def apply_deltas(norm: "Norm | None", gates: list[Gate], bin_nm: float) -> None:
    """Each observed gate's two fleet deltas, in place: the gate's own value minus the median of the norm's bin that
    holds the gate's distance. A gate the norm has no bin for, a bin without that median, and a gate with no value of
    its own, all keep a null delta, which is what the FAST and SLOW flags read as "no delta"."""
    if norm is None:
        return
    for g in gates:
        if not g.observed or g.along_nm is None:
            continue
        b = norm.bins.get(bin_of(g.along_nm, bin_nm))
        if b is None:
            continue
        if g.gp_dev_ft is not None and b.gp_p50 is not None:
            g.fleet_gp_delta_ft = g.gp_dev_ft - b.gp_p50
        if g.gs_corr is not None and b.gs_p50 is not None:
            g.fleet_gs_delta_kt = g.gs_corr - b.gs_p50


class Scorer:
    """Config-bound facade over the functions above with a per-runway geometry cache."""

    def __init__(self, stability: StabilityCfg, *, temp_max_age_s: float, qnh_max_age_s: float, gates_ft: list[int],
                 gate_max_bracket_s: float, bin_nm: float, bin_max_nm: float, stats_max_along_nm: float = 6.0) -> None:
        self.stability = stability
        self.norms: "FleetNorms | None" = None       # the fleet snapshot, swapped in whole after each rebuild
        self.temp_max_age_s = temp_max_age_s
        self.qnh_max_age_s = qnh_max_age_s
        self.gates_ft = list(gates_ft)
        self.gate_max_bracket_s = gate_max_bracket_s
        self.bin_nm = bin_nm
        self.bin_max_nm = bin_max_nm
        self.stats_max_along_nm = stats_max_along_nm
        self._geom: dict[str, RunwayGeom] = {}

    def geom(self, rwy: RunwayEnd) -> RunwayGeom:
        g = self._geom.get(rwy.key)
        if g is None:
            g = self._geom[rwy.key] = runway_geom(rwy)
        return g

    def score(self, rwy: RunwayEnd, airport: Airport, pos: Position, wx: Weather | None, *,
              ref: Reference | None = None) -> ScoredPoint:
        """`pos` against the runway's centreline, or against `ref` (one of this runway's references) when given. Every
        reference of a runway shares its geometry: the glide path, the threshold and the localizer are the runway's."""
        assert ref is None or ref.runway.key == rwy.key, f"{ref and ref.key} is not a reference of {rwy.key}"
        if ref is None or ref.path is None:
            return score_point(rwy, self.geom(rwy), airport, pos, wx, self.temp_max_age_s, self.qnh_max_age_s)
        return score_point(rwy, self.geom(rwy), airport, pos, wx, self.temp_max_age_s, self.qnh_max_age_s,
                           frame=ref.frame(pos.lat, pos.lon), reference=ref.name)

    def gp_dots(self, gp_dev_deg: float | None) -> float | None:
        return None if gp_dev_deg is None else gp_dev_deg / GS_DOT_DEG

    def loc_dots(self, rwy: RunwayEnd, loc_dev_deg: float | None) -> float | None:
        return None if loc_dev_deg is None else loc_dev_deg / (self.geom(rwy).loc_width_deg / 4.0)

    def gates(self, rwy: RunwayEnd, points: list[ScoredPoint]) -> list[Gate]:
        width = self.geom(rwy).loc_width_deg
        return [interpolate_gate(points, g, self.gate_max_bracket_s, width) for g in self.gates_ft]

    def summarize(self, points: list[ScoredPoint]) -> Summary:
        return summarize(points, self.stats_max_along_nm, self.gate_max_bracket_s)

    def flags(self, rwy: RunwayEnd, gates: list[Gate], summary: Summary, *, established_hat_ft: float | None,
              sidestep: bool, temp_uncorrected: bool, qnh_assumed: bool, fleet_key: str | None = None) -> Flags:
        return stability_flags(rwy, gates, summary, self.stability, established_hat_ft=established_hat_ft,
                               sidestep=sidestep, temp_uncorrected=temp_uncorrected, qnh_assumed=qnh_assumed,
                               fleet_key=fleet_key)

    def bins(self, points: list[ScoredPoint]) -> list[dict]:
        return bin_means(points, self.bin_nm, self.bin_max_nm)

    def set_norms(self, norms: "FleetNorms | None") -> None:
        """Swap in a rebuild's snapshot whole. One name is rebound, so a gate being scored reads either the old snapshot
        or the new one, never a half-built one."""
        self.norms = norms

    def fleet_deltas(self, airport: str, runway: str, gates: list[Gate], *, type_code: str | None,
                     traffic_class: str | None) -> str | None:
        """Fill each observed gate's two fleet deltas and return the key they were measured against, as
        approaches.fleet_key stores it ("type:B738", "class:airline" or "*"); None, leaving every delta null, without
        a snapshot or when no key of this approach has one."""
        norm = None if self.norms is None else self.norms.lookup(airport, runway, type_code=type_code,
                                                                 traffic_class=traffic_class)
        apply_deltas(norm, gates, self.bin_nm)
        return None if norm is None else norm.text


def scorer_for(cfg: Config) -> Scorer:
    """The Scorer the collector and a replay build (pipeline.Pipeline), for the commands that score stored points
    again: the fleet rebuild's coverage records and reflag's bins."""
    return Scorer(cfg.stability, temp_max_age_s=cfg.service.metar_max_age_min * 60.0,
                  qnh_max_age_s=cfg.service.qnh_max_age_min * 60.0, gates_ft=cfg.detection.gates_ft,
                  gate_max_bracket_s=cfg.detection.gate_max_bracket_s, bin_nm=cfg.fleet.bin_nm,
                  bin_max_nm=cfg.fleet.max_along_nm)


def coverage_record(scorer: Scorer, rwy: RunwayEnd, points: list[ScoredPoint]) -> dict[str, Any]:
    """An approach as the receiver's coverage statistics count it: the lowest height and closest distance tracked, and
    whether each gate was observed, from the receiver's own points only, so a spliced adsb.lol tail never moves the
    coverage floor. `points` are an approach's stored points (ScoredPoint.from_row)."""
    local = [p for p in points if p.origin == "local"]
    summary = scorer.summarize(local)
    return {"lowest_hat_ft": summary.lowest_hat_ft, "closest_along_nm": summary.closest_along_nm,
            "gates_observed": {g.gate_ft: g.observed for g in scorer.gates(rwy, local)}}

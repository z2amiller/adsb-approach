"""fit-path: propose a runway's procedure path from recorded flights.

The command prints the points of a `[[runways."R".paths]]` table, found from the flights that followed a seed line (a
charted radial, or two points) and then reached a short final on the runway. It never writes the configuration: the
proposal is pasted into config/overrides/<icao>.toml by hand and read through there.

Everything is measured in the runway's frame (geometry.runway_frame): `along` the distance out from the threshold along
the extended centreline and `cross` the distance to the right of it, both in feet. The seed is a straight line in that
frame; a flight's *offset* is its perpendicular distance to the right of the seed."""
from __future__ import annotations

import json
import logging
import math
import statistics
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from .config import Config
from .geometry import NM_FT, destination, runway_frame, track_diff_deg
from .model import Airport, Position, ProcedurePath, RunwayEnd
from .reference import JOIN_MAX_CROSS_FT, validate_path
from .scorer import Scorer, scorer_for
from .sources.files import _load_json, _trace_files, _trace_points

log = logging.getLogger(__name__)

SHORT_FINAL_MIN_NM = 0.3         # a short final (the definition the miss scan used): this far out at the least,
SHORT_FINAL_MAX_NM = 5.0         # and this far at the most,
SHORT_FINAL_CROSS_FT = 1500.0    # within this of the centreline,
SHORT_FINAL_HAT_FT = 1500.0      # at most this high above the threshold,
SHORT_FINAL_TRACK_DEG = 20.0     # and heading within this of the runway course
SEED_CORRIDOR_NM = 0.3           # a flight followed the seed while within this of it,
SEED_RUN_NM = 2.0                # for at least this far before the short final
MAX_ALONG_NM = 20.0              # bins and the seed run stop here: the matcher opens candidates inside 12 nm, and a
                                 # path's first leg runs on outwards
BIN_NM = 0.5                     # bins of distance along the runway; the nearest starts at BIN_NM, the first whole bin
MIN_FLIGHTS = 10                 # flights to fit at all, and flights a bin needs to count as covered
TURN_DEPARTURE_FT = 200.0        # the near-end run of bins further than this off the seed anchors the turn-in leg
MIN_LEG_SLOPE_FT_PER_NM = 100.0  # the leg to the join must leave the seed at least this fast: a median that is 50 ft
                                 # out moves the turn by 50 / slope nm, and the real leg (Quiet Bridge) leaves at 165-185
SEED_STRAY_WARN_FT = 300.0       # warn when the first leg's medians are typically this far from the seed (Quiet Bridge: ~100)
SEED_SPAN_NM = 30.0              # a navaid's radial becomes a seed this long
BOX_MARGIN_FT = 1000.0           # how far outside the seed's corridor a position may lie and still be worth decoding the file for

Seed = tuple[tuple[float, float], tuple[float, float]]       # two (lat, lon) points on the seed line


@dataclass(frozen=True, slots=True)
class FitResult:
    """A proposed path. `points` run from the far end to the join (lat, lon); `bins` are (along_nm, median, p25, p75),
    the median and quartiles of the flights' cross-track from the seed in feet, positive to its right, for each covered
    bin of distance along the runway (its midpoint), the far end first; `turn_nm` and `join_nm` are the distances along
    the runway of the turn and of the join."""

    points: tuple[tuple[float, float], ...]
    flights: int
    bins: list[tuple[float, float, float, float]]
    turn_nm: float
    join_nm: float


def navaid_seed(lat: float, lon: float, radial_true_deg: float) -> Seed:
    """The seed along a navaid's radial: the navaid, and the point SEED_SPAN_NM out on the radial (true degrees)."""
    return (lat, lon), destination(lat, lon, radial_true_deg % 360.0, SEED_SPAN_NM * NM_FT)


def frame_to_latlon(rwy: RunwayEnd, along_ft: float, cross_ft: float) -> tuple[float, float]:
    """The inverse of geometry.runway_frame: where `along_ft` out and `cross_ft` to the right of the centreline is."""
    c = math.radians(rwy.course_true_deg)
    east = -along_ft * math.sin(c) + cross_ft * math.cos(c)
    north = -along_ft * math.cos(c) - cross_ft * math.sin(c)
    return destination(rwy.threshold_lat, rwy.threshold_lon, math.degrees(math.atan2(east, north)) % 360.0,
                       math.hypot(east, north))


@dataclass(frozen=True, slots=True)
class _Seed:
    """The seed line in the runway's frame: cross = offset_ft + slope * along."""

    slope: float
    offset_ft: float

    @classmethod
    def through(cls, rwy: RunwayEnd, a: tuple[float, float], b: tuple[float, float]) -> _Seed:
        (a1, c1), (a2, c2) = runway_frame(rwy, *a), runway_frame(rwy, *b)
        if abs(a2 - a1) < 1.0 or abs(c2 - c1) > abs(a2 - a1):
            raise ValueError(f"fit-path: the seed must run along the approach, within 45 degrees of {rwy.key}'s course "
                             f"of {rwy.course_true_deg:.0f}, not across it")
        slope = (c2 - c1) / (a2 - a1)
        return cls(slope, c1 - slope * a1)

    @property
    def _norm(self) -> float:
        return math.hypot(1.0, self.slope)

    def cross_at(self, along_ft: float) -> float:
        """The seed's cross-track from the centreline, `along_ft` out."""
        return self.offset_ft + self.slope * along_ft

    def offset(self, along_ft: float, cross_ft: float) -> float:
        """The perpendicular distance to the right of the seed of the position (along_ft, cross_ft)."""
        return (cross_ft - self.cross_at(along_ft)) / self._norm

    def cross_from(self, along_ft: float, offset_ft: float) -> float:
        """The cross-track from the centreline of the position `offset_ft` right of the seed, `along_ft` out."""
        return self.cross_at(along_ft) + offset_ft * self._norm


class _Selector:
    """Which flights followed the seed, and the (along_ft, offset_ft) samples of their approach."""

    def __init__(self, scorer: Scorer, airport: Airport, rwy: RunwayEnd, seed: _Seed) -> None:
        self.scorer, self.airport, self.rwy, self.seed = scorer, airport, rwy, seed
        self.near_ft = SHORT_FINAL_MIN_NM * NM_FT
        self.short_max_ft = SHORT_FINAL_MAX_NM * NM_FT
        self.far_ft = MAX_ALONG_NM * NM_FT
        self.corridor_ft = SEED_CORRIDOR_NM * NM_FT
        self.run_ft = SEED_RUN_NM * NM_FT

    def short_final(self, p: Position, along: float, cross: float) -> bool:
        if not self.near_ft <= along <= self.short_max_ft or abs(cross) > SHORT_FINAL_CROSS_FT:
            return False
        if p.track is None or abs(track_diff_deg(p.track, self.rwy.course_true_deg)) > SHORT_FINAL_TRACK_DEG:
            return False
        hat = self.scorer.score(self.rwy, self.airport, p, None).hat_ft      # the standard-pressure height is close enough
        return hat is not None and hat <= SHORT_FINAL_HAT_FT

    def samples(self, positions: Iterable[Position]) -> list[tuple[float, float]] | None:
        """The approach in one flight's `positions` (time order), as (along_ft, offset_ft) from the start of the run along
        the seed to the end of the short final; None when the flight is not selected. A flight is selected when it flew
        within the corridor of the seed for SEED_RUN_NM before the first position that is on a short final, so a flight
        that is near the seed only on its short final (the centreline meets the seed there) is not. A trace file is one
        flight: the first short final decides it, and a later approach in the same file is not looked at."""
        run: list[tuple[float, float]] = []
        followed = reached = False
        for p in positions:
            if p.on_ground:
                continue
            along, cross = runway_frame(self.rwy, p.lat, p.lon)
            if along < self.near_ft:                    # at the threshold or past it
                if reached:
                    return run                          # landed or overflew: the approach is over
                run, followed = [], False               # no short final yet, so what came before counts for nothing
                continue
            if along > self.far_ft:
                continue
            offset = self.seed.offset(along, cross)
            if not reached and self.short_final(p, along, cross):
                if not followed:
                    return None
                reached = True
            if reached:
                run.append((along, offset))
            elif abs(offset) <= self.corridor_ft:
                run.append((along, offset))
                followed = followed or run[0][0] - along >= self.run_ft
            elif followed:
                run.append((along, offset))             # past the run along the seed: the turn and the join are here
            else:
                run = []
        return run if reached else None


def _search_box(rwy: RunwayEnd, seed: _Seed) -> tuple[float, float, float, float]:
    """(lat_lo, lat_hi, lon_lo, lon_hi) around the seed's corridor from the threshold out to MAX_ALONG_NM, with room to
    spare. A trace with no position inside it cannot have followed the seed."""
    reach = SEED_CORRIDOR_NM * NM_FT + BOX_MARGIN_FT
    corners = [frame_to_latlon(rwy, a, seed.cross_at(a) + side) for a in (0.0, MAX_ALONG_NM * NM_FT)
               for side in (-reach, reach)]
    lats, lons = [c[0] for c in corners], [c[1] for c in corners]
    return min(lats), max(lats), min(lons), max(lons)


def _why_not_a_trace(data: object) -> str | None:
    """Why a decoded JSON value cannot be read as a readsb trace file, or None when it can."""
    if not isinstance(data, dict):
        return "not a JSON object"
    if "icao" not in data:
        return "no icao"
    trace = data.get("trace")
    if not isinstance(trace, list):
        return "no trace list"
    if "timestamp" not in data:
        return "no timestamp"
    try:
        stamp = float(data["timestamp"])
    except (TypeError, ValueError):
        return f"timestamp {data['timestamp']!r} is not a number"
    if not math.isfinite(stamp):
        return f"timestamp {data['timestamp']!r} is not finite"
    if not all(isinstance(row, list) for row in trace):
        return "a trace point is not a list"
    return None


def _in_box(data: dict, box: tuple[float, float, float, float]) -> bool:
    lat_lo, lat_hi, lon_lo, lon_hi = box
    for row in data["trace"]:
        try:
            if lat_lo <= row[1] <= lat_hi and lon_lo <= row[2] <= lon_hi:
                return True
        except (IndexError, TypeError):                 # a point without a position, or a malformed one
            continue
    return False


def _bin_means(samples: list[tuple[float, float]]) -> dict[int, float]:
    """The flight's mean offset in each bin of distance along the runway (bin k starts at k * BIN_NM). The nearest, from
    0 to BIN_NM, is left out: a flight stops being followed 0.3 nm out, so it holds a sliver of the bin."""
    sums: dict[int, list[float]] = defaultdict(list)
    for along, offset in samples:
        k = int(along // (BIN_NM * NM_FT))
        if k >= 1:
            sums[k].append(offset)
    return {k: statistics.fmean(v) for k, v in sums.items()}


def _line(points: list[tuple[float, float]]) -> tuple[float, float]:
    """The least-squares line y = slope * x + intercept through (x, y) points of at least two distinct x."""
    mx = statistics.fmean(x for x, _ in points)
    my = statistics.fmean(y for _, y in points)
    slope = sum((x - mx) * (y - my) for x, y in points) / sum((x - mx) ** 2 for x, _ in points)
    return slope, my - slope * mx


def fit_path(cfg: Config, airport: str, runway: str, seed: Seed, trace_files: Iterable[str]) -> FitResult:
    """Fit a path to the flights in `trace_files` that followed `seed`, a line given by two (lat, lon) points, and then
    reached a short final on the runway. Raises ValueError, starting "fit-path: ", when the airport or runway is not
    configured, the seed is not along the approach, fewer than MIN_FLIGHTS flights followed it, or the flights give no
    turn and join that make a path the configuration accepts.

    The flights' offsets from the seed are binned by BIN_NM of distance along the runway (one mean per flight per bin,
    then the median and quartiles over the flights). _turn_and_join finds the leg from the turn to the join in the
    medians, anchored at the runway end of them; the turn is where a least-squares line through that leg meets the seed
    and the join is where it meets the centreline, which may be nearer than the flights were tracked. A seed the first
    leg's medians typically stray from by more than SEED_STRAY_WARN_FT is logged as a warning, not an error. A trace file
    that is not a trace, or holds a position the geometry cannot take, is skipped with a warning naming it."""
    apt = cfg.airports.get(airport)
    if apt is None:
        raise ValueError(f"fit-path: no airport {airport} in {cfg.path}; configured: {', '.join(sorted(cfg.airports))}")
    rwy = apt.runways.get(runway)
    if rwy is None:
        raise ValueError(f"fit-path: {airport} has no runway {runway}; it has {', '.join(sorted(apt.runways))}")
    line = _Seed.through(rwy, *seed)
    selector = _Selector(scorer_for(cfg), apt, rwy, line)
    box = _search_box(rwy, line)
    per_bin: dict[int, list[float]] = defaultdict(list)
    flights = 0
    for path in trace_files:
        data = _load_json(path)
        if data is None:
            continue
        why = _why_not_a_trace(data)
        if why is not None:
            log.warning("%s: skipped, not a trace file: %s", path, why)
            continue
        if not _in_box(data, box):
            continue
        try:
            samples = selector.samples(_trace_points(data, path))
        except (ValueError, OverflowError) as exc:       # a position or altitude the geometry cannot take, e.g. infinite
            log.warning("%s: skipped, a point cannot be placed: %s", path, exc)
            continue
        if samples is None:
            continue
        flights += 1
        for k, mean in _bin_means(samples).items():
            per_bin[k].append(mean)
    if flights < MIN_FLIGHTS:
        raise ValueError(f"fit-path: only {flights} flights followed the seed")
    bins = _bin_table(per_bin, flights)
    far_nm = bins[0][0] + BIN_NM / 2
    turn_nm, join_nm = _turn_and_join(line, bins)
    stray = _first_leg_stray_ft(bins, turn_nm)
    if stray is not None and stray > SEED_STRAY_WARN_FT:
        log.warning("fit-path: over the first leg, from the far end to the turn %.1f nm out, the medians are typically "
                    "%.0f ft from the seed, over %g ft: check the seed's position and bearing against the bins",
                    turn_nm, stray, SEED_STRAY_WARN_FT)
    points = tuple((round(lat, 6), round(lon, 6)) for lat, lon in (
        frame_to_latlon(rwy, far_nm * NM_FT, line.cross_at(far_nm * NM_FT)),
        frame_to_latlon(rwy, turn_nm * NM_FT, line.cross_at(turn_nm * NM_FT)),
        frame_to_latlon(rwy, join_nm * NM_FT, 0.0)))
    try:
        validate_path(rwy, ProcedurePath("fit", "fit", "fit", points))
    except ValueError as exc:
        raise ValueError(f"fit-path: the fitted path breaks the rules for a path: {exc}") from None
    return FitResult(points=points, flights=flights, bins=bins, turn_nm=turn_nm, join_nm=join_nm)


def _bin_table(per_bin: dict[int, list[float]], flights: int) -> list[tuple[float, float, float, float]]:
    """(along_nm, median, p25, p75) of the flights' offsets for each bin with at least MIN_FLIGHTS of them, the farthest
    first; along_nm is the middle of the bin."""
    covered = sorted((k for k, v in per_bin.items() if len(v) >= MIN_FLIGHTS), reverse=True)
    if not covered:
        raise ValueError(f"fit-path: no {BIN_NM:g} nm bin holds {MIN_FLIGHTS} of the {flights} flights")
    bins = []
    for k in covered:
        p25, median, p75 = statistics.quantiles(per_bin[k], n=4, method="inclusive")
        bins.append(((k + 0.5) * BIN_NM, median, p25, p75))
    return bins


def _first_leg_stray_ft(bins: list[tuple[float, float, float, float]], turn_nm: float) -> float | None:
    """How far from the seed the medians typically are over the first leg, from the far end to the turn: the median of
    their distances, so the few bins where flights are still coming onto the seed do not count. None without a bin."""
    first_leg = [abs(b[1]) for b in bins if b[0] > turn_nm]
    return statistics.median(first_leg) if first_leg else None


def _turn_and_join(seed: _Seed, bins: list[tuple[float, float, float, float]]) -> tuple[float, float]:
    """(turn_nm, join_nm) from the covered bins, far end first.

    The leg to the join is anchored at the near end. It ends at the first bin whose median is on the centreline (or the
    last bin, when the flights were not tracked that far in), and it starts at the far end of the unbroken run of bins
    more than TURN_DEPARTURE_FT off the seed that ends there. A bin further out that strays past that does not start it,
    and nor do the farthest bins, where flights still coming onto the seed leave the medians off it. A least-squares line
    through the leg's medians meets the seed at the turn and the centreline at the join."""
    end = next((i for i, b in enumerate(bins) if abs(seed.cross_from(b[0] * NM_FT, b[1])) <= JOIN_MAX_CROSS_FT), len(bins))
    if end == 0:
        raise ValueError("fit-path: the median is on the centreline from the farthest bin: there is no leg to the join")
    start = end
    while start > 0 and abs(bins[start - 1][1]) > TURN_DEPARTURE_FT:
        start -= 1
    if start == end:
        raise ValueError(f"fit-path: the median never leaves the seed by more than {TURN_DEPARTURE_FT:g} ft before the "
                         f"centreline: there is no turn to fit")
    if start == 0:
        raise ValueError(f"fit-path: the median is never within {TURN_DEPARTURE_FT:g} ft of the seed: the flights do not "
                         f"follow it; check the seed's position and bearing against the bins")
    leg = [(b[0], b[1]) for b in bins[start:end]]
    if len(leg) < 2:
        raise ValueError(f"fit-path: the leg to the join holds {len(leg)} bin; it takes 2 to fit")
    slope, intercept = _line(leg)                       # offset from the seed in ft, against nm along
    norm = math.hypot(1.0, seed.slope)
    cl_slope, cl_intercept = -seed.slope * NM_FT / norm, -seed.offset_ft / norm     # the centreline's offset from the seed
    if abs(slope) < MIN_LEG_SLOPE_FT_PER_NM:
        raise ValueError(f"fit-path: the leg to the join leaves the seed by only {abs(slope):.0f} ft per nm, under "
                         f"{MIN_LEG_SLOPE_FT_PER_NM:g}: too shallow to place the turn")
    if abs(slope - cl_slope) < 1.0:
        raise ValueError("fit-path: the leg to the join runs parallel to the centreline, so it never joins it")
    turn_nm, join_nm = -intercept / slope, (cl_intercept - intercept) / (slope - cl_slope)
    far_nm = bins[0][0] + BIN_NM / 2
    if not 0.0 < join_nm < turn_nm < far_nm:
        raise ValueError(f"fit-path: the flights put the turn {turn_nm:.1f} nm out and the join {join_nm:.1f} nm out, "
                         f"with the covered bins ending {far_nm:.1f} nm out: that is no path that turns, then joins")
    return turn_nm, join_nm


def _toml_string(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)         # a JSON string is a TOML basic string


def toml_snippet(result: FitResult, *, runway: str, name: str, label: str, source: str) -> str:
    """The `[[runways."R".paths]]` table for `result`, to paste into config/overrides/<icao>.toml."""
    far_nm = result.bins[0][0] + BIN_NM / 2
    notes = (f"on the seed, {far_nm:.1f} nm along {runway}: the far end",
             f"on the seed, {result.turn_nm:.1f} nm along: the turn",
             f"the join, on the {runway} centreline {result.join_nm:.1f} nm out")
    rows = [f"  [{lat:.6f}, {lon:.6f}],".ljust(36) + f"# {note}" for (lat, lon), note in zip(result.points, notes)]
    return "\n".join([f'[[runways."{runway}".paths]]', f"name = {_toml_string(name)}", f"label = {_toml_string(label)}",
                      f"source = {_toml_string(source)}", "points = [", *rows, "]"]) + "\n"


def fit_source(result: FitResult, *, seed: str, first: date, last: date) -> str:
    """The `source` line for a fitted path: what it was fitted to and how."""
    return (f"{seed}; turn at {result.turn_nm:.1f} nm and join at {result.join_nm:.1f} nm fitted to {result.flights:,} "
            f"flights, {first}..{last} (fit-path)")


def format_bins(result: FitResult) -> str:
    """The bins as a table of TOML comments, so that the command's whole output can be pasted into a TOML file."""
    rows = [f"# fitted to {result.flights:,} flights; cross-track from the seed in ft, positive to its right, per "
            f"{BIN_NM:g} nm along the runway, far end first",
            f"# {'along_nm':>9} {'median':>8} {'p25':>8} {'p75':>8}"]
    rows += [f"# {along:9.2f} {median:8.0f} {p25:8.0f} {p75:8.0f}" for along, median, p25, p75 in result.bins]
    return "\n".join(rows) + "\n"


def trace_files_between(root: str | Path, first: date, last: date) -> list[str]:
    """The trace files globe_history keeps under `root` for each UTC day from `first` to `last`, both included."""
    files: list[str] = []
    day = first
    while day <= last:
        files.extend(_trace_files(Path(root) / f"{day:%Y/%m/%d}"))
        day += timedelta(days=1)
    return files

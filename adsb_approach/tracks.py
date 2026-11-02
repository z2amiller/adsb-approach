"""A flight session's track, read back from readsb's trace files when a search runs: the label a search result carries
("arrival KSFO"), the lowest and highest points, the squawks heard, and the emergency runs. The database keeps no
positions for a flight without an approach, so nothing here is stored."""
from __future__ import annotations

import csv
import logging
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .emergency import SQUAWK_FLAGS, STATE_FLAGS, EmergencyWatch
from .geometry import NM_FT, distance_ft
from .model import Airport, Position
from .sources.files import iter_trace_file

log = logging.getLogger(__name__)

LOW_FT = 3000            # a lowest point below this and within NEAR_NM of an airport is an arrival, departure or pass
NEAR_NM = 8.0
HIGH_FT = 10000          # a lowest point at or above this makes an overflight
ARRIVAL_FROM = 0.6       # the low point in the last 40% of the session is an arrival, in the first 40% a departure
DEPARTURE_UNTIL = 0.4
SESSION_GAP_S = 7200.0   # the Tracker's default session_gap_s: a silence over this ends the session
BRIEF_POINTS = 3         # fewer points than this, or a track spanning less than BRIEF_S, cannot say which way it went:
BRIEF_S = 120.0          # it is "heard briefly" (N00210, 2026-09-21: three points in a minute made an "arrival")
GROUND_GEOM_MAX_FT = 1000   # an on-ground report is believed only when its geometric height is this low or unknown;
                            # fields above it (KTRK, 5,900 ft) are far beyond where this receiver hears the ground
DAY_S = 86400
HEX_ID = re.compile(r"~?[0-9a-f]{6}")   # as the flights table keeps it; "~" marks a non-ICAO one


@dataclass(frozen=True, slots=True)
class Place:
    """An airport a track can be near."""
    ident: str
    name: str
    lat: float
    lon: float


def read_places(path: str | Path) -> list[Place]:
    """The airports of a CSV with the header ident,name,type,lat,lon (the nearby-airports list build-nearby writes)."""
    with open(path, newline="", encoding="utf-8") as fh:
        return [Place(row["ident"], row["name"], float(row["lat"]), float(row["lon"])) for row in csv.DictReader(fh)]


def _is_file(path: Path) -> bool:
    """Whether `path` is a file. An OSError while looking, a PermissionError under a directory this service cannot
    enter, say, counts as no file, as iter_trace_file counts a file it cannot open: one such directory must not turn a
    search into an error, or stop the web service from starting."""
    try:
        return path.is_file()
    except OSError as exc:
        log.warning("%s: skipped, not accessible: %s", path, exc)
        return False


def load_places(path: str | Path, airports: Mapping[str, Airport]) -> list[Place]:
    """The places a track can be near: the nearby-airports CSV, if the file exists and reads, and every configured
    airport the list does not already carry, so the airports this service scores can always name a track. The list is
    optional, so a file that cannot be read (a bad number, a missing column, a cut-short row) is logged and left out
    rather than raised: this runs as the web service starts."""
    places: list[Place] = []
    if _is_file(Path(path)):
        try:
            places = read_places(path)
        except (OSError, ValueError, KeyError, TypeError, csv.Error) as exc:     # unreadable, bad number or UTF-8, ...
            log.warning("%s: nearby-airports list not used: %s: %s", path, type(exc).__name__, exc)
    listed = {pl.ident for pl in places}
    places.extend(Place(apt.icao, apt.name, apt.lat, apt.lon) for apt in airports.values() if apt.icao not in listed)
    return places


@dataclass(frozen=True, slots=True)
class EmergencyRun:
    """One emergency the watch latched. `flag` is the Flags name; `code` the squawk when the squawk source latched it,
    otherwise the ADS-B state. The run begins where the watch timed it from and ends at the last report carrying the
    same code before that source reported another value."""
    flag: str
    code: str
    start: float
    end: float
    alt_ft: int | None       # at `start`


@dataclass(frozen=True, slots=True)
class TrackSummary:
    """What a session's track says about it. `kind` is arrival, departure, low pass, overflight, transit, brief, on
    ground or no track; `airport` is the nearest place to the lowest point (None for an overflight, which the label gives
    by its altitude instead, and for a brief track unless it was low and close to one)."""
    kind: str
    airport: str | None
    lowest_ft: int | None
    lowest_nm: float | None       # from `airport`, to 0.1
    highest_ft: int | None
    key_t: float | None           # the moment to look at: the low point of a landing or takeoff, else the first point
    first_t: float | None
    last_t: float | None
    squawks: tuple[str, ...]      # distinct, in the order heard
    emergency: tuple[EmergencyRun, ...]

    @property
    def label(self) -> str:
        """The one-line description a search result carries: "arrival KSFO", "overflight FL350"."""
        if self.kind == "no track":
            return "no track on disk"
        if self.kind == "overflight":
            return f"overflight FL{round((self.highest_ft or 0) / 100):03d}"
        if self.kind == "brief":
            lowest = self.lowest_ft
            at = ("" if lowest is None else " on the ground" if lowest == 0      # a ground point, never a low QNH
                  else f" at FL{round(lowest / 100):03d}" if lowest >= HIGH_FT    # flight levels from where overflights
                  else f" at {round(lowest, -2):,} ft")                           # start, so the two read alike
            return f"heard briefly{at}" + ("" if self.airport is None else f" near {self.airport}")
        if self.kind == "transit":
            lowest = "" if self.lowest_ft is None else f", lowest {round(self.lowest_ft, -2):,} ft"
            return f"transit{lowest}" + ("" if self.airport is None else f" near {self.airport}")
        return self.kind if self.airport is None else f"{self.kind} {self.airport}"


def _day_file(globe_dir: Path, hexid: str, day: int) -> Path:
    date = datetime.fromtimestamp(day * DAY_S, timezone.utc)
    return globe_dir / f"{date:%Y/%m/%d}" / "traces" / hexid[-2:] / f"trace_full_{hexid}.json"


def _read_trace(path: Path, cache: dict[Path, list[Position]] | None) -> Iterable[Position]:
    """The points of one trace file, whole. With a `cache` the file is parsed once, the first time it is asked for."""
    if cache is None:
        return iter_trace_file(path)
    if path not in cache:
        cache[path] = list(iter_trace_file(path))
    return cache[path]


def session_positions(hexid: str, first_seen: float, last_seen: float, *, globe_dir: Path, live_dir: Path | None,
                      open_ended: bool = False, now: float | None = None,
                      cache: dict[Path, list[Position]] | None = None) -> list[Position]:
    """The track points of one session, in time order: from the globe_history file of each UTC day it touches, and for a
    day globe_history lacks (readsb writes a day's files only when the day ends) from the live trace file, which holds
    the last 24 h or so. A file that cannot be found or read gives no points. A session still open runs to `now`, so it
    keeps the points after last_seen up to the first gap over SESSION_GAP_S. A search reads several sessions of one hex,
    often from one file: with a `cache` (a dict the caller starts empty and drops when its request ends) each file is
    parsed once, and it holds the whole file, as every session takes its own days from it. A `hexid` that is not six hex
    digits (readsb puts "~" before a non-ICAO address) gives no points and builds no path: it goes into a file name."""
    if not HEX_ID.fullmatch(hexid):
        log.warning("%r: not a hex id, no track read", hexid)
        return []
    end = (time.time() if now is None else now) if open_ended else last_seen
    points: list[Position] = []
    missing: set[int] = set()
    for day in range(int(first_seen // DAY_S), int(end // DAY_S) + 1):
        path = _day_file(globe_dir, hexid, day)
        if _is_file(path):
            points.extend(_read_trace(path, cache))
        else:
            missing.add(day)
    live = None if live_dir is None else live_dir / hexid[-2:] / f"trace_full_{hexid}.json"
    if missing and live is not None and _is_file(live):
        # the live file also repeats the days globe_history has, which would put those points in twice
        points.extend(p for p in _read_trace(live, cache) if int(p.t // DAY_S) in missing)
    points.sort(key=lambda p: p.t)
    kept: list[Position] = []
    prev = last_seen
    for p in points:
        if p.t < first_seen - 1:
            continue
        if p.t > last_seen + 1 and (not open_ended or p.t - prev > SESSION_GAP_S):
            break
        kept.append(p)
        prev = p.t
    return kept


def _alt_ft(p: Position) -> int | None:
    """0 on the ground, else the barometric altitude, else the geometric; None when the point carries none. An
    on-ground report is believed only when the geometric height agrees, or is unknown: N00210's ADS-B reported on_ground
    at 2,325 and 6,050 ft over Fremont on 2026-09-20, which would otherwise have made those its lowest points."""
    if p.on_ground and (p.alt_geom is None or p.alt_geom <= GROUND_GEOM_MAX_FT):
        return 0
    return p.alt_baro if p.alt_baro is not None else p.alt_geom


def _emergency_runs(positions: Sequence[Position]) -> tuple[EmergencyRun, ...]:
    """Feed the positions, in order, to the collector's own EmergencyWatch and describe each flag it latches. The watch
    says when a flag latched and when its run began; this follows each source's value to find the code and where the run
    ends. The squawk is checked first, as the watch does, so a squawk and a state that begin together give one run."""
    watch = EmergencyWatch()
    since: dict[str, tuple[str, float, int | None]] = {}    # per source: its code, from when, and the altitude then
    running: dict[str, int] = {}                            # per source: the index in `found` of the run still going
    found: list[EmergencyRun] = []
    for p in positions:
        reports = (("squawk", p.squawk, SQUAWK_FLAGS), ("state", p.emergency, STATE_FLAGS))
        for source, code, _ in reports:
            if code is None:
                continue
            if source not in since or since[source][0] != code:
                since[source] = (code, p.t, _alt_ft(p))
                running.pop(source, None)
            elif source in running:
                found[running[source]] = replace(found[running[source]], end=p.t)
        new = watch.observe(p.squawk, p.emergency, p.t)
        for source, code, flags in reports:
            if code is None:
                continue
            flag = flags.get(code)
            if flag is None or not flag & new or since[source][1] != watch.latched_at[flag]:
                continue
            assert flag.name is not None        # a single named member of Flags, never a combination
            new &= ~flag
            running[source] = len(found)
            found.append(EmergencyRun(flag.name, code, since[source][1], p.t, since[source][2]))
    return tuple(found)


def summarize(positions: Sequence[Position], places: Sequence[Place]) -> TrackSummary:
    """Label a session's track. The lowest point (the first of any tied) and its nearest place decide the kind: low and
    close to a place, it is an arrival, departure or low pass by where in the session it falls; otherwise an overflight
    if it never came below HIGH_FT, else a transit. A track that never rises above 0 ft is on the ground, and one with
    fewer than BRIEF_POINTS points or spanning less than BRIEF_S was heard too briefly to say which way it went."""
    if not positions:
        return TrackSummary("no track", None, None, None, None, None, None, None, (), ())
    first_t, last_t = positions[0].t, positions[-1].t
    squawks = tuple(dict.fromkeys(p.squawk for p in positions if p.squawk is not None))
    emergency = _emergency_runs(positions)
    heights = [(p, alt) for p in positions if (alt := _alt_ft(p)) is not None]
    brief = len(positions) < BRIEF_POINTS or last_t - first_t < BRIEF_S
    if not heights:       # no point carries an altitude, so nothing can be said beyond the track's span
        return TrackSummary("brief" if brief else "transit", None, None, None, None, first_t, first_t, last_t, squawks,
                            emergency)
    low, lowest_ft = min(heights, key=lambda h: h[1])
    highest_ft = max(alt for _, alt in heights)
    airport, nm = _nearest_place(low, places)
    fraction = (low.t - first_t) / max(1.0, last_t - first_t)
    kind, keep_place, keyed_at_low = _kind(lowest_ft, highest_ft, nm, fraction, brief)
    if not keep_place:
        airport, nm = None, None
    return TrackSummary(kind, airport, lowest_ft, None if nm is None else round(nm, 1), highest_ft,
                        low.t if keyed_at_low else first_t, first_t, last_t, squawks, emergency)


def _nearest_place(p: Position, places: Sequence[Place]) -> tuple[str | None, float | None]:
    """The ident of the place nearest p and its distance in nm (the first of any tied), or (None, None) without
    places."""
    near = min(((distance_ft(p.lat, p.lon, pl.lat, pl.lon) / NM_FT, pl) for pl in places),
               key=lambda d: d[0], default=None)
    return (None, None) if near is None else (near[1].ident, near[0])


def _kind(lowest_ft: float, highest_ft: float, nm: float | None, fraction: float,
          brief: bool) -> tuple[str, bool, bool]:
    """summarize's label, from the track's lowest and highest heights, the lowest point's distance to its nearest place
    and where in the session that point fell (`fraction`: 0 at the start, 1 at the end). Also whether that place belongs
    on the label (an overflight names none, and a brief track only one it came low and close to), and whether the
    session is keyed by the lowest point's time rather than its start (an arrival, departure or low pass)."""
    near = nm is not None and nm <= NEAR_NM
    if highest_ft <= 0:
        return "on ground", True, False
    if brief:
        return "brief", lowest_ft < HIGH_FT and near, False
    if lowest_ft < LOW_FT and near:
        kind = "arrival" if fraction >= ARRIVAL_FROM else "departure" if fraction <= DEPARTURE_UNTIL else "low pass"
        return kind, True, True
    if lowest_ft >= HIGH_FT:
        return "overflight", False, False
    return "transit", True, False

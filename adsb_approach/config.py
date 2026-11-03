"""TOML configuration → dataclasses. Paths are resolved against the main config file."""
from __future__ import annotations

import difflib
import hashlib
import json
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, tzinfo
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .model import Airport, ProcedurePath, RunwayEnd
from .reference import validate_path


@dataclass
class ServiceCfg:
    db_path: str = "data/approaches.sqlite"
    bind: str = "127.0.0.1"
    port: int = 8090
    typedb_path: str | None = None
    type_classes_path: str | None = None
    airports_dir: str = "airports"
    tar1090_url: str = "http://127.0.0.1/tar1090/"
    metar_interval_s: int = 600
    metar_max_age_min: int = 90
    qnh_max_age_min: int = 180
    log_level: str = "info"
    timezone: str | None = None      # IANA name; unset: the host's own zone
    globe_history_dir: str = "/var/globe_history"       # readsb's day-by-day trace files, read by the flight search
    live_traces_dir: str = "/run/readsb/traces"         # readsb's live traces, for a day globe_history lacks; "": off
    nearby_airports_path: str = "nearby_airports.csv"   # the flight search's airport list; a missing file is tolerated


@dataclass
class SourceCfg:
    kind: str = "readsb-json"
    host: str = "127.0.0.1"
    port: int = 30047
    url: str | None = None
    interval_s: float = 1.0


@dataclass
class DetectionCfg:
    candidate_max_along_nm: float = 12.0
    candidate_max_cross_nm: float = 1.0
    candidate_track_tol_deg: float = 25.0
    candidate_max_height_ft: float = 6000.0
    candidate_max_climb_fpm: float = 300.0
    established_cross_nm: float = 0.3
    established_window_s: float = 30.0
    established_min_points: int = 5
    established_track_tol_deg: float = 10.0
    established_max_cross_rate_fps: float = 20.0
    score_max_along_nm: float = 10.0
    gates_ft: list[int] = field(default_factory=lambda: [1000, 500])
    gate_max_bracket_s: float = 30.0
    lost_timeout_s: float = 45.0
    presumed_landed_max_height_ft: float = 1500.0
    presumed_landed_max_along_nm: float = 4.0
    go_around_min_climb_ft: float = 300.0
    go_around_min_vr_fpm: float = 300.0
    go_around_min_s: float = 15.0
    go_around_armed_below_ft: float = 2000.0
    touch_and_go_max_height_ft: float = 50.0
    runway_change_min_s: float = 10.0
    runway_change_track_tol_deg: float = 5.0
    runway_change_near_ft: float = 300.0
    runway_change_far_ft: float = 450.0
    abandon_cross_nm: float = 1.0
    abandon_track_tol_deg: float = 45.0
    abandon_min_s: float = 15.0
    reappear_go_around_s: float = 300.0
    reappear_max_nm: float = 10.0
    arrival_link_window_min: float = 30.0
    landed_confirm_ground_min: float = 30.0
    landed_confirm_departure_nm: float = 3.0
    landed_confirm_departure_h: float = 8.0


@dataclass
class StabilityCfg:
    gp_dots_max: float = 1.0
    loc_dots_max: float = 1.0
    sink_max_fpm: float = 1000.0
    fast_delta_kt: float = 15.0
    slow_delta_kt: float = -10.0
    steep_delta_deg: float = 0.5
    late_established_hat_ft: float = 1000.0


@dataclass
class FleetCfg:
    window_days: int = 30
    min_samples: int = 30
    rebuild_interval_min: int = 60
    bin_nm: float = 0.5
    max_along_nm: float = 8.0


@dataclass
class ExternalCfg:
    """The live adsb.lol poller: off unless `enabled`. While it is on, an aircraft with an approach open is asked about
    on adsb.lol's re-api, and what comes back joins the position stream."""
    enabled: bool = False
    url: str = "https://re-api.adsb.lol/"        # re-api's base URL; a poll asks it `?find_hex=` the watched aircraft
    poll_interval_s: float = 2.0                  # start to start: at most one request this often, so none overlap
    timeout_s: float = 10.0
    backoff_min: float = 15.0                     # a refusal's pause (401, 403, 429, 5xx) without a Retry-After
    # The seconds taken off adsb.lol's times so they sit on the receiver's clock: adsb.lol stamps a position about this
    # much later than the local receiver does.
    clock_offset_s: float = 0.10
    watch_max_min: float = 20.0                   # how long an aircraft stays watched after a go-around or touch-and-go
    # A watched aircraft is asked about only once the receiver has given no position of it for more than this long, and
    # no request goes out while the receiver places every watched aircraft. adsb.lol adds to a track only where the
    # receiver has lost it, and a key's quota is counted in requests. 0 asks about every watched aircraft.
    quiet_s: float = 8.0
    # A file holding the adsb.lol key, sent as the x-api-key header: re-api.adsb.lol checks it (a bogus key got a 401 on
    # 2026-10-23). It is read again at every poll, so a new key needs no restart, and only its path is kept here, so
    # the key itself never reaches a log or a page. Empty sends no key.
    api_key_file: str = ""
    # The detail drawer's second trace link, beside the local tar1090 one (web/detail.py, index.html): adsb.lol's own
    # globe, over the same timeframe. Independent of `enabled` above -- that is the poller's own switch, and this is
    # just a place to look, not a fetch -- so the link can be on with the poller off, or off with it on. Empty turns
    # the second link off.
    globe_url: str = "https://globe.adsb.lol/"


@dataclass(frozen=True)
class CoverageEvent:
    """One dated change to the receiver, drawn as a line on every coverage chart. `at` is the local day it happened,
    spelled as coverage_daily.day spells a day."""
    at: str
    label: str


@dataclass
class CoverageCfg:
    """The coverage page's marks, in date order."""
    events: tuple[CoverageEvent, ...] = ()


@dataclass
class TypeClasses:
    airliner: frozenset[str] = frozenset()
    bizjet: frozenset[str] = frozenset()
    ga: frozenset[str] = frozenset()      # display-only: the arrivals/live pages' GA filter. Not a
                                           # traffic_class, not read by classify() or keys_for(), and excluded from
                                           # config_hash below -- it changes no score and no outcome.
    categories: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "TypeClasses":
        """Type codes and categories are upper-cased, as tar1090-db writes them. A misspelt table, or a value of the
        wrong shape, is an error naming the file: either would otherwise leave a class silently empty."""
        d = tomllib.loads(Path(path).read_text())
        _check_keys(str(path), d, frozenset({"airliner", "bizjet", "ga", "approach_category"}))

        def codes(key: str) -> frozenset[str]:
            v = d.get(key, [])
            if not isinstance(v, list) or not all(isinstance(c, str) for c in v):
                raise ValueError(f"{path}: {key} must be a list of type codes")
            return frozenset(c.strip().upper() for c in v)

        cats = d.get("approach_category", {})
        if not isinstance(cats, dict) or not all(isinstance(c, str) for c in cats.values()):
            raise ValueError(f"{path}: [approach_category] must map type codes to category letters")
        return cls(codes("airliner"), codes("bizjet"), codes("ga"),
                   {k.strip().upper(): c.strip().upper() for k, c in cats.items()})

    def is_airliner(self, t: str | None) -> bool:
        return t is not None and t.upper() in self.airliner

    def is_bizjet(self, t: str | None) -> bool:
        return t is not None and t.upper() in self.bizjet

    def is_ga(self, t: str | None) -> bool:
        return t is not None and t.upper() in self.ga

    def approach_category(self, t: str | None) -> str | None:
        return None if t is None else self.categories.get(t.upper())


@dataclass
class Config:
    path: Path
    service: ServiceCfg
    source: SourceCfg
    detection: DetectionCfg
    stability: StabilityCfg
    fleet: FleetCfg
    airports: dict[str, Airport]
    type_classes: TypeClasses
    external: ExternalCfg = field(default_factory=ExternalCfg)
    coverage: CoverageCfg = field(default_factory=CoverageCfg)

    def enabled_airports(self) -> list[Airport]:
        return [a for a in self.airports.values() if a.enabled]


_SECTIONS = frozenset({"service", "source", "detection", "stability", "fleet", "airports", "external", "coverage"})


def _typed(where: str, key: str, value: Any, hint: Any) -> Any:
    """`value` checked against a field's type. A whole number is accepted where a float is expected and returned as a
    float; TOML has no null, so a value given for an optional field must have the other type."""
    if get_origin(hint) in (Union, UnionType):
        (inner,) = [a for a in get_args(hint) if a is not type(None)]
        return _typed(where, key, value, inner)
    if get_origin(hint) is list:
        if not isinstance(value, list):
            raise ValueError(f"{where}: {key} must be a list, not {value!r}")
        return [_typed(where, f"{key}[{i}]", v, get_args(hint)[0]) for i, v in enumerate(value)]
    if hint is bool:
        if not isinstance(value, bool):
            raise ValueError(f"{where}: {key} must be true or false, not {value!r}")
        return value
    if hint is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{where}: {key} must be a number, not {value!r}")
        return float(value)
    if hint is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where}: {key} must be a whole number, not {value!r}")
        return value
    if hint is str:
        if not isinstance(value, str):
            raise ValueError(f"{where}: {key} must be a string, not {value!r}")
        return value
    raise TypeError(f"{where}: {key} has a field type the loader does not check: {hint!r}")


PORT_RANGE = range(1, 65536)          # a port outside it loads, then fails only when the service connects or binds
LOG_LEVELS = frozenset({"debug", "info", "warning", "error", "critical"})    # cli.py passes log_level.upper() to it
SOURCE_KINDS = frozenset({"readsb-json"})    # the only kind run_live reads; otherwise it loads, then crash-loops


def _check_timezone(where: str, name: str) -> None:
    """A zone this host's database holds, checked at load rather than at the first rebuild, since a coverage day is
    counted in it. ZoneInfo raises ZoneInfoNotFoundError for a name it cannot find and ValueError for one that is not a
    key at all, such as an absolute path."""
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"{where}: timezone must be an IANA zone name such as 'America/Los_Angeles', "
                         f"not {name!r} ({exc})") from None


def _build(cls, d: Any, where: str):
    if not isinstance(d, dict):
        raise ValueError(f"{where} must be a table")
    _check_keys(where, d, frozenset(f.name for f in fields(cls)))
    hints = get_type_hints(cls)
    values = {k: _typed(where, k, v, hints[k]) for k, v in d.items()}
    if cls in (ServiceCfg, SourceCfg) and values.get("port", PORT_RANGE[0]) not in PORT_RANGE:
        raise ValueError(f"{where}: port must be a port number from 1 to 65535, not {values['port']}")
    if cls is ServiceCfg and values.get("log_level", "info").lower() not in LOG_LEVELS:
        raise ValueError(f"{where}: log_level must be one of {', '.join(sorted(LOG_LEVELS))}, not {values['log_level']!r}")
    if cls is SourceCfg and values.get("kind", "readsb-json") not in SOURCE_KINDS:
        raise ValueError(f"{where}: kind must be one of {', '.join(sorted(SOURCE_KINDS))}, not {values['kind']!r}")
    return cls(**values)


COVERAGE_GATES_FT = (1000, 500)       # coverage_daily's two per-gate columns, since 0001_init.sql


def _check_detection(where: str, det: DetectionCfg) -> None:
    """coverage_daily has a column per gate, not a row per gate — gate1000_observed_frac and gate500_observed_frac,
    since 0001_init.sql — and fleet.coverage_row() fills a gate's column only when one already exists in the row.
    gates_ft must keep naming both 1000 and 500, or the missing gate's column is silently left NULL forever, with no
    error or warning (the two gates are named explicitly because the schema names them, and gates_ft cannot vary them
    without a migration). Further gates are still scored and drawn on the arrivals page; they simply get no coverage
    column."""
    missing = [ft for ft in COVERAGE_GATES_FT if ft not in det.gates_ft]
    if missing:
        raise ValueError(f"{where}: gates_ft must include 1000 and 500 for coverage_daily's columns; missing "
                         f"{' and '.join(map(str, missing))}, not {det.gates_ft!r}")


EXTERNAL_POSITIVE = ("poll_interval_s", "timeout_s", "backoff_min", "watch_max_min")
# The trace backfill's settings, which live polling replaced. Named here so a configuration that still sets one fails
# with that, not as an unknown key with a did-you-mean pointing at something else.
RETIRED_EXTERNAL_KEYS = frozenset({"trace_url", "delay_s", "retry_after_s", "min_interval_s", "max_age_min",
                                   "max_tail_min", "max_nm"})


def _check_external(where: str, ext: ExternalCfg) -> None:
    """The poller's `url` is an http(s) base, not a template; the globe URL is not a template either (it takes its
    query string the same way [service] tar1090_url does, in tar1090_link/tar1090_live_link) and empty is explicitly
    allowed -- that is how the drawer's second link is turned off; every duration is positive, so a zero cannot turn
    off the rate limit or a cap, and `clock_offset_s` may be zero (no shift) and `quiet_s` zero (ask about every watched
    aircraft) but neither may be negative."""
    if ext.globe_url and not ext.globe_url.startswith(("https://", "http://")):
        raise ValueError(f"{where}: globe_url must be empty, or an http(s) URL, not {ext.globe_url!r}")
    if not ext.url.startswith(("https://", "http://")):
        raise ValueError(f"{where}: url must be an http(s) URL, not {ext.url!r}")
    for key in EXTERNAL_POSITIVE:
        if getattr(ext, key) <= 0:
            raise ValueError(f"{where}: {key} must be greater than 0, not {getattr(ext, key)}")
    for key in ("clock_offset_s", "quiet_s"):
        if getattr(ext, key) < 0:
            raise ValueError(f"{where}: {key} must be 0 or more, not {getattr(ext, key)}")


def _event_day(where: str, value: Any) -> str:
    """A mark's `at` as YYYY-MM-DD, from TOML's own date (`at = 2026-10-04`) or from that date written as a string.
    A date-time is refused: a mark is a day, and the page draws it between two days."""
    if type(value) is date:                       # a datetime is a date subclass, and is not a day
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            pass
    raise ValueError(f"{where}: at must be a date, written 2026-10-04, not {value!r}")


def _event_label(where: str, value: Any) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise ValueError(f'{where}: label must say what changed, for example "new antenna", not {value!r}')
    return text


def _build_coverage(d: Any, where: str) -> CoverageCfg:
    """[coverage] and its [[coverage.events]] marks. `_build` cannot make this one: `events` is an array of tables
    rather than a field with a type to check, and tomllib gives a bare `at = 2026-10-04` as a datetime.date. Two marks
    on one day are refused, since the before/after table's periods run from one mark to the next and two on a day would
    leave one of them nothing to compare."""
    if not isinstance(d, dict):
        raise ValueError(f"{where} must be a table")
    _check_keys(where, d, frozenset({"events"}))
    raw = d.get("events", [])
    if not isinstance(raw, list) or not all(isinstance(e, dict) for e in raw):
        raise ValueError(f"{where}: events must be a list of [[coverage.events]] tables")
    events: list[CoverageEvent] = []
    for i, e in enumerate(raw):
        mark = f"{where} events[{i}]"
        _check_keys(mark, e, frozenset({"at", "label"}), required=frozenset({"at", "label"}))
        events.append(CoverageEvent(at=_event_day(mark, e["at"]), label=_event_label(mark, e["label"])))
    days = sorted(e.at for e in events)
    twice = next((b for a, b in zip(days, days[1:]) if a == b), None)
    if twice is not None:
        raise ValueError(f"{where}: two marks on {twice}; a mark is a day, and the before/after table's periods run "
                         f"from one mark to the next")
    return CoverageCfg(events=tuple(sorted(events, key=lambda e: e.at)))


_RUNWAY_DEFAULTS: dict[str, Any] = {
    "enabled": True, "threshold_hae_m": None, "displaced_threshold_ft": 0.0,
    "localizer_lat": None, "localizer_lon": None, "localizer_offset_ft": 1000.0,
    "loc_width_deg": None, "glide_path_deg": 3.0, "tch_ft": 50.0, "ils": None,
    "reference_source": "assumed", "paths": (),
}
_RUNWAY_KEYS = frozenset(f.name for f in fields(RunwayEnd)) - {"airport", "ident", "parallel_group"}
_AIRPORT_KEYS = frozenset({"icao", "name", "enabled", "lat", "lon", "elevation_ft", "metar", "declination_deg",
                           "geoid_offset_ft", "parallel_groups"})
_AIRPORT_OVERRIDE_KEYS = frozenset({"enabled", "name", "metar"})     # the rest of [airport] is reference data


_REMOVED_KEYS = {        # the nearest remaining names read another unit, so name the replacement instead
    "established_min_s": "removed: set established_window_s, in seconds; 30 by default",
    "runway_change_min_points": "removed: set runway_change_min_s, in seconds rather than positions; 10 by default",
}


def _check_keys(where: str, d: dict[str, Any], allowed: frozenset[str], required: frozenset[str] = frozenset()) -> None:
    unknown = sorted(set(d) - allowed)
    if unknown:
        hints = [repr(k) + (f" ({_REMOVED_KEYS[k]})" if k in _REMOVED_KEYS else
                            "".join(f" (did you mean {m!r}?)" for m in difflib.get_close_matches(k, allowed, n=1)))
                 for k in unknown]
        raise ValueError(f"{where}: unknown key {', '.join(hints)}")
    missing = sorted(required - set(d))
    if missing:
        raise ValueError(f"{where}: missing {', '.join(map(repr, missing))}")


def _flag(where: str, v: Any) -> bool:
    if not isinstance(v, bool):                 # bool("false") is True
        raise ValueError(f"{where}: enabled must be true or false, not {v!r}")
    return v


_PATH_KEYS = frozenset({"name", "label", "source", "points"})


def _path_from_dict(where: str, d: Any) -> ProcedurePath:
    """One [[runways."28R".paths]] table; validate_path checks what its values mean."""
    if not isinstance(d, dict):
        raise ValueError(f"{where} must be a table")
    _check_keys(where, d, _PATH_KEYS, required=_PATH_KEYS)
    points = _typed(where, "points", d["points"], list[list[float]])
    for i, pt in enumerate(points):
        if len(pt) != 2 or not (-90.0 <= pt[0] <= 90.0 and -180.0 <= pt[1] <= 180.0):
            raise ValueError(f"{where}: points[{i}] must be [lat, lon] in degrees, not {d['points'][i]!r}")
    return ProcedurePath(name=_typed(where, "name", d["name"], str), label=_typed(where, "label", d["label"], str),
                         source=_typed(where, "source", d["source"], str),
                         points=tuple((pt[0], pt[1]) for pt in points))


def runway_from_dict(airport: str, ident: str, d: dict[str, Any], parallel: tuple[str, ...]) -> RunwayEnd:
    where = f"runway {airport}/{ident}"
    _check_keys(where, d, _RUNWAY_KEYS, required=_RUNWAY_KEYS - set(_RUNWAY_DEFAULTS))
    merged = {**_RUNWAY_DEFAULTS, **d}
    rwy = RunwayEnd(
        airport=airport, ident=ident, enabled=_flag(where, merged["enabled"]),
        threshold_lat=float(merged["threshold_lat"]), threshold_lon=float(merged["threshold_lon"]),
        threshold_elev_ft=float(merged["threshold_elev_ft"]),
        threshold_hae_m=None if merged["threshold_hae_m"] is None else float(merged["threshold_hae_m"]),
        course_true_deg=float(merged["course_true_deg"]), runway_length_ft=float(merged["runway_length_ft"]),
        displaced_threshold_ft=float(merged["displaced_threshold_ft"]),
        localizer_lat=None if merged["localizer_lat"] is None else float(merged["localizer_lat"]),
        localizer_lon=None if merged["localizer_lon"] is None else float(merged["localizer_lon"]),
        localizer_offset_ft=float(merged["localizer_offset_ft"]),
        loc_width_deg=None if merged["loc_width_deg"] is None else float(merged["loc_width_deg"]),
        glide_path_deg=float(merged["glide_path_deg"]), tch_ft=float(merged["tch_ft"]),
        ils=merged["ils"], reference_source=str(merged["reference_source"]), parallel_group=parallel,
    )
    raw = merged["paths"]
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f'{where}: paths must be a list of [[runways."{ident}".paths]] tables')
    paths: list[ProcedurePath] = []
    for i, entry in enumerate(raw):
        path = entry if isinstance(entry, ProcedurePath) else _path_from_dict(f"{where} paths[{i}]", entry)
        validate_path(rwy, path)
        if any(path.name == p.name for p in paths):
            raise ValueError(f"{where}: duplicate path name {path.name!r}")
        paths.append(path)
    return replace(rwy, paths=tuple(paths))


def airport_from_dict(d: dict[str, Any]) -> Airport:
    a = d.get("airport", {})
    where = f"airport {a.get('icao', '?')}"
    _check_keys(where, d, frozenset({"airport", "runways"}), required=frozenset({"airport"}))
    _check_keys(where, a, _AIRPORT_KEYS, required=frozenset({"icao", "lat", "lon"}))
    groups = [tuple(g) for g in a.get("parallel_groups", [])]
    runways: dict[str, RunwayEnd] = {}
    for ident, rd in d.get("runways", {}).items():
        parallel = next((g for g in groups if ident in g), ())
        runways[ident] = runway_from_dict(a["icao"], ident, rd, parallel)
    return Airport(
        icao=a["icao"], name=a.get("name", a["icao"]), enabled=_flag(where, a.get("enabled", True)),
        lat=float(a["lat"]), lon=float(a["lon"]), elevation_ft=float(a.get("elevation_ft", 0)),
        metar=str(a.get("metar", a["icao"])).upper(), geoid_offset_ft=float(a.get("geoid_offset_ft", 0.0)),
        declination_deg=float(a.get("declination_deg", 0.0)), runways=runways,
    )


def load_airport_file(path: str | Path) -> Airport:
    try:
        return airport_from_dict(tomllib.loads(Path(path).read_text()))
    except ValueError as e:                     # tomllib.TOMLDecodeError is a ValueError too
        raise ValueError(f"{path}: {e}") from e


def _apply_overrides(apt: Airport, ov: dict[str, Any]) -> None:
    where = f"[airports.{apt.icao}]"
    fixed = sorted(set(ov) & (_AIRPORT_KEYS - _AIRPORT_OVERRIDE_KEYS))
    if fixed:
        raise ValueError(f"{where}: {', '.join(map(repr, fixed))} cannot be overridden here; "
                         "change the airport file or its build-config --overrides file")
    _check_keys(where, ov, _AIRPORT_OVERRIDE_KEYS | {"runways"})
    if "enabled" in ov:
        apt.enabled = _flag(where, ov["enabled"])
    if "name" in ov:
        apt.name = _typed(where, "name", ov["name"], str)
    if "metar" in ov:
        apt.metar = _typed(where, "metar", ov["metar"], str).upper()      # WeatherTable lookups are case-sensitive
    runway_hints = get_type_hints(RunwayEnd)
    for ident, rd in ov.get("runways", {}).items():
        base = apt.runways.get(ident)
        if base is None:
            raise ValueError(f"override for unknown runway {apt.icao}/{ident}")
        rwhere = f"runway {apt.icao}/{ident}"
        if "paths" in rd:
            raise ValueError(f"{rwhere}: paths: set procedure paths in the airport's build-config --overrides file, "
                             "not here")
        _check_keys(rwhere, rd, _RUNWAY_KEYS - {"paths"})
        typed_rd = {k: (_flag(rwhere, v) if k == "enabled" else _typed(rwhere, k, v, runway_hints[k]))
                   for k, v in rd.items()}
        current = {f.name: getattr(base, f.name) for f in fields(RunwayEnd)}
        current.pop("airport")
        current.pop("ident")
        current.pop("parallel_group")
        current.update(typed_rd)
        apt.runways[ident] = runway_from_dict(apt.icao, ident, current, base.parallel_group)


ENV_PREFIX = "ADSB_APPROACH_"
ENV_SETTINGS: dict[str, tuple[str, str]] = {        # variable -> (section, key): only the settings that locate a receiver
    "ADSB_APPROACH_SOURCE_HOST": ("source", "host"),
    "ADSB_APPROACH_SOURCE_PORT": ("source", "port"),
    "ADSB_APPROACH_TAR1090_URL": ("service", "tar1090_url"),
}


def _apply_env(raw: dict[str, Any], env: Mapping[str, str]) -> None:
    """Set the receiver's location from ADSB_APPROACH_* variables as if the file held them, so each value still goes
    through its section's type checks. A variable with the prefix that names no setting is an error with a
    did-you-mean hint, like an unknown key, and so is an empty value."""
    for name in sorted(k for k in env if k.startswith(ENV_PREFIX)):
        if name not in ENV_SETTINGS:
            hint = "".join(f" (did you mean {m}?)" for m in difflib.get_close_matches(name, list(ENV_SETTINGS), n=1))
            raise ValueError(f"environment: unknown variable {name}{hint}")
        section, key = ENV_SETTINGS[name]
        text = env[name].strip()
        if not text:
            raise ValueError(f"environment: {name} is empty")
        table = raw.setdefault(section, {})
        if not isinstance(table, dict):
            continue                                   # _build reports the section that is not a table
        if get_type_hints(SourceCfg if section == "source" else ServiceCfg)[key] is int:
            if not (text.isascii() and text.isdigit()):          # int() also takes "+1", "30_047" and non-ASCII digits
                raise ValueError(f"environment: {name} must be a whole number, not {text!r}")
            if int(text) not in PORT_RANGE:                       # named here rather than later as the file's key
                raise ValueError(f"environment: {name} must be a port number from 1 to 65535, not {text!r}")
            table[key] = int(text)
        else:
            table[key] = text


def load_config(path: str | Path, *, env: Mapping[str, str] | None = None) -> Config:
    """Load and validate a configuration. `env` (the live service passes os.environ) may set the receiver's location,
    see ENV_SETTINGS; replay and tests pass nothing, so the shell's environment cannot change their results."""
    path = Path(path).resolve()
    raw = tomllib.loads(path.read_text())
    base_dir = path.parent
    _check_keys(str(path), raw, _SECTIONS)             # a misspelt [section] would otherwise be ignored
    if env is not None:
        _apply_env(raw, env)

    def resolve(p: str) -> str:
        return p if Path(p).is_absolute() else str((base_dir / p).resolve())

    service = _build(ServiceCfg, raw.get("service", {}), f"{path}: [service]")
    if service.timezone is not None:                # checked here, as [external] is, and not inside _build: two more
        _check_timezone(f"{path}: [service]", service.timezone)     # statements there cost Pyright its call-site
    service.db_path = resolve(service.db_path)                      # return inference and every caller its type
    service.airports_dir = resolve(service.airports_dir)
    if service.typedb_path is not None:
        service.typedb_path = resolve(service.typedb_path)
    if service.type_classes_path is not None:
        service.type_classes_path = resolve(service.type_classes_path)
    service.nearby_airports_path = resolve(service.nearby_airports_path)

    airports: dict[str, Airport] = {}
    files: dict[str, Path] = {}
    apt_dir = Path(service.airports_dir)
    if apt_dir.is_dir():
        for f in sorted(apt_dir.glob("*.toml")):
            apt = load_airport_file(f)
            if apt.icao in files:
                raise ValueError(f"{f}: {apt.icao} is already defined in {files[apt.icao]}")
            airports[apt.icao], files[apt.icao] = apt, f
    for icao, ov in raw.get("airports", {}).items():
        try:
            if icao in airports:
                _apply_overrides(airports[icao], ov)
            elif "airport" in ov:                      # a whole airport inline in the main file
                airports[icao] = airport_from_dict(ov)
            else:
                raise ValueError(f"[airports.{icao}] overrides an airport with no file in {apt_dir}")
        except ValueError as e:
            raise ValueError(f"{path}: {e}") from e
    if not airports:
        raise ValueError(f"{path}: no airports configured; {apt_dir} "
                         + ("holds no *.toml files" if apt_dir.is_dir() else "does not exist"))

    detection = _build(DetectionCfg, raw.get("detection", {}), f"{path}: [detection]")
    _check_detection(f"{path}: [detection]", detection)
    external_raw = raw.get("external", {})
    retired = sorted(RETIRED_EXTERNAL_KEYS & set(external_raw)) if isinstance(external_raw, dict) else []
    if retired:                                         # before _build, so it wins over the unknown-key error
        raise ValueError(f"{path}: [external]: {retired[0]} is retired: the trace backfill was replaced by live "
                         f"polling")
    external = _build(ExternalCfg, external_raw, f"{path}: [external]")
    _check_external(f"{path}: [external]", external)
    if external.api_key_file:
        external.api_key_file = resolve(external.api_key_file)
    coverage = _build_coverage(raw.get("coverage", {}), f"{path}: [coverage]")
    type_classes = TypeClasses.load(service.type_classes_path) if service.type_classes_path else TypeClasses()
    return Config(
        path=path, service=service, source=_build(SourceCfg, raw.get("source", {}), f"{path}: [source]"),
        detection=detection,
        stability=_build(StabilityCfg, raw.get("stability", {}), f"{path}: [stability]"),
        fleet=_build(FleetCfg, raw.get("fleet", {}), f"{path}: [fleet]"),
        airports=airports, type_classes=type_classes, external=external, coverage=coverage,
    )


def zone_of(cfg: Config) -> tzinfo | None:
    """The zone a coverage day is counted in: `[service] timezone`, or None for the host's own zone — which is what
    `datetime.fromtimestamp(t, None)` and a naive `datetime.timestamp()` already use, daylight saving included, so None
    needs no name of its own."""
    return None if cfg.service.timezone is None else ZoneInfo(cfg.service.timezone)


def _canonical(v: Any) -> Any:
    """A JSON-ready value with one spelling per meaning: every number a float (45 and 45.0 alike), sets sorted."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        return {str(k): _canonical(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_canonical(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted(_canonical(x) for x in v)
    raise TypeError(f"config_hash: unexpected {type(v).__name__}")


def config_hash(cfg: Config) -> str:
    """16 hex digits naming everything that changes a replay's scores and outcomes: [detection], [stability], [fleet],
    the METAR age limits, the type classes, and every airport and runway as loaded (airport files plus overrides),
    procedure paths included. File paths, network settings and TOML formatting are left out, so one configuration
    hashes alike on every machine. So are [service] timezone and [coverage]: they cut and annotate the coverage days the
    rebuild writes, and change no score and no outcome. So is type_classes.ga: it only feeds the arrivals and live
    pages' GA filter, is never read by classify() or a fleet key, and likewise changes neither."""
    type_classes = {k: v for k, v in asdict(cfg.type_classes).items() if k != "ga"}
    effective = {
        "detection": asdict(cfg.detection), "stability": asdict(cfg.stability), "fleet": asdict(cfg.fleet),
        "metar_max_age_min": cfg.service.metar_max_age_min, "qnh_max_age_min": cfg.service.qnh_max_age_min,
        "type_classes": type_classes,
        "airports": {icao: asdict(apt) for icao, apt in cfg.airports.items()},
    }
    text = json.dumps(_canonical(effective), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

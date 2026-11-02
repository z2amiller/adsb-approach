"""Generate per-airport TOML from CIFP (primary) and OurAirports (fallback)."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from ..altitude import true_from_magnetic
from ..config import airport_from_dict
from ..geometry import FT_PER_M, NM_FT, bearing_deg, destination, distance_ft
from .cifp import CifpAirport, CifpData, CifpLocalizer, CifpRunway, parse_cifp
from .ourairports import OaAirport, OaRunwayEnd, load_ourairports

_PARALLEL_RE = re.compile(r"^(\d{2})[LRC]$")
_IDENT_RE = re.compile(r"^(\d{1,2})([LRC]?)$")
# OurAirports reuses codes across countries and keeps retired FAA idents, so a match can be another field. Built from
# FAACIFP18 and the 2026-09 download, OurAirports runway ends lie within 3.8 nm of their CIFP airport or beyond 60 nm.
OA_MAX_FROM_CIFP_NM = 10.0


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        s = repr(v)
        return s if ("." in s or "e" in s) else s + ".0"
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    raise TypeError(f"cannot format {type(v).__name__}")


def _key(k: str) -> str:
    """Bare key when it looks like an identifier; quoted otherwise (runway idents start with digits)."""
    return k if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", k) else '"' + k + '"'


def _is_table_array(v: Any) -> bool:
    return isinstance(v, (list, tuple)) and bool(v) and all(isinstance(x, dict) for x in v)


def to_toml(d: dict[str, Any]) -> str:
    """Minimal TOML emitter: top-level tables of scalars/lists, one level of sub-tables, and in a sub-table a list of
    dicts as an array of tables ([[runways."28R".paths]]) after that sub-table's scalars. None values are skipped."""
    out: list[str] = []
    for table, body in d.items():
        scalars = {k: v for k, v in body.items() if not isinstance(v, dict)}
        subs = {k: v for k, v in body.items() if isinstance(v, dict)}
        if scalars:
            out.append(f"[{_key(table)}]")
            for k, v in scalars.items():
                if v is not None:
                    out.append(f"{_key(k)} = {_fmt(v)}")
            out.append("")
        for name, sub in subs.items():
            out.append(f"[{_key(table)}.{_key(name)}]")
            arrays = {k: v for k, v in sub.items() if _is_table_array(v)}
            for k, v in sub.items():
                if v is not None and k not in arrays:
                    out.append(f"{_key(k)} = {_fmt(v)}")
            out.append("")
            for k, rows in arrays.items():
                for row in rows:
                    out.append(f"[[{_key(table)}.{_key(name)}.{_key(k)}]]")
                    for rk, rv in row.items():
                        if rv is not None:
                            out.append(f"{_key(rk)} = {_fmt(rv)}")
                    out.append("")
    return "\n".join(out)


def parallel_groups(idents: list[str]) -> list[list[str]]:
    groups: dict[str, list[str]] = {}
    for ident in idents:
        m = _PARALLEL_RE.match(ident)
        if m:
            groups.setdefault(m.group(1), []).append(ident)
    return [sorted(g) for _, g in sorted(groups.items()) if len(g) > 1]


def canonical_ident(ident: str) -> str:
    """CIFP spelling of a runway end: OurAirports writes 1L where CIFP writes 01L."""
    m = _IDENT_RE.match(ident.strip())
    return f"{int(m.group(1)):02d}{m.group(2)}" if m else ident.strip()


def opposite_ident(ident: str) -> str | None:
    """The same runway's other end: number +/- 18 within 01-36, L and R swapped, C kept (28R -> 10L, 12 -> 30)."""
    m = _IDENT_RE.match(ident.strip())
    if m is None:
        return None
    side = {"L": "R", "R": "L"}.get(m.group(2), m.group(2))
    return f"{(int(m.group(1)) + 17) % 36 + 1:02d}{side}"


def _ident_bearing_deg(ident: str) -> float:
    """A runway's magnetic bearing from its number, good to 5 degrees: 13 is 130, 36 is 0."""
    m = _IDENT_RE.match(ident.strip())
    return float(int(m.group(1)) * 10 % 360) if m else 0.0


def _course_between(lat: float, lon: float, opp_lat: float, opp_lon: float) -> float:
    """Initial great-circle bearing from a landing threshold to the opposite one, on the coordinates as written (6 dp)."""
    return round(bearing_deg(round(lat, 6), round(lon, 6), round(opp_lat, 6), round(opp_lon, 6)), 3)


def _course_from_cifp(ident: str, pg: CifpRunway, pi: CifpLocalizer | None, opp: CifpRunway | None,
                      apt_decl: float | None) -> float:
    """A CIFP runway's true course: the great circle to the opposite threshold when CIFP has it, else the PG bearing
    (or, for a blank one, the runway number) turned to true with the localizer's declination, else the airport's."""
    if opp is not None:
        return _course_between(pg.lat, pg.lon, opp.lat, opp.lon)
    # PG runway bearings are whole degrees: used only when the opposite threshold is unknown
    decl = (pi.declination_deg if pi and pi.declination_deg is not None else apt_decl) or 0.0
    if pg.mag_bearing_deg is None:            # a blank PG bearing (private strips such as 13FD): the runway number
        return round(true_from_magnetic(_ident_bearing_deg(ident), decl), 1)
    if pg.bearing_is_true:
        return round(pg.mag_bearing_deg, 1)
    return round(true_from_magnetic(pg.mag_bearing_deg, decl), 1)


def _localizer_entries(pi: CifpLocalizer) -> dict[str, Any]:
    """The runway-table entries that come from the runway's localizer: its position, width and ILS label."""
    cat = f" CAT {pi.cat}" if pi.cat else ""
    return {
        "localizer_lat": round(pi.lat, 6),
        "localizer_lon": round(pi.lon, 6),
        "loc_width_deg": pi.width_deg,
        "ils": f"{pi.ident} {pi.freq_mhz:.2f}{cat}" if pi.freq_mhz else pi.ident,
    }


def _cifp_glide_path(icao: str, ident: str, cifp: CifpData, pg: CifpRunway,
                     pi: CifpLocalizer | None) -> tuple[float | None, float | None]:
    """(glide path angle, threshold crossing height) for a CIFP runway, each None when CIFP gives none. The angle
    comes from the first source that has one: the ILS glide slope, the first path point with an angle, the first
    published approach angle. The height starts as the PG record's; the glide slope's own replaces it, and a path
    point's fills it only while still unknown."""
    gp: float | None = None
    tch: float | None = float(pg.tch_ft) if pg.tch_ft is not None else None
    if pi is not None and pi.gs_angle_deg:
        gp = pi.gs_angle_deg
        tch = float(pi.tch_ft) if pi.tch_ft is not None else tch
    if gp is None:
        for pp in cifp.path_points.get((icao, ident), []):
            if pp.gpa_deg:
                gp = pp.gpa_deg
                tch = tch if tch is not None else pp.tch_ft
                break
    if gp is None:
        angles = cifp.approach_angles.get((icao, ident), [])
        if angles:
            gp = angles[0].vertical_angle_deg
    return gp, tch


def _runway_from_cifp(icao: str, ident: str, cifp: CifpData, apt_decl: float | None, apt_elev_ft: float,
                      tag: str) -> dict[str, Any]:
    pg = cifp.runways[(icao, ident)]
    pi = cifp.localizers.get((icao, ident))
    opp = cifp.runways.get((icao, opposite_ident(ident) or ""))
    course = _course_from_cifp(ident, pg, pi, opp, apt_decl)
    d: dict[str, Any] = {
        "enabled": True,
        "threshold_lat": round(pg.lat, 6), "threshold_lon": round(pg.lon, 6),
        "threshold_elev_ft": pg.elev_ft if pg.elev_ft is not None else apt_elev_ft,
        "threshold_hae_m": pg.hae_m,
        "course_true_deg": course,
        "runway_length_ft": pg.length_ft or 0,
        "displaced_threshold_ft": pg.displaced_ft,
        "localizer_offset_ft": 1000,
    }
    if pi is not None:
        d.update(_localizer_entries(pi))
    gp, tch = _cifp_glide_path(icao, ident, cifp, pg, pi)
    assumed = gp is None
    d["glide_path_deg"] = gp if gp is not None else 3.0
    d["tch_ft"] = tch if tch is not None else 50
    d["reference_source"] = f"cifp:{tag}" + ("+assumed" if assumed else "")
    return d


def _oa_threshold(end: OaRunwayEnd, opposite: OaRunwayEnd | None) -> tuple[float, float]:
    """Runway end moved displaced_ft towards the opposite end (along the OurAirports heading when that end is unknown)."""
    if not end.displaced_ft:
        return end.lat, end.lon
    towards = (bearing_deg(end.lat, end.lon, opposite.lat, opposite.lon) if opposite is not None
               else end.heading_true_deg or 0.0)
    return destination(end.lat, end.lon, towards, end.displaced_ft)


def _runway_from_oa(end: OaRunwayEnd, opposite: OaRunwayEnd | None, airport_elev_ft: float) -> dict[str, Any]:
    """A runway end OurAirports alone knows, on an assumed 3.00 deg path; a blank end elevation (common outside the US)
    is the airport's."""
    lat, lon = _oa_threshold(end, opposite)
    if opposite is not None:
        course = _course_between(lat, lon, *_oa_threshold(opposite, end))
    else:
        course = round(end.heading_true_deg or 0.0, 1)
    return {
        "enabled": not end.closed, "threshold_lat": round(lat, 6), "threshold_lon": round(lon, 6),
        "threshold_elev_ft": end.elev_ft if end.elev_ft is not None else airport_elev_ft, "course_true_deg": course,
        "runway_length_ft": end.length_ft or 0, "displaced_threshold_ft": end.displaced_ft,
        "localizer_offset_ft": 1000, "glide_path_deg": 3.0, "tch_ft": 50,
        "reference_source": "ourairports+assumed",
    }


def _oa_ends_of_airport(icao: str, pa: CifpAirport | None, oa_runways: dict | None) -> dict[str, OaRunwayEnd]:
    """The OurAirports runway ends of `icao`, by CIFP spelling of the ident. With a CIFP airport to compare against,
    ends that lie too far from it to be the same field are dropped (OA_MAX_FROM_CIFP_NM)."""
    return {canonical_ident(ident): end for (apt, ident), end in (oa_runways or {}).items() if apt == icao
            and (pa is None or distance_ft(pa.lat, pa.lon, end.lat, end.lon) <= OA_MAX_FROM_CIFP_NM * NM_FT)}


def _geoid_offset_ft(runways: dict[str, dict[str, Any]]) -> float:
    """The airport's geoid offset: the mean, over the runways whose threshold carries a HAE, of that height above the
    ellipsoid less the threshold elevation; 0.0 when none does."""
    haes = [(r["threshold_hae_m"] * FT_PER_M - r["threshold_elev_ft"]) for r in runways.values()
            if r.get("threshold_hae_m") is not None]
    return round(sum(haes) / len(haes), 1) if haes else 0.0


def build_airport(icao: str, cifp: CifpData, oa_runways: dict | None, oa_airports: dict | None,
                  cifp_tag: str) -> dict[str, Any]:
    pa = cifp.airports.get(icao)
    oa_apt: OaAirport | None = (oa_airports or {}).get(icao)
    if pa is None and oa_apt is None:
        raise ValueError(f"{icao}: not in CIFP or OurAirports")
    runways: dict[str, dict[str, Any]] = {}
    oa = _oa_ends_of_airport(icao, pa, oa_runways)
    idents = sorted({k[1] for k in cifp.runways if k[0] == icao} | set(oa))
    elevation_ft = pa.elev_ft if pa else (oa_apt.elev_ft or 0)     # type: ignore[union-attr]
    for ident in idents:
        if (icao, ident) in cifp.runways:
            runways[ident] = _runway_from_cifp(icao, ident, cifp, pa.declination_deg if pa else None, elevation_ft,
                                               cifp_tag)
        else:
            runways[ident] = _runway_from_oa(oa[ident], oa.get(opposite_ident(ident) or ""), elevation_ft)
    geoid = _geoid_offset_ft(runways)
    airport = {
        "icao": icao,
        "name": pa.name if pa else oa_apt.name,                     # type: ignore[union-attr]
        "enabled": True,
        "lat": round(pa.lat if pa else oa_apt.lat, 6),              # type: ignore[union-attr]
        "lon": round(pa.lon if pa else oa_apt.lon, 6),              # type: ignore[union-attr]
        "elevation_ft": elevation_ft,
        "metar": icao,
        "declination_deg": (pa.declination_deg if pa and pa.declination_deg is not None else 0.0),
        "geoid_offset_ft": geoid,
        "parallel_groups": parallel_groups(idents),
    }
    return {"airport": airport, "runways": runways}


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def build_all(icaos: list[str], cifp_path: str | Path, out_dir: str | Path, ourairports_dir: str | Path | None = None,
              overrides_dir: str | Path | None = None, cifp_tag: str = "cifp") -> list[Path]:
    cifp = parse_cifp(cifp_path, airports=set(icaos))
    oa_r = oa_a = None
    if ourairports_dir is not None:
        oa_r, oa_a = load_ourairports(ourairports_dir, set(icaos))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for icao in icaos:
        d = build_airport(icao, cifp, oa_r, oa_a, cifp_tag)
        if overrides_dir is not None:
            ov_path = Path(overrides_dir) / f"{icao.lower()}.toml"
            if ov_path.exists():
                _deep_merge(d, tomllib.loads(ov_path.read_text()))
                try:
                    airport_from_dict(d)              # a typo in the overrides file fails here, not at service start
                except ValueError as e:
                    raise ValueError(f"{ov_path}: {e}") from e
        path = out_dir / f"{icao.lower()}.toml"
        path.write_text(to_toml(d))
        written.append(path)
    return written

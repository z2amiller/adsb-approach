"""OurAirports runways.csv / airports.csv readers (fallback reference data)."""
from __future__ import annotations

import csv
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..geometry import NM_FT, distance_ft

MATCH_COLUMNS = ("ident", "gps_code", "local_code")    # CIFP writes 00C for the field OurAirports files as K00C
NEARBY_TYPES = frozenset({"large_airport", "medium_airport", "small_airport"})     # of the file's ten or so "type" values


def _f(v: str) -> float | None:
    v = (v or "").strip()
    return float(v) if v else None


def _i(v: str) -> int | None:
    f = _f(v)
    return None if f is None else int(round(f))


@dataclass(slots=True)
class OaRunwayEnd:
    airport: str
    ident: str
    lat: float
    lon: float
    elev_ft: int | None
    heading_true_deg: float | None
    displaced_ft: int
    length_ft: int | None
    width_ft: int | None
    closed: bool


@dataclass(slots=True)
class OaAirport:
    icao: str                   # the ident it was requested as
    name: str
    lat: float
    lon: float
    elev_ft: int | None
    oa_ident: str = ""          # OurAirports' own ident, under which runways.csv files its runways


def parse_runways(path: str | Path, airports: set[str] | None = None) -> dict[tuple[str, str], OaRunwayEnd]:
    out: dict[tuple[str, str], OaRunwayEnd] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            apt = row["airport_ident"].strip()
            if airports is not None and apt not in airports:
                continue
            for end in ("le", "he"):
                ident = row[f"{end}_ident"].strip()
                lat, lon = _f(row[f"{end}_latitude_deg"]), _f(row[f"{end}_longitude_deg"])
                if not ident or lat is None or lon is None:
                    continue
                out[(apt, ident)] = OaRunwayEnd(
                    airport=apt, ident=ident, lat=lat, lon=lon,
                    elev_ft=_i(row[f"{end}_elevation_ft"]), heading_true_deg=_f(row[f"{end}_heading_degT"]),
                    displaced_ft=_i(row[f"{end}_displaced_threshold_ft"]) or 0,
                    length_ft=_i(row["length_ft"]), width_ft=_i(row["width_ft"]),
                    closed=row.get("closed", "0").strip() == "1",
                )
    return out


def parse_airports(path: str | Path, airports: set[str] | None = None) -> dict[str, OaAirport]:
    """Airports keyed by ident. With `airports`, a requested ident matches an OurAirports ident first, then a
    gps_code, then a local_code, and the airport is keyed by the ident it was requested as."""
    out: dict[str, OaAirport] = {}
    rank: dict[str, int] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            lat, lon = _f(row["latitude_deg"]), _f(row["longitude_deg"])
            if lat is None or lon is None:
                continue
            ident = row["ident"].strip()
            codes = [ident] if airports is None else [(row.get(column) or "").strip() for column in MATCH_COLUMNS]
            for r, code in enumerate(codes):
                if code and (airports is None or code in airports) and r < rank.get(code, len(MATCH_COLUMNS)):
                    rank[code] = r
                    out[code] = OaAirport(icao=code, name=row["name"], lat=lat, lon=lon, elev_ft=_i(row["elevation_ft"]),
                                          oa_ident=ident)
    return out


def parse_nearby(path: str | Path, centres: Sequence[tuple[float, float]], radius_nm: float = 150.0) -> list[dict[str, Any]]:
    """The airports of an airports.csv within `radius_nm` of any of `centres` (latitude, longitude), as rows
    {ident, name, type, lat, lon} sorted by ident. Only NEARBY_TYPES are kept; the file's heliports, seaplane bases,
    balloonports and closed fields are left out."""
    limit_ft = radius_nm * NM_FT
    found: list[dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            kind = row["type"].strip()
            lat, lon = _f(row["latitude_deg"]), _f(row["longitude_deg"])
            if kind not in NEARBY_TYPES or lat is None or lon is None:
                continue
            if any(distance_ft(clat, clon, lat, lon) <= limit_ft for clat, clon in centres):
                found.append({"ident": row["ident"].strip(), "name": row["name"].strip(), "type": kind, "lat": lat, "lon": lon})
    return sorted(found, key=lambda r: r["ident"])


def load_ourairports(dir_path: str | Path, idents: set[str]) -> tuple[dict[tuple[str, str], OaRunwayEnd], dict[str, OaAirport]]:
    """Runway ends and airports for `idents` from an OurAirports download, both keyed by the requested ident. runways.csv
    files runways under OurAirports' ident, which for a requested ident may be the airport whose gps_code or local_code
    it is; an ident missing from airports.csv keeps any runways filed under it. One OurAirports airport can answer two
    requested idents (its ident is a retired FAA ident that CIFP gives to another field): both get its runways, and
    build_airport keeps them only where they lie near the CIFP airport."""
    airports = parse_airports(Path(dir_path) / "airports.csv", airports=idents)
    requested: dict[str, list[str]] = {}                  # OurAirports ident -> the requested idents it answers
    for icao, apt in airports.items():
        requested.setdefault(apt.oa_ident, []).append(icao)
    for ident in sorted(i for i in idents if i not in airports):
        requested.setdefault(ident, []).append(ident)
    ends = parse_runways(Path(dir_path) / "runways.csv", airports=set(requested))
    return {(icao, ident): end for (apt, ident), end in ends.items() for icao in requested[apt]}, airports

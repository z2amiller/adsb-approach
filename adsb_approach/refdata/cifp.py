"""Parser for the FAA CIFP (ARINC 424-18, fixed-width 132-column records).

Only the record types the approach monitor needs: PA airports, PG runways,
PI localizer/glide slope, PF approach legs (vertical angle), PP path points.
Column numbers in comments are 1-based as in the ARINC spec.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

FT_PER_M = 3.280839895
_PROC_RE = re.compile(r"^[A-Z](\d{2}[LRC]?)-?([A-Z])?$")   # I28R, R28RZ, R30-Y; not VOR-A
# Continuation record number column (ARINC 424-18 5.16) per subsection: 0 or 1 marks the primary record,
# 2-9 and A-Z a continuation. FAA CIFP PF "W" continuations carry RNP values that can reach columns 103-106.
_CONT_COL = {"A": 22, "G": 22, "I": 22, "F": 39, "P": 27}


def _c(line: str, start: int, end: int) -> str:
    """1-based inclusive column slice."""
    return line[start - 1:end]


def _int(s: str) -> int | None:
    s = s.strip()
    return int(s) if s.lstrip("+-").isdigit() and s else None


def _scaled(s: str, scale: float) -> float | None:
    v = _int(s)
    return None if v is None else round(v * scale, 4)


def parse_coord(s: str) -> float:
    """'N37365011' (DD MM SS.SS), 'W122212901' (DDD MM SS.SS) and the 4-decimal path-point forms."""
    hemi = s[0]
    digits = s[1:]
    deg_w = 2 if hemi in "NS" else 3
    deg = int(digits[:deg_w])
    minutes = int(digits[deg_w:deg_w + 2])
    sec_digits = digits[deg_w + 2:]
    whole = int(sec_digits[:2])
    frac = sec_digits[2:]
    seconds = whole + (int(frac) / 10 ** len(frac) if frac else 0.0)
    value = deg + minutes / 60.0 + seconds / 3600.0
    return -value if hemi in "SW" else value


def _declination(s: str) -> float | None:
    """'E0140' -> 14.0, 'W0130' -> -13.0."""
    s = s.strip()
    if len(s) != 5 or s[0] not in "EW":
        return None
    v = int(s[1:]) / 10.0
    return v if s[0] == "E" else -v


def runway_ident(s: str) -> str:
    s = s.strip()
    return s[2:] if s.startswith("RW") else s


def procedure_runway(ident: str) -> str | None:
    m = _PROC_RE.match(ident.strip())
    return m.group(1) if m else None


@dataclass(slots=True)
class CifpAirport:
    icao: str
    name: str
    lat: float
    lon: float
    elev_ft: int
    declination_deg: float | None


@dataclass(slots=True)
class CifpRunway:
    airport: str
    ident: str
    length_ft: int | None
    mag_bearing_deg: float | None
    bearing_is_true: bool
    lat: float
    lon: float
    hae_m: float | None
    elev_ft: int | None
    displaced_ft: int
    tch_ft: int | None
    width_ft: int | None
    tch_source: str | None
    loc_ident: str | None
    ils_cat: str | None


@dataclass(slots=True)
class CifpLocalizer:
    airport: str
    ident: str
    cat: str | None
    freq_mhz: float | None
    runway: str
    lat: float
    lon: float
    mag_bearing_deg: float | None
    gs_lat: float | None
    gs_lon: float | None
    loc_from_stop_end_ft: int | None
    gs_from_threshold_ft: int | None
    width_deg: float | None
    gs_angle_deg: float | None
    declination_deg: float | None
    tch_ft: int | None
    gs_elev_ft: int | None


@dataclass(slots=True)
class CifpPathPoint:
    airport: str
    procedure: str
    runway: str
    ltp_lat: float
    ltp_lon: float
    ltp_hae_m: float | None
    gpa_deg: float | None
    course_width_m: float | None
    length_offset_m: int | None
    tch_ft: float | None


@dataclass(slots=True)
class CifpApproachAngle:
    airport: str
    procedure: str
    runway: str
    vertical_angle_deg: float


@dataclass
class CifpData:
    airports: dict[str, CifpAirport] = field(default_factory=dict)
    runways: dict[tuple[str, str], CifpRunway] = field(default_factory=dict)
    localizers: dict[tuple[str, str], CifpLocalizer] = field(default_factory=dict)
    path_points: dict[tuple[str, str], list[CifpPathPoint]] = field(default_factory=dict)
    approach_angles: dict[tuple[str, str], list[CifpApproachAngle]] = field(default_factory=dict)


def _parse_pa(line: str) -> CifpAirport:
    return CifpAirport(
        icao=_c(line, 7, 10).strip(), name=_c(line, 94, 123).strip(),
        lat=parse_coord(_c(line, 33, 41)), lon=parse_coord(_c(line, 42, 51)),
        elev_ft=_int(_c(line, 57, 61)) or 0, declination_deg=_declination(_c(line, 52, 56)),
    )


def _parse_pg(line: str) -> CifpRunway:
    bearing = _c(line, 28, 31).strip()
    is_true = bearing.endswith("T")
    return CifpRunway(
        airport=_c(line, 7, 10).strip(), ident=runway_ident(_c(line, 14, 18)),
        length_ft=_int(_c(line, 23, 27)), mag_bearing_deg=_scaled(bearing.rstrip("T"), 0.1),
        bearing_is_true=is_true,
        lat=parse_coord(_c(line, 33, 41)), lon=parse_coord(_c(line, 42, 51)),
        hae_m=_scaled(_c(line, 61, 66), 0.1), elev_ft=_int(_c(line, 67, 71)),
        displaced_ft=_int(_c(line, 72, 75)) or 0, tch_ft=_int(_c(line, 76, 77)),
        width_ft=_int(_c(line, 78, 80)), tch_source=_c(line, 81, 81).strip() or None,
        loc_ident=_c(line, 82, 85).strip() or None, ils_cat=_c(line, 86, 86).strip() or None,
    )


def _parse_pi(line: str) -> CifpLocalizer:
    gs_lat_s, gs_lon_s = _c(line, 56, 64), _c(line, 65, 74)
    return CifpLocalizer(
        airport=_c(line, 7, 10).strip(), ident=_c(line, 14, 17).strip(),
        cat=_c(line, 18, 18).strip() or None, freq_mhz=_scaled(_c(line, 23, 27), 0.01),
        runway=runway_ident(_c(line, 28, 32)),
        lat=parse_coord(_c(line, 33, 41)), lon=parse_coord(_c(line, 42, 51)),
        mag_bearing_deg=_scaled(_c(line, 52, 55), 0.1),
        gs_lat=parse_coord(gs_lat_s) if gs_lat_s.strip() else None,
        gs_lon=parse_coord(gs_lon_s) if gs_lon_s.strip() else None,
        loc_from_stop_end_ft=_int(_c(line, 75, 78)), gs_from_threshold_ft=_int(_c(line, 80, 83)),
        width_deg=_scaled(_c(line, 84, 87), 0.01), gs_angle_deg=_scaled(_c(line, 88, 90), 0.01),
        declination_deg=_declination(_c(line, 91, 95)), tch_ft=_int(_c(line, 96, 97)),
        gs_elev_ft=_int(_c(line, 98, 102)),
    )


def _parse_pp(line: str) -> CifpPathPoint | None:
    if _c(line, 25, 27) != "001":          # continuation records carry nothing we need
        return None
    tch = _scaled(_c(line, 103, 108), 0.1)
    if tch is not None and _c(line, 109, 109) == "M":
        tch = round(tch * FT_PER_M, 1)
    return CifpPathPoint(
        airport=_c(line, 7, 10).strip(), procedure=_c(line, 14, 19).strip(),
        runway=runway_ident(_c(line, 20, 24)),
        ltp_lat=parse_coord(_c(line, 38, 48)), ltp_lon=parse_coord(_c(line, 49, 60)),
        ltp_hae_m=_scaled(_c(line, 61, 66), 0.1), gpa_deg=_scaled(_c(line, 67, 70), 0.01),
        course_width_m=_scaled(_c(line, 94, 98), 0.01), length_offset_m=_int(_c(line, 99, 102)),
        tch_ft=tch,
    )


def _parse_pf(line: str) -> CifpApproachAngle | None:
    angle = _c(line, 103, 106).strip()
    if not angle or int(angle) == 0:          # " 000": no vertical angle published
        return None
    procedure = _c(line, 14, 19).strip()
    runway = procedure_runway(procedure)       # None for circling procedures, even on an RW fix
    if runway is None:
        return None
    return CifpApproachAngle(airport=_c(line, 7, 10).strip(), procedure=procedure, runway=runway,
                             vertical_angle_deg=abs(int(angle)) / 100.0)


def _off_runway_deg(loc: CifpLocalizer, rwy: CifpRunway | None) -> float:
    """Localizer bearing minus runway bearing (both magnetic); 180 when either is unknown."""
    if rwy is None or rwy.mag_bearing_deg is None or loc.mag_bearing_deg is None:
        return 180.0
    return abs((loc.mag_bearing_deg - rwy.mag_bearing_deg + 180.0) % 360.0 - 180.0)


def parse_cifp(path: str | Path, airports: set[str] | None = None) -> CifpData:
    data = CifpData()
    seen_angles: set[tuple[str, str, str]] = set()
    with open(path, encoding="latin-1") as fh:
        for line in fh:
            if len(line) < 106 or line[4] != "P":
                continue
            icao = _c(line, 7, 10).strip()
            if airports is not None and icao not in airports:
                continue
            sub = line[12]
            if sub not in _CONT_COL or line[_CONT_COL[sub] - 1] not in "01":
                continue
            if sub == "A":
                a = _parse_pa(line)
                data.airports[a.icao] = a
            elif sub == "G":
                r = _parse_pg(line)
                data.runways[(r.airport, r.ident)] = r
            elif sub == "I":
                loc = _parse_pi(line)
                key = (loc.airport, loc.runway)
                rwy, old = data.runways.get(key), data.localizers.get(key)
                if old is None or _off_runway_deg(loc, rwy) < _off_runway_deg(old, rwy):   # ILS over offset LDA
                    data.localizers[key] = loc
            elif sub == "P":
                p = _parse_pp(line)
                if p is not None:
                    data.path_points.setdefault((p.airport, p.runway), []).append(p)
            elif sub == "F":
                ang = _parse_pf(line)
                if ang is not None and (ang.airport, ang.procedure, ang.runway) not in seen_angles:
                    seen_angles.add((ang.airport, ang.procedure, ang.runway))
                    data.approach_angles.setdefault((ang.airport, ang.runway), []).append(ang)
    return data

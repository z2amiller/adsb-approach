"""Shared fixtures: reference runway/airport, config, and a synthetic straight-in track builder."""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from adsb_approach.altitude import h_isa_ft
from adsb_approach.config import Config, load_config
from adsb_approach.geometry import NM_FT, destination
from adsb_approach.model import Airport, Position, ProcedurePath, RunwayEnd
from adsb_approach.pipeline import replay
from adsb_approach.reference import Leg, Reference

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
T0 = 1789240000.0          # 2026-09-12 19:06:40Z


def make_sfo_28r(**over) -> RunwayEnd:
    base = dict(
        airport="KSFO", ident="28R", enabled=True,
        threshold_lat=37.613919, threshold_lon=-122.358058, threshold_elev_ft=13.0, threshold_hae_m=-28.7,
        course_true_deg=298.0, runway_length_ft=11870.0, displaced_threshold_ft=300.0,
        localizer_lat=37.629544, localizer_lon=-122.395311, localizer_offset_ft=1000.0,
        loc_width_deg=3.29, glide_path_deg=3.0, tch_ft=55.0, ils="IGWQ 111.70 CAT 3",
        reference_source="cifp:CIFP_260903", parallel_group=("28L", "28R"),
    )
    base.update(over)
    return RunwayEnd(**base)


def make_sfo_28l(**over) -> RunwayEnd:
    base = dict(
        airport="KSFO", ident="28L", enabled=True,
        threshold_lat=37.612097, threshold_lon=-122.359264, threshold_elev_ft=13.0, threshold_hae_m=-28.8,
        course_true_deg=298.0, runway_length_ft=11381.0, displaced_threshold_ft=300.0,
        localizer_lat=37.627075, localizer_lon=-122.394978, localizer_offset_ft=1000.0,
        loc_width_deg=3.43, glide_path_deg=2.85, tch_ft=53.0, ils="ISFO 109.55 CAT 2",
        reference_source="cifp:CIFP_260903", parallel_group=("28L", "28R"),
    )
    base.update(over)
    return RunwayEnd(**base)


# The Quiet Bridge Visual RWY 28R at KSFO as the prototype fitted it: down SFO VOR R-095 (292.0 true) to 5 nm, then
# 290.0 true to the join, 1.1 nm out on the 28R centreline. Use it with make_sfo_28r(paths=(QUIET_BRIDGE,)).
QUIET_BRIDGE = ProcedurePath(
    "quiet-bridge", "Quiet Bridge", "test",
    ((37.488179, -121.966495), (37.582913, -122.259838), (37.605340, -122.337623)))


def make_ksfo(*runways: RunwayEnd) -> Airport:
    rws = runways or (make_sfo_28r(), make_sfo_28l())
    return Airport(icao="KSFO", name="SAN FRANCISCO INTL", enabled=True, lat=37.618806, lon=-122.375417,
                   elevation_ft=13.0, metar="KSFO", geoid_offset_ft=-107.2, declination_deg=14.0,
                   runways={r.ident: r for r in rws})


def on_path_alt_baro(rwy: RunwayEnd, along_ft: float, offset_ft: float = 0.0, qnh_hpa: float = 1013.25) -> int:
    """Pressure altitude an aircraft reports when it is exactly on the glide path (+offset) at along_ft."""
    true_alt = rwy.threshold_elev_ft + rwy.tch_ft + max(along_ft, 0.0) * math.tan(math.radians(rwy.glide_path_deg)) + offset_ft
    return int(round(true_alt + h_isa_ft(qnh_hpa)))


def synthetic_track(rwy: RunwayEnd, *, hexid: str = "a44540", callsign: str | None = "UAL2177",
                    start_nm: float = 8.0, end_nm: float = 0.5, step_s: float = 1.0, gs_kt: float = 150.0,
                    offset_ft: float = 0.0, cross_ft: float = 0.0, qnh_hpa: float = 1013.25, t0: float = T0,
                    track_offset_deg: float = 0.0, nav_modes: frozenset[str] = frozenset({"approach"}),
                    nav_altitude_mcp: int | None = 3000, category: str = "A3") -> list[Position]:
    """Straight-in approach on the extended centreline, descending on the glide path (+offset), constant ground speed."""
    out: list[Position] = []
    step_ft = gs_kt * NM_FT / 3600.0 * step_s
    n = math.floor((start_nm - end_nm) * NM_FT / step_ft + 1e-6)      # steps to end_nm, free of accumulated rounding
    vr = int(round(-gs_kt * 101.27 * math.tan(math.radians(rwy.glide_path_deg))))
    for i in range(n + 1):
        along, t = start_nm * NM_FT - i * step_ft, t0 + i * step_s
        lat, lon = destination(rwy.threshold_lat, rwy.threshold_lon, (rwy.course_true_deg + 180) % 360, along)
        if cross_ft:
            lat, lon = destination(lat, lon, (rwy.course_true_deg + 90) % 360, cross_ft)
        out.append(Position(
            t=t, hex=hexid, lat=lat, lon=lon, alt_baro=on_path_alt_baro(rwy, along, offset_ft, qnh_hpa), on_ground=False,
            alt_geom=None, gs=gs_kt, track=(rwy.course_true_deg + track_offset_deg) % 360, baro_rate=vr, geom_rate=None,
            callsign=callsign, squawk="1234", category=category, nav_qnh=qnh_hpa, nav_altitude_mcp=nav_altitude_mcp,
            nav_modes=nav_modes, source="adsb_icao",
        ))
    return out


def path_track(ref: Reference, *, start_dtg_nm: float, end_dtg_nm: float, step_s: float = 1.0, gs_kt: float = 180.0,
               cross_ft: float = 0.0, hexid: str = "407699", callsign: str | None = "VIR19Z", t0: float = T0) -> list[Position]:
    """Positions flown along ref's legs from start_dtg_nm to end_dtg_nm still to fly to the threshold, cross_ft to the right
    of the reference, at constant ground speed on the glide path. The track is the leg's course. Beyond the first vertex the
    first leg runs on outwards, and past the threshold the last runs on, as Reference.frame measures them."""
    rwy = ref.runway
    if ref.legs:
        legs = ref.legs
    else:                                           # the centreline: one leg from 50 nm out to the threshold
        te, tn = math.sin(math.radians(rwy.course_true_deg)), math.cos(math.radians(rwy.course_true_deg))
        legs = (Leg(a=(-te * 50 * NM_FT, -tn * 50 * NM_FT), t=(te, tn), length=50 * NM_FT, course=rwy.course_true_deg, rem=0.0),)
    out: list[Position] = []
    step_ft = gs_kt * NM_FT / 3600.0 * step_s
    n = math.floor((start_dtg_nm - end_dtg_nm) * NM_FT / step_ft + 1e-6)
    vr = int(round(-gs_kt * 101.27 * math.tan(math.radians(rwy.glide_path_deg))))
    for i in range(n + 1):
        dtg = start_dtg_nm * NM_FT - i * step_ft
        leg = next((x for x in legs if x.rem <= dtg), legs[-1])
        (ae, an), (te, tn) = leg.a, leg.t
        u = leg.length - (dtg - leg.rem)
        e, nn = ae + u * te + cross_ft * tn, an + u * tn - cross_ft * te
        lat, lon = destination(rwy.threshold_lat, rwy.threshold_lon, math.degrees(math.atan2(e, nn)) % 360.0, math.hypot(e, nn))
        out.append(Position(
            t=t0 + i * step_s, hex=hexid, lat=lat, lon=lon, alt_baro=on_path_alt_baro(rwy, dtg), on_ground=False, alt_geom=None,
            gs=gs_kt, track=leg.course, baro_rate=vr, geom_rate=None, callsign=callsign, squawk="1234", category="A3",
            nav_qnh=1013.25, nav_altitude_mcp=3000, nav_modes=frozenset({"approach"}), source="adsb_icao"))
    return out


def climb_segment(rwy: RunwayEnd, *, start_along_nm: float, end_along_nm: float, start_alt_ft: float, vr_fpm: float,
                  gs_kt: float = 160.0, t0: float, step_s: float = 1.0, hexid: str = "a44540", callsign: str | None = "UAL2177",
                  nav_altitude_mcp: int | None = 5000, nav_modes: frozenset[str] = frozenset({"autopilot"}),
                  cross_ft: float = 0.0, qnh_hpa: float = 1013.25) -> list[Position]:
    """Continue along the course (along may go negative past the threshold) while climbing at vr_fpm."""
    out: list[Position] = []
    speed_fps = gs_kt * NM_FT / 3600.0
    along = start_along_nm * NM_FT
    alt = start_alt_ft
    t = t0
    while along >= end_along_nm * NM_FT:
        lat, lon = destination(rwy.threshold_lat, rwy.threshold_lon, (rwy.course_true_deg + 180) % 360, along)
        if cross_ft:
            lat, lon = destination(lat, lon, (rwy.course_true_deg + 90) % 360, cross_ft)
        out.append(Position(
            t=t, hex=hexid, lat=lat, lon=lon, alt_baro=int(round(alt + h_isa_ft(qnh_hpa))), on_ground=False, alt_geom=None,
            gs=gs_kt, track=rwy.course_true_deg, baro_rate=int(vr_fpm), geom_rate=None, callsign=callsign, squawk="1234",
            category="A3", nav_qnh=qnh_hpa, nav_altitude_mcp=nav_altitude_mcp, nav_modes=nav_modes, source="adsb_icao",
        ))
        along -= speed_fps * step_s
        alt += vr_fpm * step_s / 60.0
        t += step_s
    return out


def ground_point(rwy: RunwayEnd, *, along_nm: float, t: float, hexid: str = "a44540", callsign: str | None = "UAL2177",
                 gs: float = 40.0, cross_ft: float = 0.0) -> Position:
    lat, lon = destination(rwy.threshold_lat, rwy.threshold_lon, (rwy.course_true_deg + 180) % 360, along_nm * NM_FT)
    if cross_ft:
        lat, lon = destination(lat, lon, (rwy.course_true_deg + 90) % 360, cross_ft)
    return Position(t=t, hex=hexid, lat=lat, lon=lon, alt_baro=None, on_ground=True, alt_geom=None, gs=gs,
                    track=rwy.course_true_deg, baro_rate=None, geom_rate=None, callsign=callsign, squawk="1234",
                    category="A3", nav_qnh=None, nav_altitude_mcp=None, nav_modes=frozenset(), source="adsb_icao")


def flown_track(rwy: RunwayEnd, *, start_nm: float, legs: list[tuple[float, float]], start_cross_ft: float = 0.0,
                start_offset_deg: float = 0.0, step_s: float = 0.5, gs_kt: float = 160.0, t0: float = T0,
                hexid: str = "a1dc60", callsign: str | None = "EJA219", category: str = "A3") -> list[Position]:
    """A path flown in rwy's frame at constant ground speed on the glide path, sampled every step_s (0.5 s: the live
    port; 8 s: tar1090 history). The track offset from the course (positive: right) starts at start_offset_deg and moves
    linearly to each (seconds, offset_deg) leg's offset over its seconds; 0 s is a step. The path is integrated in 0.1 s
    steps whatever the sampling, so every sampling describes the same flight."""
    dt = 0.1
    every = round(step_s / dt)
    v = gs_kt * NM_FT / 3600.0
    vr = int(round(-gs_kt * 101.27 * math.tan(math.radians(rwy.glide_path_deg))))
    along, cross, offset, i = start_nm * NM_FT, start_cross_ft, start_offset_deg, 0
    out: list[Position] = []
    for seconds, end_offset in legs:
        n = round(seconds / dt)
        for k in range(n):
            cur = offset + (end_offset - offset) * k / n
            if i % every == 0:
                lat, lon = destination(rwy.threshold_lat, rwy.threshold_lon, (rwy.course_true_deg + 180) % 360, along)
                lat, lon = destination(lat, lon, (rwy.course_true_deg + 90) % 360, cross)
                out.append(Position(
                    t=t0 + i * dt, hex=hexid, lat=lat, lon=lon, alt_baro=on_path_alt_baro(rwy, along), on_ground=False,
                    alt_geom=None, gs=gs_kt, track=(rwy.course_true_deg + cur) % 360, baro_rate=vr, geom_rate=None,
                    callsign=callsign, squawk="1234", category=category, nav_qnh=1013.25, nav_altitude_mcp=3000,
                    nav_modes=frozenset({"approach"}), source="adsb_icao"))
            along -= v * math.cos(math.radians(cur)) * dt
            cross += v * math.sin(math.radians(cur)) * dt
            i += 1
        offset = end_offset
    return out


@pytest.fixture(scope="session")
def example_cfg() -> Config:
    """The example configuration, loaded once for the session: read it, never change it."""
    return load_config(ROOT / "config" / "adsb-approach.example.toml")


@pytest.fixture(scope="session")
def replay_db(tmp_path_factory: pytest.TempPathFactory, example_cfg: Config) -> Path:
    """The 2026-09-12 chunk fixtures and their METAR, replayed into a database once for the session. Read it; write
    only to a copy (tests/webdb.py)."""
    db = tmp_path_factory.mktemp("replay") / "replay.sqlite"
    replay(example_cfg, kind="chunks", path=FIXTURES / "raw" / "tar1090-chunks-20260912", db_path=db, run_tag="web",
           metar_path=FIXTURES / "metar-20260912.json", typedb_path=FIXTURES / "typedb-sample.csv")
    return db

"""readsb JSON object (aircraft.json entry or JSON-port line) → Position."""
from __future__ import annotations

import json

from ..model import Position


def _i(obj: dict, key: str) -> int | None:
    v = obj.get(key)
    return None if v is None else int(round(float(v)))


def _f(obj: dict, key: str) -> float | None:
    v = obj.get(key)
    return None if v is None else float(v)


def _modes(v: object) -> frozenset[str]:
    """readsb sends nav_modes as a list of mode names; any other shape carries none (a string is not a set of letters)."""
    return frozenset(m for m in v if isinstance(m, str)) if isinstance(v, list) else frozenset()


def position_from_readsb(obj: dict, now: float | None = None) -> Position | None:
    """Return None when the object carries no position, or a field cannot be converted (a JSON-port line with
    "lat": "x" and the like: bad data is skipped, never fatal to the reader). `now` is the aircraft.json file time
    for polled objects; JSON-port lines carry their own `now`."""
    lat, lon = obj.get("lat"), obj.get("lon")
    if lat is None or lon is None or not obj.get("hex"):
        return None
    base = obj.get("now", now)
    if base is None:
        return None
    cs = obj.get("flight")
    callsign = (cs.strip() or None) if isinstance(cs, str) else None
    try:
        t = float(base) - float(obj.get("seen_pos") or 0.0)
        ab = obj.get("alt_baro")
        on_ground = ab == "ground"
        alt_baro = None if on_ground or ab is None else int(round(float(ab)))
        lat, lon = float(lat), float(lon)
        alt_geom, gs, track = _i(obj, "alt_geom"), _f(obj, "gs"), _f(obj, "track")
        if track is None:
            track = _f(obj, "calc_track")          # readsb's derived-from-position track when no heading was reported
        baro_rate, geom_rate = _i(obj, "baro_rate"), _i(obj, "geom_rate")
        nav_qnh, nav_altitude_mcp = _f(obj, "nav_qnh"), _i(obj, "nav_altitude_mcp")
    except (TypeError, ValueError, OverflowError):
        return None
    # Everything left is a straight passthrough or already converted, so a mismatch here is a coding bug, not bad
    # data, and must raise rather than being folded into the catch above.
    return Position(
        t=t, hex=str(obj["hex"]).lower(), lat=lat, lon=lon,
        alt_baro=alt_baro, on_ground=on_ground, alt_geom=alt_geom,
        gs=gs, track=track, baro_rate=baro_rate, geom_rate=geom_rate,
        callsign=callsign, squawk=obj.get("squawk"), category=obj.get("category"), emergency=obj.get("emergency"),
        nav_qnh=nav_qnh, nav_altitude_mcp=nav_altitude_mcp,
        nav_modes=_modes(obj.get("nav_modes")), source=str(obj.get("type") or "unknown"),
        reg=obj.get("r"), type_code=obj.get("t"),
    )


def parse_readsb_line(line: str | bytes) -> Position | None:
    """One JSON-port or capture line → Position. None for a blank or malformed line, a JSON value that is not an
    object, an object without a position, or a field of the wrong type ("lat": "x"): a bad line is skipped, never
    fatal to the reader."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except ValueError:                      # json.JSONDecodeError and UnicodeDecodeError are ValueErrors
        return None
    if not isinstance(obj, dict):
        return None
    return position_from_readsb(obj)


def readsb_object(p: Position) -> dict:
    """Inverse of position_from_readsb for writing fixtures: a JSON-port style object with seen_pos 0."""
    obj: dict = {"now": p.t, "hex": p.hex, "type": p.source, "lat": p.lat, "lon": p.lon, "seen_pos": 0.0,
                 "alt_baro": "ground" if p.on_ground else p.alt_baro}
    for key, val in (("alt_geom", p.alt_geom), ("gs", p.gs), ("track", p.track), ("baro_rate", p.baro_rate),
                     ("geom_rate", p.geom_rate), ("flight", p.callsign), ("squawk", p.squawk), ("category", p.category),
                     ("nav_qnh", p.nav_qnh), ("nav_altitude_mcp", p.nav_altitude_mcp), ("r", p.reg), ("t", p.type_code),
                     ("emergency", p.emergency)):
        if val is not None:
            obj[key] = val
    if p.nav_modes:
        obj["nav_modes"] = sorted(p.nav_modes)
    if obj["alt_baro"] is None:
        del obj["alt_baro"]
    return obj

"""Hand-written trace files in readsb's format, as adsb.lol serves them: gzip-compressed JSON with a base `timestamp`
and `trace` rows of offset, lat, lon, altitude or "ground", ground speed, track, flags, vertical rate, details, source
type, geometric altitude and geometric rate, then two unused fields. Tests build them from synthetic positions: data
fetched from adsb.lol never becomes a fixture."""
from __future__ import annotations

import gzip
import json
from collections.abc import Iterable
from dataclasses import replace

from adsb_approach.model import Position


def trace_row(pos: Position, t0: float, source: str | None = None) -> list:
    """One `trace` row for a position, with its barometric altitude or "ground", as readsb writes it."""
    return [round(pos.t - t0, 2), pos.lat, pos.lon, "ground" if pos.on_ground else pos.alt_baro, pos.gs, pos.track, 0,
            pos.baro_rate, None, source or pos.source, pos.alt_geom, pos.geom_rate, None, None]


def trace_body(hexid: str, positions: Iterable[Position], *, sources: dict[int, str] | None = None) -> bytes:
    """A gzip trace file for `hexid` holding `positions` in order. `sources` gives the rows at those indexes another
    source type (mlat, tisb_icao, ...)."""
    rows = list(positions)
    t0 = float(int(rows[0].t)) if rows else 1789257600.0
    trace = [trace_row(p, t0, (sources or {}).get(i)) for i, p in enumerate(rows)]
    return gzip.compress(json.dumps({"icao": hexid, "timestamp": t0, "trace": trace}).encode())


ADSB_LOL_LATE_S = 0.10     # adsb.lol stamps a position this much later than the local receiver


def as_adsb_lol(positions: Iterable[Position], late_s: float = ADSB_LOL_LATE_S) -> list[Position]:
    """The positions as adsb.lol's trace holds them: identical to the last digit of lat, lon and altitude, but stamped
    late_s later. A copy of a receiver position is an exact match; a copy of a position the receiver never had is one
    adsb.lol can add inside a gap in the receiver's track."""
    return [replace(p, t=p.t + late_s) for p in positions]

"""File-based sources: JSON-port captures, tar1090 history chunks, globe-history trace files."""
from __future__ import annotations

import glob
import gzip
import heapq
import itertools
import json
import logging
import os
import re
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TextIO

from ..model import Position
from .base import _modes, parse_readsb_line

log = logging.getLogger(__name__)

TRACE_FLAG_NEW_LEG = 2
TRACE_FLAG_VR_GEOMETRIC = 4
TRACE_FLAG_ALT_GEOMETRIC = 8
GZIP_MAGIC = b"\x1f\x8b"
GZIP_ERRORS = (EOFError, zlib.error, gzip.BadGzipFile)     # a stream cut short raises EOFError


def _open_text(path: str | Path) -> TextIO:
    """Open UTF-8 text, decompressing by content rather than by name: readsb writes globe-history
    trace_full_<hex>.json files gzip-compressed despite the .json suffix. Undecodable bytes become U+FFFD,
    so a damaged line fails as JSON instead of stopping the reader."""
    with open(path, "rb") as fh:
        gzipped = fh.read(2) == GZIP_MAGIC
    if gzipped:
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "rt", encoding="utf-8", errors="replace")


def _load_json(path: str | Path) -> Any:
    """A whole JSON file, plain or gzip; None, with a warning, when it cannot be read or parsed."""
    try:
        with _open_text(path) as fh:
            return json.load(fh)
    except (OSError, ValueError, *GZIP_ERRORS) as exc:
        log.warning("%s: skipped, unreadable: %s", path, exc)
        return None


def iter_jsonl(path: str | Path) -> Iterator[Position]:
    """Positions from a JSON-port capture, plain or gzip. Lines that are not readsb position objects are skipped,
    and a capture cut off mid-write (an interrupted `nc | gzip`) ends with a warning after its last whole line."""
    with _open_text(path) as fh:
        try:
            for line in fh:
                p = parse_readsb_line(line)
                if p is not None:
                    yield p
        except GZIP_ERRORS as exc:
            log.warning("%s: capture ends early: %s", path, exc)


def _chunk_key(name: str) -> int:
    stem = os.path.basename(name)
    try:
        return int(stem.split("_")[1].split(".")[0])
    except (IndexError, ValueError):
        return 0


REORDER_S = 16.0       # a fix first reaches a snapshot up to one 8 s interval after it was received


def _chunk_snapshots(path: str) -> list | None:
    """The snapshots in one chunk file; None when it is not a tar1090 chunk. An unreadable file has already been
    warned about by _load_json, so only a readable one that is not a chunk is warned about here."""
    data = _load_json(path)
    snaps = data.get("files") if isinstance(data, dict) else None
    if not isinstance(snaps, list):
        if data is not None:
            log.warning("%s: skipped, not a tar1090 chunk", path)
        return None
    return snaps


def _chunk_position(e: list, now: float, last: dict[str, tuple]) -> Position | None:
    """The Position for one aircraft entry of a snapshot taken at `now`: [hex, altitude, gs, track, lat, lon, seen_pos,
    source, callsign]. None when the entry has no fix, or its fix and ground state are those last emitted for that
    airframe; `last` holds each airframe's last emitted (lat, lon, on_ground) and is updated here."""
    if len(e) < 7 or e[4] is None or e[5] is None:
        return None
    hexid = str(e[0]).lower()
    alt = e[1]
    on_ground = alt == "ground"
    alt_baro = None if on_ground or alt is None else int(alt)
    key = (e[4], e[5], on_ground)
    if last.get(hexid) == key:
        return None
    last[hexid] = key
    callsign = (e[8].strip() or None) if len(e) > 8 and isinstance(e[8], str) else None
    return Position(
        t=now - float(e[6] or 0.0), hex=hexid, lat=float(e[4]), lon=float(e[5]),
        alt_baro=alt_baro, on_ground=on_ground, alt_geom=None,
        gs=None if e[2] is None else float(e[2]), track=None if e[3] is None else float(e[3]),
        baro_rate=None, geom_rate=None, callsign=callsign, squawk=None, category=None,
        nav_qnh=None, nav_altitude_mcp=None, nav_modes=frozenset(),
        source=str(e[7]) if len(e) > 7 and e[7] else "unknown", emergency=None,   # chunks carry neither
    )


def iter_tar1090_chunks(dir_path: str | Path) -> Iterator[Position]:
    """Yield distinct positions from tar1090 chunk_*.gz files in time order. A position is new for an airframe
    when its fix (lat, lon) or ground state changed since its last emitted one. An altitude change on an unchanged
    fix is skipped: that altitude is newer than the fix by seen_pos. Positions wait REORDER_S of snapshot time in a
    heap, so a fix that reaches a snapshot late still leaves ahead of other aircraft's newer positions. A chunk that
    cannot be read is skipped with a warning."""
    files = sorted(glob.glob(str(Path(dir_path) / "chunk_*.gz")), key=_chunk_key)
    last: dict[str, tuple] = {}
    pending: list[tuple[float, int, Position]] = []
    order = itertools.count()
    for f in files:
        snaps = _chunk_snapshots(f)
        if snaps is None:
            continue
        for snap in snaps:
            now = float(snap.get("now", 0))
            for e in snap.get("aircraft", []):
                p = _chunk_position(e, now, last)
                if p is not None:
                    heapq.heappush(pending, (p.t, next(order), p))
            while pending and pending[0][0] <= now - REORDER_S:
                yield heapq.heappop(pending)[2]
    while pending:
        yield heapq.heappop(pending)[2]


def iter_trace_file(path: str | Path) -> Iterator[Position]:
    """Positions from one globe-history trace file; nothing, with a warning, when the file cannot be read. A point
    whose values cannot be converted is skipped, with a warning, rather than ending the file: a coding bug elsewhere
    in this function is left to raise, but one corrupt point does not call the rest of the file into question."""
    data = _load_json(path)
    if data is None:
        return
    yield from _trace_points(data, path)


class TraceDecodeError(ValueError):
    """A body that is not a readsb trace at all: neither gzip nor JSON text, not an object, or without icao, a usable
    timestamp and a trace list."""


def decode_trace(body: bytes, where: str) -> list[Position]:
    """Positions from one trace file's bytes, as adsb.lol serves them (gzip-compressed despite the .json name) or plain,
    in time order. Raises TraceDecodeError when the body is not a trace; a malformed point inside one is skipped with a
    warning, as iter_trace_file skips it."""
    try:
        text = gzip.decompress(body) if body[:2] == GZIP_MAGIC else body
        data = json.loads(text.decode("utf-8", errors="replace"))
    except (ValueError, *GZIP_ERRORS) as exc:
        raise TraceDecodeError(f"{where}: not a trace: {exc}") from None
    if not isinstance(data, dict) or "icao" not in data or not isinstance(data.get("trace"), list):
        raise TraceDecodeError(f"{where}: not a trace: no icao and trace list")
    try:
        float(data["timestamp"])
    except (KeyError, TypeError, ValueError):
        raise TraceDecodeError(f"{where}: not a trace: no usable timestamp") from None
    return sorted(_trace_points(data, where), key=lambda p: p.t)


def _trace_details(details: dict, snapshot: dict | None, flags: int) -> dict:
    """The aircraft details in force after one trace point: those carried from earlier points, emptied when the point
    starts a new leg and then overlaid with the point's own snapshot, if it has one."""
    if flags & TRACE_FLAG_NEW_LEG:
        details = {}
    if snapshot is not None:           # a full aircraft snapshot: nav_* keys it lacks were no longer valid
        details = {**{k: v for k, v in details.items() if not k.startswith("nav_")}, **snapshot}
    return details


def _trace_altitudes(alt: Any, alt_geom: Any, flags: int, on_ground: bool) -> tuple[int | None, int | None]:
    """(alt_baro, alt_geom) of one trace point. Its `alt` is barometric unless the flags say it is geometric, in which
    case it replaces the point's separate geometric altitude; on the ground, or absent, it contributes none."""
    alt_baro: int | None = None
    geom: int | None = None if alt_geom is None else int(alt_geom)
    if not on_ground and alt is not None:
        if flags & TRACE_FLAG_ALT_GEOMETRIC:
            geom = int(alt)
        else:
            alt_baro = int(alt)
    return alt_baro, geom


def _trace_rates(vr: Any, geom_rate: Any, flags: int) -> tuple[int | None, int | None]:
    """(baro_rate, geom_rate) of one trace point. Its `vr` is a barometric rate unless the flags say it is geometric,
    in which case it replaces the point's separate geometric rate."""
    baro_rate: int | None = None
    grate: int | None = None if geom_rate is None else int(geom_rate)
    if vr is not None:
        if flags & TRACE_FLAG_VR_GEOMETRIC:
            grate = int(vr)
        else:
            baro_rate = int(vr)
    return baro_rate, grate


def _trace_points(data: dict, where: str | Path) -> Iterator[Position]:
    """The positions of one decoded trace object, in file order (iter_trace_file and decode_trace)."""
    hexid = str(data["icao"]).lower()
    t0 = float(data["timestamp"])
    reg, type_code = data.get("r"), data.get("t")
    details: dict = {}
    for e in data.get("trace", []):
        try:
            e = list(e) + [None] * (14 - len(e))
            dt, lat, lon, alt, gs, track, flags, vr, det, source, alt_geom, geom_rate = e[:12]
            if lat is None or lon is None:
                continue
            flags = int(flags or 0)
            snapshot = det if isinstance(det, dict) else None    # this point's own detail snapshot, if it has one
            details = _trace_details(details, snapshot, flags)
            on_ground = alt == "ground"
            alt_baro, geom = _trace_altitudes(alt, alt_geom, flags, on_ground)
            baro_rate, grate = _trace_rates(vr, geom_rate, flags)
            cs = details.get("flight")
            point = Position(
                t=t0 + float(dt), hex=hexid, lat=float(lat), lon=float(lon),
                alt_baro=alt_baro, on_ground=on_ground, alt_geom=geom,
                gs=None if gs is None else float(gs), track=None if track is None else float(track),
                baro_rate=baro_rate, geom_rate=grate,
                callsign=(cs.strip() or None) if isinstance(cs, str) else None,
                # Unlike callsign/category/nav_*, squawk and emergency are never carried forward from an earlier
                # snapshot: a trace's detail snapshots are occasional, and the watch (EmergencyWatch) times a run
                # from reports that carry a value, so a stale 7700 sitting in `details` between snapshots would time
                # a run for a squawk the aircraft may have held for a single second. None on a point without its own
                # snapshot means no information, which observe() already treats as inert rather than as the ordinary
                # code that would end a run.
                squawk=None if snapshot is None else snapshot.get("squawk"), category=details.get("category"),
                emergency=None if snapshot is None else snapshot.get("emergency"),
                nav_qnh=None if details.get("nav_qnh") is None else float(details["nav_qnh"]),
                nav_altitude_mcp=None if details.get("nav_altitude_mcp") is None else int(details["nav_altitude_mcp"]),
                nav_modes=_modes(details.get("nav_modes")),
                source=str(source or "unknown"), reg=reg, type_code=type_code,
            )
        except (IndexError, TypeError, ValueError) as exc:
            log.warning("%s: skipped a malformed point: %s", where, exc)
            continue
        yield point


def _trace_files(dir_path: str | Path) -> list[str]:
    """Every trace_full_*.json(.gz) under dir_path, recursively; readsb's in-progress *.readsb_tmp files are skipped."""
    return sorted(f for f in glob.glob(str(Path(dir_path) / "**" / "trace_full_*.json*"), recursive=True)
                  if f.endswith((".json", ".json.gz")))


TRACE_HEAD_CHARS = 4096
_NUMBER = r"(-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)"
_TRACE_TIMESTAMP = re.compile(r'"timestamp"\s*:\s*' + _NUMBER)
_TRACE_FIRST_DT = re.compile(r'"trace"\s*:\s*\[\s*\[\s*' + _NUMBER)


def _trace_start(path: str) -> float | None:
    """When a trace file's first point was received: its `timestamp` (the start of the UTC day in globe history) plus
    that point's offset. readsb writes both in the first few hundred bytes, so only the head is read; any other layout
    falls back to a full parse. None, with a warning, when the file cannot be read or holds no points."""
    try:
        with _open_text(path) as fh:
            head = fh.read(TRACE_HEAD_CHARS)
    except (OSError, ValueError, *GZIP_ERRORS) as exc:
        log.warning("%s: skipped, unreadable: %s", path, exc)
        return None
    ts, dt = _TRACE_TIMESTAMP.search(head), _TRACE_FIRST_DT.search(head)
    if ts is not None and dt is not None:
        return float(ts.group(1)) + float(dt.group(1))
    data = _load_json(path)
    if data is None:
        return None
    try:
        return float(data["timestamp"]) + float(data["trace"][0][0])
    except (KeyError, IndexError, TypeError, ValueError):
        log.warning("%s: skipped, not a trace with points", path)
        return None


def iter_trace_dir(dir_path: str | Path) -> Iterator[Position]:
    """Merge every trace file under dir_path by time. A file is decoded when the merge reaches its first point and
    released after its last, so memory is bounded by the files active at once, not by any one file: a multi-leg
    aircraft's file stays decoded across the gap between legs, from its first point of the day to its last. Equal
    times leave in path order, as from a merge of every file at once."""
    starts = sorted((t, index, f) for index, f in enumerate(_trace_files(dir_path)) if (t := _trace_start(f)) is not None)
    heap: list[tuple[float, int, Position, Iterator[Position]]] = []
    upcoming = 0
    while upcoming < len(starts) or heap:
        while upcoming < len(starts) and (not heap or starts[upcoming][0] <= heap[0][0]):
            _, index, f = starts[upcoming]
            upcoming += 1
            stream = iter_trace_file(f)
            first = next(stream, None)
            if first is not None:
                heapq.heappush(heap, (first.t, index, first, stream))
        if not heap:
            continue
        _, index, p, stream = heapq.heappop(heap)
        yield p
        following = next(stream, None)
        if following is not None:
            heapq.heappush(heap, (following.t, index, following, stream))


def iter_positions(kind: str, path: str | Path) -> Iterator[Position]:
    """Positions from a replay source. Raises at the call, not on first use, when there is nothing to read."""
    if kind not in ("chunks", "jsonl", "traces"):
        raise ValueError(f"unknown source kind {kind!r}")
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"{kind} source {p} does not exist")
    if kind == "chunks":
        if not glob.glob(str(p / "chunk_*.gz")):
            raise FileNotFoundError(f"{p} holds no tar1090 chunk_*.gz files")
        return iter_tar1090_chunks(p)
    if kind == "traces":
        if not _trace_files(p):
            raise FileNotFoundError(f"{p} holds no trace_full_*.json files")
        return iter_trace_dir(p)
    return iter_jsonl(p)

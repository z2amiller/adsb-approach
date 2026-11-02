"""One approach for the detail drawer (GET /api/approaches/{id}), with its tar1090 links: the local one and,
over the same timeframe, adsb.lol's own globe."""
from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from ..config import Config
from ..fleet import parse_key
from ..geometry import NM_FT
from ..model import RunwayEnd, flag_names
from ..reference import label_for, reference_for, reference_labels
from .db import has_column

# `reference` is the procedure path a point was measured against, None for the centreline (migration 0008): a switch
# between a runway's references is where the drawer puts a marker.
POINT_COLUMNS = ("t", "along_nm", "cross_ft", "hat_ft", "alt_baro", "gp_dev_ft", "loc_dev_ft", "gs", "gs_corr", "sink_fpm",
                 "nav_modes", "origin", "reference")
GATE_COLUMNS = ("gate_ft", "observed", "t", "along_nm", "gp_dev_ft", "gp_dots", "loc_dev_ft", "loc_dots", "gs", "gs_corr",
                "sink_fpm", "nav_modes", "fleet_gp_delta_ft", "fleet_gs_delta_kt")
# The band the charts draw and the counts the drawer names come from the approach's own key.
FLEET_BIN_COLUMNS = ("bin_nm", "n", "gp_p25", "gp_p50", "gp_p75", "gs_p25", "gs_p50", "gs_p75")
WEATHER_COLUMNS = ("observed_at", "raw", "altim_hpa", "temp_c", "dewp_c", "wind_dir", "wind_kt", "gust_kt")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TRACE_BEFORE_S, TRACE_AFTER_S = 600.0, 300.0
LIVE_SEEN_WITHIN_S = 120.0
STILL_FLYING_OUTCOMES = ("go_around", "touch_and_go", "abandoned")
# A clean refit (reference_from_points) lands within float noise of 0 ft on real data (max residual measured 0.000 ft
# over every closed approach in a live database). A real reference change, by contrast, moves the line by feet of TCH
# or tenths of a degree -- tenths of a degree alone are tens of feet by a few nm out -- so 1 ft cleanly
# separates "these points agree on one reference" from "they do not," with headroom to spare over any float rounding.
REFERENCE_FIT_MAX_RESIDUAL_FT = 1.0


def tar1090_base(configured: str, page_host: str | None, page_scheme: str = "http") -> str:
    """Where tar1090 is: the configured URL, unless its host is a loopback address, as in the shipped default, which a
    browser on another machine cannot reach. Then it is on the host the browser reached this page by, on the scheme's
    default port, at the configured path."""
    parts = urlsplit(configured)
    if parts.hostname not in LOOPBACK_HOSTS or not page_host:
        return configured
    netloc = f"[{page_host}]" if ":" in page_host else page_host
    return urlunsplit((page_scheme, netloc, parts.path or "/tar1090/", "", ""))


def tar1090_link(base: str, hexid: str, established_at: float, ended_at: float | None,
                 first_established_at: float | None = None, *, day: date | None = None) -> str:
    """The aircraft's trace from 10 minutes before the approach was established to 5 minutes after it ended. tar1090
    takes one UTC date and UTC times on it, so the link keeps to the day the approach was established: a window that
    starts the day before starts at midnight, and one that runs past midnight stops at the end of the day.

    `first_established_at`, for an attempt after the first in its arrival (a go-around's or touch-and-go's next
    attempt, or a runway change's new one), is that first attempt's own established_at: the trace then starts 10
    minutes before it instead, so it shows the missed approach or the runway change back to this attempt. The window
    still keeps to the UTC day *this* attempt was established, clipped to that day's start same as always.

    `day`, for a link that is not about an approach (the flight search's, which spans a whole session), is the UTC date
    to keep to instead of `established_at`'s; the window is clipped to it the same way."""
    if day is None:
        day = datetime.fromtimestamp(established_at, timezone.utc).date()
    start_from = established_at if first_established_at is None else first_established_at
    start = datetime.fromtimestamp(start_from - TRACE_BEFORE_S, timezone.utc)
    end = datetime.fromtimestamp((established_at if ended_at is None else ended_at) + TRACE_AFTER_S, timezone.utc)
    query = urlencode({"icao": hexid, "showTrace": day.isoformat(),
                       "startTime": start.strftime("%H:%M:%S") if start.date() == day else "00:00:00",
                       "endTime": end.strftime("%H:%M:%S") if end.date() == day else "23:59:59"}, safe=":")
    return f"{base}{'&' if '?' in base else '?'}{query}"


def tar1090_live_link(base: str, hexid: str) -> str:
    """Where tar1090 shows the aircraft now, while its approach is still open."""
    query = urlencode({"icao": hexid})
    return f"{base}{'&' if '?' in base else '?'}{query}"


def live_points(conn: sqlite3.Connection, approach_id: int, established_at: float) -> list[dict[str, Any]]:
    """The points of an attempt still in progress: one per approach.started or approach.updated event stored so far,
    in time order, mapped to the fields the drawer's charts read (chartData in logic.js) — `cross_ft` is the event's
    `loc_dev_ft`, and `origin` the origin of the position the event reports. Reads through
    the events_t index from the attempt's establishment onwards, as live_cards does (web/live.py), so this
    never scans the whole events table."""
    rows = conn.execute(
        "SELECT t, payload_json FROM events INDEXED BY events_t WHERE t >= ? "
        "AND kind IN ('approach.started', 'approach.updated') AND approach_id = ? ORDER BY id",
        (established_at, approach_id))
    points = []
    for row in rows:
        p = json.loads(row["payload_json"])
        points.append({"t": row["t"], "along_nm": p.get("along_nm"), "hat_ft": p.get("hat_ft"),
                       "cross_ft": p.get("loc_dev_ft"), "gs": p.get("gs"), "gs_corr": p.get("gs_corr"),
                       # events written before the live merge carry no origin, and came from the receiver
                       "origin": p.get("origin", "local")})
    return points


def reference_from_points(points: list[dict[str, Any]]) -> tuple[float, float] | None:
    """(tch_ft, glide_path_deg) refit from a closed approach's own stored points, or None when fewer than two of them
    are usable or the refit is not clean.

    score_point (scorer.py) measures gp_dev_ft = hat_ft - (tch_ft + along_nm * NM_FT * tan(glide_path_deg)) against
    whatever reference was configured at the time. So hat_ft - gp_dev_ft is that reference's own height at along_nm,
    exactly -- not approximately -- for every point of one approach, whatever the aircraft actually flew. Fitting a
    line to it therefore recovers the reference the stored deviations were measured from, which need not be rwy's
    reference now: a CIFP cycle can move tch_ft or glide_path_deg, or drop a runway to REFERENCE_ASSUMED, after this
    approach was already scored. Do not "simplify" this back to reading rwy.tch_ft/rwy.glide_path_deg
    directly -- that is the bug this recovers from. Checked against a copy of a live database: refitting every
    closed approach with two or more usable points this way is exact, e.g.
        id=435 KSFO 28R  n=82 pts   slope=318.4 ft/nm -> 3.000 deg   intercept(TCH)=55.0 ft   max residual=0.000 ft
        id=66  KNUQ 32R  n=84 pts   slope=318.4 ft/nm -> 3.000 deg   intercept(TCH)=57.0 ft   max residual=0.000 ft
        id=95  KSJC 30L  n=113 pts  slope=318.4 ft/nm -> 3.000 deg   intercept(TCH)=54.0 ft   max residual=0.000 ft

    A runway change opens a new approach (matcher.py), so one approach's points are never scored against more than one
    glide path -- a residual over REFERENCE_FIT_MAX_RESIDUAL_FT means that invariant did not hold, and this backs off
    the same as too few points do, rather than draw a line that may not be either reference. (A switch between a
    runway's own references, its centreline and a procedure path, changes where along_nm and cross_ft are measured
    from, not the glide path: hat_ft - gp_dev_ft is the same line of along_nm for every point of the approach.)"""
    usable = [p for p in points if p.get("along_nm") is not None and p["along_nm"] > 0
              and p.get("hat_ft") is not None and p.get("gp_dev_ft") is not None]
    if len(usable) < 2:
        return None
    xs = [p["along_nm"] for p in usable]
    ys = [p["hat_ft"] - p["gp_dev_ft"] for p in usable]
    n = len(usable)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    var = sum((x - mean_x) ** 2 for x in xs)
    if var == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var
    intercept = mean_y - slope * mean_x
    residual = max(abs(y - (intercept + slope * x)) for x, y in zip(xs, ys))
    if residual > REFERENCE_FIT_MAX_RESIDUAL_FT:
        return None
    return intercept, math.degrees(math.atan(slope / NM_FT))


def _summary_row(a: sqlite3.Row) -> dict[str, Any]:
    """The approaches row as the drawer's `approach` object, with the flag bits both as the stored mask and as names."""
    summary = dict(a)
    summary.setdefault("path", None)             # a database not yet migrated to 0008 has no such column
    summary["flags_mask"], summary["flags"] = a["flags"], flag_names(a["flags"])
    return summary


def _arrival_attempts(conn: sqlite3.Connection, arrival_id: int) -> list[dict[str, Any]]:
    """Every attempt of the arrival, in attempt order, each reduced to what the drawer lists to switch between them."""
    return [{"approach_id": r["id"], "attempt_no": r["attempt_no"], "runway": r["runway"], "outcome": r["outcome"],
             "flags": flag_names(r["flags"]), "established_at": r["established_at"]}
            for r in conn.execute("SELECT id, attempt_no, runway, outcome, flags, established_at FROM approaches "
                                  "WHERE arrival_id = ? ORDER BY attempt_no, id", (arrival_id,))]


def _stored_gates(conn: sqlite3.Connection, approach_id: int) -> list[dict[str, Any]]:
    """The gates scored when the approach closed, the highest first, with `observed` as a bool."""
    return [{**dict(g), "observed": bool(g["observed"])} for g in conn.execute(
        f"SELECT {', '.join(GATE_COLUMNS)} FROM approach_gates WHERE approach_id = ? "
        "ORDER BY gate_ft DESC", (approach_id,))]


def _stored_points(conn: sqlite3.Connection, approach_id: int) -> list[dict[str, Any]]:
    """The points stored when the approach closed, in time order. Each point's `origin` is 'local' or 'adsb.lol'. A
    database from before migration 0008 has no `reference` column, and its points read as measured against the
    centreline."""
    stored = has_column(conn, "approach_points", "reference")
    columns = ", ".join(c if c != "reference" or stored else "NULL AS reference" for c in POINT_COLUMNS)
    return [dict(p) for p in conn.execute(
        f"SELECT {columns} FROM approach_points WHERE approach_id = ? ORDER BY t", (approach_id,))]


def _link_renderer(conn: sqlite3.Connection, a: sqlite3.Row, arrival: list[dict[str, Any]], in_progress: bool,
                   now: float) -> Callable[[str], str]:
    """The function that renders this approach's link against a tar1090 base URL: tar1090's live view for an open
    attempt, and for a go-around, touch-and-go or abandoned one that is its arrival's latest and whose hex was seen
    within LIVE_SEEN_WITHIN_S of `now`; otherwise the trace, from the arrival's first attempt to the later of this
    attempt's end and the hex's last sighting. approach_detail's docstring gives the reasons."""

    def live(base: str) -> str:
        return tar1090_live_link(base, a["hex"])

    if in_progress:
        return live
    last_seen = None
    is_latest_attempt = arrival[-1]["approach_id"] == a["id"]
    if a["outcome"] in STILL_FLYING_OUTCOMES and is_latest_attempt and a["flight_id"] is not None:
        last_seen = conn.execute("SELECT MAX(last_seen) FROM flights WHERE hex = ?", (a["hex"],)).fetchone()[0]
    if last_seen is not None and now - last_seen <= LIVE_SEEN_WITHIN_S:
        return live
    trace_ended_at = a["ended_at"] if last_seen is None else max(a["ended_at"], last_seen)

    def trace(base: str) -> str:
        return tar1090_link(base, a["hex"], a["established_at"], trace_ended_at, arrival[0]["established_at"])

    return trace


def _weather(conn: sqlite3.Connection, a: sqlite3.Row) -> dict[str, Any] | None:
    """The METAR row the approach was scored against, or None when it has none, or the row is gone."""
    if a["weather_id"] is None:
        return None
    row = conn.execute(f"SELECT {', '.join(WEATHER_COLUMNS)} FROM weather WHERE id = ?", (a["weather_id"],)).fetchone()
    return None if row is None else dict(row)


def _path_fields(cfg: Config, a: sqlite3.Row, path: str | None, rwy: RunwayEnd | None) -> dict[str, Any]:
    """The path-naming fields `reference` carries: the path, its label and source, and the centreline's label."""
    # The path the approach flew last (None for a straight-in), and what the configuration says about it. The labels
    # are the ones /api/arrivals gives, from the same map and fallbacks: a path no longer configured keeps its stored
    # name as its label, and has no source to give. `centreline_label` is what the approach's runway is called when it
    # is not on a path ("ILS" or "straight-in"), so that the drawer names it as the log does.
    labels = reference_labels(cfg.airports)
    configured = None if rwy is None or path is None else reference_for(rwy, path)
    return {
        "path": path, "path_label": None if path is None else label_for(labels, a["airport"], a["runway"], path),
        "path_source": None if configured is None or configured.path is None else configured.path.source,
        "centreline_label": label_for(labels, a["airport"], a["runway"], None)}


def _reference(cfg: Config, a: sqlite3.Row, path: str | None, points: list[dict[str, Any]],
               in_progress: bool) -> dict[str, Any] | None:
    """The glide path the approach's deviations were measured from (`reference` in approach_detail's result): refit
    from a closed approach's own points when they allow it, else the runway's configuration, else None."""
    airport = cfg.airports.get(a["airport"])
    rwy = None if airport is None else airport.runways.get(a["runway"])
    path_fields = _path_fields(cfg, a, path, rwy)
    reference = None if rwy is None else {
        "glide_path_deg": rwy.glide_path_deg, "tch_ft": rwy.tch_ft, "threshold_elev_ft": rwy.threshold_elev_ft,
        "ft_per_nm": NM_FT, "reference_source": rwy.reference_source, **path_fields}
    if not in_progress:
        derived = reference_from_points(points)
        if derived is not None:
            tch_ft, glide_path_deg = derived
            reference = {
                "glide_path_deg": glide_path_deg, "tch_ft": tch_ft,
                "threshold_elev_ft": None if rwy is None else rwy.threshold_elev_ft,
                "ft_per_nm": NM_FT, "reference_source": a["reference_source"], **path_fields}
    return reference


def _external_lookup(conn: sqlite3.Connection, approach_id: int) -> dict[str, Any] | None:
    """The approach's adsb.lol lookup as the drawer shows it, or None for an approach without one."""
    # `external` is the approach's adsb.lol lookup, None without one.
    # Gap filling adds the exact matches, the offset applied and its source, and what the
    # lookup filled inside gaps in the receiver's track; points_added still counts the tail's points only.
    lookup = conn.execute(
        "SELECT status, points_added, due_at, http_status, last_error, overlap_n, offset_s, offset_spread_s, "
        "offset_ft, offset_spread_ft, match_n, match_offset_s, applied_offset_s, offset_source, gaps_filled, "
        "gap_points_added FROM external_lookups WHERE approach_id = ?", (approach_id,)).fetchone()
    return None if lookup is None else {
        "status": lookup["status"], "points_added": lookup["points_added"], "due_at": lookup["due_at"],
        "http_status": lookup["http_status"], "error": lookup["last_error"], "overlap_n": lookup["overlap_n"],
        "offset_s": lookup["offset_s"], "offset_spread_s": lookup["offset_spread_s"], "offset_ft": lookup["offset_ft"],
        "offset_spread_ft": lookup["offset_spread_ft"], "match_n": lookup["match_n"],
        "match_offset_s": lookup["match_offset_s"], "applied_offset_s": lookup["applied_offset_s"],
        "offset_source": lookup["offset_source"], "gaps_filled": lookup["gaps_filled"],
        "gap_points_added": lookup["gap_points_added"]}


def _fleet_norm(conn: sqlite3.Connection, cfg: Config, a: sqlite3.Row) -> dict[str, Any] | None:
    """The fleet norm the approach was judged against, from the newest rebuild's bins, or None."""
    # The result carries the key's kind and key, the key's own approach count in the window, the width of a bin and the
    # bins. None for an approach with no fleet_key and for a key the latest rebuild no longer writes.
    key = parse_key(a["fleet_key"])
    if key is None:
        return None
    rows = [dict(r) for r in conn.execute(
        f"SELECT {', '.join(FLEET_BIN_COLUMNS)}, key_n FROM fleet_stats WHERE airport = ? AND runway = ? "
        f"AND key_kind = ? AND key = ? ORDER BY bin_nm", (a["airport"], a["runway"], *key))]
    if not rows:
        return None
    bins = [{c: r[c] for c in FLEET_BIN_COLUMNS} for r in rows]
    return {"key_kind": key[0], "key": key[1], "n": rows[0]["key_n"], "bin_width_nm": cfg.fleet.bin_nm, "bins": bins}


def approach_detail(conn: sqlite3.Connection, approach_id: int, cfg: Config, tar1090: str, now: float) -> dict[str, Any] | None:
    """Everything the drawer shows about one approach, or None when there is no such approach. `reference` is the
    glide path the scorer measured this approach's deviations from: height above the threshold tch_ft + distance ×
    ft_per_nm × tan(glide_path_deg), for distances of 0 nm or more. For a closed approach with enough usable points,
    that is refit from its own stored points (reference_from_points), since the runway's reference now may have moved
    on since this approach was scored; short of that -- an in-progress attempt, too few usable points, or a refit that
    is not clean -- it falls back to the runway's current configuration, and is None only when that too is
    unavailable (an unconfigured runway with no usable points to refit from). `in_progress` is true for an attempt
    that hasn't ended: its `gates` are empty (scored only at close) and its `points` come from the live events seen so
    far (live_points) rather than the stored table, and `tar1090_url` is tar1090's live view instead of the
    time-filtered trace.

    `external_globe_url` is the drawer's second link: the same live-or-trace choice as `tar1090_url`, with the same
    window and starting attempt, rendered against `cfg.external.globe_url` (adsb.lol's own globe by default) instead
    of `tar1090`, so it always shows the same aircraft over the same timeframe. It is None when `globe_url` is empty,
    which turns the second link off -- independent of `[external] enabled`, which only switches the poller's own
    requests.

    A go-around, touch-and-go or abandoned attempt that is still its arrival's latest, with a flight row, gets that
    same live view instead of the plain trace while the hex has been seen within LIVE_SEEN_WITHIN_S of `now`: the
    circuit after a go-around is not an open attempt, but the aircraft is usually still in view. "Seen" is the newest
    `last_seen` among every flights row for the hex, not only the one the attempt's own `flight_id` names, because a
    collector restart opens a new flight row for the same aircraft (observed on a live collector: a
    go-around's own flight_id stayed frozen at its pre-restart value while the restart's new flight_id kept tracking
    the same hex).
    `now` is the request's clock, passed in rather than read here, the way the rest of the web package injects it
    (e.g. web.live.health_report). Once the hex's newest `last_seen` is older than that, the trace's end moves out to
    5 minutes after it (or the attempt's own end, whichever is later) instead of stopping 5 minutes after the attempt
    ended, so the trace keeps showing the aircraft for as long as the collector was still seeing it.
    `Pipeline.process` (item A) persists an active flight's `last_seen` at least every 30 s, so LIVE_SEEN_WITHIN_S
    only has to cover that cadence, not a whole idle session."""
    a = conn.execute("SELECT * FROM approaches WHERE id = ?", (approach_id,)).fetchone()
    if a is None:
        return None
    summary = _summary_row(a)
    arrival = _arrival_attempts(conn, a["arrival_id"])
    in_progress = a["ended_at"] is None
    if in_progress:
        gates: list[dict[str, Any]] = []
        points = live_points(conn, approach_id, a["established_at"])
    else:
        gates = _stored_gates(conn, approach_id)
        points = _stored_points(conn, approach_id)
    render = _link_renderer(conn, a, arrival, in_progress, now)
    # The live-or-trace choice above, and a trace's window, are decided once and rendered against both tar1090 bases,
    # so the local link and the drawer's second one (adsb.lol's own globe) can never show different timeframes for
    # the same approach -- that agreement is the whole point of the second link.
    tar1090_url = render(tar1090)
    external_globe_url = render(cfg.external.globe_url) if cfg.external.globe_url else None
    weather = _weather(conn, a)
    reference = _reference(cfg, a, summary["path"], points, in_progress)
    external = _external_lookup(conn, approach_id)
    fleet = _fleet_norm(conn, cfg, a)
    return {"approach": summary, "arrival": arrival, "gates": gates, "points": points, "weather": weather,
            "reference": reference, "tar1090_url": tar1090_url, "external_globe_url": external_globe_url,
            "in_progress": in_progress, "external": external, "fleet": fleet}

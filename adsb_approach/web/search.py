"""Find a flight: a callsign, registration or hex typed into a box, matched against every
`flights` session the collector has stored. A session that has approach rows is listed as its arrivals; one that has
none is listed as a plain flight row, newest first."""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from ..tracks import TrackSummary
from .arrivals import DEFAULT_LIMIT, Labels, _float, arrival_rows
from .detail import tar1090_link

SEARCH_PARAMS = frozenset({"q", "since", "until"})
MIN_TEXT = 2                            # characters; "N1" still matches about 11,000 sessions in a live database
KINDS = ("arrival", "flight")           # 'arrival' sorts first, so it comes before a flight row of the same time
HEX_ID = re.compile(r"[0-9A-F]{6}")
# The text of the index flights_reg_key (migration 0006). SQLite uses an expression index only for this exact text.
REGISTRATION_KEY = "replace(f.registration, '-', '')"


@dataclass(frozen=True)
class SearchQuery:
    text: str                                  # trimmed and upper-case
    since: float | None = None                 # a session that was heard at or after this (UNIX seconds)
    until: float | None = None                 # ...and first heard before this

    @classmethod
    def parse(cls, params: Mapping[str, str]) -> SearchQuery:
        """From query parameters q, since and until. An unknown parameter, a missing or one-character q, or a bad time
        is a ValueError that names it. One character is refused because it matches nearly every session, and the
        query that sorts them all is too slow for a Raspberry Pi. Hyphens do not count: the registration branch
        matches without them, so `N-` would prefix every US registration. Text with a character above U+FFFF is refused
        too, as it has no business in a callsign, registration or hex."""
        unknown = sorted(set(params) - SEARCH_PARAMS)
        if unknown:
            raise ValueError(f"unknown parameter {unknown[0]!r}")
        text = params.get("q", "").strip()
        if not text:
            raise ValueError("q: search text is required")
        if max(text) > "\uffff":        # no callsign, registration or hex has one; _after relies on it
            raise ValueError("q: search text has a character that can't be searched")
        if len(text.replace("-", "")) < MIN_TEXT:
            raise ValueError(f"q: type at least {MIN_TEXT} characters")
        return cls(text=text.upper(), since=_float(params, "since"), until=_float(params, "until"))


def _after(prefix: str) -> str:
    """The least string above every string that starts with `prefix`. `col >= prefix AND col < _after(prefix)` is a
    prefix match an index can answer, and unlike LIKE it reads `%`, `_` and quotes as the characters they are. The
    prefix comes from SearchQuery.parse, which refuses a character above U+FFFF, so adding one to the last cannot pass
    U+10FFFF, where chr() raises. U+D7FF is followed by U+E000: the code points between are surrogates, which sqlite3
    cannot encode."""
    step = ord(prefix[-1]) + 1
    return prefix[:-1] + chr(0xE000 if step == 0xD800 else step)


def match_sql(q: SearchQuery) -> tuple[str, list[Any]]:
    """The condition on a flights row `f` for the sessions `q` selects, and its arguments: a callsign prefix, a
    registration prefix with hyphens ignored on both sides, and, for exactly six hex digits, that hex; within the
    window, if any, as a session that overlaps it."""
    by = ["(f.callsign >= ? AND f.callsign < ?)"]
    args: list[Any] = [q.text, _after(q.text)]
    key = q.text.replace("-", "")
    if key:                                    # a text of hyphens alone has no registration to prefix
        by.append(f"({REGISTRATION_KEY} >= ? AND {REGISTRATION_KEY} < ?)")
        args += [key, _after(key)]
    if HEX_ID.fullmatch(q.text):
        by.append("f.hex = ?")
        args.append(q.text.lower())
    conds = [f"({' OR '.join(by)})"]
    if q.since is not None:
        conds.append("f.last_seen >= ?")
        args.append(q.since)
    if q.until is not None:
        conds.append("f.first_seen < ?")
        args.append(q.until)
    return " AND ".join(conds), args


def page_sql(q: SearchQuery, cursor: tuple[float, str, int] | None, limit: int) -> tuple[str, list[Any]]:
    """The query for one page, a row more than `limit` so that the caller can tell whether another page follows.
    Each row is (t, kind, row_id, flight_id): an arrival's first attempt, at the time it was established, or a
    session with no approach at all, at the time it was first heard. They are ordered by t, newest first, then
    kind and row_id, which makes the cursor a total order."""
    where, args = match_sql(q)
    sql = (f"WITH m AS (SELECT f.id, f.first_seen FROM flights f WHERE {where}) "
           "SELECT * FROM ("
           "SELECT a.established_at AS t, 'arrival' AS kind, a.id AS row_id, a.flight_id AS flight_id "
           "FROM approaches a JOIN m ON a.flight_id = m.id WHERE a.id = a.arrival_id "
           "UNION ALL "
           "SELECT m.first_seen, 'flight', m.id, m.id FROM m "
           "WHERE NOT EXISTS (SELECT 1 FROM approaches x WHERE x.flight_id = m.id)"
           ")")
    if cursor is not None:
        t, kind, row_id = cursor
        sql += " WHERE t < ? OR (t = ? AND kind > ?) OR (t = ? AND kind = ? AND row_id < ?)"
        args += [t, t, kind, t, kind, row_id]
    sql += " ORDER BY t DESC, kind ASC, row_id DESC LIMIT ?"
    args.append(limit + 1)
    return sql, args


def encode_search_cursor(t: float, kind: str, row_id: int) -> str:
    return f"{t!r}:{kind}:{row_id}"


def decode_search_cursor(text: str | None) -> tuple[float, str, int] | None:
    """(t, kind, row_id) of the last row on the previous page, or None for the first page."""
    if text is None or not text.strip():
        return None
    parts = text.rsplit(":", 2)
    try:
        if len(parts) != 3 or parts[1] not in KINDS:
            raise ValueError(text)
        return float(parts[0]), parts[1], int(parts[2])
    except ValueError:
        raise ValueError(f"cursor: {text!r} is not a cursor from this API") from None


def list_search(conn: sqlite3.Connection, q: SearchQuery, *, limit: int = DEFAULT_LIMIT,
                cursor: tuple[float, str, int] | None = None, labels: Labels | None = None) -> dict[str, Any]:
    """One page of rows for `q`, newest first; `next_cursor` fetches the next page and is None on the last. `counts`
    covers every matching session, not just this page: how many, and how many of them have an approach row. `labels`
    names the references of the attempts in its arrival rows, as it does for the log (arrivals.arrival_rows)."""
    where, args = match_sql(q)
    flights, with_approach = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(EXISTS (SELECT 1 FROM approaches x WHERE x.flight_id = f.id)), 0) "
        f"FROM flights f WHERE {where}", args).fetchone()
    sql, args = page_sql(q, cursor, limit)
    page = conn.execute(sql, args).fetchall()
    more = len(page) > limit
    page = page[:limit]
    arrivals = {a["arrival_id"]: a for a in arrival_rows(conn, [r["row_id"] for r in page if r["kind"] == "arrival"],
                                                         labels=labels)}
    ids = sorted({r["flight_id"] for r in page})
    sessions = {r["id"]: r for r in conn.execute(
        "SELECT f.id, f.hex, f.callsign, f.registration, f.type_code, f.traffic_class, f.first_seen, f.last_seen, "
        # Later by when the session began, then by id, as a hex has no two sessions that began together.
        "NOT EXISTS (SELECT 1 FROM flights g WHERE g.hex = f.hex AND (g.first_seen, g.id) > (f.first_seen, f.id)) "
        f"AS newest FROM flights f WHERE f.id IN ({', '.join('?' * len(ids))})", ids)}
    rows: list[dict[str, Any]] = []
    for r in page:
        s = sessions[r["flight_id"]]
        if r["kind"] == "arrival":
            rows.append({**arrivals[r["row_id"]], "kind": "arrival", "flight_id": s["id"],
                         "registration": s["registration"]})
        else:
            rows.append({"kind": "flight", "flight_id": s["id"], "hex": s["hex"], "callsign": s["callsign"],
                         "registration": s["registration"], "type_code": s["type_code"],
                         "traffic_class": s["traffic_class"], "first_seen": s["first_seen"],
                         "last_seen": s["last_seen"], "newest": bool(s["newest"])})
    return {"rows": rows, "counts": {"flights": flights, "with_approach": with_approach},
            "next_cursor": encode_search_cursor(page[-1]["t"], page[-1]["kind"], page[-1]["row_id"]) if more else None}


def _utc_day(t: float) -> date:
    return datetime.fromtimestamp(t, timezone.utc).date()


def grey_row(row: dict[str, Any], summary: TrackSummary, *, tar1090: str, globe_url: str | None,
             configured: Collection[str]) -> dict[str, Any]:
    """A flight row from list_search with what its track says (`summary`, from tracks.summarize): its label, lowest and
    highest points, squawks and emergency runs, and the two links that open it in tar1090 and on adsb.lol.

    `no_approach_logged` marks a likely matcher miss, an arrival at an airport the analyser scores (`configured`); an
    arrival at any other field has no approach row by design. Both links cover the session from first heard to last
    heard (`heard_to` runs on to the track's last point when a session still open was read past `last_seen`), padded as
    the approach drawer's are. tar1090 takes a single UTC date, so they keep to the day of the label's key moment, or
    of first heard when there is no track; a session across midnight does not link to a day it was not at that moment.
    `external_globe_url` is None when `globe_url` is empty, which turns the link off."""
    heard_to = row["last_seen"] if summary.last_t is None else max(row["last_seen"], summary.last_t)
    day = _utc_day(row["first_seen"] if summary.key_t is None else summary.key_t)

    def link(base: str) -> str:
        return tar1090_link(base, row["hex"], row["first_seen"], heard_to, day=day)

    return {**row, "label": summary.label, "label_airport": summary.airport,
            "no_approach_logged": summary.kind == "arrival" and summary.airport in configured,
            "lowest_ft": summary.lowest_ft, "lowest_nm": summary.lowest_nm, "highest_ft": summary.highest_ft,
            "squawks": list(summary.squawks),
            "emergency_flags": list(dict.fromkeys(run.flag for run in summary.emergency)),
            # not `emergency`, which on an arrival row is a boolean, so that a row's emergency is never a list to test
            "emergency_runs": [{"flag": run.flag, "code": run.code, "start": run.start, "end": run.end,
                                "alt_ft": run.alt_ft} for run in summary.emergency],
            "heard_from": row["first_seen"], "heard_to": heard_to,
            "tar1090_url": link(tar1090), "external_globe_url": link(globe_url) if globe_url else None}

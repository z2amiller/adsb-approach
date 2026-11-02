"""Seeding for the web tests: the external_lookups rows and spliced approaches the trace backfill used to write. The
trace backfill is retired in favour of merging adsb.lol positions live, and `Store` no longer has these methods, but the
tables stay and a database from before the change still holds such rows, which the detail drawer still reads
(web/detail.py). The bodies are the retired `Store` methods', unchanged but for the leading `store`."""
from __future__ import annotations

from typing import Any

from adsb_approach.model import LATCH_MASK, Flags, Outcome
from adsb_approach.store.db import GATE_COLUMNS, POINT_COLUMNS, SUMMARY_COLUMNS, Store, _insert_sql, _point_values

# external_lookups' seam measurement: the trace vs. the receiver's points, ADS-B positions only.
SEAM_COLUMNS = ("overlap_n", "offset_s", "offset_spread_s", "offset_ft", "offset_spread_ft")
# external_lookups' offset: the exact matches, and the offset the trace was shifted by.
OFFSET_COLUMNS = ("match_n", "match_offset_s", "match_spread_s", "applied_offset_s", "offset_source")


def schedule_lookup(store: Store, approach_id: int, hexid: str, due_at: float, closed_outcome: str) -> None:
    """A pending lookup for a closed approach; an approach has at most one, so scheduling it again does nothing.
    `closed_outcome` is the outcome the attempt closed with, so a later splice can judge the tail by it even once
    the approach's own outcome has moved on (a reappearance, a ground report)."""
    store.conn.execute("INSERT OR IGNORE INTO external_lookups (approach_id, hex, closed_outcome, due_at) "
                       "VALUES (?, ?, ?, ?)", (approach_id, hexid.lower(), closed_outcome, due_at))


def record_lookup(store: Store, approach_id: int, *, status: str, due_at: float, attempts: int, fetched_at: float,
                  http_status: int | None, error: str | None, points_added: int = 0, note: str | None = None,
                  seam: dict[str, float | int | None] | None = None,
                  offset: dict[str, float | int | str | None] | None = None, gaps_filled: int = 0,
                  gap_points_added: int = 0) -> None:
    """What a fetch came to: the lookup's new status and due time, its attempts so far, and the fetch's time, HTTP
    status and error (None when the fetch succeeded). `points_added` counts the tail's points; `gaps_filled` and
    `gap_points_added` count the gaps in the receiver's track that gained points and the points they gained, written on
    every fetch as points_added is. `seam` is the splice's seam measurement: overlap_n, offset_s, offset_spread_s,
    offset_ft and offset_spread_ft. `offset` is the exact matches and the offset the trace was shifted by: match_n,
    match_offset_s, match_spread_s, applied_offset_s and offset_source. Given, either is written; None leaves the stored
    values alone, so a fetch that measures nothing keeps the last measurement."""
    sets = ["status = ?", "due_at = ?", "attempts = ?", "last_fetch_at = ?", "http_status = ?", "last_error = ?",
            "points_added = ?", "note = ?", "gaps_filled = ?", "gap_points_added = ?"]
    values: list[Any] = [status, due_at, attempts, fetched_at, http_status, error, points_added, note, gaps_filled,
                         gap_points_added]
    if seam is not None:
        sets += [f"{c} = ?" for c in SEAM_COLUMNS]
        values += [seam[c] for c in SEAM_COLUMNS]
    if offset is not None:
        sets += [f"{c} = ?" for c in OFFSET_COLUMNS]
        values += [offset[c] for c in OFFSET_COLUMNS]
    store.conn.execute(f"UPDATE external_lookups SET {', '.join(sets)} WHERE approach_id = ?", (*values, approach_id))


def extend_approach(store: Store, approach_id: int, *, outcome: Outcome, flags: Flags, landed_confirmed_by: str | None,
                    summary: dict[str, Any] | None, gates: list[dict[str, Any]] | None, points: list[dict[str, Any]],
                    ga: dict[str, Any] | None, clear_ga: bool = False, bins: list[dict[str, Any]] | None = None,
                    fleet_key: str | None = None) -> None:
    """Splice an external tail into a closed approach, in one unit of work: its points are added beside the stored ones,
    never over them, and its outcome, flags and confirmation rewritten; so are its summary and gates when given (a
    splice that scored nothing again gives neither). Its go-around fields are set from `ga` when given; with `ga` None,
    `clear_ga` says what to do with whatever the receiver's own inferred go-around may have left there: True sets them
    all to NULL (a tail that lands an approach the receiver had inferred a go-around for), False leaves them as they
    are. `ga` and `clear_ga=True` are never both given. ended_at stays as the receiver's own points left it, while
    `bins`, given, replaces the approach's distance bins with the ones every point it now holds makes and `fleet_key`
    records, with the rewritten gates, the norm they were judged against."""
    with store.atomic():
        # flags keeps LATCH_MASK bits already stored, the same as every other writer.
        store.conn.execute("UPDATE approaches SET outcome=?, flags=? | (flags & ?), landed_confirmed_by=? WHERE id=?",
                           (outcome.value, int(flags), int(LATCH_MASK), landed_confirmed_by, approach_id))
        if summary is not None:
            sets = ", ".join(f"{c}=?" for c in SUMMARY_COLUMNS)
            store.conn.execute(f"UPDATE approaches SET {sets} WHERE id=?",
                               [*[summary.get(c) for c in SUMMARY_COLUMNS], approach_id])
        if ga is not None:
            store.conn.execute("UPDATE approaches SET ga_initiated_at=?, ga_initiated_along_nm=?, ga_initiated_hat_ft=?, "
                               "ga_initiated_gs=? WHERE id=?", (ga.get("initiated_at"), ga.get("initiated_along_nm"),
                                                               ga.get("initiated_hat_ft"), ga.get("initiated_gs"), approach_id))
        elif clear_ga:
            store.conn.execute("UPDATE approaches SET ga_initiated_at=NULL, ga_initiated_along_nm=NULL, "
                               "ga_initiated_hat_ft=NULL, ga_initiated_gs=NULL WHERE id=?", (approach_id,))
        if gates is not None:
            store.conn.execute("DELETE FROM approach_gates WHERE approach_id=?", (approach_id,))
            store.conn.executemany(_insert_sql("approach_gates", GATE_COLUMNS),
                                   [(approach_id, *[int(g[c]) if c == "observed" else g.get(c) for c in GATE_COLUMNS])
                                    for g in gates])
            store.conn.execute("UPDATE approaches SET fleet_key=? WHERE id=?", (fleet_key, approach_id))
        if bins is not None:
            store.replace_bins(approach_id, bins)
        store.conn.executemany(_insert_sql("approach_points", POINT_COLUMNS), [_point_values(approach_id, p) for p in points])

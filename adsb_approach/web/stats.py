"""The coverage page's data: the coverage_daily rows of a span, the daily outcome counts per
airport, the marks in the configuration and a summary of the norms in force — one read of the database, no stream.

Nothing here scores a point again: every number comes from `coverage_daily`, which the rebuild wrote, or
from a count over `approaches`. The days are the configured zone's, so the outcome counts are folded in Python for
the same reason the rebuild's are — SQLite's 'localtime' knows only the host's own zone."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import date, timedelta, tzinfo
from typing import Any

from ..config import Config, zone_of
from ..fleet import day_bounds, local_day
from ..model import Flags

COVERAGE_FIELDS = ("airport", "runway", "day", "n", "lowest_hat_p25", "lowest_hat_p50", "lowest_hat_p75",
                   "closest_nm_p50", "gate1000_observed_frac", "gate500_observed_frac", "backfill_n", "backfill_frac")
DEFAULT_DAYS, MAX_DAYS = 30, 3650        # the page opens on 30 days; 'all' spans every day stored
STATS_PARAMS = frozenset({"days"})


def coverage_days(conn: sqlite3.Connection, *, since_day: str | None) -> list[dict[str, Any]]:
    """The span's coverage_daily rows, by airport, runway and day. A day is a YYYY-MM-DD string, so the bound is a
    string comparison and the table's own primary key orders them. There is no upper bound: the rebuild writes no day
    after the one in progress. `built_at` is left out — the page draws the days, and the norms summary already says
    when the last rebuild ran."""
    where = "" if since_day is None else " WHERE day >= ?"
    args: tuple = () if since_day is None else (since_day,)
    return [dict(r) for r in conn.execute(
        f"SELECT {', '.join(COVERAGE_FIELDS)} FROM coverage_daily{where} ORDER BY airport, runway, day", args)]


def outcome_days(conn: sqlite3.Connection, *, since: float | None,
                 day_of: Callable[[float], str]) -> list[dict[str, Any]]:
    """How each local day's approaches ended, per airport: one row per (airport, day, outcome). The
    attempts are counted, never scored again, and the rows are folded as the cursor yields them, so the pass holds
    one entry per day, airport and outcome. An attempt still open has no outcome yet; one the startup sweep closed as
    INTERRUPTED is left out, as it is from coverage_daily, since it has no points and says nothing about what the
    receiver saw. Every run's attempts are counted, live or replay: the page is descriptive, and a database of
    replays on a workstation would otherwise draw an empty chart beside a full arrivals log."""
    where = "" if since is None else " AND ended_at >= ?"
    args: tuple = (int(Flags.INTERRUPTED),) if since is None else (int(Flags.INTERRUPTED), since)
    counts: dict[tuple[str, str, str], int] = {}
    for airport, outcome, ended_at in conn.execute(
            f"SELECT airport, outcome, ended_at FROM approaches "
            f"WHERE outcome IS NOT NULL AND (flags & ?) = 0{where}", args):
        key = (airport, day_of(ended_at), outcome)
        counts[key] = counts.get(key, 0) + 1
    return [dict(airport=a, day=d, outcome=o, n=counts[(a, d, o)]) for a, d, o in sorted(counts)]


def norms_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """The norms in force: how many keys of each kind fleet_stats holds, and when they were built. A key
    is (airport, runway, key_kind, key) — the unit the rebuild counts — so one type flown to two runways is two keys,
    and `built_at` is the newest row's, which is the last rebuild that wrote one."""
    counts = {kind: n for kind, n in conn.execute(
        "SELECT key_kind, COUNT(*) FROM (SELECT DISTINCT airport, runway, key_kind, key FROM fleet_stats) "
        "GROUP BY key_kind")}
    built_at = conn.execute("SELECT MAX(built_at) FROM fleet_stats").fetchone()[0]
    return {"built_at": built_at, "keys": {kind: counts.get(kind, 0) for kind in ("type", "class", "*")}}


def parse_days(params: Mapping[str, str]) -> int | None:
    """The span from the query: DEFAULT_DAYS when it is absent, None for `days=all`. An unknown parameter or a span
    that is not a whole number of days from 1 to MAX_DAYS is a ValueError naming it, as the arrivals filters are —
    int() would otherwise take '+7', '3_0' and non-ASCII digits."""
    unknown = sorted(set(params) - STATS_PARAMS)
    if unknown:
        raise ValueError(f"unknown parameter {unknown[0]!r}")
    text = params.get("days", "").strip()
    if not text:
        return DEFAULT_DAYS
    if text.lower() == "all":
        return None
    if not (text.isascii() and text.isdigit()) or not 1 <= int(text) <= MAX_DAYS:
        raise ValueError(f"days: {text!r} is not a whole number of days from 1 to {MAX_DAYS}, or 'all'")
    return int(text)


def span(days: int | None, now: float, tz: tzinfo | None) -> tuple[str | None, str]:
    """(first day, last day) of the span as local days. The last is the day `now` falls in — the day in progress,
    which the last rebuild has written only as far as it had got — and the first is `days - 1` days before it, so
    `days=30` is thirty of the page's own days counting today, not 30 × 86,400 s. `days=None` spans every day
    stored, so it has no first day."""
    until = local_day(now, tz)
    if days is None:
        return None, until
    return (date.fromisoformat(until) - timedelta(days=days - 1)).isoformat(), until


def marks(cfg: Config) -> list[dict[str, str]]:
    """The configured marks, in the date order the loader put them in."""
    return [{"at": event.at, "label": event.label} for event in cfg.coverage.events]


def stats_payload(conn: sqlite3.Connection, cfg: Config, *, days: int | None, now: float) -> dict[str, Any]:
    """Everything GET /api/stats answers: the coverage rows, the daily outcome counts, the marks and the
    norms in force, with the span it answered and the zone its days were cut in. One read of the database, so the
    four parts agree with each other."""
    tz = zone_of(cfg)
    since_day, until_day = span(days, now, tz)
    since = None if since_day is None else day_bounds(since_day, tz)[0]
    return {
        "days": days, "since_day": since_day, "until_day": until_day, "timezone": cfg.service.timezone,
        "coverage": coverage_days(conn, since_day=since_day),
        "outcomes": outcome_days(conn, since=since, day_of=lambda t: local_day(t, tz)),
        "marks": marks(cfg),
        "norms": norms_summary(conn),
    }

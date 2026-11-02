"""Aircraft on final now, and the collector's health (GET /api/live and GET /api/health)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import ExternalCfg
from ..model import EMERGENCY_MASK, flag_names
from ..store.db import current_poll_pause
from .db import DatabaseUnavailable, last_event_id, snapshot

# `path` and `path_label` are the reference the attempt is on now; an event published before
# procedure paths carries neither, and its card has no label.
CARD_FIELDS = ("along_nm", "hat_ft", "gp_dev_ft", "gp_dots", "loc_dev_ft", "loc_dots", "gs", "gs_corr", "sink_fpm",
               "path", "path_label")
FEED_SILENT_AFTER_S = 1800.0


def latest_live_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT id, started_at, ended_at, note FROM runs WHERE kind = 'live' ORDER BY id DESC LIMIT 1"
                        ).fetchone()


def live_cards(conn: sqlite3.Connection, *, rotorcraft: bool = False, ga: bool = False,
               ga_types: frozenset[str] = frozenset()) -> dict[str, Any]:
    """One card per aircraft with an attempt open in the running live run, nearest the threshold first. During a runway
    change both of an aircraft's attempts are open for a moment, and its card shows the newer. A card's values are
    those of its attempt's latest approach.started or approach.updated event: points are stored only when an attempt
    closes. Rotorcraft attempts are left out unless `rotorcraft` is set, and GA attempts (type_code in `ga_types`,
    the caller's cfg.type_classes.ga) unless `ga` is set — unless the attempt holds an EMERGENCY_MASK bit
    (a latched emergency squawk), which bypasses both exclusions the same way ArrivalFilter.where() does for the
    arrivals log."""
    newest_event = last_event_id(conn)
    run = latest_live_run(conn)
    if run is None or run["ended_at"] is not None:
        return {"cards": [], "last_event_id": newest_event}
    newest: dict[str, sqlite3.Row] = {}
    args: list[Any] = [run["id"]]
    exclude = ""
    if not rotorcraft:
        # COALESCE for the same reason as ArrivalFilter.where() — NULL != 'rotorcraft' is NULL, not true,
        # so an unclassified attempt would be dropped from the cards as if it were a helicopter. OR'd
        # with an EMERGENCY_MASK bit, so a rotorcraft that has latched one is never hidden by this exclusion.
        exclude += " AND (COALESCE(traffic_class, '') != 'rotorcraft' OR (flags & ?) != 0)"
        args.append(int(EMERGENCY_MASK))
    if not ga and ga_types:
        # The same NULL trap, one column over, and the same empty-list guard as ArrivalFilter.where() --
        # `and ga_types` keeps an empty list from building `NOT IN ()`, which SQLite accepts but standard SQL rejects.
        # The same EMERGENCY_MASK bypass as rotorcraft above.
        exclude += f" AND (COALESCE(type_code, '') NOT IN ({', '.join('?' * len(ga_types))}) OR (flags & ?) != 0)"
        args.extend(sorted(ga_types))
        args.append(int(EMERGENCY_MASK))
    for row in conn.execute("SELECT id, arrival_id, attempt_no, hex, callsign, type_code, airport, runway, established_at, "
                            f"flags FROM approaches WHERE run_id = ? AND outcome IS NULL{exclude} ORDER BY id", args):
        newest[row["hex"]] = row                   # in id order, so an aircraft's newer attempt replaces the older
    if not newest:
        return {"cards": [], "last_event_id": newest_event}
    ids = [r["id"] for r in newest.values()]
    values: dict[int, dict[str, Any]] = {}
    seen_at: dict[int, float] = {}
    # INDEXED BY: unhinted, SQLite reads the whole events table in id order to avoid a sort, and that table keeps every
    # approach.updated until events are pruned
    for e in conn.execute("SELECT approach_id, t, payload_json FROM events INDEXED BY events_t WHERE t >= ? "
                          "AND kind IN ('approach.started', 'approach.updated') "
                          f"AND approach_id IN ({', '.join('?' * len(ids))}) ORDER BY id",
                          [min(r["established_at"] for r in newest.values()), *ids]):
        values.setdefault(e["approach_id"], {}).update(json.loads(e["payload_json"]))
        seen_at[e["approach_id"]] = e["t"]
    cards = []
    for r in newest.values():
        vals = values.get(r["id"], {})
        cards.append({"approach_id": r["id"], "arrival_id": r["arrival_id"], "attempt_no": r["attempt_no"],
                      "hex": r["hex"], "callsign": vals.get("callsign", r["callsign"]),
                      "type_code": vals.get("type_code", r["type_code"]), "airport": r["airport"],
                      "runway": r["runway"], "established_at": r["established_at"], "t": seen_at.get(r["id"]),
                      # Read from the open row's flags, not the event payload -- the matcher keeps them
                      # current by OR-ing a newly latched flag onto the row at once.
                      "emergency": flag_names(int(r["flags"]) & int(EMERGENCY_MASK)),
                      **{k: vals.get(k) for k in CARD_FIELDS}})
    cards.sort(key=lambda c: (c["along_nm"] is None, c["along_nm"] or 0.0, c["approach_id"]))
    return {"cards": cards, "last_event_id": newest_event}


def _size_bytes(path: Path) -> int:
    total = 0
    for p in (path, path.with_name(path.name + "-wal")):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def _unpolled(external: ExternalCfg) -> dict[str, Any]:
    """The poller's part of the health report when there is nothing to read it from: every value but `enabled` None."""
    return {"enabled": external.enabled, "watching": None, "last_success_at": None, "paused_until": None,
            "paused_status": None, "paused_error": None, "failures_last_hour": None}


def poll_health(conn: sqlite3.Connection, external: ExternalCfg, now: float) -> dict[str, Any]:
    """The live poller's part of the health report: whether it is enabled, how many aircraft its
    newest poll asked about (0 before any poll), when a poll last succeeded, when the pause after a refused poll ends
    with the HTTP status and error that started it (None when none lasts; an unreadable key file has an error and no
    status), and how many polls failed within the hour. Every value but `enabled` is None on a database the collector
    has not migrated to external_polls yet: the web opens it read-only and never migrates it."""
    try:
        newest = conn.execute("SELECT asked FROM external_polls ORDER BY arrived_at DESC, id DESC LIMIT 1").fetchone()
        last_success = conn.execute("SELECT MAX(arrived_at) FROM external_polls WHERE http_status = 200").fetchone()[0]
        failures = conn.execute("SELECT COUNT(*) FROM external_polls WHERE error IS NOT NULL AND arrived_at >= ?",
                                (now - 3600.0,)).fetchone()[0]
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return _unpolled(external)
    pause = current_poll_pause(conn, external.backoff_min * 60.0, now)
    return {"enabled": external.enabled, "watching": 0 if newest is None else newest["asked"],
            "last_success_at": last_success, "paused_until": None if pause is None else pause.until,
            "paused_status": None if pause is None else pause.status,
            "paused_error": None if pause is None else pause.error, "failures_last_hour": failures}


def health_report(db_path: str | Path, now: float, external: ExternalCfg | None = None) -> dict[str, Any]:
    """What the page's banner shows. collector.state: none (no live run in this database), running, stopped, failed
    (its run ended with an error), or unknown (the database could not be read). feed.state: ok; silent, when the
    collector runs but nothing was stored for FEED_SILENT_AFTER_S, counted from the later of the newest event and the
    run's start; or unknown, when no collector runs. external: poll_health for `external`, the defaults (off) when
    not given. fleet: built_at, the newest fleet_stats.built_at, and age_s, now - built_at; both null while
    fleet_stats is empty (a fresh install, or a replay database, which never rebuilds norms) and in the
    unreadable-database branch. status: error when the database cannot be read, warning when the collector is not
    running, its feed is silent, or an enabled poller is paused, otherwise ok. The poller's failures within the hour
    are counted in external.failures_last_hour but, alone, no longer make status warning. The norms' age never
    affects status either: empty is normal on a fresh install, and a rebuild that has quietly stopped is not by
    itself a reason to call the service unhealthy — the age is only reported here, for whoever is watching."""
    path = Path(db_path)
    ext = external if external is not None else ExternalCfg()
    report: dict[str, Any] = {"version": __version__, "now": now,
                              "db": {"ok": True, "path": str(path), "size_bytes": _size_bytes(path), "error": None}}
    try:
        with snapshot(path) as conn:
            run = latest_live_run(conn)
            last = conn.execute("SELECT id, t FROM events ORDER BY id DESC LIMIT 1").fetchone()
            polls = poll_health(conn, ext, now)
            fleet_built_at = conn.execute("SELECT MAX(built_at) FROM fleet_stats").fetchone()[0]
    except DatabaseUnavailable as exc:
        report["db"].update(ok=False, error=str(exc))
        report.update(status="error", collector={"state": "unknown"}, feed={"state": "unknown"},
                      external=_unpolled(ext),
                      fleet={"built_at": None, "age_s": None})
        return report
    feed: dict[str, Any] = {"state": "unknown", "last_event_id": None if last is None else last["id"],
                            "last_event_t": None if last is None else last["t"], "quiet_s": None}
    if run is None:
        report["collector"] = {"state": "none"}
    else:
        state = "running" if run["ended_at"] is None else "failed" if "[failed" in (run["note"] or "") else "stopped"
        report["collector"] = {"state": state, "run_id": run["id"], "started_at": run["started_at"],
                               "ended_at": run["ended_at"], "note": run["note"]}
        if state == "running":
            quiet_s = max(0.0, now - max(run["started_at"], feed["last_event_t"] or 0.0))
            feed.update(state="silent" if quiet_s > FEED_SILENT_AFTER_S else "ok", quiet_s=quiet_s)
    report["feed"] = feed
    report["external"] = polls
    report["fleet"] = {"built_at": fleet_built_at, "age_s": None if fleet_built_at is None else now - fleet_built_at}
    troubled = polls["enabled"] and polls["paused_until"] is not None
    report["status"] = "ok" if feed["state"] == "ok" and not troubled else "warning"
    return report

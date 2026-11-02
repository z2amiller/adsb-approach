"""SQLite persistence. One connection per Store, used by one thread; the live service batches its writes."""
from __future__ import annotations

import fcntl
import itertools
import json
import sqlite3
import time
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from ..model import LATCH_MASK, Flags, Outcome, Position, Weather

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

SUMMARY_COLUMNS = ("duration_s", "first_along_nm", "closest_along_nm", "lowest_hat_ft", "points", "descent_angle_deg",
                   "gp_dev_mean_ft", "gp_dev_rms_ft", "gp_dev_max_abs_ft", "loc_dev_rms_ft", "loc_dev_max_abs_ft",
                   "gs_at_4nm", "gs_at_2nm", "sink_max_fpm")
GATE_COLUMNS = ("gate_ft", "observed", "t", "along_nm", "gp_dev_ft", "gp_dev_deg", "gp_dots", "loc_dev_ft", "loc_dev_deg",
                "loc_dots", "gs", "gs_corr", "ias_est", "sink_fpm", "nav_modes", "fleet_gp_delta_ft", "fleet_gs_delta_kt")
# `source` is readsb's position type (adsb_icao, mlat, ...) and `origin` is where a point came from ('local', the
# local receiver, or 'adsb.lol', an external tail). on_ground and qnh_assumed let the scorer run again from stored
# points.
# `reference` is the procedure path a point was measured against (migration 0008); NULL is the runway's centreline.
POINT_COLUMNS = ("t", "lat", "lon", "alt_baro", "alt_geom", "alt_qnh", "alt_corr", "temp_corrected", "qnh_assumed",
                 "on_ground", "gs", "track", "baro_rate", "geom_rate", "along_nm", "cross_ft", "hat_ft", "gp_dev_ft",
                 "gp_dev_deg", "loc_dev_ft", "loc_dev_deg", "gs_corr", "sink_fpm", "nav_altitude_mcp", "nav_qnh",
                 "nav_modes", "source", "origin", "reference")
BOOL_POINT_COLUMNS = frozenset({"temp_corrected", "qnh_assumed", "on_ground"})
BIN_COLUMNS = ("bin_nm", "n", "gp_dev_mean_ft", "loc_dev_mean_ft", "gs_corr_mean", "sink_mean_fpm")
# The two tables a fleet rebuild rewrites; built_at is added by replace_norms.
FLEET_COLUMNS = ("airport", "runway", "key_kind", "key", "bin_nm", "n", "key_n", "gp_p25", "gp_p50", "gp_p75",
                 "gs_p25", "gs_p50", "gs_p75")
COVERAGE_COLUMNS = ("airport", "runway", "day", "n", "lowest_hat_p25", "lowest_hat_p50", "lowest_hat_p75",
                    "closest_nm_p50", "gate1000_observed_frac", "gate500_observed_frac", "backfill_n", "backfill_frac")
# A day's backfill share: the approaches whose stored track carries adsb.lol positions. The retired trace backfill's
# splice set EXTERNAL_TAIL when it stored tail points and EXTERNAL_GAPS when it filled a gap, each exactly when it
# stored an origin='adsb.lol' point, so the count is an aggregate over `approaches` and never reads a point.
BACKFILL_FLAGS = Flags.EXTERNAL_TAIL | Flags.EXTERNAL_GAPS
# A run that merged adsb.lol's live positions (runs.external_merge) puts adsb.lol points into nearly every approach it
# watches, so EXTERNAL_GAPS no longer says the receiver needed adsb.lol: only a tail does.
MERGED_BACKFILL_FLAGS = Flags.EXTERNAL_TAIL
# external_positions' columns after poll_id and arrived_at: Position's fields, less `origin`, which a stored position
# always reads back as 'adsb.lol'.
EXTERNAL_POSITION_COLUMNS = ("hex", "t", "lat", "lon", "alt_baro", "on_ground", "alt_geom", "gs", "track", "baro_rate",
                             "geom_rate", "callsign", "squawk", "category", "nav_qnh", "nav_altitude_mcp", "nav_modes",
                             "source", "reg", "type_code", "emergency")


def _insert_sql(table: str, columns: tuple[str, ...], first: str = "approach_id") -> str:
    cols = (first,) + columns
    return f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"


def _point_values(approach_id: int, p: dict[str, Any]) -> tuple:
    """One approach_points row: booleans as 0 or 1, and `origin` 'local' unless the point names another."""
    return (approach_id, *[int(bool(p.get(c))) if c in BOOL_POINT_COLUMNS else p.get(c, "local") if c == "origin"
                           else p.get(c) for c in POINT_COLUMNS])


def _rowid(cur: sqlite3.Cursor) -> int:
    rowid = cur.lastrowid         # typed int | None; every successful single-row INSERT through execute() sets it
    if rowid is None:
        raise RuntimeError("INSERT reported no rowid")
    return rowid


def _external_position_values(poll_id: int, arrived_at: float, p: Position) -> tuple:
    """One external_positions row: booleans as 0 or 1 and nav_modes as the sorted, comma-joined string that
    ScoredPoint.as_row writes into approach_points."""
    values = {c: getattr(p, c) for c in EXTERNAL_POSITION_COLUMNS}
    values.update(on_ground=int(p.on_ground), nav_modes=",".join(sorted(p.nav_modes)) or None)
    return (poll_id, arrived_at, *values.values())


def _external_position(row: Sequence[Any]) -> Position:
    """The Position an external_positions row was stored from, always of origin 'adsb.lol'."""
    values = dict(zip(EXTERNAL_POSITION_COLUMNS, row))
    modes = values["nav_modes"]
    values.update(on_ground=bool(values["on_ground"]), nav_modes=frozenset(modes.split(",")) if modes else frozenset())
    return Position(**values, origin="adsb.lol")


def external_positions(conn: sqlite3.Connection) -> Iterator[tuple[float, Position]]:
    """Every position the poller stored, as (arrived_at, position) in the order they arrived, the oldest fix first
    within one arrival (and by hex after that, so a replay's order never depends on the table's layout). It reads
    through whichever connection it is given, so a read-only one will do."""
    cols = ", ".join(EXTERNAL_POSITION_COLUMNS)
    for row in conn.execute(f"SELECT arrived_at, {cols} FROM external_positions ORDER BY arrived_at, t, hex"):
        yield row[0], _external_position(row[1:])


@dataclass(frozen=True)
class PollPause:
    """Polling is paused until `until` (UNIX seconds), because the newest poll was refused with `status` (None when no
    request went out, a key file that could not be read) and `error`."""
    until: float
    status: int | None
    error: str | None


def current_poll_pause(conn: sqlite3.Connection, backoff_s: float, now: float) -> PollPause | None:
    """The pause the newest poll puts polling under, read from the table rather than held in memory so a restart
    keeps it. Only the newest row counts: a refusal ends with the next poll that was not one, and a timeout or other
    network failure, which is recorded but not `refused`, pauses nothing. None on a database the collector has not
    migrated yet, so the web, which opens it read-only, copes with an older one."""
    order = "FROM external_polls ORDER BY arrived_at DESC, id DESC LIMIT 1"
    try:
        try:
            row = conn.execute(f"SELECT arrived_at, http_status, error, refused, retry_after_s {order}").fetchone()
        except sqlite3.OperationalError as exc:
            if "no such column" not in str(exc):
                raise
            # a database the collector hasn't migrated to 0008 yet: no Retry-After, so the backoff decides
            row = conn.execute(f"SELECT arrived_at, http_status, error, refused, NULL {order}").fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    if row is None or not row[3]:
        return None
    until = row[0] + (backoff_s if row[4] is None else row[4])    # re-api's Retry-After when it sent one
    return PollPause(until, row[1], row[2]) if until > now else None


class Store:
    def __init__(self, path: str | Path, *, busy_timeout_s: float = 5.0):
        """`busy_timeout_s` is how long a write waits for another connection's transaction before it gives up with
        "database is locked" — sqlite3.connect's own `timeout`, whose default 5 s the collector keeps. The fleet
        rebuild and reflag ask for longer: they write beside a running collector, whose batches commit once a second
        but can be held up by a slow disk."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, timeout=busy_timeout_s)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._units = itertools.count()
        self._live_lock: IO[str] | None = None

    # --- schema -----------------------------------------------------------------------------------------------
    def migrate(self) -> None:
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
        applied = {r[0] for r in self.conn.execute("SELECT version FROM schema_version")}
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(sql_file.name.split("_")[0])
            if version in applied:
                continue
            # executescript() adds no BEGIN on this autocommit connection, so the script opens one: its tables and
            # version row commit together, or `with self.conn` rolls both back. Migration files must not contain
            # BEGIN or COMMIT themselves.
            with self.conn:
                self.conn.executescript("BEGIN;\n" + sql_file.read_text(encoding="utf-8"))
                self.conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, time.time()))

    # --- transactions -----------------------------------------------------------------------------------------
    @contextmanager
    def atomic(self) -> Generator[None, None, None]:
        """A unit of work written whole or not at all. On its own it commits when the block ends; inside a batch it is a
        savepoint, so a unit that fails rolls back without ending the batch. If SQLite has already rolled back the
        whole transaction itself (SQLITE_INTERRUPT, IOERR, NOMEM), the savepoint is already gone: let the unit's own
        error surface instead of the "no such savepoint" from trying to release it."""
        name = f"unit{next(self._units)}"
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute(f"ROLLBACK TO {name}")
                self.conn.execute(f"RELEASE {name}")
            raise
        self.conn.execute(f"RELEASE {name}")

    def begin_batch(self) -> None:
        """Hold every write until commit_batch(), in one transaction: the live service commits once a second. Without a
        batch each write commits on its own, as in replay. BEGIN IMMEDIATE takes the write lock at once, waiting for
        another writer as a single statement does: a deferred transaction that has read cannot wait, and its first write
        fails at once with "database is locked" while another connection writes."""
        if not self.conn.in_transaction:
            self.conn.execute("BEGIN IMMEDIATE")

    def commit_batch(self) -> None:
        if self.conn.in_transaction:
            self.conn.execute("COMMIT")

    # --- runs, flights --------------------------------------------------------------------------------------
    def new_run(self, kind: str, source: str, note: str = "", code_version: str = "", config_hash: str = "",
                external_merge: bool = False) -> int:
        """`external_merge` records that this run merged adsb.lol's live positions into its tracking: its outcomes
        depend on them, and the coverage count reads its approaches' flags by it."""
        cur = self.conn.execute(
            "INSERT INTO runs (kind, started_at, source, code_version, config_hash, note, external_merge) "
            "VALUES (?,?,?,?,?,?,?)", (kind, time.time(), source, code_version, config_hash, note, int(external_merge)))
        return _rowid(cur)

    def end_run(self, run_id: int, error: str | None = None) -> None:
        """Set ended_at. A run cut short by `error` keeps its tag in `note`, followed by " [failed: <error>]"."""
        suffix = "" if error is None else f" [failed: {error}]"
        self.conn.execute("UPDATE runs SET ended_at=?, note=LTRIM(COALESCE(note, '') || ?) WHERE id=?", (time.time(), suffix, run_id))

    def insert_flight(self, hexid: str, callsign: str | None, reg: str | None, type_code: str | None,
                      traffic_class: str, first_seen: float) -> int:
        hexid = hexid.lower()
        # A hex cannot open two sessions at the same instant, so (hex, first_seen) is the session: a replay into the
        # same database finds the row it stored before, even while this run has not yet seen the callsign.
        row = self.conn.execute("SELECT id FROM flights WHERE hex=? AND first_seen=? ORDER BY id LIMIT 1",
                                (hexid, first_seen)).fetchone()
        if row is not None:
            return int(row[0])
        cur = self.conn.execute(
            "INSERT INTO flights (hex, callsign, registration, type_code, traffic_class, first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
            (hexid, callsign, reg, type_code, traffic_class, first_seen, first_seen))
        return _rowid(cur)

    def touch_flight(self, flight_id: int, last_seen: float, callsign: str | None = None, reg: str | None = None,
                     type_code: str | None = None, traffic_class: str | None = None) -> None:
        self.conn.execute(
            "UPDATE flights SET last_seen=MAX(last_seen, ?), callsign=COALESCE(?, callsign), registration=COALESCE(?, registration), "
            "type_code=COALESCE(?, type_code), traffic_class=COALESCE(?, traffic_class) WHERE id=?",
            (last_seen, callsign, reg, type_code, traffic_class, flight_id))

    # --- approaches -------------------------------------------------------------------------------------------
    def open_approach(self, *, run_id: int, flight_id: int | None, arrival_id: int | None, attempt_no: int, hex: str,
                      callsign: str | None, type_code: str | None, traffic_class: str, approach_cat: str | None,
                      airport: str, runway: str, established_at: float, first_along_nm: float, weather_id: int | None,
                      reference_source: str, prev_approach_id: int | None, path: str | None = None) -> int:
        """Insert an attempt. A first attempt is its own arrival; a later one links its predecessor forward in the same
        unit of work, so a crash cannot leave a chain that points one way only. `path` is the procedure path it is
        flown on, if it opens on one (approaches.path holds the last path the attempt flew)."""
        with self.atomic():
            cur = self.conn.execute(
                "INSERT INTO approaches (run_id, flight_id, arrival_id, attempt_no, hex, callsign, type_code, traffic_class, "
                "approach_cat, airport, runway, established_at, first_along_nm, weather_id, reference_source, prev_approach_id, "
                "created_at, path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, flight_id, arrival_id, attempt_no, hex.lower(), callsign, type_code, traffic_class, approach_cat,
                 airport, runway, established_at, first_along_nm, weather_id, reference_source, prev_approach_id, time.time(),
                 path))
            aid = _rowid(cur)
            if arrival_id is None:
                self.conn.execute("UPDATE approaches SET arrival_id=? WHERE id=?", (aid, aid))
            if prev_approach_id is not None:
                self.conn.execute("UPDATE approaches SET next_approach_id=? WHERE id=?", (aid, prev_approach_id))
        return aid

    def add_flags(self, approach_id: int, flags: Flags) -> None:
        """OR bits onto a row's flags without touching the rest or waiting for its next full write: the emergency
        latch, stamped the moment it happens, whether the approach is still open or was already closed when it
        latched."""
        self.conn.execute("UPDATE approaches SET flags = flags | ? WHERE id = ?", (int(flags), approach_id))

    def latest_ended_approach(self, hexid: str, airport: str, since: float) -> sqlite3.Row | None:
        """The latest attempt for hexid at airport that ended at or after `since`, for Matcher._chain_arrival's
        fallback: chaining a new establishment onto its arrival's previous attempt when no in-memory record of it
        survived (e.g. a restart started a fresh Matcher). `ended_at >= since` already excludes an attempt still
        open (ended_at is NULL). None when there is no such attempt."""
        return self.conn.execute(
            "SELECT id, arrival_id, attempt_no FROM approaches WHERE hex = ? AND airport = ? AND ended_at >= ? "
            "ORDER BY ended_at DESC LIMIT 1", (hexid, airport, since)).fetchone()

    def close_approach(self, approach_id: int, *, outcome: Outcome, flags: Flags, ended_at: float, summary: dict[str, Any],
                       gates: list[dict[str, Any]], points: list[dict[str, Any]], bins: list[dict[str, Any]],
                       ga: dict[str, Any] | None, fleet_key: str | None = None,
                       landed_confirmed_by: str | None = None, path: str | None = None) -> None:
        """Close an attempt. `landed_confirmed_by`, given, names who confirmed the landing ('adsb.lol' when a polled
        position did); None leaves the column as it is. `path` is the last procedure path the attempt flew; a close that
        names none leaves what the row opened with, since a path once flown is never unflown."""
        sets = ", ".join(f"{c}=?" for c in SUMMARY_COLUMNS)
        values = [summary.get(c) for c in SUMMARY_COLUMNS]
        ga = ga or {}
        with self.atomic():
            # flags=? | (flags & ?) keeps whatever LATCH_MASK bits (the emergency latch) the row already carries: a
            # 30 s run can straddle this very close, latching on the position that closes it before the matcher's
            # while-open path could stamp it, so the close must not overwrite a bit the row already holds either way.
            self.conn.execute(
                f"UPDATE approaches SET outcome=?, flags=? | (flags & ?), ended_at=?, fleet_key=?, "
                f"landed_confirmed_by=COALESCE(?, landed_confirmed_by), path=COALESCE(?, path), ga_initiated_at=?, "
                f"ga_initiated_along_nm=?, ga_initiated_hat_ft=?, ga_initiated_gs=?, {sets} WHERE id=?",
                [outcome.value, int(flags), int(LATCH_MASK), ended_at, fleet_key, landed_confirmed_by, path,
                 ga.get("initiated_at"), ga.get("initiated_along_nm"), ga.get("initiated_hat_ft"),
                 ga.get("initiated_gs"), *values, approach_id])
            self.conn.executemany(_insert_sql("approach_gates", GATE_COLUMNS),
                                  [(approach_id, *[int(g[c]) if c == "observed" else g.get(c) for c in GATE_COLUMNS]) for g in gates])
            self.conn.executemany(_insert_sql("approach_points", POINT_COLUMNS).replace("INSERT INTO", "INSERT OR REPLACE INTO"),
                                  [_point_values(approach_id, p) for p in points])
            self.conn.executemany(_insert_sql("approach_bins", BIN_COLUMNS),
                                  [(approach_id, *[b.get(c) for c in BIN_COLUMNS]) for b in bins])

    def set_path(self, approach_id: int, path: str) -> None:
        """The procedure path an open attempt is flying now (approaches.path holds the last path flown), for a reader of
        the database mid-approach: the close writes it again, from the points."""
        self.conn.execute("UPDATE approaches SET path=? WHERE id=?", (path, approach_id))

    def reclassify(self, approach_id: int, outcome: Outcome, flags: Flags) -> None:
        """Late outcome change: a lost approach that reappeared climbing. It withdraws any landing confirmation.
        `flags`=? | (flags & LATCH_MASK) keeps the row's own LATCH_MASK bits: this writes back a copy of `flags` the
        matcher captured at close, so without the mask a flag the after-close stamp wrote afterward would be lost right
        here."""
        self.conn.execute("UPDATE approaches SET outcome=?, flags=? | (flags & ?), landed_confirmed_by=NULL WHERE id=?",
                          (outcome.value, int(flags), int(LATCH_MASK), approach_id))

    def set_confirmation(self, approach_id: int, by: str, flags: Flags) -> None:
        """Same trap as reclassify: this also writes back the matcher's copy of `flags` captured at close, so the
        LATCH_MASK mask is what keeps a later latch from being erased by a landing confirmation."""
        self.conn.execute("UPDATE approaches SET landed_confirmed_by=?, flags=? | (flags & ?) WHERE id=?",
                          (by, int(flags), int(LATCH_MASK), approach_id))

    def sweep_interrupted(self) -> tuple[int, int]:
        """At service start: a live run that never ended was cut short by a crash or a kill. Its open approaches have no
        points, gates or summary, which are written at the close, so each closes as lost with INTERRUPTED, ending when it
        was established, and the run ends as failed. Replay runs are left alone. Returns (approaches, runs).

        A live run that did end can hold open approaches too: a COMMIT that fails with SQLITE_FULL or an I/O error rolls
        its batch back, reopening the approaches that batch closed, and run_live still ends the run. They close the same
        way.

        A live run that never ended may belong to another collector still running on this database, so the sweep
        first takes an exclusive lock on `<database>.live.lock`, held until close(), and raises if another Store holds
        it. The operating system releases the lock when a process ends, so a crash or a kill never leaves it held. Once
        the lock is held, no approach of any live run is still in progress."""
        self._claim_live()
        with self.atomic():
            runs = [r[0] for r in self.conn.execute("SELECT id FROM runs WHERE kind='live' AND ended_at IS NULL ORDER BY id")]
            closed = self.conn.execute(
                "UPDATE approaches SET outcome=?, flags=flags | ?, ended_at=established_at "
                "WHERE outcome IS NULL AND run_id IN (SELECT id FROM runs WHERE kind='live')",
                (Outcome.LOST.value, int(Flags.INTERRUPTED))).rowcount
            for run_id in runs:
                self.end_run(run_id, error="interrupted")
        return closed, len(runs)

    def _claim_live(self) -> None:
        if self._live_lock is not None:
            return
        lock = open(self.path.with_name(self.path.name + ".live.lock"), "a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError(f"another live collector is using {self.path}") from None
        self._live_lock = lock

    # --- weather, events --------------------------------------------------------------------------------------
    def insert_weather(self, w: Weather) -> int:
        self.conn.execute(
            "INSERT OR IGNORE INTO weather (airport, observed_at, fetched_at, altim_hpa, temp_c, dewp_c, wind_dir, wind_kt, gust_kt, raw) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (w.airport, w.observed_at, time.time(), w.altim_hpa, w.temp_c, w.dewp_c, w.wind_dir, w.wind_kt, w.gust_kt, w.raw))
        row = self.conn.execute("SELECT id FROM weather WHERE airport=? AND observed_at=?", (w.airport, w.observed_at)).fetchone()
        return int(row[0])

    def insert_event(self, t: float, kind: str, approach_id: int | None, flight_id: int | None, payload: dict[str, Any]) -> int:
        cur = self.conn.execute("INSERT INTO events (t, kind, approach_id, flight_id, payload_json) VALUES (?,?,?,?,?)",
                                (t, kind, approach_id, flight_id, json.dumps(payload, separators=(",", ":"))))
        return _rowid(cur)

    def add_points(self, approach_id: int, points: list[dict[str, Any]], *, charts_only: bool = False) -> None:
        """Append points to an approach, closed or not, in one unit of work. A point at a time the approach already
        holds is left as stored: this never replaces one. `charts_only` marks the rows as drawn but never scored:
        points(scored_only=True) leaves them out, so a fleet rebuild's re-scoring doesn't count a go-around's
        climb-out."""
        with self.atomic():
            sql = _insert_sql("approach_points", POINT_COLUMNS + ("charts_only",)).replace(
                "INSERT INTO", "INSERT OR IGNORE INTO")
            self.conn.executemany(sql, [(*_point_values(approach_id, p), int(charts_only)) for p in points])

    # --- the live poller --------------------------------------------------------------------------------------
    def record_poll(self, *, run_id: int, requested_at: float, arrived_at: float, asked: int, returned: int | None,
                    http_status: int | None, error: str | None, refused: bool, retry_after_s: float | None = None) -> int:
        """One poll of re-api and what it came to: `asked` aircraft requested, `returned` of them in the reply (None
        when none arrived), the HTTP status (None when no reply or no request did) and the error, if any. `refused`
        is the poll that pauses the next ones (current_poll_pause), for `retry_after_s` when re-api's Retry-After gave
        one and [external] backoff_min otherwise. Returns the poll's id."""
        cur = self.conn.execute(
            "INSERT INTO external_polls (run_id, requested_at, arrived_at, asked, returned, http_status, error, refused, "
            "retry_after_s) VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, requested_at, arrived_at, asked, returned, http_status, error, int(refused), retry_after_s))
        return _rowid(cur)

    def insert_external_positions(self, poll_id: int, arrived_at: float, positions: Sequence[Position]) -> int:
        """Keep a poll's positions for a replay to merge as the live run did. Each (hex, t) is kept once, from the
        poll that brought it first: adsb.lol repeats a position until it has a newer one. Returns how many were new."""
        cols = ("poll_id", "arrived_at", *EXTERNAL_POSITION_COLUMNS)
        sql = f"INSERT OR IGNORE INTO external_positions ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
        with self.atomic():
            before = self.conn.total_changes
            self.conn.executemany(sql, [_external_position_values(poll_id, arrived_at, p) for p in positions])
            return self.conn.total_changes - before

    def external_positions(self) -> Iterator[tuple[float, Position]]:
        """The stored polled positions, (arrived_at, position) in arrival order; see the module's external_positions."""
        return external_positions(self.conn)

    # --- fleet norms and coverage -----------------------------------------------------------------------------
    def _run_kinds(self, include_replays: bool) -> str:
        """The SQL for the runs a rebuild reads: live runs, and replay runs too on request, so a database of replays
        on a workstation can have norms of its own."""
        return "('live', 'replay')" if include_replays else "('live')"

    def norm_key_counts(self, *, outcomes: tuple[str, ...], since: float,
                        include_replays: bool = False) -> list[sqlite3.Row]:
        """How many approaches each (airport, runway, type_code, traffic_class) has in the window: those that ended at
        or after `since` with one of `outcomes` and left at least one bin behind. fleet.key_counts folds these into
        the type, class and `*` keys that min_samples is measured against."""
        marks = ", ".join("?" for _ in outcomes)
        return list(self.conn.execute(
            f"SELECT a.airport, a.runway, a.type_code, a.traffic_class, COUNT(*) AS n FROM approaches a "
            f"WHERE a.outcome IN ({marks}) AND a.ended_at >= ? "
            f"AND a.run_id IN (SELECT id FROM runs WHERE kind IN {self._run_kinds(include_replays)}) "
            f"AND EXISTS (SELECT 1 FROM approach_bins b WHERE b.approach_id = a.id) "
            f"GROUP BY a.airport, a.runway, a.type_code, a.traffic_class", (*outcomes, since)))

    def norm_bin_rows(self, *, outcomes: tuple[str, ...], since: float,
                      include_replays: bool = False) -> Iterator[sqlite3.Row]:
        """The window's bins, each with the approach's keys, ordered by airport, runway and bin so a rebuild can
        finish one bin before it starts the next. The cursor is returned unread: nothing holds the whole window."""
        marks = ", ".join("?" for _ in outcomes)
        return self.conn.execute(
            f"SELECT a.airport, a.runway, a.type_code, a.traffic_class, b.bin_nm, b.gp_dev_mean_ft, b.gs_corr_mean "
            f"FROM approach_bins b JOIN approaches a ON a.id = b.approach_id "
            f"WHERE a.outcome IN ({marks}) AND a.ended_at >= ? "
            f"AND a.run_id IN (SELECT id FROM runs WHERE kind IN {self._run_kinds(include_replays)}) "
            f"ORDER BY a.airport, a.runway, b.bin_nm", (*outcomes, since))

    def coverage_day_counts(self, *, since: float, day_of: Callable[[float], str],
                            include_replays: bool = False) -> list[dict[str, Any]]:
        """How many closed approaches each (airport, runway, day) holds in the window. `day_of` turns an end time
        into its day, and `backfill_n` counts the day's approaches that needed adsb.lol: those carrying either
        BACKFILL_FLAGS flag, or only EXTERNAL_TAIL when the approach's run merged adsb.lol's live positions
        (MERGED_BACKFILL_FLAGS). A coverage day is the configured zone's, which SQLite cannot cut — its 'localtime'
        modifier knows only the host's own zone and no IANA name. The rows are folded as the cursor yields them, so the
        pass holds one entry per runway and day instead of the window's approaches. An approach the startup sweep closed
        as INTERRUPTED is left out: it has no points, gates or summary, so it is not a coverage measurement of
        anything."""
        days: dict[tuple[str, str, str], dict[str, Any]] = {}
        for airport, runway, ended_at, flags, merged in self.conn.execute(
                f"SELECT a.airport, a.runway, a.ended_at, a.flags, r.external_merge FROM approaches a "
                f"JOIN runs r ON r.id = a.run_id "
                f"WHERE a.outcome IS NOT NULL AND a.ended_at >= ? AND (a.flags & ?) = 0 "
                f"AND r.kind IN {self._run_kinds(include_replays)}",
                (since, int(Flags.INTERRUPTED))):
            key = (airport, runway, day_of(ended_at))
            row = days.get(key)
            if row is None:
                row = days[key] = dict(airport=airport, runway=runway, day=key[2], n=0, backfill_n=0)
            row["n"] += 1
            row["backfill_n"] += int((flags & int(MERGED_BACKFILL_FLAGS if merged else BACKFILL_FLAGS)) != 0)
        return [days[k] for k in sorted(days)]

    def coverage_approaches(self, airport: str, runway: str, start: float, end: float, *,
                            include_replays: bool = False) -> list[sqlite3.Row]:
        """The ids of one day's closed approaches on one runway, through the (airport, runway, ended_at) index."""
        return list(self.conn.execute(
            f"SELECT id FROM approaches WHERE airport = ? AND runway = ? AND ended_at >= ? AND ended_at < ? "
            f"AND outcome IS NOT NULL AND (flags & ?) = 0 "
            f"AND run_id IN (SELECT id FROM runs WHERE kind IN {self._run_kinds(include_replays)}) ORDER BY id",
            (airport, runway, start, end, int(Flags.INTERRUPTED))))

    def coverage_daily_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM coverage_daily ORDER BY airport, runway, day"))

    def fleet_stats_rows(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM fleet_stats ORDER BY airport, runway, key_kind, key, bin_nm"))

    def replace_norms(self, fleet: list[dict[str, Any]], coverage: list[dict[str, Any]],
                      keep: set[tuple[str, str, str]], *, built_at: float) -> None:
        """A rebuild's two tables in one transaction: fleet_stats is replaced whole, while coverage_daily keeps the days
        `keep` names and were not computed again, and loses the days that fell out of the window."""
        with self.atomic():
            self.conn.execute("DELETE FROM fleet_stats")
            self.conn.executemany(
                f"INSERT INTO fleet_stats ({', '.join(FLEET_COLUMNS)}, built_at) "
                f"VALUES ({', '.join('?' for _ in FLEET_COLUMNS)}, ?)",
                [(*[r[c] for c in FLEET_COLUMNS], built_at) for r in fleet])
            gone = [k for k in ((r["airport"], r["runway"], r["day"]) for r in self.coverage_daily_rows())
                    if k not in keep]
            self.conn.executemany("DELETE FROM coverage_daily WHERE airport = ? AND runway = ? AND day = ?", gone)
            self.conn.executemany(
                f"INSERT OR REPLACE INTO coverage_daily ({', '.join(COVERAGE_COLUMNS)}, built_at) "
                f"VALUES ({', '.join('?' for _ in COVERAGE_COLUMNS)}, ?)",
                [(*[r[c] for c in COVERAGE_COLUMNS], built_at) for r in coverage])

    def approaches_to_reflag(self, *, since: float, until: float | None = None,
                             include_replays: bool = False) -> list[sqlite3.Row]:
        """The closed approaches reflag judges again: those that ended in the range, whatever their outcome, from
        live runs unless include_replays -- the same rule norm_key_counts, norm_bin_rows and coverage_approaches
        already read their own windows by, so reflag never judges a replay's approaches against norms they were
        never part of, nor writes FAST or SLOW onto them, unless asked."""
        end = "" if until is None else " AND ended_at < ?"
        args: tuple = (since,) if until is None else (since, until)
        return list(self.conn.execute(
            "SELECT id, airport, runway, type_code, traffic_class, flags FROM approaches "
            f"WHERE outcome IS NOT NULL AND ended_at >= ?{end} "
            f"AND run_id IN (SELECT id FROM runs WHERE kind IN {self._run_kinds(include_replays)}) ORDER BY id", args))

    def replace_bins(self, approach_id: int, bins: list[dict[str, Any]]) -> None:
        """An approach's distance bins, rewritten from every point it now holds."""
        self.conn.execute("DELETE FROM approach_bins WHERE approach_id = ?", (approach_id,))
        self.conn.executemany(_insert_sql("approach_bins", BIN_COLUMNS),
                              [(approach_id, *[b.get(c) for c in BIN_COLUMNS]) for b in bins])

    def set_gate_deltas(self, approach_id: int, deltas: list[tuple[int, float | None, float | None]]) -> None:
        """Only the two fleet delta columns of the named gates; every measured value stays as it was scored."""
        self.conn.executemany(
            "UPDATE approach_gates SET fleet_gp_delta_ft = ?, fleet_gs_delta_kt = ? WHERE approach_id = ? AND gate_ft = ?",
            [(gp, gs, approach_id, gate_ft) for gate_ft, gp, gs in deltas])

    def set_fleet(self, approach_id: int, *, flags: Flags, fleet_key: str | None) -> None:
        # flags keeps LATCH_MASK bits already stored, the same as every other writer.
        self.conn.execute("UPDATE approaches SET flags = ? | (flags & ?), fleet_key = ? WHERE id = ?",
                          (int(flags), int(LATCH_MASK), fleet_key, approach_id))

    # --- queries ----------------------------------------------------------------------------------------------
    def approach(self, approach_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM approaches WHERE id=?", (approach_id,)).fetchone()

    def approaches(self, run_id: int | None = None) -> list[sqlite3.Row]:
        if run_id is None:
            return list(self.conn.execute("SELECT * FROM approaches ORDER BY id"))
        return list(self.conn.execute("SELECT * FROM approaches WHERE run_id=? ORDER BY id", (run_id,)))

    def arrival(self, arrival_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM approaches WHERE arrival_id=? ORDER BY attempt_no", (arrival_id,)))

    def gates(self, approach_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM approach_gates WHERE approach_id=? ORDER BY gate_ft DESC", (approach_id,)))

    def points(self, approach_id: int, *, scored_only: bool = False) -> list[sqlite3.Row]:
        """An approach's stored points in time order. `scored_only` leaves out the charts_only rows (a go-around's
        climb-out): what anything that scores the points again must read."""
        where = " AND charts_only = 0" if scored_only else ""
        return list(self.conn.execute(
            f"SELECT * FROM approach_points WHERE approach_id=?{where} ORDER BY t", (approach_id,)))

    def bins(self, approach_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM approach_bins WHERE approach_id=? ORDER BY bin_nm", (approach_id,)))

    def weather_row(self, weather_id: int) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM weather WHERE id=?", (weather_id,)).fetchone()

    def weather_since(self, since: float) -> list[Weather]:
        """Every stored observation at or after `since`, oldest first: what a live run reloads at startup so an
        approach already in progress does not get QNH_ASSUMED just because the process restarted."""
        rows = self.conn.execute(
            "SELECT airport, observed_at, altim_hpa, temp_c, dewp_c, wind_dir, wind_kt, gust_kt, raw "
            "FROM weather WHERE observed_at >= ? ORDER BY observed_at", (since,))
        return [Weather(*row) for row in rows]

    def events(self, since_id: int = 0, limit: int = 1000) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (since_id, limit)))

    def flights(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM flights ORDER BY id"))

    def close(self) -> None:
        self.conn.close()
        if self._live_lock is not None:
            self._live_lock.close()                   # releases the live lock
            self._live_lock = None

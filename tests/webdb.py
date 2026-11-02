"""Databases for the web tests to write to: a copy of the replayed fixtures, and the rows the live collector adds."""
from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from adsb_approach.model import Flags, Outcome, flag_names
from adsb_approach.store.db import Store

# What the collector adds to every approach event since procedure paths (matcher._publish): the path flown, if any, and
# the label of the reference. An attempt the writer opens is on no path unless it is given one, and the label of a
# centreline is not known here.
NO_PATH: dict[str, Any] = {"path": None, "path_label": None}


def copy_db(replay_db: Path, dest: Path) -> Path:
    """A copy to write to. The replay closed its database cleanly, so the one file holds everything."""
    shutil.copyfile(replay_db, dest)
    return dest


def before_migration_8(db: Path) -> Path:
    """`db` as it was before procedure paths: without approaches.path and approach_points.reference, and without the
    schema_version row for 0008. The web process may be deployed, and start answering, before the collector restarts
    and migrates, so it must read this too."""
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute("ALTER TABLE approaches DROP COLUMN path")
        conn.execute("ALTER TABLE approach_points DROP COLUMN reference")
        conn.execute("DELETE FROM schema_version WHERE version = 8")
    finally:
        conn.close()
    return db


class LiveWriter:
    """Writes what `adsb-approach run` writes for aircraft on final, through the collector's own Store: a live run,
    attempts opened and closed, and their events. Event times are t0 + dt, where t0 is the run's start by default."""

    def __init__(self, db: Path, t0: float | None = None) -> None:
        self.store = Store(db)
        self.run_id = self.store.new_run("live", "readsb-json:test")
        self.t0: float = t0 if t0 is not None else self.store.conn.execute(
            "SELECT started_at FROM runs WHERE id = ?", (self.run_id,)).fetchone()[0]
        self._flight: dict[int, tuple[str, str | None]] = {}      # approach_id -> (callsign, type_code), for update()
        self._attempt: dict[int, dict[str, Any]] = {}      # approach_id -> what approach.completed reports about it
        self._path: dict[int, dict[str, Any]] = {}         # approach_id -> the path and label its events carry

    def flight_row(self, hexid: str, callsign: str, *, last_seen: float, first_seen: float | None = None,
                   type_code: str = "B738", traffic_class: str = "airline", reg: str | None = None) -> int:
        """A flights row for open()'s flight_id, with last_seen set directly to whatever a test needs — not through
        the collector's own cadence (Tracker.on_flight_update), which only touches it at session boundaries."""
        fid = self.store.insert_flight(hexid, callsign, reg, type_code, traffic_class,
                                       first_seen if first_seen is not None else last_seen)
        self.store.touch_flight(fid, last_seen)
        return fid

    def open(self, hexid: str, callsign: str, *, runway: str = "28L", airport: str = "KSFO", dt: float = 0.0,
             along_nm: float = 6.0, hat_ft: float = 1900.0, arrival_id: int | None = None, attempt_no: int = 1,
             prev_approach_id: int | None = None, sidestep: bool = False, traffic_class: str = "airline",
             flight_id: int | None = None, type_code: str | None = "B738", path: str | None = None,
             path_label: str | None = None, origin: str | None = None) -> int:
        """`path` is the procedure path the attempt opens on (approaches.path, and every event it publishes), and
        `path_label` the label those events carry for the reference it is on. `origin` puts the key on the event as the
        collector's events carry it since the live merge; left None, the event is one written before that."""
        t = self.t0 + dt
        aid = self.store.open_approach(
            run_id=self.run_id, flight_id=flight_id, arrival_id=arrival_id, attempt_no=attempt_no, hex=hexid, callsign=callsign,
            type_code=type_code, traffic_class=traffic_class, approach_cat="C", airport=airport, runway=runway, established_at=t,
            first_along_nm=along_nm, weather_id=None, reference_source="cifp:test", prev_approach_id=prev_approach_id,
            path=path)
        self._flight[aid] = (callsign, type_code)
        self._path[aid] = {"path": path, "path_label": path_label}
        self._attempt[aid] = {"attempt_no": attempt_no, "arrival_id": aid if arrival_id is None else arrival_id,
                              "lowest_hat_ft": hat_ft, "closest_along_nm": along_nm}
        payload: dict[str, Any] = {"hex": hexid, "runway": runway, "airport": airport, "callsign": callsign,
                                   "type_code": type_code, "along_nm": along_nm, "attempt_no": attempt_no,
                                   "arrival_id": self._attempt[aid]["arrival_id"], "hat_ft": hat_ft,
                                   "traffic_class": traffic_class, **self._path[aid]}
        if sidestep:
            payload["sidestep"] = True                 # as the collector writes a runway change's new attempt
        if origin is not None:
            payload["origin"] = origin
        self.store.insert_event(t, "approach.started", aid, None, payload)
        return aid

    def update(self, approach_id: int, hexid: str, *, dt: float, along_nm: float, hat_ft: float, runway: str = "28L",
               airport: str = "KSFO", origin: str | None = None) -> int:
        callsign, type_code = self._flight.get(approach_id, (None, None))
        attempt = self._attempt.get(approach_id)
        if attempt is not None:
            attempt["lowest_hat_ft"] = min(attempt["lowest_hat_ft"], hat_ft)
            attempt["closest_along_nm"] = min(attempt["closest_along_nm"], along_nm)
        payload: dict[str, Any] = {
            "hex": hexid, "runway": runway, "airport": airport, "callsign": callsign, "type_code": type_code,
            "along_nm": along_nm, "hat_ft": hat_ft, "gp_dev_ft": -20,
            "gp_dots": -0.2, "loc_dev_ft": 15, "loc_dots": 0.1, "gs": 140.0, "gs_corr": 150.0, "sink_fpm": -750,
            **self._path.get(approach_id, NO_PATH)}
        if origin is not None:
            payload["origin"] = origin
        return self.event("approach.updated", approach_id, payload, dt=dt)

    def close_attempt(self, approach_id: int, hexid: str, outcome: Outcome, *, dt: float, runway: str = "28L",
                      airport: str = "KSFO", flags: Flags = Flags.NONE,
                      references: Sequence[str | None] = ()) -> int:
        """`references` stores one point per entry, a second apart and ending at the close, each measured against that
        procedure path (None: the centreline). Without it no point is stored, as for an attempt closed before any
        point inside 10 nm. approaches.path stays what the attempt opened with."""
        points = [{"t": self.t0 + dt - (len(references) - i), "along_nm": 4.0 - 0.1 * i, "reference": reference}
                  for i, reference in enumerate(references)]
        self.store.close_approach(approach_id, outcome=outcome, flags=flags, ended_at=self.t0 + dt, summary={}, gates=[],
                                  points=points, bins=[], ga=None)
        attempt = self._attempt.get(approach_id)
        if attempt is None:                                # opened by the replay, not by this writer: its row says which
            arrival_id, attempt_no = self.store.conn.execute(
                "SELECT arrival_id, attempt_no FROM approaches WHERE id = ?", (approach_id,)).fetchone()
            attempt = {"attempt_no": attempt_no, "arrival_id": arrival_id, "lowest_hat_ft": None, "closest_along_nm": None}
        return self.event("approach.completed", approach_id, {
            "hex": hexid, "runway": runway, "airport": airport, "outcome": outcome.value, "flags": flag_names(flags),
            **attempt, "gate1000": None,       # no gates are written, so the collector would report none
            **self._path.get(approach_id, NO_PATH)}, dt=dt)

    def event(self, kind: str, approach_id: int | None, payload: dict[str, Any], *, dt: float) -> int:
        return self.store.insert_event(self.t0 + dt, kind, approach_id, None, payload)

    def set_flags(self, approach_id: int, flags: Flags) -> None:
        """Sets an open attempt's stored flags directly: no LiveWriter method opens or closes an attempt with
        EMERGENCY_MASK bits already set, because in real life only the collector writes to an open row's flags, while
        its attempt is still in progress (the matcher's stamping path 2). The collector itself is not in this clone, so
        tests build that state directly, the same plain UPDATE it would run."""
        self.store.conn.execute("UPDATE approaches SET flags = ? WHERE id = ?", (int(flags), approach_id))

    def end_run(self, error: str | None = None) -> None:
        self.store.end_run(self.run_id, error=error)

    def close(self) -> None:
        self.store.close()

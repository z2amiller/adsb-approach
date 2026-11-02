"""Glue: positions → tracker → matcher → store + events; the replay runner."""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from . import __version__
from .config import Config, config_hash
from .events import EventBus
from .matcher import Matcher
from .model import Position
from .poller import WatchList, replay_merge
from .scorer import scorer_for
from .sources.files import iter_positions
from .store.db import Store, external_positions
from .tracker import Flight, Tracker
from .typedb import TypeDb
from .weather import WeatherTable, parse_awc_json

log = logging.getLogger(__name__)
FLIGHT_TOUCH_INTERVAL_S = 30.0     # how stale an active flight's stored last_seen may be while positions keep arriving


@dataclass
class PipelineStats:
    positions: int = 0
    opened: int = 0
    closed: int = 0
    by_outcome: Counter = field(default_factory=Counter)
    by_runway: Counter = field(default_factory=Counter)
    go_arounds: list[dict] = field(default_factory=list)
    runway_changes: list[dict] = field(default_factory=list)
    external_kept: int = 0        # adsb.lol positions that joined the stream
    external_dropped: int = 0     # adsb.lol positions not newer than their aircraft's latest, so not processed
    external_local_dropped: int = 0     # local positions older than an adsb.lol one already taken for the aircraft


class Pipeline:
    def __init__(self, cfg: Config, store: Store, *, run_kind: str, source_desc: str, typedb: TypeDb | None,
                 weather: WeatherTable, note: str = "", external_merge: bool = False) -> None:
        self.cfg = cfg
        self.store = store
        self.run_id = store.new_run(run_kind, source_desc, note=note, code_version=__version__,
                                    config_hash=config_hash(cfg), external_merge=external_merge)
        self.events = EventBus()
        self.events.subscribe(self._on_event)
        self.tracker = Tracker(typedb, cfg.type_classes, on_new_flight=self._new_flight, on_flight_update=self._touch_flight)
        self.scorer = scorer_for(cfg)
        self.matcher = Matcher(cfg, self.scorer, weather, store, self.events, self.run_id)
        self.stats = PipelineStats()
        self._last_expire = 0.0
        self._last_touch: dict[int, float] = {}    # flight id -> pos.t it was last persisted at (FLIGHT_TOUCH_INTERVAL_S)
        self._external_newest: dict[str, float] = {}    # hex -> pos.t of the newest adsb.lol position taken for it

    def _admit(self, pos: Position) -> bool:
        """Whether a position joins the stream. Each aircraft's positions must stay in time order whichever source is
        ahead, so an adsb.lol position is taken only when newer than the aircraft's latest here, and a local one only
        when newer than the newest adsb.lol position taken for it."""
        if pos.origin == "local":
            newest = self._external_newest.get(pos.hex)
            if newest is not None and pos.t <= newest:
                self.stats.external_local_dropped += 1
                return False
            return True
        track = self.tracker.get(pos.hex)
        if track is not None and pos.t <= track.last.t:
            self.stats.external_dropped += 1
            return False
        self.stats.external_kept += 1
        self._external_newest[pos.hex] = pos.t
        return True

    def process(self, pos: Position) -> None:
        if not self._admit(pos):
            return
        track = self.tracker.update(pos)
        self.matcher.process(pos, track)
        self.stats.positions += 1
        fid = track.flight.id
        if fid is not None and pos.t - self._last_touch.get(fid, 0.0) >= FLIGHT_TOUCH_INTERVAL_S:
            self._touch_flight(track.flight)
            self._last_touch[fid] = pos.t
        if pos.t - self._last_expire > 600.0:
            self.tracker.expire(pos.t)
            self._last_expire = pos.t

    def feed(self, positions: Iterable[Position]) -> PipelineStats:
        for pos in positions:
            self.process(pos)
        return self.stats

    def finish(self, error: str | None = None) -> PipelineStats:
        """End of stream: close open attempts, flush flights, end the run. `error` records why a stream was cut short."""
        self.matcher.finish()
        self.tracker.close_all()
        self.store.end_run(self.run_id, error=error)
        return self.stats

    def _on_event(self, kind: str, t: float, approach_id: int | None, flight_id: int | None, payload: dict) -> None:
        self.store.insert_event(t, kind, approach_id, flight_id, payload)
        if kind == "approach.started":
            self.stats.opened += 1
        elif kind == "approach.completed":
            self.stats.closed += 1
            self.stats.by_outcome[payload["outcome"]] += 1
            self.stats.by_runway[f"{payload['airport']}/{payload['runway']}"] += 1
            if log.isEnabledFor(logging.INFO):
                log.info("%s", self._log_line(approach_id, payload, payload["outcome"]))
        elif kind == "approach.go_around":
            self.stats.go_arounds.append({"approach_id": approach_id, **payload})
        elif kind == "approach.runway_change":
            self.stats.runway_changes.append({"approach_id": approach_id, **payload})
        elif kind == "approach.reclassified":
            prev = payload["from"]
            self.stats.by_outcome[prev] -= 1
            if self.stats.by_outcome[prev] <= 0:
                del self.stats.by_outcome[prev]
            self.stats.by_outcome[payload["outcome"]] += 1
            if payload["outcome"] in ("go_around", "touch_and_go"):
                self.stats.go_arounds.append({"approach_id": approach_id, **payload})
            if log.isEnabledFor(logging.INFO):
                log.info("%s", self._log_line(approach_id, payload, f"reclassified {prev} -> {payload['outcome']}"))

    def _log_line(self, approach_id: int | None, payload: dict, what: str) -> str:
        """One line per completed or reclassified approach, for the service log."""
        track = self.tracker.get(payload["hex"])
        callsign = (track.flight.callsign if track is not None else None) or "-"
        lowest, closest = payload.get("lowest_hat_ft"), payload.get("closest_along_nm")
        where = "" if lowest is None or closest is None else f", lowest {lowest:.0f} ft at {closest:.2f} nm"
        flags = ",".join(payload.get("flags") or []) or "none"
        return f"approach #{approach_id} {payload['hex']} {callsign} {payload['airport']}/{payload['runway']} {what}{where}, flags {flags}"

    def _new_flight(self, f: Flight) -> int:
        return self.store.insert_flight(f.hex, f.callsign, f.reg, f.type_code, f.traffic_class, f.first_seen)

    def _touch_flight(self, f: Flight) -> None:
        if f.id is not None:
            self.store.touch_flight(f.id, f.last_seen, callsign=f.callsign, traffic_class=f.traffic_class)


def load_weather(metar_path: str | Path | None, store: Store | None = None) -> WeatherTable:
    table = WeatherTable()
    if metar_path:
        obs = parse_awc_json(json.loads(Path(metar_path).read_text()))
        table.extend(obs)
        if store is not None:
            for o in obs:
                store.insert_weather(o)
    return table


def replay(cfg: Config, *, kind: str, path: str | Path, db_path: str | Path, run_tag: str,
           metar_path: str | Path | None = None, typedb_path: str | Path | None = None,
           external_from: str | Path | None = None) -> tuple[int, PipelineStats]:
    """Replay recorded positions into a new run. A missing source, type database or `external_from` raises before the
    output database is opened. A source that fails part-way still ends its run: open attempts close as at the end of a
    stream, the run's note records the error, and the error propagates.

    `external_from` is a collector's database. Its stored adsb.lol positions are merged into the stream through a watch
    list built and fed as the live one was, so the run lands what the live run landed; it is opened read-only, and the
    run is marked `external_merge`."""
    positions = iter_positions(kind, path)
    if typedb_path is not None and not Path(typedb_path).exists():
        raise FileNotFoundError(f"type database {typedb_path} does not exist")
    if external_from is not None and not Path(external_from).is_file():
        raise FileNotFoundError(f"external database {external_from} is not a file")
    store = Store(db_path)
    external_conn: sqlite3.Connection | None = None
    try:
        store.migrate()
        typedb: TypeDb | None = None
        candidate = typedb_path or cfg.service.typedb_path
        if candidate and Path(candidate).exists():
            typedb = TypeDb.load(candidate)
        weather = load_weather(metar_path, store)
        source_desc = f"{kind}:{path}"
        if external_from is not None:
            source_desc += f"+external:{external_from}"
        pipe = Pipeline(cfg, store, run_kind="replay", source_desc=source_desc, typedb=typedb, weather=weather,
                        note=run_tag, external_merge=external_from is not None)
        try:
            if external_from is not None:
                external_conn = sqlite3.connect(f"file:{quote(str(external_from))}?mode=ro", uri=True)
                watch = WatchList(cfg.external.watch_max_min * 60.0)
                pipe.events.subscribe(watch.on_event)
                positions = replay_merge(positions, external_positions(external_conn), watch)
            pipe.feed(positions)
        except BaseException as exc:
            pipe.finish(error=f"{type(exc).__name__}: {exc}")
            raise
        return pipe.run_id, pipe.finish()
    finally:
        if external_conn is not None:
            external_conn.close()
        store.close()


def format_stats(stats: PipelineStats) -> str:
    lines = [f"positions {stats.positions}, attempts opened {stats.opened}, closed {stats.closed}", "", "by runway:"]
    for key, n in sorted(stats.by_runway.items()):
        lines.append(f"  {key:10} {n:5}")
    lines += ["", "by outcome:"]
    for key, n in sorted(stats.by_outcome.items()):
        lines.append(f"  {key:14} {n:5}")
    if stats.go_arounds:
        lines += ["", "go-arounds / touch-and-goes:"]
        for g in stats.go_arounds:
            where = ("inferred" if g.get("initiated_hat_ft") is None
                     else f"min {g.get('initiated_hat_ft')} ft at {g.get('initiated_along_nm')} nm")
            lines.append(f"  #{g['approach_id']} {g.get('hex')} {g.get('airport')}/{g.get('runway')} {g.get('outcome')} {where}")
    if stats.runway_changes:
        lines += ["", "runway changes:"]
        for r in stats.runway_changes:
            lines.append(f"  #{r['approach_id']} {r.get('hex')} {r.get('from_runway')} -> {r.get('to_runway')}")
    return "\n".join(lines)

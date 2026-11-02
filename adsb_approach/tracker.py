"""Per-airframe rolling history and flight sessions."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import TypeClasses
from .emergency import EmergencyWatch
from .model import Position
from .typedb import TypeDb, classify


@dataclass(slots=True)
class Flight:
    id: int | None
    hex: str
    callsign: str | None
    reg: str | None
    type_code: str | None
    traffic_class: str
    approach_cat: str | None
    first_seen: float
    last_seen: float
    # A fresh watch per flight: a new session after the session gap starts over, which is what "for the rest of the
    # flight's life" means for a latched emergency flag.
    emergency: EmergencyWatch = field(default_factory=EmergencyWatch)


@dataclass(slots=True)
class Track:
    hex: str
    flight: Flight
    history: deque[Position]
    last: Position


class Tracker:
    """Per-hex flight sessions and rolling position history.

    `on_new_flight` runs when a session opens and returns the flight's stored id. `on_flight_update` runs
    only when a callsign fills in (which also reclassifies the flight), when a callsign change or a gap
    over `session_gap_s` replaces the session, when `expire` drops the track, and for every live flight
    in `close_all`. `last_seen` advances in memory on every position but reaches the callback only at
    those points, so a stored `last_seen` lags a session that is still open.
    """

    def __init__(self, typedb: TypeDb | None, classes: TypeClasses, *, session_gap_s: float = 7200.0,
                 history_s: float = 120.0, on_new_flight: Callable[[Flight], int | None] | None = None,
                 on_flight_update: Callable[[Flight], None] | None = None) -> None:
        self.typedb = typedb
        self.classes = classes
        self.session_gap_s = session_gap_s
        self.history_s = history_s
        self.on_new_flight = on_new_flight
        self.on_flight_update = on_flight_update
        self._tracks: dict[str, Track] = {}

    def _resolve(self, pos: Position) -> tuple[str | None, str | None]:
        info = self.typedb.get(pos.hex) if self.typedb else None
        reg = (info.reg if info else None) or pos.reg
        type_code = (info.type_code if info else None) or pos.type_code
        return reg, type_code

    def _new_flight(self, pos: Position) -> Flight:
        reg, type_code = self._resolve(pos)
        f = Flight(id=None, hex=pos.hex, callsign=pos.callsign, reg=reg, type_code=type_code,
                   traffic_class=classify(pos.callsign, type_code, pos.category, self.classes),
                   approach_cat=self.classes.approach_category(type_code), first_seen=pos.t, last_seen=pos.t)
        if self.on_new_flight is not None:
            f.id = self.on_new_flight(f)
        return f

    def update(self, pos: Position) -> Track:
        track = self._tracks.get(pos.hex)
        new_session = (
            track is None
            or pos.t - track.last.t > self.session_gap_s
            or (pos.callsign is not None and track.flight.callsign is not None and pos.callsign != track.flight.callsign)
        )
        if new_session:
            if track is not None and self.on_flight_update is not None:
                self.on_flight_update(track.flight)
            history = track.history if track is not None else deque()     # per airframe: survives a new session
            track = Track(hex=pos.hex, flight=self._new_flight(pos), history=history, last=pos)
            self._tracks[pos.hex] = track
        else:
            assert track is not None
            f = track.flight
            if pos.callsign is not None and f.callsign is None:
                f.callsign = pos.callsign
                f.traffic_class = classify(f.callsign, f.type_code, pos.category, self.classes)
                if self.on_flight_update is not None:
                    self.on_flight_update(f)
        track.flight.emergency.observe(pos.squawk, pos.emergency, pos.t)
        track.history.append(pos)
        while track.history and track.history[0].t < pos.t - self.history_s:
            track.history.popleft()
        track.last = pos
        track.flight.last_seen = max(track.flight.last_seen, pos.t)
        return track

    def get(self, hexid: str) -> Track | None:
        return self._tracks.get(hexid)

    def tracks(self) -> list[Track]:
        return list(self._tracks.values())

    def expire(self, now: float, max_age_s: float | None = None) -> list[str]:
        """Drop tracks idle for more than max_age_s, by default session_gap_s: an airframe back inside the gap
        must find its track, or its flight splits in two. A track idle for more than history_s keeps its flight and
        last position but releases its history, which nothing reads once it is that old."""
        max_age = self.session_gap_s if max_age_s is None else max_age_s
        gone = []
        for h, tr in self._tracks.items():
            idle = now - tr.last.t
            if idle > max_age:
                gone.append(h)
            elif idle > self.history_s:
                tr.history.clear()
        for h in gone:
            if self.on_flight_update is not None:
                self.on_flight_update(self._tracks[h].flight)
            del self._tracks[h]
        return gone

    def close_all(self) -> None:
        """End of stream: hand every live flight to on_flight_update (final last_seen), then forget the tracks."""
        if self.on_flight_update is not None:
            for tr in self._tracks.values():
                self.on_flight_update(tr.flight)
        self._tracks.clear()

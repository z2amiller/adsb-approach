"""One approach attempt in progress: an aircraft matched against one runway end."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from .model import Airport, Flags, Position, RunwayEnd
from .reference import Reference
from .scorer import ScoredPoint


class State(Enum):
    CANDIDATE = "candidate"
    ESTABLISHED = "established"


@dataclass
class Attempt:
    hex: str
    runway: RunwayEnd
    airport: Airport
    state: State
    points: list[ScoredPoint]
    raw: list[Position]
    last_t: float
    flight_key: int                  # arrival chaining: the stored flight id, or id() of a flight never stored
    flight_id: int | None = None     # the stored flight, carried by every event about this attempt
    established_at: float | None = None
    established_hat_ft: float | None = None
    approach_id: int | None = None
    arrival_id: int | None = None
    attempt_no: int = 1
    prev_approach_id: int | None = None
    sidestep: bool = False
    weather_id: int | None = None
    min_hat_ft: float = math.inf
    min_alt_corr: float = math.inf
    min_alt_baro: int | None = None
    min_alt_t: float | None = None
    min_alt_point: ScoredPoint | None = None
    touched: bool = False            # ground contact or below 100 ft inside the footprint: a landing unless it climbs away
    touched_low: bool = False        # ground contact or below touch_and_go_max_height_ft inside the footprint
    armed: bool = False              # go-around detection armed (descended below the arming height)
    climb_start_t: float | None = None
    descent_mcp: int | None = None
    approach_mode_seen: bool = False
    off_course_since: float | None = None   # first position of the current run of off-course positions
    change_near_since: dict[str, float] = field(default_factory=dict)     # parallel member -> first position of the
    change_aligned_since: dict[str, float] = field(default_factory=dict)  # current run near it / near and aligned
    switch_near_since: dict[str, float] = field(default_factory=dict)     # another reference of this runway -> first position
    switch_aligned_since: dict[str, float] = field(default_factory=dict)  # of the current run near it / near and aligned
    temp_uncorrected: bool = False
    qnh_assumed: bool = False
    last_update_event_t: float = -math.inf
    type_code: str | None = None      # the flight's type and class at establishment, which key the fleet norm
    traffic_class: str | None = None  # this attempt is judged against
    emergency_flags: Flags = Flags.NONE   # LATCH_MASK bits already ORed onto this attempt's row (open or while open),
                                           # so the matcher can tell what is new
    reference: Reference | None = None   # what the attempt is measured against now: the runway's centreline unless given.
                                         # Never None once built; it changes when the attempt switches reference
    key: str = ""                        # the key of the reference the attempt was created on, which the matcher files it
                                         # under: it never changes, even when `reference` does

    def __post_init__(self) -> None:
        if self.reference is None:
            self.reference = Reference(self.runway)
        if not self.key:
            self.key = self.reference.key

    @property
    def last(self) -> ScoredPoint:
        return self.points[-1]

    def add(self, sp: ScoredPoint, pos: Position) -> None:
        self.raw.append(pos)
        self.add_scored(sp)

    def add_scored(self, sp: ScoredPoint) -> None:
        """Follow a scored point without its raw position: what the outcome rules read is all on the scored point."""
        self.points.append(sp)
        self.last_t = sp.t
        if sp.hat_ft is not None and sp.hat_ft < self.min_hat_ft:
            self.min_hat_ft = sp.hat_ft
        if sp.alt_corr is not None and sp.alt_corr < self.min_alt_corr:
            self.min_alt_corr = sp.alt_corr
            self.min_alt_baro = sp.alt_baro
            self.min_alt_t = sp.t
            self.min_alt_point = sp
        if sp.alt_qnh is not None:                 # an airborne point whose altitude was corrected
            self.temp_uncorrected = self.temp_uncorrected or not sp.temp_corrected
            self.qnh_assumed = self.qnh_assumed or sp.qnh_assumed
        if "approach" in sp.nav_modes:
            self.approach_mode_seen = True

"""Domain records shared by every module. Units: feet, degrees, UNIX seconds UTC."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntFlag, StrEnum


class Outcome(StrEnum):
    LANDED = "landed"
    LOST_ON_FINAL = "lost_on_final"
    LOST = "lost"
    GO_AROUND = "go_around"
    TOUCH_AND_GO = "touch_and_go"
    RUNWAY_CHANGE = "runway_change"
    ABANDONED = "abandoned"


class Flags(IntFlag):
    NONE = 0
    GATE1000_UNOBSERVED = 1 << 0
    GATE500_UNOBSERVED = 1 << 1
    HIGH = 1 << 2
    LOW = 1 << 3
    OFF_COURSE = 1 << 4
    FAST = 1 << 5
    SLOW = 1 << 6
    HIGH_SINK = 1 << 7
    UNSTABLE_1000 = 1 << 8
    UNSTABLE_500 = 1 << 9
    STEEP = 1 << 10
    SHALLOW = 1 << 11
    LATE_ESTABLISHED = 1 << 12
    SIDESTEP = 1 << 13
    PRESUMED_LANDED = 1 << 14
    LANDING_CONFIRMED = 1 << 15
    INFERRED_GO_AROUND = 1 << 16
    MCP_JUMP = 1 << 17
    APPROACH_MODE_DROPPED = 1 << 18
    REFERENCE_ASSUMED = 1 << 19
    TEMP_UNCORRECTED = 1 << 20
    QNH_ASSUMED = 1 << 21              # standard pressure: no METAR altimeter setting within qnh_max_age_min
    INTERRUPTED = 1 << 22              # left open when the live service stopped without closing it (startup sweep)
    EXTERNAL_TAIL = 1 << 23            # positions after the receiver lost the aircraft were spliced in from adsb.lol
    EXTERNAL_OUTCOME = 1 << 24         # the tail, not the receiver, decided the approach's outcome
    EXTERNAL_GAPS = 1 << 25            # positions inside gaps in the receiver's own track were filled in from adsb.lol
    # The aircraft declared an emergency somewhere on its flight: a squawk or ADS-B emergency state held for 30 s.
    # Latched per flight and stamped on every attempt it closes.
    EMERGENCY_GENERAL = 1 << 26        # squawk 7700, or ADS-B emergency state "general"
    EMERGENCY_NORDO = 1 << 27          # squawk 7600, or "nordo": radio failure
    EMERGENCY_UNLAWFUL = 1 << 28       # squawk 7500, or "unlawful": unlawful interference
    EMERGENCY_DOWNED = 1 << 29         # ADS-B "downed"; no squawk equivalent
    MINIMUM_FUEL = 1 << 30             # ADS-B "minfuel": attention, not an emergency


# EXTERNAL_TAIL, EXTERNAL_OUTCOME and EXTERNAL_GAPS say where points came from and who decided the outcome, not how the
# approach was flown, so they leave a landing normal.
NORMAL_LANDING_MASK = (
    Flags.GATE500_UNOBSERVED | Flags.PRESUMED_LANDED | Flags.LANDING_CONFIRMED | Flags.TEMP_UNCORRECTED | Flags.QNH_ASSUMED
    | Flags.EXTERNAL_TAIL | Flags.EXTERNAL_OUTCOME | Flags.EXTERNAL_GAPS
)


# Drawn red on the page, and exempt from every rule that hides traffic: the circuit rule and the GA and Helicopters
# filters. An emergency landing is not a normal one, so none of these is in NORMAL_LANDING_MASK.
EMERGENCY_MASK = Flags.EMERGENCY_GENERAL | Flags.EMERGENCY_NORDO | Flags.EMERGENCY_UNLAWFUL | Flags.EMERGENCY_DOWNED
# Latched per flight, and sticky in the store: no write of approaches.flags drops one already stored, because
# reclassify and set_confirmation overwrite with a copy captured at close, and a 30 s run can straddle the close.
# So no code path clears them; a false positive is removed by hand in SQL.
LATCH_MASK = EMERGENCY_MASK | Flags.MINIMUM_FUEL


def flag_names(flags: Flags | int) -> list[str]:
    flags = Flags(flags)
    return [f.name for f in Flags if f in flags]  # type: ignore[misc]


def is_normal_landing(outcome: Outcome, flags: Flags) -> bool:
    if outcome not in (Outcome.LANDED, Outcome.LOST_ON_FINAL):
        return False
    return (flags & ~NORMAL_LANDING_MASK) == Flags.NONE


@dataclass(slots=True)
class Position:
    """One received position, normalised from any source."""

    t: float
    hex: str
    lat: float
    lon: float
    alt_baro: int | None          # pressure altitude ft; None when unknown or on ground
    on_ground: bool
    alt_geom: int | None          # WGS84 ellipsoid height ft
    gs: float | None
    track: float | None
    baro_rate: int | None
    geom_rate: int | None
    callsign: str | None
    squawk: str | None
    category: str | None
    nav_qnh: float | None
    nav_altitude_mcp: int | None
    nav_modes: frozenset[str]
    source: str
    reg: str | None = None
    type_code: str | None = None
    emergency: str | None = None  # readsb's ADS-B emergency state: "general", "nordo", "lifeguard", ...
    origin: str = "local"         # the local receiver, or "adsb.lol" for a position polled from re-api

    @property
    def vertical_rate(self) -> int | None:
        return self.baro_rate if self.baro_rate is not None else self.geom_rate


@dataclass(slots=True)
class Weather:
    airport: str
    observed_at: float
    altim_hpa: float | None
    temp_c: float | None
    dewp_c: float | None
    wind_dir: int | None          # degrees true the wind blows from; None for VRB or missing; calm is 0 with wind_kt 0
    wind_kt: int | None
    gust_kt: int | None
    raw: str


@dataclass(frozen=True, slots=True)
class ProcedurePath:
    """A flown path to a runway end that is not its centreline: straight legs through `points`, far end first. The last
    point is the join on the extended centreline; the threshold is the implied final vertex."""

    name: str                     # stable id, [a-z0-9-]+: stored on approaches, used in URLs
    label: str                    # shown on the page
    source: str                   # how the points were found
    points: tuple[tuple[float, float], ...]   # (lat, lon), far end first; the last is the join on the centreline


@dataclass(slots=True)
class RunwayEnd:
    airport: str
    ident: str                    # "28R"
    enabled: bool
    threshold_lat: float          # landing threshold
    threshold_lon: float
    threshold_elev_ft: float
    threshold_hae_m: float | None
    course_true_deg: float
    runway_length_ft: float
    displaced_threshold_ft: float
    localizer_lat: float | None
    localizer_lon: float | None
    localizer_offset_ft: float    # fallback: antenna this far beyond the far end
    loc_width_deg: float | None   # full course width
    glide_path_deg: float
    tch_ft: float
    ils: str | None
    reference_source: str         # "cifp:CIFP_260903" | "ourairports+assumed"
    parallel_group: tuple[str, ...] = ()
    paths: tuple[ProcedurePath, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.airport}/{self.ident}"

    @property
    def reference_assumed(self) -> bool:
        return "assumed" in self.reference_source

    @property
    def landing_length_ft(self) -> float:
        """Pavement from the landing threshold to the far end: the runway length less this end's displaced threshold."""
        return self.runway_length_ft - self.displaced_threshold_ft


@dataclass(slots=True)
class Airport:
    icao: str
    name: str
    enabled: bool
    lat: float
    lon: float
    elevation_ft: float
    metar: str
    geoid_offset_ft: float        # ellipsoid minus MSL, negative here
    declination_deg: float        # east positive
    runways: dict[str, RunwayEnd] = field(default_factory=dict)

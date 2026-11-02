"""The emergency-squawk watch: two independent 30 s runs, squawk and ADS-B emergency state, each latching its flag
onto the flight for good once it has held that long."""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Flags

LATCH_S = 30.0     # a run held this long latches; readsb can hold a dial-through's old squawk 5-12 s, so 30 s clears
                   # it with room to spare while still catching a real emergency promptly

SQUAWK_FLAGS: dict[str, Flags] = {
    "7700": Flags.EMERGENCY_GENERAL, "7600": Flags.EMERGENCY_NORDO, "7500": Flags.EMERGENCY_UNLAWFUL,
}
STATE_FLAGS: dict[str, Flags] = {
    "general": Flags.EMERGENCY_GENERAL, "nordo": Flags.EMERGENCY_NORDO, "unlawful": Flags.EMERGENCY_UNLAWFUL,
    "downed": Flags.EMERGENCY_DOWNED, "minfuel": Flags.MINIMUM_FUEL,
}   # lifeguard, reserved and none carry no flag: an air ambulance broadcasts lifeguard on every flight, so it marks
    # a kind of flight, not an event, and it ends a run the same as any other unmapped value


@dataclass(slots=True)
class _Run:
    """One source's run in progress: the flag it is timing, and when that run started. Both None between runs."""
    flag: Flags | None = None
    start: float | None = None


@dataclass(slots=True)
class EmergencyWatch:
    """Held on Flight and fed one report at a time from Tracker.update. Each source -- squawk and ADS-B emergency
    state -- is judged only by reports that carry it, with its own run, so a transponder squawking 7700 while its
    ADS-B state still says "none" is not cancelled out by the state. `latched` only ever grows: once a flag has run 30 s
    it stays set for the rest of the flight's life, and a fresh Flight after a session gap gets a fresh watch, which is
    what "for the rest of the flight's life" means."""
    latched: Flags = Flags.NONE
    # When each latched flag's run started, keyed by the single flag bit. matcher._stamp_closed_emergency reads this to
    # tell a run that straddled a close from one that only started after it: the flight's watch itself cannot tell the
    # two apart from `latched` alone, since a departure less than session_gap_s later is still the same Flight.
    latched_at: dict[Flags, float] = field(default_factory=dict)
    _squawk: _Run = field(default_factory=_Run)
    _state: _Run = field(default_factory=_Run)

    def observe(self, squawk: str | None, emergency: str | None, t: float) -> Flags:
        """One report's squawk and ADS-B emergency value. Returns whatever this call newly latched (Flags.NONE most
        of the time); `self.latched` holds everything latched so far, this call included."""
        new = self._advance(self._squawk, SQUAWK_FLAGS.get(squawk) if squawk is not None else None, squawk is not None, t)
        new |= self._advance(self._state, STATE_FLAGS.get(emergency) if emergency is not None else None,
                             emergency is not None, t)
        return new

    def _advance(self, run: _Run, flag: Flags | None, present: bool, t: float) -> Flags:
        """Continue, start or end one source's run, and latch it once it has lasted LATCH_S. `present` is False when
        this report carries nothing for this source at all (no squawk field, or no emergency field): that must not
        touch the run either way. A report whose t is earlier than the run's own start can be a position that arrived
        out of order; it is ignored outright rather than latching or ending a run it may know nothing about."""
        if not present or (run.start is not None and t < run.start):
            return Flags.NONE
        if flag != run.flag:
            run.flag, run.start = flag, t if flag is not None else None
        if flag is None or run.start is None or t - run.start < LATCH_S:
            return Flags.NONE
        new = flag & ~self.latched
        if new:
            self.latched_at[new] = run.start
        self.latched |= flag
        return new

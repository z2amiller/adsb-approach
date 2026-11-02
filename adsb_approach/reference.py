"""References: a runway's centreline, or one of its procedure paths, and where a position lies against one. A position's
frame against a reference is its distance still to fly to the threshold, its signed distance right of the reference, and
the reference's local course."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import NoReturn

from .geometry import NM_FT, enu_ft, runway_frame
from .model import Airport, ProcedurePath, RunwayEnd

PATH_NAME_RE = re.compile(r"[a-z0-9-]+")
JOIN_MAX_CROSS_FT = 100.0          # a path's last point lies within this of the extended centreline
JOIN_MAX_ALONG_NM = 5.0            # and this far out, at most
MIN_LEG_FT = 1.0                   # a leg shorter than this is a repeated point
_TIE_FT = 1e-6                     # gaps this close are one gap: a corner's two legs tie, however the rounding falls


@dataclass(frozen=True, slots=True)
class Frame:
    """A position against a reference: along_ft is the distance still to fly to the threshold (negative past it),
    cross_ft the signed distance to the right of the direction of flight, course_deg the reference's course there."""

    along_ft: float
    cross_ft: float
    course_deg: float


@dataclass(frozen=True, slots=True)
class Leg:
    """One straight leg of a path in the threshold's east/north frame (feet)."""

    a: tuple[float, float]         # where the leg starts
    t: tuple[float, float]         # unit vector in the direction of flight
    length: float
    course: float                  # degrees true
    rem: float                     # distance still to fly from the leg's end to the threshold


def _legs(rwy: RunwayEnd, path: ProcedurePath) -> tuple[Leg, ...]:
    """The legs of `path`, far end first, ending with the join -> threshold leg."""
    verts = [enu_ft(rwy.threshold_lat, rwy.threshold_lon, lat, lon) for lat, lon in path.points] + [(0.0, 0.0)]
    spans = []
    for (ae, an), (be, bn) in zip(verts, verts[1:]):
        de, dn = be - ae, bn - an
        length = math.hypot(de, dn)
        if length < MIN_LEG_FT:
            raise ValueError(f"path {path.name!r} on {rwy.key}: zero-length leg at ({ae:.0f}, {an:.0f}) ft from the "
                             f"threshold: a point repeats, or the join is the threshold")
        spans.append(((ae, an), (de / length, dn / length), length, math.degrees(math.atan2(de, dn)) % 360.0))
    legs: list[Leg] = []
    rem = 0.0
    for a, t, length, course in reversed(spans):
        legs.append(Leg(a=a, t=t, length=length, course=course, rem=rem))
        rem += length
    return tuple(reversed(legs))


def validate_path(rwy: RunwayEnd, path: ProcedurePath) -> None:
    """Raise ValueError naming the rule `path` breaks on `rwy`. The rules run when an airport file loads."""
    where = f"path {path.name!r} on {rwy.key}"
    if not PATH_NAME_RE.fullmatch(path.name):
        raise ValueError(f"{where}: name must match [a-z0-9-]+")
    if not path.label.strip():
        raise ValueError(f"{where}: label must say what to show on the page")
    if not path.source.strip():
        raise ValueError(f"{where}: source must say how the points were found")
    if len(path.points) < 2:
        raise ValueError(f"{where}: a path needs at least 2 points, far end first and the join last")
    _legs(rwy, path)                                          # raises for a zero-length leg
    along, cross = runway_frame(rwy, *path.points[-1])
    if abs(cross) > JOIN_MAX_CROSS_FT:
        raise ValueError(f"{where}: join must lie on the centreline: the last point is {abs(cross):.0f} ft off it, "
                         f"limit {JOIN_MAX_CROSS_FT:.0f} ft")
    if not 0.0 <= along <= JOIN_MAX_ALONG_NM * NM_FT:
        raise ValueError(f"{where}: join must lie within {JOIN_MAX_ALONG_NM:g} nm of the threshold, on the approach "
                         f"side: the last point is {along / NM_FT:.2f} nm out")


class Reference:
    """A runway's centreline (`path` None) or one of its procedure paths. Immutable after construction; two
    references with the same key are equal."""

    __slots__ = ("_runway", "_path", "_legs", "_key")

    def __init__(self, runway: RunwayEnd, path: ProcedurePath | None = None) -> None:
        legs = () if path is None else _legs(runway, path)
        key = runway.key if path is None else f"{runway.key}@{path.name}"
        for name, value in (("_runway", runway), ("_path", path), ("_legs", legs), ("_key", key)):
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError(f"{type(self).__name__} is immutable")

    @property
    def runway(self) -> RunwayEnd:
        return self._runway

    @property
    def path(self) -> ProcedurePath | None:
        return self._path

    @property
    def legs(self) -> tuple[Leg, ...]:
        """The path's legs, far end first; empty for the centreline."""
        return self._legs

    @property
    def name(self) -> str | None:
        """The path's name, or None for the centreline."""
        return None if self._path is None else self._path.name

    @property
    def key(self) -> str:
        return self._key

    @property
    def label(self) -> str:
        if self._path is not None:
            return self._path.label
        return "ILS" if self._runway.ils else "straight-in"

    def frame(self, lat: float, lon: float) -> Frame:
        rwy = self._runway
        if self._path is None:
            along, cross = runway_frame(rwy, lat, lon)
            return Frame(along, cross, rwy.course_true_deg)
        pe, pn = enu_ft(rwy.threshold_lat, rwy.threshold_lon, lat, lon)
        last = len(self._legs) - 1
        best_gap = math.inf
        best = Frame(0.0, 0.0, 0.0)
        for i, leg in enumerate(self._legs):
            (ae, an), (te, tn) = leg.a, leg.t
            u = (pe - ae) * te + (pn - an) * tn
            d = (pe - ae) * tn - (pn - an) * te                   # right of the direction of flight is positive
            lo = -math.inf if i == 0 else 0.0                     # the first leg extends outwards
            hi = math.inf if i == last else leg.length            # the last (join -> threshold) extends past the threshold
            if lo <= u <= hi:
                gap, along_ft, cross_ft = abs(d), leg.rem + leg.length - u, d
            else:                                                 # outside a corner: measured to the vertex
                uc = min(max(u, lo), hi)
                gap = math.hypot(pe - (ae + uc * te), pn - (an + uc * tn))
                along_ft, cross_ft = leg.rem + leg.length - uc, math.copysign(gap, d)
            if gap < best_gap - _TIE_FT:                          # a tie keeps the earlier leg
                best_gap, best = gap, Frame(along_ft, cross_ft, leg.course)
        return best

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Reference) and other._key == self._key

    def __hash__(self) -> int:
        return hash(self._key)

    def __repr__(self) -> str:
        return f"Reference({self._key!r})"


def references(rwy: RunwayEnd) -> tuple[Reference, ...]:
    """The centreline first, then the paths in file order."""
    return (Reference(rwy), *(Reference(rwy, p) for p in rwy.paths))


def reference_for(rwy: RunwayEnd, name: str | None) -> Reference | None:
    """The reference `name` names on `rwy`: its centreline for None, else the path of that name; None when no
    configured path has the name."""
    if name is None:
        return Reference(rwy)
    path = next((p for p in rwy.paths if p.name == name), None)
    return None if path is None else Reference(rwy, path)


# Every reference's label, keyed by (icao, runway ident, path name), the name None for a runway's centreline.
Labels = Mapping[tuple[str, str, str | None], str]


def reference_labels(airports: Mapping[str, Airport]) -> dict[tuple[str, str, str | None], str]:
    """Every reference's label, keyed by (icao, runway ident, path name), the name None for the centreline."""
    return {(icao, ident, ref.name): ref.label
            for icao, airport in airports.items() for ident, rwy in airport.runways.items() for ref in references(rwy)}


def label_for(labels: Labels, icao: str, ident: str, name: str | None) -> str:
    """The label of reference `name` on a runway, from reference_labels' map. A path no longer configured has its
    stored name for its label, and a centreline the configuration does not know is "straight-in": the one fallback
    every place that shows a reference uses."""
    return labels.get((icao, ident, name), "straight-in" if name is None else name)

"""The arrivals log (GET /api/arrivals): one row per arrival, newest first. The filters apply to each closed
attempt, and an arrival is listed when any of its attempts passes them all."""
from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..model import EMERGENCY_MASK, Flags, LATCH_MASK, Outcome, flag_names
from ..reference import Labels, label_for
from .db import has_column, last_event_id

ATTENTION_OUTCOMES = (Outcome.GO_AROUND, Outcome.TOUCH_AND_GO, Outcome.ABANDONED)
# FAST and SLOW join the attention preset: a speed well off the fleet's is worth a look.
# LATCH_MASK (the latched emergency squawk flags) adds all five emergency/minimum-fuel flags: MINIMUM_FUEL is
# attention only, and the four EMERGENCY_* flags are attention too, on top of the bypass they get below.
ATTENTION_FLAGS = (Flags.FAST | Flags.SLOW | Flags.UNSTABLE_1000 | Flags.UNSTABLE_500 | Flags.STEEP | Flags.SHALLOW
                   | Flags.LATE_ESTABLISHED | LATCH_MASK)
# A CIRCUIT_TRAFFIC_CLASS arrival with more than one logged attempt is never attention, whatever any one
# attempt's outcome or flags say — see _is_circuit_traffic and the matching exclusion in ArrivalFilter.where(). This
# is deliberately NOT keyed on the touch_and_go outcome: measured on a live database of a few days' traffic across all
# 2,693 stored arrivals, touch_and_go closed only 5 of 2,793 attempts, all at KOAK (0.2% of traffic) — filtering on
# that outcome would hide almost nothing. The real signature is repeated attempts by one "other"-class aircraft: six
# of the nine arrivals with more than one attempt in that sample were light singles circuiting at KOAK, but three were
# genuine repeated KSFO airline/bizjet go-arounds that must stay in attention. Do not "fix" this back to touch_and_go.
# (A configurable GA-type-code toggle driven by a type-code list is a separate feature — not this.)
CIRCUIT_TRAFFIC_CLASS = "other"
# Every EMERGENCY_MASK bypass below is arrival-level, not per attempt row. list_arrivals only
# ever looks at closed attempts (a.outcome IS NOT NULL in where(), below), so a per-row (a.flags & ?) != 0 test could
# never see a flag that has so far landed only on an attempt still open — the matcher ORs newly latched flags onto the
# open row while an approach is in progress — and the arrival stayed hidden until that attempt closed, even though
# _arrival() already reports "emergency": true for it the moment the flag lands. The EXISTS looks across every
# attempt sharing this row's arrival_id, open or closed, for the one true source of "does this arrival hold an
# emergency at all."
EMERGENCY_BYPASS = "EXISTS (SELECT 1 FROM approaches ax WHERE ax.arrival_id = a.arrival_id AND (ax.flags & ?) != 0)"
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
FILTER_PARAMS = frozenset({"airport", "airport_not", "runway", "runway_not", "outcome", "outcome_not", "flags_any",
                           "flags_none", "since", "until", "preset", "arrival_id", "rotorcraft", "ga", "approach"})
APPROACH_KINDS = ("all", "straight", "path")        # any attempt, those that flew no procedure path, those that did
ATTEMPT_COLUMNS = ("id", "arrival_id", "attempt_no", "hex", "callsign", "type_code", "traffic_class", "airport", "runway",
                   "outcome", "flags", "landed_confirmed_by", "established_at", "ended_at", "lowest_hat_ft",
                   "closest_along_nm", "ga_initiated_hat_ft")
GATE1000_COLUMNS = ("observed", "along_nm", "gp_dev_ft", "gp_dots", "loc_dev_ft", "loc_dots", "gs", "gs_corr", "sink_fpm")


def _values(params: Mapping[str, str], key: str) -> list[str]:
    """A comma-separated parameter's values, without blanks or repeats."""
    return list(dict.fromkeys(v.strip() for v in params.get(key, "").split(",") if v.strip()))


def _outcomes(params: Mapping[str, str], key: str) -> tuple[str, ...]:
    known = {o.value for o in Outcome}
    values = tuple(v.lower() for v in _values(params, key))
    for v in values:
        if v not in known:
            raise ValueError(f"{key}: unknown outcome {v!r}")
    return values


def _flag_mask(params: Mapping[str, str], key: str) -> int:
    mask = 0
    for v in _values(params, key):
        if v.upper() not in Flags.__members__:
            raise ValueError(f"{key}: unknown flag {v!r}")
        mask |= int(Flags[v.upper()])
    return mask


def _runways(params: Mapping[str, str], key: str) -> tuple[str, ...]:
    values = tuple(v.upper() for v in _values(params, key))
    for v in values:
        airport, _, ident = v.partition("/")
        if not airport or not ident:
            raise ValueError(f"{key}: {v!r} is not AIRPORT/RUNWAY, for example KSFO/28L")
    return values


def _float(params: Mapping[str, str], key: str) -> float | None:
    text = params.get(key, "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{key}: {text!r} is not a number") from None


def _int(params: Mapping[str, str], key: str) -> int | None:
    text = params.get(key, "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"{key}: {text!r} is not a whole number") from None


@dataclass(frozen=True)
class ArrivalFilter:
    airport: tuple[str, ...] = ()
    airport_not: tuple[str, ...] = ()
    runway: tuple[str, ...] = ()               # AIRPORT/RUNWAY
    runway_not: tuple[str, ...] = ()
    outcome: tuple[str, ...] = ()
    outcome_not: tuple[str, ...] = ()
    flags_any: int = 0                         # an attempt with any of these flags
    flags_none: int = 0                        # an attempt with none of these flags
    since: float | None = None                 # an attempt that ended at or after this (UNIX seconds)
    until: float | None = None                 # ...and before this
    attention: bool = False                    # went around, touched and went, was abandoned or unstable; minus a
                                                # circuit (CIRCUIT_TRAFFIC_CLASS with >1 attempt)
    arrival_id: int | None = None
    rotorcraft: bool = False                   # include rotorcraft attempts, left out by default
    ga: bool = False                            # include GA attempts, left out by default
    ga_types: frozenset[str] = frozenset()      # the configured GA type codes; see parse()'s docstring
    approach: str = "all"                       # APPROACH_KINDS: all, or only the attempts that flew no path / a path

    @classmethod
    def parse(cls, params: Mapping[str, str], *, ga_types: frozenset[str] = frozenset()) -> ArrivalFilter:
        """From query parameters: comma-separated lists, runways as AIRPORT/RUNWAY, since and until in UNIX seconds,
        preset=attention, rotorcraft=1, ga=1, approach=all|straight|path. An unknown parameter, outcome, flag, preset or
        approach is a ValueError that names it.

        `ga_types` is not a query parameter: it is the type codes GA means, cfg.type_classes.ga. This
        method has no access to Config, so the call site (web/app.py) passes it explicitly -- an explicit argument
        here rather than, say, a Config reference, since parsing a request's filters should not need the whole
        configuration and this is the one piece of it the filter actually uses."""
        unknown = sorted(set(params) - FILTER_PARAMS)
        if unknown:
            raise ValueError(f"unknown parameter {unknown[0]!r}")
        preset = params.get("preset", "").strip()
        if preset not in ("", "attention"):
            raise ValueError(f"preset: unknown preset {preset!r}")
        approach = params.get("approach", "").strip()
        if (approach.lower() or "all") not in APPROACH_KINDS:
            raise ValueError(f"approach: unknown value {approach!r}")
        return cls(airport=tuple(v.upper() for v in _values(params, "airport")),
                   airport_not=tuple(v.upper() for v in _values(params, "airport_not")),
                   runway=_runways(params, "runway"), runway_not=_runways(params, "runway_not"),
                   outcome=_outcomes(params, "outcome"), outcome_not=_outcomes(params, "outcome_not"),
                   flags_any=_flag_mask(params, "flags_any"), flags_none=_flag_mask(params, "flags_none"),
                   since=_float(params, "since"), until=_float(params, "until"), attention=preset == "attention",
                   arrival_id=_int(params, "arrival_id"), rotorcraft=params.get("rotorcraft", "").strip() == "1",
                   ga=params.get("ga", "").strip() == "1", ga_types=ga_types, approach=approach.lower() or "all")

    def where(self, *, has_path: bool) -> tuple[str, list[Any]]:
        """The SQL condition on an attempt `a`, and its arguments. `has_path` says whether approaches has its `path`
        column (web.db.has_column), and has no default: a database the collector has not yet migrated to procedure
        paths (0008) has none, and a caller that assumed one would fail on it. No attempt in such a database flew a
        path, so there the straight-in filter matches every attempt and the path filter none."""
        conds: list[str] = ["a.outcome IS NOT NULL"]
        args: list[Any] = []

        def among(expr: str, values: tuple[str, ...], negate: bool = False) -> None:
            if values:
                conds.append(f"{expr} {'NOT IN' if negate else 'IN'} ({', '.join('?' * len(values))})")
                args.extend(values)

        runway = "a.airport || '/' || a.runway"
        among("a.airport", self.airport)
        among("a.airport", self.airport_not, negate=True)
        among(runway, self.runway)
        among(runway, self.runway_not, negate=True)
        among("a.outcome", self.outcome)
        among("a.outcome", self.outcome_not, negate=True)
        if self.flags_any:
            conds.append("(a.flags & ?) != 0")
            args.append(self.flags_any)
        if self.flags_none:
            conds.append("(a.flags & ?) = 0")
            args.append(self.flags_none)
        if self.since is not None:
            conds.append("a.ended_at >= ?")
            args.append(self.since)
        if self.until is not None:
            conds.append("a.ended_at < ?")
            args.append(self.until)
        if self.attention:
            # Exclude a circuit (CIRCUIT_TRAFFIC_CLASS with >1 logged attempt) from the attention preset,
            # mirroring _is_circuit_traffic so the row's own "attention" field and this filter agree. The COALESCE
            # keeps a NULL (never-classified) traffic_class from matching 'other' by SQL's three-valued NULL = x
            # rule — a missing class must not count as a circuit, same as the Python side.
            # An arrival holding an EMERGENCY_MASK bit on any attempt, the open one included, is
            # attention outright -- red implies attention -- so EMERGENCY_BYPASS leads and short-circuits both tests
            # after it. The outcome/flags test alone would miss a flag held only by the attempt still open, since the
            # rows here are closed attempts; and the circuit exclusion must not hide it either. Mirrors "attention"
            # in _arrival() below, so the row's field and this filter agree.
            conds.append(
                f"({EMERGENCY_BYPASS} OR ("
                f"(a.outcome IN ({', '.join('?' * len(ATTENTION_OUTCOMES))}) OR (a.flags & ?) != 0) AND NOT ("
                "COALESCE((SELECT a2.traffic_class FROM approaches a2 WHERE a2.arrival_id = a.arrival_id "
                "ORDER BY a2.attempt_no DESC, a2.id DESC LIMIT 1), '') = ? "
                "AND (SELECT COUNT(*) FROM approaches a3 WHERE a3.arrival_id = a.arrival_id) > 1)))"
            )
            args.extend([int(EMERGENCY_MASK), *(o.value for o in ATTENTION_OUTCOMES), int(ATTENTION_FLAGS),
                         CIRCUIT_TRAFFIC_CLASS])
        if self.arrival_id is not None:
            conds.append("a.arrival_id = ?")
            args.append(self.arrival_id)
        if self.approach == "straight" and has_path:
            conds.append("a.path IS NULL")
        elif self.approach == "path":
            conds.append("a.path IS NOT NULL" if has_path else "1 = 0")
        if not self.rotorcraft:
            # COALESCE, because SQL's NULL != 'rotorcraft' is NULL rather than true. An attempt that was
            # never classified is not a rotorcraft, and without this it fails the default filter and its arrival
            # disappears from every query — silently, and with no rotorcraft involved.
            # OR'd with the arrival-level EMERGENCY_BYPASS, so a rotorcraft (a medevac helicopter, say)
            # that has latched one anywhere on the arrival is never hidden by this exclusion.
            conds.append(f"(COALESCE(a.traffic_class, '') != 'rotorcraft' OR {EMERGENCY_BYPASS})")
            args.append(int(EMERGENCY_MASK))
        if not self.ga and self.ga_types:
            # The same NULL trap as rotorcraft above, one column over -- SQL's NOT IN is
            # NULL, not true, when a.type_code is NULL, so an unclassified attempt would silently fail this filter
            # too without COALESCE. `and self.ga_types` guards the other case: an empty list must exclude nothing.
            # SQLite happens to accept `NOT IN ()` and treat it as true, but an empty IN-list is its own extension that
            # standard SQL rejects, so the clause is left out entirely rather than built empty.
            # OR'd with the arrival-level EMERGENCY_BYPASS, same bypass as rotorcraft above (a C172 with
            # an engine failure must not be hidden as GA).
            conds.append(f"(COALESCE(a.type_code, '') NOT IN ({', '.join('?' * len(self.ga_types))}) OR {EMERGENCY_BYPASS})")
            args.extend(sorted(self.ga_types))
            args.append(int(EMERGENCY_MASK))
        return " AND ".join(conds), args


def is_attention(outcome: str | None, flags: int) -> bool:
    return outcome in {o.value for o in ATTENTION_OUTCOMES} or bool(flags & ATTENTION_FLAGS)


def _is_circuit_traffic(traffic_class: str | None, attempt_count: int) -> bool:
    """Light traffic making repeated attempts on one arrival, never attention — see CIRCUIT_TRAFFIC_CLASS
    above. A traffic_class that was never classified (None) does not count: only an exact "other" match does."""
    return traffic_class == CIRCUIT_TRAFFIC_CLASS and attempt_count > 1


def parse_limit(text: str | None) -> int:
    if text is None or not text.strip():
        return DEFAULT_LIMIT
    try:
        limit = int(text)
    except ValueError:
        raise ValueError(f"limit: {text!r} is not a whole number") from None
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit: must be 1 to {MAX_LIMIT}, not {limit}")
    return limit


def encode_cursor(started_at: float, arrival_id: int) -> str:
    return f"{started_at!r}:{arrival_id}"


def decode_cursor(text: str | None) -> tuple[float, int] | None:
    """(started_at, arrival_id) of the last arrival on the previous page, or None for the first page."""
    if text is None or not text.strip():
        return None
    started_at, sep, arrival_id = text.rpartition(":")
    try:
        if not sep:
            raise ValueError(text)
        return float(started_at), int(arrival_id)
    except ValueError:
        raise ValueError(f"cursor: {text!r} is not a cursor from this API") from None


def list_arrivals(conn: sqlite3.Connection, filt: ArrivalFilter, *, limit: int = DEFAULT_LIMIT,
                  cursor: tuple[float, int] | None = None, labels: Labels | None = None) -> dict[str, Any]:
    """One page of arrivals, newest first by the time their first attempt was established; `next_cursor` fetches the
    next page and is None on the last. `last_event_id` is read in the same transaction as the page. `labels` names
    the references of its attempts (arrival_rows)."""
    where, args = filt.where(has_path=has_column(conn, "approaches", "path"))
    sql = ("SELECT f.id, f.established_at FROM approaches f "
           f"WHERE f.id = f.arrival_id AND f.id IN (SELECT a.arrival_id FROM approaches a WHERE {where})")
    if cursor is not None:
        sql += " AND (f.established_at < ? OR (f.established_at = ? AND f.id < ?))"
        args += [cursor[0], cursor[0], cursor[1]]
    sql += " ORDER BY f.established_at DESC, f.id DESC LIMIT ?"
    page = conn.execute(sql, [*args, limit + 1]).fetchall()
    more = len(page) > limit
    page = page[:limit]
    return {"arrivals": arrival_rows(conn, [r["id"] for r in page], labels=labels),
            "next_cursor": encode_cursor(page[-1]["established_at"], page[-1]["id"]) if more else None,
            "last_event_id": last_event_id(conn)}


def arrival_rows(conn: sqlite3.Connection, arrival_ids: list[int], *,
                 labels: Labels | None = None) -> list[dict[str, Any]]:
    """These arrivals in the order given, each with every attempt in order and its 1,000 ft gate. `labels` names the
    references of an attempt, as reference.reference_labels builds them; without it, or for a path it does not know
    (renamed or removed from the configuration), a path's label is its stored name and a centreline's is "straight-in".

    A database the collector has not yet migrated to procedure paths (0008) has neither approaches.path nor
    approach_points.reference: there every attempt has no path and flew the centreline."""
    if not arrival_ids:
        return []
    marks = ", ".join("?" * len(arrival_ids))
    path = "path" if has_column(conn, "approaches", "path") else "NULL AS path"
    attempts: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(f"SELECT {', '.join(ATTEMPT_COLUMNS)}, {path} FROM approaches WHERE arrival_id IN ({marks}) "
                            "ORDER BY arrival_id, attempt_no, id", arrival_ids):
        attempts.setdefault(row["arrival_id"], []).append(row)
    gates = {row["approach_id"]: row for row in conn.execute(
        f"SELECT g.approach_id, {', '.join('g.' + c for c in GATE1000_COLUMNS)} FROM approach_gates g "
        f"JOIN approaches a ON a.id = g.approach_id WHERE g.gate_ft = 1000 AND a.arrival_id IN ({marks})", arrival_ids)}
    # The references each attempt's stored points were measured against, in the order it first flew them: one query
    # for the page, grouped so that it returns a row per reference and not per point.
    flown: dict[int, list[str | None]] = {}
    if has_column(conn, "approach_points", "reference"):
        for row in conn.execute(
                "SELECT p.approach_id, p.reference, MIN(p.t) AS first_t FROM approach_points p "
                f"JOIN approaches a ON a.id = p.approach_id WHERE a.arrival_id IN ({marks}) "
                "GROUP BY p.approach_id, p.reference ORDER BY p.approach_id, first_t", arrival_ids):
            flown.setdefault(row["approach_id"], []).append(row["reference"])
    return [_arrival(attempts[aid], gates, flown, labels or {}) for aid in arrival_ids if aid in attempts]


def _arrival(attempts: list[sqlite3.Row], gates: Mapping[int, sqlite3.Row], flown: Mapping[int, list[str | None]],
             labels: Labels) -> dict[str, Any]:
    closed = [a for a in attempts if a["outcome"] is not None]
    union = 0
    for a in closed:
        union |= a["flags"]
    traffic_class = attempts[-1]["traffic_class"]
    is_circuit = _is_circuit_traffic(traffic_class, len(attempts))
    # Computed once, across every attempt open or closed, and reused for both fields below --
    # not just "emergency" but the circuit bypass in "attention" too, since a flag that has so far landed only on an
    # attempt still open (the matcher stamps open rows as flags latch) is exactly the case
    # ArrivalFilter.where()'s EMERGENCY_BYPASS also reaches, and the two must keep agreeing.
    emergency = any(bool(a["flags"] & EMERGENCY_MASK) for a in attempts)
    return {
        "arrival_id": attempts[0]["arrival_id"],
        "hex": attempts[0]["hex"],
        "callsign": next((a["callsign"] for a in reversed(attempts) if a["callsign"]), None),
        "type_code": next((a["type_code"] for a in reversed(attempts) if a["type_code"]), None),
        "traffic_class": traffic_class,
        "airport": attempts[0]["airport"],
        "started_at": attempts[0]["established_at"],
        "ended_at": attempts[-1]["ended_at"] if len(closed) == len(attempts) else None,     # None while one is open
        "flags": flag_names(union),
        "emergency": emergency,
        # An emergency anywhere on the arrival, the open attempt included, makes it attention outright: red implies
        # attention, even while the only flagged attempt is still open and every closed one is
        # unremarkable. Otherwise a circuit is never attention, whatever a closed attempt says. Mirrors
        # the same arrival-level test in ArrivalFilter.where(), so the two agree.
        "attention": emergency or (not is_circuit and any(is_attention(a["outcome"], a["flags"]) for a in closed)),
        "attempts": [_attempt(a, gates.get(a["id"]), flown.get(a["id"], []), labels) for a in attempts],
    }


def _references(a: sqlite3.Row, flown: list[str | None], labels: Labels) -> list[dict[str, Any]]:
    """The references an attempt was measured against, in the order it first flew them, each as its name (None for the
    centreline) and its label: "Quiet Bridge" then "ILS", for an approach that joined the localizer.

    approaches.path is the last path flown, but only points within score_max_along_nm are stored, so a path may have
    been left before any stored point: it still leads. An attempt with no stored point at all is on its path, or on the
    centreline without one: never an empty list."""
    names = list(flown)
    if a["path"] is not None and a["path"] not in names:
        names.insert(0, a["path"])
    return [{"name": name, "label": label_for(labels, a["airport"], a["runway"], name)} for name in names or [None]]


def _attempt(a: sqlite3.Row, gate: sqlite3.Row | None, flown: list[str | None], labels: Labels) -> dict[str, Any]:
    return {
        "approach_id": a["id"], "attempt_no": a["attempt_no"], "runway": a["runway"], "outcome": a["outcome"],
        "flags": flag_names(a["flags"]), "established_at": a["established_at"], "ended_at": a["ended_at"],
        "lowest_hat_ft": a["lowest_hat_ft"], "closest_along_nm": a["closest_along_nm"],
        "ga_initiated_hat_ft": a["ga_initiated_hat_ft"], "landed_confirmed_by": a["landed_confirmed_by"],
        "path": a["path"],
        "path_label": None if a["path"] is None else label_for(labels, a["airport"], a["runway"], a["path"]),
        "references": _references(a, flown, labels),
        "gate1000": None if gate is None else {c: bool(gate[c]) if c == "observed" else gate[c] for c in GATE1000_COLUMNS},
    }

"""Fleet norms: the window's `approach_bins` become `fleet_stats` rows, the scorer looks each gate's deltas up in an
immutable snapshot of them, `coverage_daily` records what the receiver's own points saw, and `reflag` rewrites what the
stored approaches derive from both. Nothing here runs on the ingestion path."""
from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import Any

from .config import Config, zone_of
from .model import Flags, Outcome
from .scorer import GATE_FLAG_MASK, Gate, Scorer, ScoredPoint, coverage_record, gate_flags, key_may_flag, scorer_for
from .store.db import Store

log = logging.getLogger(__name__)

# Only an approach whose outcome reached the runway feeds a norm.
NORM_OUTCOMES = (Outcome.LANDED.value, Outcome.TOUCH_AND_GO.value)
MIN_BIN_SAMPLES = 2       # statistics.quantiles(n=4) needs two values, and one would give a band of no width
DAY_S = 86400.0
REFLAG_COMMIT_EVERY = 50  # a group's worth of approaches per write-lock acquisition; see reflag's docstring


def keys_for(type_code: str | None, traffic_class: str | None) -> list[tuple[str, str]]:
    """The keys an approach may be judged against, best first: its own type, then its traffic class, then `*`, every one
    of them on its own runway. An approach without a type code, or without a class, skips that step."""
    keys: list[tuple[str, str]] = []
    if type_code:
        keys.append(("type", type_code.upper()))
    if traffic_class:
        keys.append(("class", traffic_class))
    keys.append(("*", "*"))
    return keys


def key_text(key_kind: str, key: str) -> str:
    """The key as approaches.fleet_key stores it: "type:B738", "class:airline" or "*"."""
    return "*" if key_kind == "*" else f"{key_kind}:{key}"


def parse_key(text: str | None) -> tuple[str, str] | None:
    """(key_kind, key) from a stored fleet_key, or None when there is none or it is not one of the three forms."""
    if not text:
        return None
    if text == "*":
        return "*", "*"
    kind, _, key = text.partition(":")
    return (kind, key) if kind in ("type", "class") and key else None


def quartiles(values: list[float]) -> tuple[float, float, float] | tuple[None, None, None]:
    """(p25, p50, p75), or three Nones for fewer than MIN_BIN_SAMPLES values, which statistics.quantiles cannot take.
    The inclusive method, unlike the exclusive default the seam measures its spreads with, never reaches past the
    values it was given: a band drawn from two approaches stays between them."""
    if len(values) < MIN_BIN_SAMPLES:
        return None, None, None
    q1, med, q3 = statistics.quantiles(values, n=4, method="inclusive")
    return q1, med, q3


@dataclass(frozen=True, slots=True)
class NormBin:
    """One bin of one key: how many approaches reached it, and the quartiles of their mean deviation from the
    published path and of their mean wind-corrected speed. Either triple is None where too few approaches carried
    that value."""
    bin_nm: float
    n: int
    gp_p25: float | None
    gp_p50: float | None
    gp_p75: float | None
    gs_p25: float | None
    gs_p50: float | None
    gs_p75: float | None


@dataclass(frozen=True, slots=True)
class Norm:
    """One key's bins on one runway. `n` is the key's own approach count in the window, `fleet_stats.key_n` — the
    same number the page names — while each bin's own `n` counts the approaches that left that bin behind."""
    airport: str
    runway: str
    key_kind: str
    key: str
    n: int
    bins: dict[float, NormBin]

    @property
    def text(self) -> str:
        return key_text(self.key_kind, self.key)


@dataclass(frozen=True, slots=True)
class FleetNorms:
    """An immutable snapshot of fleet_stats, swapped in whole after each rebuild."""
    built_at: float | None
    by_key: dict[tuple[str, str, str, str], Norm] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.by_key)

    def get(self, airport: str, runway: str, key_kind: str, key: str) -> Norm | None:
        return self.by_key.get((airport, runway, key_kind, key))

    def lookup(self, airport: str, runway: str, *, type_code: str | None, traffic_class: str | None) -> Norm | None:
        """The best key this snapshot holds for an approach, down the chain keys_for gives: type, then class, then
        `*`."""
        for kind, key in keys_for(type_code, traffic_class):
            norm = self.by_key.get((airport, runway, kind, key))
            if norm is not None:
                return norm
        return None


def norms_from_rows(rows: Iterable[Any], built_at: float | None) -> FleetNorms:
    """A snapshot from fleet_stats rows, whether read back from the database or just computed by a rebuild."""
    bins: dict[tuple[str, str, str, str], dict[float, NormBin]] = {}
    key_n: dict[tuple[str, str, str, str], int] = {}
    for r in rows:
        key = (r["airport"], r["runway"], r["key_kind"], r["key"])
        key_n[key] = int(r["key_n"])
        bins.setdefault(key, {})[float(r["bin_nm"])] = NormBin(
            bin_nm=float(r["bin_nm"]), n=int(r["n"]), gp_p25=r["gp_p25"], gp_p50=r["gp_p50"], gp_p75=r["gp_p75"],
            gs_p25=r["gs_p25"], gs_p50=r["gs_p50"], gs_p75=r["gs_p75"])
    return FleetNorms(built_at, {k: Norm(k[0], k[1], k[2], k[3], key_n[k], v) for k, v in bins.items()})


def load_norms(store: Store) -> FleetNorms:
    """The snapshot the database holds now, for a collector starting up and for the commands that judge stored
    approaches. Empty, and so giving every delta as null, until the first rebuild has written a row."""
    rows = [dict(r) for r in store.fleet_stats_rows()]
    return norms_from_rows(rows, max((r["built_at"] for r in rows), default=None))


def key_counts(rows: Iterable[Any]) -> dict[tuple[str, str, str, str], int]:
    """How many approaches each key has in the window, from Store.norm_key_counts' (airport, runway, type_code,
    traffic_class, n) groups: every approach counts once towards its type, once towards its class and once towards
    `*`."""
    counts: dict[tuple[str, str, str, str], int] = {}
    for r in rows:
        for kind, key in keys_for(r["type_code"], r["traffic_class"]):
            k = (r["airport"], r["runway"], kind, key)
            counts[k] = counts.get(k, 0) + int(r["n"])
    return counts


def fleet_rows(bin_rows: Iterable[Any], counts: dict[tuple[str, str, str, str], int],
               min_samples: int) -> list[dict[str, Any]]:
    """The fleet_stats rows for a window. `bin_rows` are its approach_bins joined to their approaches, ordered by
    airport, runway and bin_nm (Store.norm_bin_rows), and `counts` the key counts above. A key is used only with at
    least `min_samples` approaches in the window; a bin needs MIN_BIN_SAMPLES of them for each triple, and a bin with
    neither triple writes no row. The rows of one (airport, runway, bin) are finished before the next begins, so the
    pass holds one bin's values instead of the window's. Every row of a key also carries `key_n`, that key's own count
    from `counts` — the same value on every bin of the key, so `key_n >= n` always."""
    out: list[dict[str, Any]] = []
    group: tuple[str, str, float] | None = None
    seen: dict[tuple[str, str], int] = {}
    gp: dict[tuple[str, str], list[float]] = {}
    gs: dict[tuple[str, str], list[float]] = {}

    def flush() -> None:
        if group is None:
            return
        airport, runway, bin_nm = group
        for kind, key in sorted(seen):
            gp25, gp50, gp75 = quartiles(gp.get((kind, key), []))
            gs25, gs50, gs75 = quartiles(gs.get((kind, key), []))
            if gp50 is None and gs50 is None:
                continue
            out.append(dict(airport=airport, runway=runway, key_kind=kind, key=key, bin_nm=bin_nm,
                            n=seen[(kind, key)], key_n=counts.get((airport, runway, kind, key), 0),
                            gp_p25=gp25, gp_p50=gp50, gp_p75=gp75,
                            gs_p25=gs25, gs_p50=gs50, gs_p75=gs75))

    for r in bin_rows:
        here = (r["airport"], r["runway"], float(r["bin_nm"]))
        if here != group:
            flush()
            group, seen, gp, gs = here, {}, {}, {}
        for kind, key in keys_for(r["type_code"], r["traffic_class"]):
            if counts.get((here[0], here[1], kind, key), 0) < min_samples:
                continue
            seen[(kind, key)] = seen.get((kind, key), 0) + 1
            if r["gp_dev_mean_ft"] is not None:
                gp.setdefault((kind, key), []).append(float(r["gp_dev_mean_ft"]))
            if r["gs_corr_mean"] is not None:
                gs.setdefault((kind, key), []).append(float(r["gs_corr_mean"]))
    flush()
    return out


def local_day(t: float, tz: tzinfo | None) -> str:
    """The day a time belongs to, in the zone coverage days are counted in. `tz` None is the host's own zone, which is
    what [service] timezone unset means."""
    return datetime.fromtimestamp(t, tz).strftime("%Y-%m-%d")


def day_bounds(day: str, tz: tzinfo | None) -> tuple[float, float]:
    """(start, end) in UNIX seconds of a local day written YYYY-MM-DD, so a day's approaches are found through the
    (airport, runway, ended_at) index instead of a scan that computes the day of every row.

    The end is the next calendar midnight, never start + 86,400 s: adding a day to an aware datetime moves its wall
    clock, so the two days a year daylight saving makes 23 or 25 hours long come out exactly as long as they were
    lived here. These bounds also seed coverage_rows's `keep` set, so an hour's error here would quietly drop a day of
    history whenever the clocks change."""
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def coverage_row(records: list[dict[str, Any]], gates_ft: Iterable[int], *, backfill_n: int) -> dict[str, Any]:
    """One coverage_daily row's values from the coverage_record of each of a day's approaches: how many there were, the
    quartiles of the lowest height tracked, the median closest distance, and the fraction of them whose 1,000 ft and 500
    ft gates the receiver's own points observed.

    `backfill_n` is how many of the day's approaches needed adsb.lol to reach the ground, and `backfill_frac` its share
    of the day — the antenna's shortfall read from the other side, which should fall towards zero as the antenna
    improves. A day with no approaches has no share, not a share of nothing."""
    lowest = sorted(r["lowest_hat_ft"] for r in records if r["lowest_hat_ft"] is not None)
    closest = sorted(r["closest_along_nm"] for r in records if r["closest_along_nm"] is not None)
    p25, p50, p75 = quartiles(lowest)
    _, closest_p50, _ = quartiles(closest)
    row: dict[str, Any] = dict(n=len(records), lowest_hat_p25=p25, lowest_hat_p50=p50, lowest_hat_p75=p75,
                               closest_nm_p50=closest_p50, gate1000_observed_frac=None, gate500_observed_frac=None,
                               backfill_n=backfill_n,
                               backfill_frac=backfill_n / len(records) if records else None)
    for gate_ft in gates_ft:
        column = f"gate{gate_ft}_observed_frac"
        if column in row and records:
            row[column] = sum(1 for r in records if r["gates_observed"].get(gate_ft)) / len(records)
    return row


def coverage_rows(store: Store, cfg: Config, scorer: Scorer, *, since: float, include_replays: bool,
                  stored: dict[tuple[str, str, str], tuple[int, int]]
                  ) -> tuple[list[dict[str, Any]], set[tuple[str, str, str]]]:
    """(rows to write, keys to keep) for coverage_daily. Each day's numbers come from the receiver's own points alone,
    through coverage_record, so a spliced adsb.lol tail never moves the coverage floor.

    A day is read again only when its approach count or its backfill count has moved; otherwise its stored row is
    kept as it stands. The receiver's own points of an approach are written once, at its close, and neither a splice
    nor reflag adds or changes one, so a day's quartiles and gate fractions cannot change once the day is over. Its
    backfill share can, and that is why the test is both counts: a splice adds adsb.lol points to an approach minutes
    or hours after its close, in a day that is over, without changing that day's approach count. Both counts come from
    one pass over `approaches` and its flags, so only the days that moved have their points read and scored again —
    which is what keeps an hourly rebuild off the window's whole point table.

    A day older than the norm window is kept as well. `[fleet].window_days` bounds the approaches a *norm* rests on;
    coverage_daily is the receiver's own record of what it saw, the number that should move when the antenna does, so
    the rebuild neither reads those days again nor throws them away."""
    tz = zone_of(cfg)
    # A coverage day is counted whole, so the window opens at the start of the local day `since` lands in. That
    # day's earlier approaches are read and scored regardless -- coverage_approaches takes the day's bounds, not
    # `since` -- so counting only the ones after it would put a part-day backfill_n under a whole-day n and
    # understate that day's share; and for good, since the day is kept as it stands once it leaves the window.
    since = day_bounds(local_day(since, tz), tz)[0]
    keep: set[tuple[str, str, str]] = {k for k in stored if day_bounds(k[2], tz)[1] <= since}
    rows: list[dict[str, Any]] = []
    for c in store.coverage_day_counts(since=since, day_of=lambda t: local_day(t, tz),
                                       include_replays=include_replays):
        key = (c["airport"], c["runway"], c["day"])
        keep.add(key)
        if stored.get(key) == (c["n"], c["backfill_n"]):
            continue
        airport = cfg.airports.get(c["airport"])
        rwy = None if airport is None else airport.runways.get(c["runway"])
        if rwy is None:                              # a runway that is no longer configured has no geometry to score
            keep.discard(key)
            continue
        start, end = day_bounds(c["day"], tz)
        records = [coverage_record(scorer, rwy,
                                   [ScoredPoint.from_row(p) for p in store.points(a["id"], scored_only=True)])
                   for a in store.coverage_approaches(c["airport"], c["runway"], start, end,
                                                      include_replays=include_replays)]
        rows.append(dict(airport=c["airport"], runway=c["runway"], day=c["day"],
                         **coverage_row(records, cfg.detection.gates_ft, backfill_n=c["backfill_n"])))
    return rows, keep


@dataclass(frozen=True, slots=True)
class Rebuild:
    """What one rebuild did, for the log line and the command's output."""
    approaches: int
    bin_rows: int
    rows: int
    keys: dict[str, int]
    coverage_written: int
    coverage_kept: int
    norms: FleetNorms
    elapsed_s: float

    def text(self) -> str:
        kinds = ", ".join(f"{self.keys.get(k, 0)} {k}" for k in ("type", "class", "*"))
        return (f"fleet: {self.approaches} approaches, {self.bin_rows} bin rows -> {self.rows} rows "
                f"over {len(self.norms)} keys ({kinds}); coverage {self.coverage_written} days written, "
                f"{self.coverage_kept} kept; {self.elapsed_s:.2f} s")


def rebuild(store: Store, cfg: Config, *, now: float | None = None, include_replays: bool = False) -> Rebuild:
    """Recompute fleet_stats and coverage_daily over the last [fleet].window_days and write both in one transaction,
    returning the snapshot to swap in. Approaches of replay runs are left out unless `include_replays`, so a replay into
    a live database never joins the norms."""
    started = time.monotonic()
    now = time.time() if now is None else now
    since = now - cfg.fleet.window_days * DAY_S
    count_rows = [dict(r) for r in store.norm_key_counts(outcomes=NORM_OUTCOMES, since=since,
                                                         include_replays=include_replays)]
    counts = key_counts(count_rows)
    read = 0

    def counted() -> Iterator[Any]:
        """The window's bins one at a time, counted for the log line. fleet_rows finishes a bin before it starts the
        next, so with the cursor handed straight to it neither holds the window."""
        nonlocal read
        for row in store.norm_bin_rows(outcomes=NORM_OUTCOMES, since=since, include_replays=include_replays):
            read += 1
            yield row

    rows = fleet_rows(counted(), counts, cfg.fleet.min_samples)
    stored = {(r["airport"], r["runway"], r["day"]): (r["n"], r["backfill_n"]) for r in store.coverage_daily_rows()}
    coverage, keep = coverage_rows(store, cfg, scorer_for(cfg), since=since, include_replays=include_replays,
                                   stored=stored)
    store.replace_norms(rows, coverage, keep, built_at=now)
    norms = norms_from_rows(rows, now)
    keys: dict[str, int] = {}
    for norm in norms.by_key.values():
        keys[norm.key_kind] = keys.get(norm.key_kind, 0) + 1
    return Rebuild(approaches=sum(int(r["n"]) for r in count_rows), bin_rows=read, rows=len(rows), keys=keys,
                   coverage_written=len(coverage), coverage_kept=len(keep) - len(coverage), norms=norms,
                   elapsed_s=time.monotonic() - started)


@dataclass(frozen=True, slots=True)
class Reflag:
    """What one reflag run did. `flagged` counts an approach once whenever gate_flags' recompute changed any gate
    flag of it -- not FAST/SLOW alone, since reflag rebuilds everything a gate can carry."""
    approaches: int
    bins: int
    gates: int
    keyed: int
    flagged: int
    elapsed_s: float

    def text(self) -> str:
        return (f"reflag: {self.approaches} approaches, {self.bins} bins rewritten, {self.gates} gates gained a delta, "
                f"{self.keyed} approaches keyed, {self.flagged} gained or lost a gate flag; {self.elapsed_s:.2f} s")


def reflag(store: Store, cfg: Config, *, since: float, until: float | None = None,
           norms: FleetNorms | None = None, include_replays: bool = False) -> Reflag:
    """Give the stored approaches what the norms say about them: each approach's bins are rewritten from its stored
    points, adsb.lol's included, each observed gate's two deltas are measured again, and fleet_key follows. Every flag a
    gate can carry -- GATE1000_UNOBSERVED, GATE500_UNOBSERVED, HIGH, LOW, OFF_COURSE, HIGH_SINK, FAST, SLOW,
    UNSTABLE_1000 and UNSTABLE_500, scorer.GATE_FLAG_MASK -- is cleared and rebuilt through scorer.gate_flags, the same
    verdict a live close reaches, so a threshold that no longer fires clears a stale UNSTABLE mark exactly as one that
    newly fires sets it. Outcomes, points, the gates' own measured values, and every flag that is not a gate's
    (established, sidestep, METAR, external, the summary's STEEP/SHALLOW, ...) are left alone.

    `include_replays` reads replay runs too, the same switch fleet.rebuild takes for the same reason: a workstation
    database holding both collected and replayed approaches would otherwise have reflag judge the replays against the
    live norms and write FAST or SLOW onto them, which is never what a database of replays wants by default.

    Each approach's three writes are their own store.atomic() unit -- a savepoint -- but REFLAG_COMMIT_EVERY
    approaches' units share one batch (store.begin_batch()/commit_batch()), committing every REFLAG_COMMIT_EVERY
    approaches and once more for whatever is left at the end: that takes the write lock about REFLAG_COMMIT_EVERY
    times less often than committing every approach, so a collector writing beside this waits at most one group's
    burst, not the whole run. Decision: a failing unit rolls back only its own approach; the approaches already
    written earlier in that still-open group are not part of its savepoint, so the batch's commit -- reached however
    the loop ends, on the way out even when a unit raises -- keeps them rather than losing the group. Nothing is left
    holding the write lock either way."""
    started = time.monotonic()
    scorer = scorer_for(cfg)
    scorer.set_norms(load_norms(store) if norms is None else norms)
    counts = dict(approaches=0, bins=0, gates=0, keyed=0, flagged=0)
    rows = store.approaches_to_reflag(since=since, until=until, include_replays=include_replays)
    total = len(rows)
    try:
        for i, row in enumerate(rows, start=1):
            store.begin_batch()        # a no-op once the group before this one is already open
            aid = row["id"]
            bins = scorer.bins([ScoredPoint.from_row(p) for p in store.points(aid, scored_only=True)])
            gates = [Gate(gate_ft=g["gate_ft"], observed=bool(g["observed"]), along_nm=g["along_nm"],
                          gp_dev_ft=g["gp_dev_ft"], gp_dots=g["gp_dots"], loc_dots=g["loc_dots"],
                          sink_fpm=g["sink_fpm"], gs_corr=g["gs_corr"]) for g in store.gates(aid)]
            key = scorer.fleet_deltas(row["airport"], row["runway"], gates, type_code=row["type_code"],
                                      traffic_class=row["traffic_class"])
            # Every flag a gate can carry is rebuilt from these very gates; everything else -- established, sidestep,
            # METAR, external, the summary's STEEP/SHALLOW -- is not a gate's and survives untouched.
            flags = Flags(row["flags"]) & ~GATE_FLAG_MASK
            may_flag = key_may_flag(key)       # only a type or class norm may set FAST or SLOW
            for g in gates:                    # the same verdict a live close reaches, through scorer.gate_flags
                flags |= gate_flags(g, cfg.stability, may_flag=may_flag)
            with store.atomic():
                store.replace_bins(aid, bins)
                store.set_gate_deltas(aid, [(g.gate_ft, g.fleet_gp_delta_ft, g.fleet_gs_delta_kt) for g in gates])
                store.set_fleet(aid, flags=flags, fleet_key=key)
            counts["approaches"] += 1
            counts["bins"] += len(bins)
            counts["gates"] += sum(1 for g in gates if g.fleet_gp_delta_ft is not None or g.fleet_gs_delta_kt is not None)
            counts["keyed"] += key is not None
            counts["flagged"] += int(flags) != row["flags"]
            if i % REFLAG_COMMIT_EVERY == 0:
                store.commit_batch()
                if i % (REFLAG_COMMIT_EVERY * 10) == 0:
                    log.info("reflag: %d of %d approaches done", i, total)
    finally:
        store.commit_batch()           # whatever the loop leaves open, including a group cut short by a raise
    return Reflag(elapsed_s=time.monotonic() - started, **counts)

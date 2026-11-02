"""Databases and snapshots for the fleet tests: approaches with bins and gates, and the norms to judge them by."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from adsb_approach.config import Config, load_config
from adsb_approach.fleet import FleetNorms, norms_from_rows
from adsb_approach.model import Flags, Outcome
from adsb_approach.scorer import scorer_for
from adsb_approach.store.db import Store
from tests.conftest import ROOT, T0, synthetic_track


def cfg_for(tmp_path: Path, **fleet) -> Config:
    """The example configuration with its database under tmp_path and [fleet] fields overridden, loaded afresh so a
    test never changes the session-wide example_cfg."""
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.service.db_path, cfg.service.typedb_path = str(tmp_path / "fleet.sqlite"), None
    for name, value in fleet.items():
        setattr(cfg.fleet, name, value)
    return cfg


def new_store(tmp_path: Path, *, name: str = "fleet.sqlite", kind: str = "live") -> tuple[Store, int]:
    store = Store(tmp_path / name)
    store.migrate()
    return store, store.new_run(kind, "test")


def add_approach(store: Store, run_id: int, *, airport: str = "KSFO", runway: str = "28R",
                 type_code: str | None = "B738", traffic_class: str | None = "airline",
                 outcome: Outcome = Outcome.LANDED, ended_at: float = T0, flags: Flags = Flags.NONE,
                 bins: tuple[tuple[float, float | None, float | None], ...] = (),
                 gates: tuple[tuple[int, float, float, float], ...] = ()) -> int:
    """A stored approach with the bins (bin_nm, gp_dev_mean_ft, gs_corr_mean) and observed gates (gate_ft, along_nm,
    gp_dev_ft, gs_corr) given: enough for the rebuild, which reads bins, and for reflag, which reads gates."""
    aid = store.open_approach(run_id=run_id, flight_id=None, arrival_id=None, attempt_no=1, hex="a44540",
                              callsign="UAL1", type_code=type_code, traffic_class=traffic_class, approach_cat="C",
                              airport=airport, runway=runway, established_at=ended_at - 300.0, first_along_nm=8.0,
                              weather_id=None, reference_source="cifp:test", prev_approach_id=None)
    store.close_approach(
        aid, outcome=outcome, flags=flags, ended_at=ended_at, summary={}, points=[],
        gates=[dict(gate_ft=g, observed=True, along_nm=a, gp_dev_ft=gp, gs_corr=gs) for g, a, gp, gs in gates],
        bins=[dict(bin_nm=b, n=4, gp_dev_mean_ft=gp, loc_dev_mean_ft=0.0, gs_corr_mean=gs, sink_mean_fpm=-700.0)
              for b, gp, gs in bins], ga=None)
    return aid


def store_flight(store: Store, run_id: int, cfg: Config, *, runway: str = "28R", t0: float = T0, end_nm: float = 0.5,
                 outcome: Outcome = Outcome.LANDED, type_code: str | None = "B738", gs_kt: float = 150.0,
                 tail_to_nm: float | None = None, flags: Flags = Flags.NONE) -> int:
    """A stored approach scored from a synthetic straight-in, so it has real points, gates and bins. `tail_to_nm`
    adds points from `end_nm` in to it with origin 'adsb.lol', as a splice does."""
    airport = cfg.airports["KSFO"]
    rwy = airport.runways[runway]
    scorer = scorer_for(cfg)
    points = [scorer.score(rwy, airport, p, None)
              for p in synthetic_track(rwy, start_nm=8.0, end_nm=end_nm, t0=t0, gs_kt=gs_kt)]
    if tail_to_nm is not None:
        points += [replace(scorer.score(rwy, airport, p, None), origin="adsb.lol")
                   for p in synthetic_track(rwy, start_nm=end_nm, end_nm=tail_to_nm, t0=points[-1].t + 1.0,
                                            gs_kt=gs_kt)]
    gates = scorer.gates(rwy, points)
    summary = scorer.summarize(points)
    aid = store.open_approach(run_id=run_id, flight_id=None, arrival_id=None, attempt_no=1, hex="a44540",
                              callsign="UAL1", type_code=type_code, traffic_class="airline", approach_cat="C",
                              airport="KSFO", runway=runway, established_at=points[0].t, first_along_nm=8.0,
                              weather_id=None, reference_source="cifp:test", prev_approach_id=None)
    store.close_approach(aid, outcome=outcome, flags=flags, ended_at=points[-1].t, summary=summary.as_row(),
                         gates=[g.as_row() for g in gates], points=[p.as_row() for p in points],
                         bins=scorer.bins(points), ga=None)
    return aid


def bin_row(airport: str, runway: str, type_code: str | None, traffic_class: str | None, bin_nm: float,
            gp: float | None, gs: float | None) -> dict:
    """One row as Store.norm_bin_rows yields it."""
    return dict(airport=airport, runway=runway, type_code=type_code, traffic_class=traffic_class, bin_nm=bin_nm,
                gp_dev_mean_ft=gp, gs_corr_mean=gs)


def stats_row(airport: str = "KSFO", runway: str = "28R", key_kind: str = "type", key: str = "B738",
              bin_nm: float = 1.5, n: int = 40, key_n: int | None = None, gp: float = 10.0, gs: float = 142.0) -> dict:
    """One row as fleet_stats holds it, with both triples a fixed width around their median. `key_n` defaults to `n`,
    as it does whenever a test's rows are the only bin of their key."""
    return dict(airport=airport, runway=runway, key_kind=key_kind, key=key, bin_nm=bin_nm, n=n,
                key_n=n if key_n is None else key_n,
                gp_p25=gp - 20.0, gp_p50=gp, gp_p75=gp + 20.0, gs_p25=gs - 5.0, gs_p50=gs, gs_p75=gs + 5.0)


def norms_for(*, airport: str = "KSFO", runway: str = "28R", key_kind: str = "type", key: str = "B738",
              gp: float = 0.0, gs: float = 140.0, bins: int = 16, built_at: float = T0) -> FleetNorms:
    """A snapshot with a bin every 0.5 nm out to 8 nm for one key on one runway, so every gate of a synthetic
    approach falls in one: `gp` is the height median and `gs` the speed median."""
    return norms_from_rows([stats_row(airport=airport, runway=runway, key_kind=key_kind, key=key, bin_nm=i / 2,
                                      gp=gp, gs=gs) for i in range(bins)], built_at)

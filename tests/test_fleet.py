"""Fleet norms: the keys, the rebuild's rows, the gates' deltas, coverage and reflag."""
from __future__ import annotations

import gc
import sqlite3
import time
import weakref
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from adsb_approach.cli import main
from adsb_approach.fleet import (DAY_S, NORM_OUTCOMES, REFLAG_COMMIT_EVERY, FleetNorms, coverage_rows, day_bounds,
                                 coverage_row, fleet_rows, key_counts, key_text, keys_for, local_day,
                                 norms_from_rows, parse_key, quartiles, rebuild, reflag)
from adsb_approach.model import Flags, Outcome
from adsb_approach.store.db import BACKFILL_FLAGS, Store
from adsb_approach.scorer import Gate, apply_deltas, bin_of, scorer_for
from tests.conftest import ROOT, T0, make_ksfo, make_sfo_28r, synthetic_track
from tests.fleetdb import add_approach, bin_row, cfg_for, new_store, norms_for, stats_row, store_flight

LA = ZoneInfo("America/Los_Angeles")          # the example configuration's [service] timezone, so cfg_for's zone


# --- the keys, the quartiles and the snapshot ---------------------------------------------------------------------

def test_keys_go_type_then_class_then_star_and_skip_what_an_approach_lacks():
    assert keys_for("b738", "airline") == [("type", "B738"), ("class", "airline"), ("*", "*")]
    assert keys_for(None, "other") == [("class", "other"), ("*", "*")]
    assert keys_for("E75L", None) == [("type", "E75L"), ("*", "*")]
    assert keys_for(None, None) == [("*", "*")]


def test_a_key_reads_back_as_approaches_fleet_key_stores_it():
    assert key_text("type", "B738") == "type:B738" and parse_key("type:B738") == ("type", "B738")
    assert key_text("class", "airline") == "class:airline" and parse_key("class:airline") == ("class", "airline")
    assert key_text("*", "*") == "*" and parse_key("*") == ("*", "*")
    assert parse_key(None) is None and parse_key("") is None          # an approach scored before the first rebuild
    assert parse_key("B738") is None and parse_key("type:") is None and parse_key("airline:x") is None


def test_quartiles_come_from_statistics_and_need_two_values():
    assert quartiles([]) == (None, None, None)
    assert quartiles([5.0]) == (None, None, None)                     # one approach would give a band of no width
    assert quartiles([0.0, 10.0]) == (2.5, 5.0, 7.5)                  # inclusive: never past the values given
    assert quartiles([1.0, 2.0, 3.0, 4.0]) == (1.75, 2.5, 3.25)


def test_a_gate_falls_in_the_bin_its_own_approachs_points_built(example_cfg):
    assert (bin_of(0.0, 0.5), bin_of(0.49, 0.5), bin_of(0.5, 0.5), bin_of(1.5, 0.5)) == (0.0, 0.0, 0.5, 1.5)
    assert bin_of(-0.2, 0.5) == -0.5                                   # past the threshold: no norm has such a bin
    rwy = make_sfo_28r()
    scorer = scorer_for(example_cfg)
    points = [scorer.score(rwy, make_ksfo(rwy), p, None) for p in synthetic_track(rwy, start_nm=4.0, end_nm=0.5)]
    assert {b["bin_nm"] for b in scorer.bins(points)} == {bin_of(p.along_nm, 0.5) for p in points}


def test_a_snapshot_groups_its_bins_by_key_and_counts_its_densest():
    norms = norms_from_rows([stats_row(bin_nm=1.0, n=31), stats_row(bin_nm=1.5, n=40)], 1789000000.0)
    norm = norms.get("KSFO", "28R", "type", "B738")
    assert norm is not None and norm.text == "type:B738" and set(norm.bins) == {1.0, 1.5}
    assert norm.n == 40 and norm.bins[1.5].gs_p50 == 142.0 and norm.bins[1.0].n == 31
    assert len(norms) == 1 and norms.built_at == 1789000000.0
    assert norms.get("KSFO", "28L", "type", "B738") is None


def test_a_snapshot_falls_back_from_type_to_class_to_star():
    norms = norms_from_rows([stats_row(key_kind="type", key="B738"), stats_row(key_kind="class", key="airline"),
                             stats_row(key_kind="*", key="*")], 1789000000.0)
    best = norms.lookup("KSFO", "28R", type_code="b738", traffic_class="airline")
    assert best is not None and best.text == "type:B738"
    fell = norms.lookup("KSFO", "28R", type_code="A20N", traffic_class="airline")
    assert fell is not None and fell.text == "class:airline"
    last = norms.lookup("KSFO", "28R", type_code="A20N", traffic_class="bizjet")
    assert last is not None and last.text == "*"
    assert norms.lookup("KOAK", "30", type_code="B738", traffic_class="airline") is None


# --- the window's bins become fleet_stats rows --------------------------------------------------------------------

def test_key_counts_add_each_approach_to_its_type_its_class_and_star():
    counts = key_counts([dict(airport="KSFO", runway="28R", type_code="B738", traffic_class="airline", n=30),
                         dict(airport="KSFO", runway="28R", type_code="A20N", traffic_class="airline", n=12),
                         dict(airport="KSFO", runway="28R", type_code=None, traffic_class="other", n=3)])
    assert counts[("KSFO", "28R", "type", "B738")] == 30
    assert counts[("KSFO", "28R", "class", "airline")] == 42
    assert counts[("KSFO", "28R", "*", "*")] == 45
    assert ("KSFO", "28R", "type", "A20N") in counts and counts[("KSFO", "28R", "type", "A20N")] == 12


def test_only_keys_with_min_samples_of_their_own_get_rows():
    rows = [bin_row("KSFO", "28R", "B738", "airline", 1.5, 10.0 + i, 140.0 + i) for i in range(4)]
    counts = {("KSFO", "28R", "type", "B738"): 4, ("KSFO", "28R", "class", "airline"): 4, ("KSFO", "28R", "*", "*"): 4}
    assert [r["key_kind"] for r in fleet_rows(rows, counts, 4)] == ["*", "class", "type"]
    assert [r["key_kind"] for r in fleet_rows(rows, counts, 5)] == []
    thin = {("KSFO", "28R", "type", "B738"): 2, ("KSFO", "28R", "class", "airline"): 4, ("KSFO", "28R", "*", "*"): 4}
    assert [r["key"] for r in fleet_rows(rows, thin, 4)] == ["*", "airline"]


def test_every_row_of_a_key_carries_the_keys_own_window_count_as_key_n():
    rows = [bin_row("KSFO", "28R", "B738", "airline", 1.0, 0.0, 140.0),
            bin_row("KSFO", "28R", "B738", "airline", 1.0, 10.0, 145.0),
            bin_row("KSFO", "28R", "B738", "airline", 1.5, 0.0, 140.0),
            bin_row("KSFO", "28R", "B738", "airline", 1.5, 10.0, 145.0),
            bin_row("KSFO", "28R", "B738", "airline", 1.5, 20.0, 150.0)]
    counts = {("KSFO", "28R", "type", "B738"): 30, ("KSFO", "28R", "class", "airline"): 30}
    written = [r for r in fleet_rows(rows, counts, 2) if r["key_kind"] == "type"]
    assert [(r["bin_nm"], r["n"]) for r in written] == [(1.0, 2), (1.5, 3)]      # the bins' own counts differ
    assert [r["key_n"] for r in written] == [30, 30]                            # but key_n is the key's, not the bin's
    assert all(r["key_n"] >= r["n"] for r in written)


def test_a_row_carries_its_bins_count_and_both_triples():
    rows = [bin_row("KSFO", "28R", "B738", "airline", 1.5, gp, gs)
            for gp, gs in ((0.0, 140.0), (10.0, 145.0), (20.0, 150.0), (30.0, 155.0))]
    counts = {("KSFO", "28R", "type", "B738"): 4}
    row = next(r for r in fleet_rows(rows, counts, 4) if r["key_kind"] == "type")
    assert (row["airport"], row["runway"], row["key"], row["bin_nm"], row["n"]) == ("KSFO", "28R", "B738", 1.5, 4)
    assert (row["gp_p25"], row["gp_p50"], row["gp_p75"]) == (7.5, 15.0, 22.5)
    assert (row["gs_p25"], row["gs_p50"], row["gs_p75"]) == (143.75, 147.5, 151.25)


def test_a_bin_needs_two_values_for_each_triple_and_none_writes_no_row():
    counts = {("KSFO", "28R", "type", "B738"): 3}
    rows = [bin_row("KSFO", "28R", "B738", None, 0.5, 10.0, None), bin_row("KSFO", "28R", "B738", None, 0.5, 20.0, None),
            bin_row("KSFO", "28R", "B738", None, 0.5, 30.0, 150.0),
            bin_row("KSFO", "28R", "B738", None, 7.5, None, None), bin_row("KSFO", "28R", "B738", None, 7.5, None, None),
            bin_row("KSFO", "28R", "B738", None, 7.5, None, None)]
    written = [r for r in fleet_rows(rows, counts, 3) if r["key_kind"] == "type"]
    assert [r["bin_nm"] for r in written] == [0.5]                 # the 7.5 bin has no value on either side
    assert written[0]["n"] == 3 and written[0]["gp_p50"] == 20.0
    assert written[0]["gs_p25"] is None and written[0]["gs_p50"] is None and written[0]["gs_p75"] is None


def test_each_runway_and_bin_is_finished_before_the_next_begins():
    counts = {("KSFO", "28R", "*", "*"): 2, ("KSFO", "28L", "*", "*"): 2}
    rows = [bin_row("KSFO", "28L", None, None, 0.5, 1.0, 130.0), bin_row("KSFO", "28L", None, None, 0.5, 3.0, 134.0),
            bin_row("KSFO", "28L", None, None, 1.0, 5.0, 140.0), bin_row("KSFO", "28L", None, None, 1.0, 7.0, 144.0),
            bin_row("KSFO", "28R", None, None, 0.5, 9.0, 150.0), bin_row("KSFO", "28R", None, None, 0.5, 11.0, 154.0)]
    written = fleet_rows(rows, counts, 2)
    assert [(r["runway"], r["bin_nm"], r["gp_p50"]) for r in written] == [
        ("28L", 0.5, 2.0), ("28L", 1.0, 6.0), ("28R", 0.5, 10.0)]


def test_the_window_the_outcomes_and_the_run_kind_choose_the_bins(tmp_path):
    store, live = new_store(tmp_path)
    replay = store.new_run("replay", "chunks")
    add_approach(store, live, bins=((1.5, 10.0, 140.0),))                                        # kept
    add_approach(store, live, outcome=Outcome.LOST_ON_FINAL, bins=((1.5, 99.0, 99.0),))          # wrong outcome
    add_approach(store, live, ended_at=T0 - 10 * 86400.0, bins=((1.5, 98.0, 98.0),))             # outside the window
    add_approach(store, replay, bins=((1.5, 97.0, 97.0),))                                       # a replay run
    add_approach(store, live, type_code=None, traffic_class="other", bins=((1.5, 20.0, 150.0),))
    # An outcome that reached the runway feeds a norm, so a touch-and-go does and a go-around does not (touch-and-goes
    # are kept in deliberately).
    add_approach(store, live, type_code="A320", outcome=Outcome.TOUCH_AND_GO, bins=((1.5, 30.0, 160.0),))
    add_approach(store, live, type_code="A321", outcome=Outcome.GO_AROUND, bins=((1.5, 96.0, 96.0),))
    since = T0 - 86400.0
    rows = list(store.norm_bin_rows(outcomes=NORM_OUTCOMES, since=since))
    assert [(r["type_code"], r["gp_dev_mean_ft"]) for r in rows] == [("B738", 10.0), (None, 20.0), ("A320", 30.0)]
    counts = key_counts(store.norm_key_counts(outcomes=NORM_OUTCOMES, since=since))
    assert counts == {("KSFO", "28R", "type", "B738"): 1, ("KSFO", "28R", "type", "A320"): 1,
                      ("KSFO", "28R", "class", "airline"): 2, ("KSFO", "28R", "class", "other"): 1,
                      ("KSFO", "28R", "*", "*"): 3}
    with_replays = list(store.norm_bin_rows(outcomes=NORM_OUTCOMES, since=since, include_replays=True))
    assert sorted(r["gp_dev_mean_ft"] for r in with_replays) == [10.0, 20.0, 30.0, 97.0]
    store.close()


def test_a_rebuild_takes_its_window_from_fleet_window_days(tmp_path):
    """The window is 30 days, and it is the rebuild that turns [fleet].window_days into the `since` its queries use —
    the same bound the store filter above is only handed."""
    store, run = new_store(tmp_path)
    add_approach(store, run, ended_at=T0, bins=((1.5, 10.0, 140.0),))
    add_approach(store, run, ended_at=T0 - 60.0, bins=((1.5, 10.0, 150.0),))
    add_approach(store, run, ended_at=T0 - 20 * DAY_S, bins=((1.5, 90.0, 190.0),))
    wide = rebuild(store, cfg_for(tmp_path, window_days=30, min_samples=1), now=T0 + 60.0)
    assert (wide.approaches, wide.bin_rows) == (3, 3)
    assert wide.norms.get("KSFO", "28R", "type", "B738").bins[1.5].gs_p50 == 150.0

    narrow = rebuild(store, cfg_for(tmp_path, window_days=2, min_samples=1), now=T0 + 60.0)
    assert (narrow.approaches, narrow.bin_rows) == (2, 2)         # the 20-day-old approach is outside this window
    assert narrow.norms.get("KSFO", "28R", "type", "B738").bins[1.5].gs_p50 == 145.0
    store.close()


# --- the gates' deltas --------------------------------------------------------------------------------------------

def gate(gate_ft: int = 1000, *, observed: bool = True, along_nm: float | None = 3.2, gp_dev_ft: float | None = 30.0,
         gs_corr: float | None = 160.0) -> Gate:
    return Gate(gate_ft=gate_ft, observed=observed, along_nm=along_nm, gp_dev_ft=gp_dev_ft, gs_corr=gs_corr)


def test_each_observed_gate_is_measured_against_the_bin_that_holds_it():
    norms = norms_from_rows([stats_row(bin_nm=3.0, gp=10.0, gs=150.0), stats_row(bin_nm=1.5, gp=6.0, gs=140.0)], 1.0)
    gates = [gate(1000, along_nm=3.2, gp_dev_ft=30.0, gs_corr=160.0),
             gate(500, along_nm=1.6, gp_dev_ft=-4.0, gs_corr=128.0)]
    apply_deltas(norms.get("KSFO", "28R", "type", "B738"), gates, 0.5)
    assert (gates[0].fleet_gp_delta_ft, gates[0].fleet_gs_delta_kt) == (20.0, 10.0)
    assert (gates[1].fleet_gp_delta_ft, gates[1].fleet_gs_delta_kt) == (-10.0, -12.0)


def test_a_gate_with_no_bin_no_value_or_no_norm_keeps_null_deltas():
    norms = norms_from_rows([stats_row(bin_nm=3.0), dict(stats_row(bin_nm=2.0), gs_p25=None, gs_p50=None, gs_p75=None)],
                            1.0)
    norm = norms.get("KSFO", "28R", "type", "B738")
    gates = [gate(1000, observed=False), gate(900, along_nm=None), gate(800, along_nm=-0.2), gate(700, along_nm=7.5),
             gate(600, along_nm=2.4), gate(500, along_nm=3.4, gp_dev_ft=None, gs_corr=None)]
    apply_deltas(norm, gates, 0.5)
    assert [(g.fleet_gp_delta_ft, g.fleet_gs_delta_kt) for g in gates[:4]] == [(None, None)] * 4
    assert (gates[4].fleet_gp_delta_ft, gates[4].fleet_gs_delta_kt) == (20.0, None)  # that bin has no speed median
    assert (gates[5].fleet_gp_delta_ft, gates[5].fleet_gs_delta_kt) == (None, None)  # the gate has neither value
    plain = [gate()]
    apply_deltas(None, plain, 0.5)
    assert (plain[0].fleet_gp_delta_ft, plain[0].fleet_gs_delta_kt) == (None, None)


def test_the_scorer_fills_the_deltas_and_names_the_key_it_used(example_cfg):
    scorer = scorer_for(example_cfg)
    gates = [gate(1000, along_nm=3.2, gs_corr=160.0)]
    assert scorer.fleet_deltas("KSFO", "28R", gates, type_code="B738", traffic_class="airline") is None
    assert gates[0].fleet_gs_delta_kt is None                     # no snapshot yet: every delta stays null
    scorer.set_norms(norms_from_rows([stats_row(key_kind="class", key="airline", bin_nm=3.0, gs=150.0)], 1.0))
    assert scorer.fleet_deltas("KSFO", "28R", gates, type_code="B738", traffic_class="airline") == "class:airline"
    assert gates[0].fleet_gs_delta_kt == 10.0
    assert scorer.fleet_deltas("KOAK", "30", gates, type_code="B738", traffic_class="airline") is None


# --- coverage_daily from the receiver's own points -----------------------------------------------------------------

def test_a_days_coverage_counts_its_approaches_and_quartiles_what_the_receiver_reached(tmp_path):
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    store_flight(store, run, cfg, t0=T0)                       # tracked to 0.5 nm: both gates observed
    store_flight(store, run, cfg, t0=T0 + 600.0, end_nm=2.0)   # lost at 2 nm: the 500 ft gate never crossed
    rows, keep = coverage_rows(store, cfg, scorer_for(cfg), since=T0 - DAY_S, include_replays=False, stored={})
    assert keep == {("KSFO", "28R", "2026-09-12")} and len(rows) == 1
    row = rows[0]
    assert (row["airport"], row["runway"], row["day"], row["n"]) == ("KSFO", "28R", "2026-09-12", 2)
    assert (row["gate1000_observed_frac"], row["gate500_observed_frac"]) == (1.0, 0.5)
    assert 30.0 < row["lowest_hat_p25"] < row["lowest_hat_p50"] < row["lowest_hat_p75"] < 700.0
    assert 0.5 < row["closest_nm_p50"] < 2.0
    store.close()


def test_a_day_is_read_again_only_when_its_approach_count_or_its_backfill_count_has_moved(tmp_path):
    """The receiver's own points of an approach are written once, at its close, so a day's quartiles and gate fractions
    cannot change once it is over — but a splice adds adsb.lol points to it hours later without touching its approach
    count, so the backfill count is the other half of the freshness test."""
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    store_flight(store, run, cfg, t0=T0, flags=Flags.EXTERNAL_TAIL)
    key = ("KSFO", "28R", "2026-09-12")
    read = dict(store=store, cfg=cfg, scorer=scorer_for(cfg))

    def pass_over(stored):
        return coverage_rows(read["store"], read["cfg"], read["scorer"], since=T0 - DAY_S, include_replays=False,
                             stored=stored)

    rows, keep = pass_over({key: (1, 1)})
    assert rows == [] and keep == {key}                        # neither count has moved: not read again
    rows, keep = pass_over({key: (0, 1)})
    assert [r["n"] for r in rows] == [1] and keep == {key}      # an approach arrived late
    rows, keep = pass_over({key: (1, 0)})
    assert [r["backfill_n"] for r in rows] == [1] and keep == {key}     # a splice landed in a day that was over
    rows, keep = pass_over({})
    assert [r["n"] for r in rows] == [1] and keep == {key}
    store.close()


def test_coverage_counts_only_the_receivers_own_points(tmp_path):
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    aid = store_flight(store, run, cfg, t0=T0, end_nm=2.0, tail_to_nm=0.2)
    store_flight(store, run, cfg, t0=T0 + 600.0, end_nm=2.0, tail_to_nm=0.2)
    assert [g["observed"] for g in store.gates(aid)] == [1, 1]           # the spliced tail crossed the 500 ft gate
    rows, _ = coverage_rows(store, cfg, scorer_for(cfg), since=T0 - DAY_S, include_replays=False, stored={})
    assert rows[0]["gate500_observed_frac"] == 0.0 and rows[0]["gate1000_observed_frac"] == 1.0
    assert rows[0]["lowest_hat_p50"] > 500.0 and rows[0]["closest_nm_p50"] > 1.9
    store.close()


# --- the rebuild --------------------------------------------------------------------------------------------------

def test_a_rebuild_writes_the_keys_swaps_a_snapshot_in_and_says_what_it_did(tmp_path):
    cfg = cfg_for(tmp_path, min_samples=3)
    store, run = new_store(tmp_path)
    for i in range(3):
        add_approach(store, run, ended_at=T0 - i, bins=((1.0, 5.0 + i, 130.0 + i), (1.5, 10.0 + i, 140.0 + i)))
    result = rebuild(store, cfg, now=T0 + 60.0)
    assert (result.approaches, result.bin_rows, result.rows) == (3, 6, 6)
    assert result.keys == {"type": 1, "class": 1, "*": 1} and len(result.norms) == 3
    norm = result.norms.lookup("KSFO", "28R", type_code="B738", traffic_class="airline")
    assert norm is not None and norm.text == "type:B738" and norm.n == 3 and norm.bins[1.5].gs_p50 == 141.0
    stored = store.fleet_stats_rows()
    assert len(stored) == 6 and {r["built_at"] for r in stored} == {T0 + 60.0}
    assert "3 approaches, 6 bin rows -> 6 rows over 3 keys (1 type, 1 class, 1 *)" in result.text()
    store.close()


def test_a_rebuild_leaves_replay_runs_out_unless_it_is_asked(tmp_path):
    cfg = cfg_for(tmp_path, min_samples=2)
    store, live = new_store(tmp_path)
    replay = store.new_run("replay", "chunks")
    for i in range(2):
        add_approach(store, replay, ended_at=T0 - i, bins=((1.5, 10.0 + i, 140.0 + i),))
    assert rebuild(store, cfg, now=T0 + 60.0).rows == 0
    assert rebuild(store, cfg, now=T0 + 60.0, include_replays=True).rows == 3
    for i in range(2):
        add_approach(store, live, ended_at=T0 - i, bins=((1.5, 20.0 + i, 150.0 + i),))
    assert rebuild(store, cfg, now=T0 + 60.0).rows == 3          # the replay's approaches are out again
    store.close()


def test_a_second_rebuild_writes_the_same_rows(tmp_path):
    cfg = cfg_for(tmp_path, min_samples=2)
    store, run = new_store(tmp_path)
    for i in range(4):
        store_flight(store, run, cfg, t0=T0 + 600.0 * i, end_nm=0.5 + 0.1 * i)
    first = rebuild(store, cfg, now=T0 + 3600.0)
    # built_at legitimately differs between the two rebuilds; drop it by name rather than position, since key_n
    # (migration 0004) is appended after it in the table's physical column order.
    rows = [{k: v for k, v in dict(r).items() if k != "built_at"} for r in store.fleet_stats_rows()]
    coverage = [tuple(r)[:-1] for r in store.coverage_daily_rows()]
    second = rebuild(store, cfg, now=T0 + 7200.0)
    assert [{k: v for k, v in dict(r).items() if k != "built_at"} for r in store.fleet_stats_rows()] == rows and rows
    assert [tuple(r)[:-1] for r in store.coverage_daily_rows()] == coverage and coverage
    assert (second.rows, second.keys) == (first.rows, first.keys)
    assert (second.coverage_written, second.coverage_kept) == (0, 1)   # the day was already built and has not moved
    store.close()


def cli_database(tmp_path) -> Path:
    """A database of 30 landed approaches that ended a minute ago, so the shipped [fleet] window and min_samples of 30
    take every one of them. Each has one bin at 3.0 nm and two gates, the 1,000 ft one inside that bin."""
    store, run = new_store(tmp_path, name="cli.sqlite")
    now = time.time()
    for i in range(30):
        add_approach(store, run, ended_at=now - 60.0 - i, bins=((3.0, 10.0 + i * 0.1, 150.0 + i * 0.1),),
                     gates=((1000, 3.2, 12.0, 175.0), (500, 1.6, 8.0, 150.0)))
    store.close()
    return tmp_path / "cli.sqlite"


def test_the_fleet_rebuild_command_writes_the_norms(tmp_path, capsys):
    db, config = cli_database(tmp_path), str(ROOT / "config" / "adsb-approach.example.toml")
    assert main(["fleet-rebuild", "--config", config, "--db", str(db)]) == 0
    assert "30 approaches, 30 bin rows -> 3 rows over 3 keys (1 type, 1 class, 1 *)" in capsys.readouterr().out
    store = Store(db)
    assert {(r["key_kind"], r["key"], r["bin_nm"]) for r in store.fleet_stats_rows()} == {
        ("type", "B738", 3.0), ("class", "airline", 3.0), ("*", "*", 3.0)}
    days = store.coverage_daily_rows()
    assert [(r["n"], r["lowest_hat_p50"]) for r in days] == [(30, None)]  # these approaches have no points to cover
    store.close()


# --- reflag -------------------------------------------------------------------------------------------------------

def test_reflag_gives_a_stored_approach_its_deltas_its_flags_and_its_key(tmp_path):
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    aid = store_flight(store, run, cfg, t0=T0, gs_kt=150.0)
    assert store.approach(aid)["fleet_key"] is None and store.gates(aid)[0]["fleet_gs_delta_kt"] is None
    result = reflag(store, cfg, since=T0 - DAY_S, norms=norms_for(gs=130.0))
    assert (result.approaches, result.gates, result.keyed, result.flagged) == (1, 2, 1, 1)
    row = store.approach(aid)
    assert row["fleet_key"] == "type:B738" and Flags(row["flags"]) & Flags.FAST
    assert not Flags(row["flags"]) & Flags.SLOW
    gates = store.gates(aid)
    assert gates[0]["fleet_gs_delta_kt"] == 20.0 and abs(gates[0]["fleet_gp_delta_ft"]) < 1.0
    assert "1 approaches, " in result.text() and "2 gates gained a delta" in result.text()
    store.close()


def test_reflag_against_the_all_aircraft_norm_writes_the_delta_but_clears_fast(tmp_path):
    """Only a type or class norm may set FAST or SLOW (deliberately); a * norm still measures the delta, and reflag
    still stores it and the key, exactly as fleet_deltas does on the live path and at a splice."""
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    aid = store_flight(store, run, cfg, t0=T0, gs_kt=150.0, flags=Flags.FAST)   # a stale flag reflag must clear
    result = reflag(store, cfg, since=T0 - DAY_S, norms=norms_for(key_kind="*", key="*", gs=130.0))
    assert (result.approaches, result.gates, result.keyed) == (1, 2, 1)
    row = store.approach(aid)
    assert row["fleet_key"] == "*"
    assert not Flags(row["flags"]) & (Flags.FAST | Flags.SLOW)
    gates = store.gates(aid)
    assert gates[0]["fleet_gs_delta_kt"] == 20.0 and abs(gates[0]["fleet_gp_delta_ft"]) < 1.0
    store.close()


def test_reflag_leaves_outcomes_points_and_the_other_flags_alone(tmp_path):
    """PRESUMED_LANDED, STEEP, LATE_ESTABLISHED and EXTERNAL_TAIL are none of them a gate's: unlike UNSTABLE_1000,
    gate_flags never sets or clears them, so reflag's mask must never reach them."""
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    kept = Flags.PRESUMED_LANDED | Flags.STEEP | Flags.LATE_ESTABLISHED | Flags.EXTERNAL_TAIL
    aid = store_flight(store, run, cfg, t0=T0, outcome=Outcome.LOST_ON_FINAL, flags=kept)
    before = [tuple(p) for p in store.points(aid)]
    gates_before = [(g["gate_ft"], g["gs_corr"], g["gp_dev_ft"], g["along_nm"]) for g in store.gates(aid)]
    reflag(store, cfg, since=T0 - DAY_S, norms=norms_for(gs=130.0))
    row = store.approach(aid)
    assert row["outcome"] == "lost_on_final" and Flags(row["flags"]) & kept == kept
    assert [tuple(p) for p in store.points(aid)] == before
    assert [(g["gate_ft"], g["gs_corr"], g["gp_dev_ft"], g["along_nm"]) for g in store.gates(aid)] == gates_before
    store.close()


def test_reflag_keeps_an_emergency_latch_end_to_end(tmp_path):
    """The gate mask reflag rebuilds (GATE_FLAG_MASK) never reaches LATCH_MASK, and the store's own sticky rule on
    set_fleet backs that up."""
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    aid = store_flight(store, run, cfg, t0=T0, flags=Flags.EMERGENCY_GENERAL)
    reflag(store, cfg, since=T0 - DAY_S, norms=norms_for(gs=130.0))
    row = store.approach(aid)
    assert Flags.EMERGENCY_GENERAL in Flags(row["flags"]) and Flags.FAST in Flags(row["flags"])
    store.close()


def test_reflag_rewrites_the_bins_from_every_stored_point(tmp_path):
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    aid = store_flight(store, run, cfg, t0=T0, end_nm=2.0, tail_to_nm=0.2)
    store.conn.execute("DELETE FROM approach_bins WHERE approach_id = ? AND bin_nm < 2.0", (aid,))
    assert min(r["bin_nm"] for r in store.bins(aid)) == 2.0      # an approach binned before the splice was written
    result = reflag(store, cfg, since=T0 - DAY_S, norms=norms_for())
    assert min(r["bin_nm"] for r in store.bins(aid)) == 0.0 and result.bins == len(store.bins(aid))
    store.close()


def test_reflag_takes_a_flag_back_when_the_norms_no_longer_support_it(tmp_path):
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    aid = store_flight(store, run, cfg, t0=T0)
    reflag(store, cfg, since=T0 - DAY_S, norms=norms_for(gs=130.0))
    assert Flags(store.approach(aid)["flags"]) & Flags.FAST
    result = reflag(store, cfg, since=T0 - DAY_S, norms=FleetNorms(None, {}))
    row = store.approach(aid)
    assert not Flags(row["flags"]) & (Flags.FAST | Flags.SLOW) and row["fleet_key"] is None
    assert store.gates(aid)[0]["fleet_gs_delta_kt"] is None and (result.keyed, result.gates) == (0, 0)
    store.close()


# --- a gate's own verdict is rebuilt whole, not just its speed flags ------------------------------------------------

def test_reflag_clears_a_gates_unstable_mark_when_only_its_speed_flag_stops_firing(tmp_path):
    """A live close always pairs FAST or SLOW with UNSTABLE_1000/UNSTABLE_500 (scorer.gate_flags); the old reflag
    cleared FAST and SLOW but never the mark that came with them. A gate that is otherwise clean loses UNSTABLE_1000
    the moment the norm that made it FAST no longer does."""
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    stale = Flags.FAST | Flags.UNSTABLE_1000
    aid = store_flight(store, run, cfg, t0=T0, flags=stale)              # a clean track; only the stored flags are stale
    reflag(store, cfg, since=T0 - DAY_S, norms=FleetNorms(None, {}))     # no norm at all: no delta, so speed_flags is NONE
    flags = Flags(store.approach(aid)["flags"])
    assert not flags & (Flags.FAST | Flags.UNSTABLE_1000)
    store.close()


def test_reflag_keeps_a_gates_unstable_mark_for_a_non_speed_reason_while_clearing_a_stale_fast(tmp_path):
    """A gate bad from a steep sink (HIGH_SINK) alone stays UNSTABLE after reflag rebuilds it, exactly as
    scorer.gate_flags would compute fresh from a live close -- it is FAST and SLOW's own thresholds that may now
    clear, never a non-speed bad flag's."""
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    stale = Flags.HIGH_SINK | Flags.UNSTABLE_1000 | Flags.UNSTABLE_500 | Flags.FAST
    aid = store_flight(store, run, cfg, t0=T0, gs_kt=200.0, flags=stale)  # a steep sink at both gates, plus a stale FAST
    # key_kind="*": the all-aircraft norm may never set FAST or SLOW (by choice), which isolates the sink-driven
    # UNSTABLE from any speed-driven one.
    reflag(store, cfg, since=T0 - DAY_S, norms=norms_for(key_kind="*", key="*", gs=140.0))
    flags = Flags(store.approach(aid)["flags"])
    assert flags & Flags.HIGH_SINK and flags & Flags.UNSTABLE_1000 and flags & Flags.UNSTABLE_500
    assert not flags & (Flags.FAST | Flags.SLOW)
    store.close()


def test_reflag_sets_a_gates_unstable_mark_for_a_non_speed_reason_even_with_no_speed_flag(tmp_path):
    """The same gap closed from the other side: a gate whose only bad condition is HIGH_SINK must gain
    UNSTABLE_1000/UNSTABLE_500 on reflag even when no FAST or SLOW ever fires for it, since gate_flags pairs UNSTABLE
    with any of the four bad conditions, not only speed."""
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    aid = store_flight(store, run, cfg, t0=T0, gs_kt=200.0, flags=Flags.NONE)   # stored as if nothing had been noticed
    reflag(store, cfg, since=T0 - DAY_S, norms=norms_for(key_kind="*", key="*", gs=140.0))
    flags = Flags(store.approach(aid)["flags"])
    assert flags & Flags.HIGH_SINK and flags & Flags.UNSTABLE_1000 and flags & Flags.UNSTABLE_500
    assert not flags & (Flags.FAST | Flags.SLOW)
    store.close()


def test_a_second_reflag_changes_nothing(tmp_path):
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    aid = store_flight(store, run, cfg, t0=T0)
    reflag(store, cfg, since=T0 - DAY_S, norms=norms_for(gs=130.0))
    after = ([tuple(r) for r in store.bins(aid)], [tuple(g) for g in store.gates(aid)], tuple(store.approach(aid)))
    again = reflag(store, cfg, since=T0 - DAY_S, norms=norms_for(gs=130.0))
    assert ([tuple(r) for r in store.bins(aid)], [tuple(g) for g in store.gates(aid)],
            tuple(store.approach(aid))) == after
    assert again.flagged == 0 and again.keyed == 1
    store.close()


# --- reflag reads the same run kinds rebuild does -------------------------------------------------------------------

def test_reflag_skips_replay_approaches_by_default_and_touches_them_with_include_replays(tmp_path):
    """Store.approaches_to_reflag had no run-kind filter, so reflag judged a workstation's replayed approaches
    against the live norms and wrote FAST/SLOW onto them. It now takes the same --include-replays rule fleet.rebuild
    already reads its window by: out by default, in only when asked."""
    cfg = cfg_for(tmp_path)
    store, live = new_store(tmp_path)
    replay = store.new_run("replay", "chunks")
    live_aid = store_flight(store, live, cfg, t0=T0, gs_kt=150.0)
    replay_aid = store_flight(store, replay, cfg, t0=T0 + 600.0, gs_kt=150.0)
    norms = norms_for(gs=130.0)          # a type:B738 norm that makes both approaches' gates FAST
    result = reflag(store, cfg, since=T0 - DAY_S, norms=norms)
    assert result.approaches == 1 and Flags(store.approach(live_aid)["flags"]) & Flags.FAST
    assert store.approach(replay_aid)["fleet_key"] is None
    assert not Flags(store.approach(replay_aid)["flags"]) & Flags.FAST      # skipped: untouched by default
    again = reflag(store, cfg, since=T0 - DAY_S, norms=norms, include_replays=True)
    assert again.approaches == 2
    assert Flags(store.approach(replay_aid)["flags"]) & Flags.FAST and store.approach(replay_aid)["fleet_key"]
    store.close()


# --- reflag commits in groups --------------------------------------------------------------------------------------

def test_reflag_commits_once_per_group_and_once_more_for_the_remainder(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    total = REFLAG_COMMIT_EVERY * 2 + 5
    aids = [add_approach(store, run, ended_at=T0 - i, bins=((1.5, 10.0, 140.0),), gates=((1000, 1.5, 10.0, 140.0),))
           for i in range(total)]
    commits = {"n": 0}
    real_commit = Store.commit_batch

    def counting_commit(self):
        commits["n"] += 1
        real_commit(self)

    monkeypatch.setattr(Store, "commit_batch", counting_commit)
    result = reflag(store, cfg, since=T0 - DAY_S, norms=norms_for(gs=130.0))
    assert result.approaches == total
    assert commits["n"] == 3                                        # two full groups of REFLAG_COMMIT_EVERY, plus the 5 left over
    assert store.approach(aids[0])["fleet_key"] == "type:B738"       # the first group: committed
    assert store.approach(aids[-1])["fleet_key"] == "type:B738"      # the trailing partial group: also committed
    store.close()


def test_reflag_keeps_a_groups_earlier_approaches_when_one_units_write_fails(tmp_path, monkeypatch):
    """Decision: each approach's three writes are their own store.atomic() unit inside the group's shared batch, so
    a failing unit rolls back only its own approach -- its savepoint. The approaches already written earlier in the
    same still-open group are not part of that savepoint: reflag's own finally commits them, along with the rest of
    the batch so far, before the exception reaches the caller, so they are kept rather than lost with the group."""
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    aids = [add_approach(store, run, ended_at=T0 - i, bins=((1.5, 10.0, 140.0),), gates=((1000, 1.5, 10.0, 140.0),))
           for i in range(5)]
    boom_aid = aids[2]
    real_set_fleet = Store.set_fleet

    def set_fleet_boom(self, approach_id, *, flags, fleet_key):
        if approach_id == boom_aid:
            raise sqlite3.OperationalError("boom")
        real_set_fleet(self, approach_id, flags=flags, fleet_key=fleet_key)

    monkeypatch.setattr(Store, "set_fleet", set_fleet_boom)
    with pytest.raises(sqlite3.OperationalError, match="boom"):
        reflag(store, cfg, since=T0 - DAY_S, norms=norms_for(gs=130.0))
    assert store.approach(aids[0])["fleet_key"] == "type:B738"       # earlier in the still-open group: kept
    assert store.approach(aids[1])["fleet_key"] == "type:B738"
    assert store.approach(boom_aid)["fleet_key"] is None              # its own unit rolled back
    outside = sqlite3.connect(tmp_path / "fleet.sqlite", timeout=0, isolation_level=None)   # no wait: the lock must be free
    try:
        outside.execute("BEGIN IMMEDIATE")
        outside.execute("COMMIT")
    finally:
        outside.close()
    store.close()


def test_the_reflag_command_gives_the_stored_approaches_their_flags(tmp_path, capsys):
    db, config = cli_database(tmp_path), str(ROOT / "config" / "adsb-approach.example.toml")
    assert main(["fleet-rebuild", "--config", config, "--db", str(db)]) == 0
    assert main(["reflag", "--config", config, "--db", str(db)]) == 0
    assert "30 approaches, 0 bins rewritten, 30 gates gained a delta, 30 approaches keyed" in capsys.readouterr().out
    store = Store(db)
    row = store.approach(1)
    assert row["fleet_key"] == "type:B738" and Flags(row["flags"]) & Flags.FAST
    assert store.gates(1)[1]["fleet_gs_delta_kt"] is None        # the 500 ft gate's bin has no norm
    store.close()


def test_a_rebuild_keeps_the_coverage_days_that_fell_out_of_the_norm_window(tmp_path):
    """coverage_daily is the receiver's own coverage record over time, the number that should move when the antenna
    does. [fleet].window_days bounds the norms, not that history, so a rebuild whose window no longer reaches a day
    leaves the day's row exactly as it was instead of deleting it."""
    store, run = new_store(tmp_path)
    old_day, new_day = T0 - 10 * DAY_S, T0
    add_approach(store, run, ended_at=old_day, bins=((1.5, 10.0, 140.0),))
    add_approach(store, run, ended_at=new_day, bins=((1.5, 12.0, 142.0),))
    wide = rebuild(store, cfg_for(tmp_path, window_days=30), now=T0 + 60.0)
    assert wide.coverage_written == 2
    before = {r["day"]: tuple(r) for r in store.coverage_daily_rows()}
    assert len(before) == 2

    narrow = rebuild(store, cfg_for(tmp_path, window_days=2), now=T0 + 60.0)
    after = {r["day"]: tuple(r) for r in store.coverage_daily_rows()}
    assert sorted(after) == sorted(before)                       # the day outside the window is still there
    assert after[local_day(old_day, LA)] == before[local_day(old_day, LA)]   # untouched, not rebuilt from a shorter window
    assert narrow.coverage_written == 0 and narrow.coverage_kept == 2   # neither read again, neither thrown away
    store.close()


class _BinRow:
    """A norm_bin_rows row that can be weak-referenced, so a test can see whether the rebuild still holds it."""
    __slots__ = ("values", "__weakref__")

    def __init__(self, **values):
        self.values = values

    def __getitem__(self, name):
        return self.values[name]


WINDOW_BINS, WINDOW_PER_BIN = 16, 4


def test_a_rebuild_reads_the_window_a_bin_at_a_time_and_never_holds_it(tmp_path, monkeypatch):
    """The rebuild finishes each group before starting the next, so it holds one group's values, not the window's rows.
    Handing the cursor straight to fleet_rows is what makes that true — a list() of it keeps every row alive, which on a
    live database's 30-day window of about 260,000 rows is roughly 105 MB an hour."""
    cfg = cfg_for(tmp_path, min_samples=1)
    store, run = new_store(tmp_path)
    add_approach(store, run, bins=((1.5, 10.0, 140.0),))
    live: weakref.WeakSet = weakref.WeakSet()
    still_held = []

    def window(**kwargs):
        """The window a bin at a time, holding no reference of its own, counting what is alive at the last row."""
        total = WINDOW_BINS * WINDOW_PER_BIN
        for i in range(total):
            row = _BinRow(airport="KSFO", runway="28R", type_code="B738", traffic_class="airline",
                          bin_nm=(i // WINDOW_PER_BIN) / 2, gp_dev_mean_ft=10.0 + i, gs_corr_mean=140.0 + i)
            live.add(row)
            if i == total - 1:
                gc.collect()
                still_held.append(len(live))
            yield row
            del row

    monkeypatch.setattr(type(store), "norm_bin_rows", lambda self, **kw: window(**kw))
    result = rebuild(store, cfg, now=T0 + 60.0)     # the window ends just after the approach, as in every test here
    assert result.bin_rows == WINDOW_BINS * WINDOW_PER_BIN       # every row still counted for the log line
    assert still_held[0] <= WINDOW_PER_BIN                       # one group's worth at most, never the window's 64
    assert result.rows == WINDOW_BINS * 3                        # and the rows came out whole: type, class and *
    store.close()


# --- a coverage day is a local day ---------------------------------------------------------------------------------

# 2026-09-13 05:00:00Z is 22:00 on 2026-09-12 in America/Los_Angeles, and 08:00:00Z is 01:00 on the 13th: one UTC day
# holds both, two local days hold one each. T0 itself (19:06:40Z) is 12:06 on the 12th in both.
EVENING_Z, MORNING_Z = 1789275600.0, 1789286400.0


def test_a_day_is_the_configured_zones_day_not_utc():
    assert local_day(T0, LA) == local_day(T0, timezone.utc) == "2026-09-12"
    assert local_day(EVENING_Z, timezone.utc) == "2026-09-13" and local_day(EVENING_Z, LA) == "2026-09-12"
    assert local_day(MORNING_Z, timezone.utc) == local_day(MORNING_Z, LA) == "2026-09-13"
    assert local_day(EVENING_Z, ZoneInfo("Europe/London")) == "2026-09-13"


def test_a_days_bounds_hold_its_own_times_and_nothing_of_the_next_day():
    start, end = day_bounds("2026-09-12", LA)
    assert start == 1789196400.0 and end == 1789282800.0          # 07:00Z to 07:00Z: PDT is UTC−7
    assert start <= T0 < end and start <= EVENING_Z < end and not (start <= MORNING_Z < end)
    assert local_day(start, LA) == "2026-09-12" and local_day(end - 1.0, LA) == "2026-09-12"
    assert local_day(end, LA) == "2026-09-13"


def test_the_two_days_a_year_that_are_not_24_hours_long_are_exactly_as_long_as_they_were_lived():
    """DST makes two local days a year 23 or 25 hours long, and that is accepted as it is: so the bounds come from the
    calendar, never from adding 86,400 s. Getting this wrong drops or double-counts an hour of approaches twice a
    year."""
    spring = day_bounds("2026-03-08", LA)                          # clocks forward at 02:00: 23 hours
    autumn = day_bounds("2026-11-01", LA)                          # clocks back at 02:00: 25 hours
    assert spring[1] - spring[0] == 23 * 3600.0
    assert autumn[1] - autumn[0] == 25 * 3600.0
    assert day_bounds("2026-11-02", LA)[0] == autumn[1]            # and the days still join, with no hour lost
    assert day_bounds("2026-09-12", timezone.utc) == (1789171200.0, 1789257600.0)


def test_an_approach_either_side_of_local_midnight_falls_in_its_own_local_day(tmp_path):
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    store_flight(store, run, cfg, t0=EVENING_Z - 180.0)             # ends at 22:00 on the 12th, local
    store_flight(store, run, cfg, t0=MORNING_Z - 180.0)             # ends at 01:00 on the 13th, local
    counts = store.coverage_day_counts(since=T0, day_of=lambda t: local_day(t, LA))
    assert [(c["day"], c["n"]) for c in counts] == [("2026-09-12", 1), ("2026-09-13", 1)]
    utc = store.coverage_day_counts(since=T0, day_of=lambda t: local_day(t, timezone.utc))
    assert [(c["day"], c["n"]) for c in utc] == [("2026-09-13", 2)]          # one UTC day held both
    rows, keep = coverage_rows(store, cfg, scorer_for(cfg), since=T0, include_replays=False, stored={})
    assert sorted(r["day"] for r in rows) == ["2026-09-12", "2026-09-13"]
    assert keep == {("KSFO", "28R", "2026-09-12"), ("KSFO", "28R", "2026-09-13")}
    assert [r["n"] for r in rows] == [1, 1]
    store.close()


# --- the approaches that needed the backfill -----------------------------------------------------------------------

def test_the_backfill_count_agrees_with_the_points_a_splice_stored(tmp_path):
    """The backfill count is the approaches whose stored track carries an adsb.lol point, and it comes from the flags
    external.py sets when it stores them rather than from a scan of approach_points. This writes both together and
    checks the two say the same thing."""
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    store_flight(store, run, cfg, t0=T0, end_nm=2.0, tail_to_nm=0.2, flags=Flags.EXTERNAL_TAIL)
    store_flight(store, run, cfg, t0=T0 + 600.0)                                    # the receiver saw it to the end
    store_flight(store, run, cfg, t0=T0 + 1200.0, end_nm=2.0, tail_to_nm=0.2,
                 flags=Flags.EXTERNAL_TAIL | Flags.EXTERNAL_OUTCOME)
    store_flight(store, run, cfg, t0=T0 + 1800.0, flags=Flags.LATE_ESTABLISHED)     # a flag about the flying, not the points
    counts = store.coverage_day_counts(since=T0 - DAY_S, day_of=lambda t: local_day(t, LA))
    assert [(c["n"], c["backfill_n"]) for c in counts] == [(4, 2)]
    with_points = {r[0] for r in store.conn.execute(
        "SELECT DISTINCT approach_id FROM approach_points WHERE origin = 'adsb.lol'")}
    flagged = {r[0] for r in store.conn.execute("SELECT id FROM approaches WHERE (flags & ?) != 0",
                                                (int(BACKFILL_FLAGS),))}
    assert flagged == with_points and len(flagged) == 2
    store.close()


# --- the share written, and the freshness rule that carries it -----------------------------------------------------

def test_a_days_row_carries_the_backfill_count_and_the_share_it_is_of(tmp_path):
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    store_flight(store, run, cfg, t0=T0, end_nm=2.0, tail_to_nm=0.2, flags=Flags.EXTERNAL_TAIL)
    store_flight(store, run, cfg, t0=T0 + 600.0, flags=Flags.EXTERNAL_GAPS)
    store_flight(store, run, cfg, t0=T0 + 1200.0)
    store_flight(store, run, cfg, t0=T0 + 1800.0)
    rows, _ = coverage_rows(store, cfg, scorer_for(cfg), since=T0 - DAY_S, include_replays=False, stored={})
    assert [(r["n"], r["backfill_n"], r["backfill_frac"]) for r in rows] == [(4, 2, 0.5)]
    assert rows[0]["gate500_observed_frac"] == 0.75            # and the coverage itself is still receiver-only
    store.close()


def test_a_days_share_counts_the_whole_day_even_when_the_window_opens_inside_it(tmp_path):
    """`since` lands in the middle of a local day, and that day's earlier approaches are read and scored all the same --
    coverage_approaches takes the day's bounds, not `since`. So the day's backfill count has to cover the whole day too,
    or backfill_frac is a whole-day n under a part-day count: understated, and for good, since the day is kept as it
    stands once it falls out of the window."""
    cfg = cfg_for(tmp_path)
    store, run = new_store(tmp_path)
    store_flight(store, run, cfg, t0=T0 - 3600.0, end_nm=2.0, tail_to_nm=0.2, flags=Flags.EXTERNAL_TAIL)
    store_flight(store, run, cfg, t0=T0 + 600.0, end_nm=2.0, tail_to_nm=0.2, flags=Flags.EXTERNAL_TAIL)
    rows, keep = coverage_rows(store, cfg, scorer_for(cfg), since=T0, include_replays=False, stored={})
    assert [(r["day"], r["n"], r["backfill_n"], r["backfill_frac"]) for r in rows] == [("2026-09-12", 2, 2, 1.0)]
    assert keep == {("KSFO", "28R", "2026-09-12")}
    store.close()


def test_a_day_with_no_approaches_of_its_own_writes_no_share(tmp_path):
    cfg = cfg_for(tmp_path)
    store, _ = new_store(tmp_path)
    assert coverage_rows(store, cfg, scorer_for(cfg), since=T0 - DAY_S, include_replays=False, stored={}) == ([], set())
    row = coverage_row([], cfg.detection.gates_ft, backfill_n=0)
    assert (row["n"], row["backfill_n"], row["backfill_frac"]) == (0, 0, None)     # no share of nothing
    assert row["gate1000_observed_frac"] is None and row["lowest_hat_p50"] is None
    store.close()


def test_a_replays_approaches_never_join_the_coverage_a_rebuild_writes(tmp_path):
    """coverage_daily is the receiver's own record of what it saw, so a replay dropped into a live database stays out of
    it -- while the coverage page's outcome charts do count it."""
    cfg = cfg_for(tmp_path, min_samples=1)
    store, live = new_store(tmp_path)
    replay = store.new_run("replay", "chunks")
    store_flight(store, live, cfg, t0=T0)
    store_flight(store, replay, cfg, t0=T0 + 600.0)
    rebuild(store, cfg, now=T0 + 3600.0)
    assert [(r["day"], r["n"]) for r in store.coverage_daily_rows()] == [("2026-09-12", 1)]
    store.close()


def test_a_splice_into_a_day_that_is_over_is_picked_up_by_the_next_rebuild(tmp_path):
    """The collision this task exists for, end to end: a lookup resolves minutes after an approach closed and the
    day's approach count never moves again."""
    cfg = cfg_for(tmp_path, min_samples=1)
    store, run = new_store(tmp_path)
    aid = store_flight(store, run, cfg, t0=T0, end_nm=2.0)
    store_flight(store, run, cfg, t0=T0 + 600.0)
    first = rebuild(store, cfg, now=T0 + 3600.0)
    assert first.coverage_written == 1
    assert [(r["n"], r["backfill_n"], r["backfill_frac"]) for r in store.coverage_daily_rows()] == [(2, 0, 0.0)]

    store.conn.execute("UPDATE approaches SET flags = flags | ? WHERE id = ?", (int(Flags.EXTERNAL_TAIL), aid))
    again = rebuild(store, cfg, now=T0 + 7200.0)
    assert (again.coverage_written, again.coverage_kept) == (1, 0)
    assert [(r["n"], r["backfill_n"], r["backfill_frac"]) for r in store.coverage_daily_rows()] == [(2, 1, 0.5)]

    third = rebuild(store, cfg, now=T0 + 10800.0)              # nothing moved: the day is kept as it stands
    assert (third.coverage_written, third.coverage_kept) == (0, 1)
    assert [(r["n"], r["backfill_n"]) for r in store.coverage_daily_rows()] == [(2, 1)]
    store.close()

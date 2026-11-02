"""The reads behind GET /api/stats: the coverage days of a span, the daily outcome counts, and the norms in force."""
from __future__ import annotations

import time
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from adsb_approach.config import CoverageEvent, load_config
from adsb_approach.fleet import day_bounds, local_day
from adsb_approach.model import Flags, Outcome
from adsb_approach.store.db import Store
from adsb_approach.web.app import create_app
from adsb_approach.web.db import snapshot
from adsb_approach.web.stats import MAX_DAYS, coverage_days, norms_summary, outcome_days
from tests.conftest import ROOT, T0
from tests.fleetdb import stats_row

LA = ZoneInfo("America/Los_Angeles")
EVENING_Z, MORNING_Z = 1789275600.0, 1789286400.0      # 22:00 on 2026-09-12 and 01:00 on the 13th, in Los Angeles


def coverage(day: str, *, airport: str = "KSFO", runway: str = "28R", n: int = 10, p50: float = 800.0,
             backfill_n: int = 2) -> dict:
    return dict(airport=airport, runway=runway, day=day, n=n, lowest_hat_p25=p50 - 100.0, lowest_hat_p50=p50,
                lowest_hat_p75=p50 + 100.0, closest_nm_p50=2.5, gate1000_observed_frac=0.6,
                gate500_observed_frac=0.0, backfill_n=backfill_n, backfill_frac=backfill_n / n)


def add_attempt(store: Store, run: int, *, airport: str = "KSFO", outcome: Outcome = Outcome.LANDED,
                ended_at: float = T0, flags: Flags = Flags.NONE, close: bool = True) -> int:
    aid = store.open_approach(run_id=run, flight_id=None, arrival_id=None, attempt_no=1, hex="a44540",
                              callsign="UAL1", type_code="B738", traffic_class="airline", approach_cat="C",
                              airport=airport, runway="28R", established_at=ended_at - 300.0, first_along_nm=8.0,
                              weather_id=None, reference_source="cifp:test", prev_approach_id=None)
    if close:
        store.close_approach(aid, outcome=outcome, flags=flags, ended_at=ended_at, summary={}, gates=[], points=[],
                             bins=[], ga=None)
    return aid


@pytest.fixture
def db(tmp_path):
    """A database holding what the endpoint reads: coverage days over three days, two runways and two airports;
    norms of each kind; and closed approaches either side of local midnight."""
    store = Store(tmp_path / "stats.sqlite")
    store.migrate()
    run = store.new_run("live", "test")
    days = [coverage("2026-09-10"), coverage("2026-09-11", n=8, p50=900.0, backfill_n=4),
            coverage("2026-09-12", n=12, p50=700.0, backfill_n=1),
            coverage("2026-09-12", runway="28L", n=6), coverage("2026-09-12", airport="KOAK", runway="30", n=4)]
    norms = [stats_row(key_kind="type", key="B738"), stats_row(key_kind="type", key="A20N"),
             stats_row(key_kind="type", key="B738", runway="28L"), stats_row(key_kind="class", key="airline"),
             stats_row(key_kind="*", key="*")]
    store.replace_norms(norms, days, {(d["airport"], d["runway"], d["day"]) for d in days}, built_at=T0)
    add_attempt(store, run, ended_at=T0)
    add_attempt(store, run, ended_at=T0 + 60.0, outcome=Outcome.LOST)
    add_attempt(store, run, ended_at=T0 + 120.0, airport="KOAK")
    add_attempt(store, run, ended_at=EVENING_Z, outcome=Outcome.GO_AROUND)      # 22:00 on the 12th, local
    add_attempt(store, run, ended_at=MORNING_Z, outcome=Outcome.LOST)           # 01:00 on the 13th, local
    add_attempt(store, run, ended_at=T0, outcome=Outcome.LOST, flags=Flags.INTERRUPTED)
    add_attempt(store, run, ended_at=T0, close=False)
    store.close()
    return tmp_path / "stats.sqlite"


def test_the_coverage_days_of_a_span_come_back_whole_and_in_order(db):
    with snapshot(db) as conn:
        rows = coverage_days(conn, since_day="2026-09-11")
    assert [(r["airport"], r["runway"], r["day"]) for r in rows] == [
        ("KOAK", "30", "2026-09-12"), ("KSFO", "28L", "2026-09-12"),
        ("KSFO", "28R", "2026-09-11"), ("KSFO", "28R", "2026-09-12")]
    twelfth = rows[-1]
    assert (twelfth["n"], twelfth["lowest_hat_p50"], twelfth["closest_nm_p50"]) == (12, 700.0, 2.5)
    assert (twelfth["lowest_hat_p25"], twelfth["lowest_hat_p75"]) == (600.0, 800.0)
    assert (twelfth["gate1000_observed_frac"], twelfth["gate500_observed_frac"]) == (0.6, 0.0)
    assert (twelfth["backfill_n"], twelfth["backfill_frac"]) == (1, 1 / 12)
    assert "built_at" not in twelfth                         # the page draws days, not when they were computed


def test_the_whole_history_comes_back_when_no_day_bounds_it(db):
    with snapshot(db) as conn:
        assert len(coverage_days(conn, since_day=None)) == 5
        assert coverage_days(conn, since_day="2026-09-13") == []


def test_the_outcomes_are_counted_per_airport_per_local_day(db):
    """They are counted, never scored again. An attempt still open has no outcome, and one the startup sweep closed as
    INTERRUPTED is left out, as it is from coverage_daily."""
    with snapshot(db) as conn:
        rows = outcome_days(conn, since=None, day_of=lambda t: local_day(t, LA))
    assert [(r["airport"], r["day"], r["outcome"], r["n"]) for r in rows] == [
        ("KOAK", "2026-09-12", "landed", 1),
        ("KSFO", "2026-09-12", "go_around", 1), ("KSFO", "2026-09-12", "landed", 1), ("KSFO", "2026-09-12", "lost", 1),
        ("KSFO", "2026-09-13", "lost", 1)]


def test_the_outcomes_are_bounded_by_the_spans_first_moment(db):
    with snapshot(db) as conn:
        rows = outcome_days(conn, since=EVENING_Z, day_of=lambda t: local_day(t, LA))
    assert [(r["day"], r["outcome"], r["n"]) for r in rows] == [("2026-09-12", "go_around", 1), ("2026-09-13", "lost", 1)]


def test_a_replay_runs_attempts_are_counted_in_the_outcomes(tmp_path):
    """The page is descriptive, so every run's attempts are counted, live or replay -- otherwise a database of replays
    on a workstation draws an empty chart beside a full arrivals log. Coverage is the other way about, and test_fleet.py
    holds that half."""
    store = Store(tmp_path / "replays.sqlite")
    store.migrate()
    live, replay = store.new_run("live", "test"), store.new_run("replay", "chunks")
    add_attempt(store, live, ended_at=T0)
    add_attempt(store, replay, ended_at=T0 + 60.0, outcome=Outcome.LOST)
    store.close()
    with snapshot(tmp_path / "replays.sqlite") as conn:
        rows = outcome_days(conn, since=None, day_of=lambda t: local_day(t, LA))
    assert [(r["outcome"], r["n"]) for r in rows] == [("landed", 1), ("lost", 1)]


def test_the_norms_summary_counts_the_keys_of_each_kind_and_when_they_were_built(db, tmp_path):
    with snapshot(db) as conn:
        assert norms_summary(conn) == {"built_at": T0, "keys": {"type": 3, "class": 1, "*": 1}}
    empty = Store(tmp_path / "empty.sqlite")
    empty.migrate()
    empty.close()
    with snapshot(tmp_path / "empty.sqlite") as conn:
        assert norms_summary(conn) == {"built_at": None, "keys": {"type": 0, "class": 0, "*": 0}}


# --- GET /api/stats ------------------------------------------------------------------------------------------------

SPAN_DAYS = 40          # days of coverage in the endpoint's database, more than the default span


@pytest.fixture
def client(tmp_path):
    """The endpoint over a database whose days run back from today in the configured zone, so the span is tested
    against the same clock the endpoint reads. Two marks, and one approach today and one three days ago."""
    now = time.time()
    today = date.fromisoformat(local_day(now, LA))
    store = Store(tmp_path / "api.sqlite")
    store.migrate()
    run = store.new_run("live", "test")
    days = [coverage((today - timedelta(days=k)).isoformat(), n=10 + k, backfill_n=k % 5) for k in range(SPAN_DAYS)]
    store.replace_norms([stats_row(key_kind="type", key="B738")], days,
                        {(d["airport"], d["runway"], d["day"]) for d in days}, built_at=now - 600.0)
    # Just after each local midnight, so which day an attempt belongs to never depends on the hour the test runs.
    add_attempt(store, run, ended_at=day_bounds(today.isoformat(), LA)[0] + 1.0)
    add_attempt(store, run, ended_at=day_bounds((today - timedelta(days=3)).isoformat(), LA)[0] + 1.0,
                outcome=Outcome.GO_AROUND)
    store.close()
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.coverage.events = (CoverageEvent(at=(today - timedelta(days=20)).isoformat(), label="LNA in line"),
                           CoverageEvent(at=(today - timedelta(days=4)).isoformat(), label="new antenna"))
    return TestClient(create_app(cfg, tmp_path / "api.sqlite")), today


def test_the_default_span_is_thirty_local_days_counting_today(client):
    api, today = client
    body = api.get("/api/stats").json()
    assert body["days"] == 30 and body["until_day"] == today.isoformat()
    assert body["since_day"] == (today - timedelta(days=29)).isoformat()
    assert body["timezone"] == "America/Los_Angeles"
    assert len(body["coverage"]) == 30
    assert [r["day"] for r in body["coverage"]][:2] == [body["since_day"], (today - timedelta(days=28)).isoformat()]
    assert body["coverage"][-1]["day"] == today.isoformat()


def test_a_span_of_all_reaches_every_stored_day_and_one_day_reaches_only_today(client):
    api, today = client
    every = api.get("/api/stats", params={"days": "all"}).json()
    assert every["days"] is None and every["since_day"] is None and len(every["coverage"]) == SPAN_DAYS
    one = api.get("/api/stats", params={"days": 1}).json()
    assert one["days"] == 1 and one["since_day"] == one["until_day"] == today.isoformat()
    assert [r["day"] for r in one["coverage"]] == [today.isoformat()]
    assert [(r["outcome"], r["n"]) for r in one["outcomes"]] == [("landed", 1)]      # the go-around is three days back
    assert [(r["outcome"], r["n"]) for r in every["outcomes"]] == [("go_around", 1), ("landed", 1)]


def test_the_payload_carries_the_marks_and_the_norms_in_force(client):
    api, today = client
    body = api.get("/api/stats").json()
    assert body["marks"] == [{"at": (today - timedelta(days=20)).isoformat(), "label": "LNA in line"},
                             {"at": (today - timedelta(days=4)).isoformat(), "label": "new antenna"}]
    assert body["norms"]["keys"] == {"type": 1, "class": 0, "*": 0}
    assert body["norms"]["built_at"] is not None
    assert set(body) == {"days", "since_day", "until_day", "timezone", "coverage", "outcomes", "marks", "norms"}


def test_a_coverage_row_carries_every_field_the_page_draws(client):
    api, today = client
    row = api.get("/api/stats", params={"days": 1}).json()["coverage"][0]
    assert set(row) == {"airport", "runway", "day", "n", "lowest_hat_p25", "lowest_hat_p50", "lowest_hat_p75",
                        "closest_nm_p50", "gate1000_observed_frac", "gate500_observed_frac", "backfill_n",
                        "backfill_frac"}
    assert (row["airport"], row["runway"], row["n"], row["backfill_n"]) == ("KSFO", "28R", 10, 0)


@pytest.mark.parametrize("query, message", [
    ("days=0", "days: '0' is not a whole number of days"),
    ("days=-1", "days: '-1' is not a whole number of days"),
    ("days=abc", "days: 'abc' is not a whole number of days"),
    ("days=1.5", "days: '1.5' is not a whole number of days"),
    (f"days={MAX_DAYS + 1}", f"days: '{MAX_DAYS + 1}' is not a whole number of days"),
    ("window=7d", "unknown parameter 'window'"),
    ("since=100", "unknown parameter 'since'"),
])
def test_a_bad_span_is_a_400_that_names_it(client, query, message):
    api, _ = client
    r = api.get(f"/api/stats?{query}")
    assert r.status_code == 400 and message in r.json()["detail"]


def test_an_empty_database_answers_with_empty_lists(tmp_path, example_cfg):
    """What the page gets between the migration and the first rebuild: the table has just been emptied."""
    store = Store(tmp_path / "fresh.sqlite")
    store.migrate()
    store.close()
    body = TestClient(create_app(example_cfg, tmp_path / "fresh.sqlite")).get("/api/stats").json()
    assert body["coverage"] == [] and body["outcomes"] == [] and body["marks"] == []
    assert body["norms"] == {"built_at": None, "keys": {"type": 0, "class": 0, "*": 0}}
    assert body["days"] == 30 and body["until_day"] == local_day(time.time(), LA)

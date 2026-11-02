import sqlite3

import pytest
from fastapi.testclient import TestClient

from adsb_approach.config import load_config
from adsb_approach.model import Outcome
from adsb_approach.web.app import create_app
from tests.conftest import ROOT
from tests.webdb import LiveWriter, copy_db

SIDESTEPS = {"a7ccce", "ab0f89", "a4b5b6", "ac2706", "a4dfd2"}


@pytest.fixture(scope="module")
def client(replay_db, example_cfg):
    return TestClient(create_app(example_cfg, replay_db))        # not entered: no lifespan, so no poller, for plain requests


def approach_id(db, hexid, attempt_no):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT id FROM approaches WHERE hex = ? AND attempt_no = ?", (hexid, attempt_no)).fetchone()[0]
    finally:
        conn.close()


def test_config_lists_the_airports_and_the_names_the_filters_use(client, example_cfg):
    body = client.get("/api/config").json()
    sfo = next(a for a in body["airports"] if a["icao"] == "KSFO")
    assert {"28L", "28R"} <= {r["ident"] for r in sfo["runways"]}
    assert "runway_change" in body["outcomes"] and "SIDESTEP" in body["flags"] and "NONE" not in body["flags"]
    # LATCH_MASK's five flags join ATTENTION_FLAGS, in Flags' own declaration order (last, after EXTERNAL_GAPS) --
    # app.py builds this list by iterating Flags itself, so growing the mask is enough here.
    assert body["attention"] == {"outcomes": ["go_around", "touch_and_go", "abandoned"],
                                 "flags": ["FAST", "SLOW", "UNSTABLE_1000", "UNSTABLE_500", "STEEP", "SHALLOW",
                                           "LATE_ESTABLISHED", "EMERGENCY_GENERAL", "EMERGENCY_NORDO",
                                           "EMERGENCY_UNLAWFUL", "EMERGENCY_DOWNED", "MINIMUM_FUEL"]}
    # The client filters live SSE events for GA client-side (applyLiveEvent), since the event stream, unlike /api/live,
    # is not filtered server-side -- so it needs the configured list, the same way it already gets the airports and
    # runways it filters by.
    assert body["ga_types"] == sorted(example_cfg.type_classes.ga) and "C172" in body["ga_types"]


def test_the_arrivals_and_live_apis_apply_the_configured_ga_filter(tmp_path, replay_db, example_cfg):
    """ArrivalFilter.parse() and live_cards() have no access to Config, so app.py's own handlers are the seam that must
    carry cfg.type_classes.ga through to both -- covered end to end here, not just at the arrivals.py/live.py unit level
    above."""
    db = copy_db(replay_db, tmp_path / "app.sqlite")
    w = LiveWriter(db)
    ga_type = sorted(example_cfg.type_classes.ga)[0]
    try:
        w.open("aaa060", "N0152AB", type_code=ga_type, traffic_class="other", along_nm=6.0, hat_ft=1900)   # left open, for /api/live
        landed = w.open("aaa061", "N0152AC", type_code=ga_type, traffic_class="other", runway="28R", along_nm=5.0,
                        hat_ft=1500, dt=1)
        w.close_attempt(landed, "aaa061", Outcome.LANDED, dt=61)                                           # closed, for /api/arrivals
    finally:
        w.close()
    c = TestClient(create_app(example_cfg, db))
    assert "aaa060" not in {r["hex"] for r in c.get("/api/live").json()["cards"]}
    assert "aaa060" in {r["hex"] for r in c.get("/api/live", params={"ga": "1"}).json()["cards"]}
    hidden = c.get("/api/arrivals", params={"limit": 200}).json()["arrivals"]
    shown = c.get("/api/arrivals", params={"ga": "1", "limit": 200}).json()["arrivals"]
    assert "aaa061" not in {a["hex"] for a in hidden} and "aaa061" in {a["hex"] for a in shown}


def test_arrivals_are_filtered_and_paged_by_cursor(client):
    first = client.get("/api/arrivals", params={"outcome": "runway_change", "limit": 3}).json()
    assert len(first["arrivals"]) == 3 and first["next_cursor"]
    rest = client.get("/api/arrivals", params={"outcome": "runway_change", "limit": 3, "cursor": first["next_cursor"]}).json()
    assert rest["next_cursor"] is None
    assert {a["hex"] for a in first["arrivals"] + rest["arrivals"]} == SIDESTEPS


@pytest.mark.parametrize("query, message", [("outcome=crashed", "unknown outcome"), ("limit=500", "limit"),
                                            ("cursor=x", "cursor")])
def test_a_bad_arrivals_query_is_a_400_that_says_why(client, query, message):
    r = client.get(f"/api/arrivals?{query}")
    assert r.status_code == 400 and message in r.json()["detail"]


def test_approach_detail_links_to_tar1090_on_the_pages_host(client, replay_db):
    body = client.get(f"/api/approaches/{approach_id(replay_db, 'a44540', 1)}").json()
    assert body["approach"]["outcome"] == "go_around" and body["points"]
    assert body["tar1090_url"].startswith("http://testserver/tar1090/?icao=a44540&showTrace=2026-09-12&")
    assert client.get("/api/approaches/999999999").status_code == 404


def test_a_tar1090_url_set_in_the_environment_is_used_as_it_is(replay_db):
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml",
                      env={"ADSB_APPROACH_TAR1090_URL": "http://receiver.example/tar1090/"})
    body = TestClient(create_app(cfg, replay_db)).get(f"/api/approaches/{approach_id(replay_db, 'a44540', 1)}").json()
    assert body["tar1090_url"].startswith("http://receiver.example/tar1090/?icao=a44540&")


def test_live_and_health_on_a_replayed_database(client):
    live = client.get("/api/live").json()
    assert live["cards"] == [] and live["last_event_id"] > 0
    health = client.get("/api/health").json()
    assert health["collector"]["state"] == "none" and health["db"]["ok"] is True
    assert health["stream"] == {"clients": 0, "error": None, "last_poll_at": None}


def test_an_unreadable_database_is_a_503_and_health_says_why(tmp_path, example_cfg):
    c = TestClient(create_app(example_cfg, tmp_path / "missing.sqlite"))
    r = c.get("/api/live")
    assert r.status_code == 503 and r.json()["detail"].startswith("database unavailable: ")
    health = c.get("/api/health")
    assert health.status_code == 200 and health.json()["status"] == "error"


def test_the_page_and_its_files_are_revalidated_on_every_load(client):
    for path in ("/", "/static/index.html"):
        r = client.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert r.headers["cache-control"] == "no-cache"


def test_a_bad_last_event_id_is_a_400(client):
    assert client.get("/api/events", headers={"Last-Event-ID": "yesterday"}).status_code == 400


def test_health_carries_the_poller_as_configured(client):
    assert client.get("/api/health").json()["external"] == {
        "enabled": False, "watching": 0, "last_success_at": None, "paused_until": None, "paused_status": None,
        "paused_error": None, "failures_last_hour": 0}

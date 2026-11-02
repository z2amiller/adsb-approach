"""GET /api/search and /search: the search rows, with a grey row's track read back from tests/fixtures/tracks, which
holds real readsb files for the three flights below."""
from __future__ import annotations

import copy
import gzip
import json
import logging
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adsb_approach import tracks
from adsb_approach.model import Outcome, Position
from adsb_approach.web.app import create_app
from tests.traces import trace_body
from tests.webdb import LiveWriter, copy_db

FIX = Path(__file__).parent / "fixtures" / "tracks"
MIDNIGHT = 1792713600.0         # 2026-10-23 00:00:00Z

FLIGHTS = [       # callsign, hex, registration, first_seen, last_seen: the sessions tests/test_tracks.py reads
    ("VIR19Z", "4077d3", "G-VTEA", 1792275517.54, 1792276598.399),
    ("N00210", "0010fd", "N00210", 1792199408.388, 1792204091.658),
    ("SWA4020", "ac7686", "N9019R", 1792708559.048, 1792714429.195),
]


def point(t: float, alt: int = 3000) -> Position:
    return Position(t=t, hex="c0ffee", lat=37.5, lon=-122.2, alt_baro=alt, on_ground=False, alt_geom=None, gs=120.0,
                    track=90.0, baro_rate=None, geom_rate=None, callsign=None, squawk=None, category=None,
                    nav_qnh=None, nav_altitude_mcp=None, nav_modes=frozenset(), source="adsb_icao")


def write_trace(root: Path, relative: str, hexid: str, positions: list[Position]) -> None:
    path = root / relative / f"trace_full_{hexid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(trace_body(hexid, positions))


@pytest.fixture
def make_client(tmp_path, replay_db, example_cfg):
    """A factory for a client on a copy of the replayed database holding the sessions of FLIGHTS and any `extra`
    ((callsign, hex, first, last) rows). It reads tracks from `globe`, and live traces from `live`, which is a new
    empty directory unless given, and "" for none. The session-scoped example_cfg is copied, never changed."""
    def make(*, globe: Path = FIX, live: Path | str | None = None, extra=()) -> TestClient:
        cfg = copy.deepcopy(example_cfg)
        cfg.service.globe_history_dir = str(globe)
        cfg.service.live_traces_dir = str(tmp_path / "live" if live is None else live)
        cfg.service.nearby_airports_path = str(FIX / "places.csv")
        (tmp_path / "live").mkdir(exist_ok=True)
        db = copy_db(replay_db, tmp_path / "search.sqlite")
        w = LiveWriter(db)
        try:
            for callsign, hexid, reg, first, last in FLIGHTS:
                w.flight_row(hexid, callsign, first_seen=first, last_seen=last, reg=reg)
            for callsign, hexid, first, last in extra:
                w.flight_row(hexid, callsign, first_seen=first, last_seen=last)
        finally:
            w.close()
        return TestClient(create_app(cfg, db))
    return make


@pytest.fixture
def client(make_client):
    return make_client()


def only_row(client, text: str) -> dict:
    r = client.get("/api/search", params={"q": text})
    assert r.status_code == 200, r.text
    (row,) = r.json()["rows"]
    return row


def test_a_missed_arrival_is_a_grey_row_with_links(client):
    row = only_row(client, "VIR19Z")
    assert (row["kind"], row["registration"], row["hex"], row["callsign"]) == ("flight", "G-VTEA", "4077d3", "VIR19Z")
    assert row["label"] == "arrival KSFO" and row["label_airport"] == "KSFO"
    assert row["no_approach_logged"] is True
    assert "icao=4077d3&showTrace=2026-10-17" in row["tar1090_url"]
    assert row["external_globe_url"].startswith("https://globe.adsb.lol/")
    assert row["external_globe_url"].split("?", 1)[1] == row["tar1090_url"].split("?", 1)[1]
    assert (row["lowest_ft"], row["lowest_nm"], row["highest_ft"], row["squawks"]) == (575, 2.9, 20350, ["1715"])
    assert row["heard_from"] == row["first_seen"]
    assert row["heard_to"] == pytest.approx(row["last_seen"], abs=1.0)       # the track's last point, to 0.01 s
    assert row["emergency_flags"] == [] and row["emergency_runs"] == []
    assert "emergency" not in row            # an arrival row's `emergency` is a boolean; a flight row's runs are not


def test_an_unconfigured_arrival_is_not_a_miss_and_carries_its_emergency(client):
    row = only_row(client, "N00210")
    assert row["label"] == "arrival KSQL" and row["no_approach_logged"] is False
    assert row["emergency_flags"] == ["EMERGENCY_NORDO"]
    (run,) = row["emergency_runs"]
    assert (run["flag"], run["code"], run["alt_ft"]) == ("EMERGENCY_NORDO", "7600", 2600)
    assert (run["start"], run["end"]) == pytest.approx((1792203289.99, 1792203422.92), abs=0.01)
    assert row["squawks"] == ["7600"]


def test_a_midnight_crossing_overflight_links_to_its_first_day(client):
    row = only_row(client, "SWA4020")
    assert row["label"] == "overflight FL337" and row["label_airport"] is None and row["lowest_nm"] is None
    assert row["no_approach_logged"] is False
    # first heard 22:35Z on the 22nd, last heard 00:13Z on the 23rd: the link stops at the end of the 22nd
    assert "showTrace=2026-10-22" in row["tar1090_url"] and "endTime=23:59:59" in row["tar1090_url"]
    assert "startTime=22:25:59" in row["tar1090_url"]


def test_a_midnight_crossing_arrival_links_to_the_day_of_its_low_point(tmp_path, make_client):
    """Heard 23:30Z to 00:30Z, it comes down to 800 ft at the very end: the link is for the 23rd, from midnight."""
    flown = [point(MIDNIGHT - 1800.0 + 60.0 * i, alt=8000 - 120 * i) for i in range(61)]
    globe = tmp_path / "globe"
    write_trace(globe, "2026/10/22/traces/ee", "c0ffee", [p for p in flown if p.t < MIDNIGHT])
    write_trace(globe, "2026/10/23/traces/ee", "c0ffee", [p for p in flown if p.t >= MIDNIGHT])
    c = make_client(globe=globe, extra=[("MIDARR", "c0ffee", MIDNIGHT - 1800.0, MIDNIGHT + 1800.0)])
    row = only_row(c, "MIDARR")
    assert row["label"] == "arrival KSQL"
    assert row["tar1090_url"].endswith("?icao=c0ffee&showTrace=2026-10-23&startTime=00:00:00&endTime=00:35:00")


@pytest.mark.parametrize("text", ["VIR19Z", "N00210"], ids=["corrupt", "missing"])
def test_missing_and_corrupt_tracks_still_answer(tmp_path, make_client, text):
    globe = tmp_path / "globe"
    corrupt = globe / "2026/10/17/traces/d3/trace_full_4077d3.json"       # VIR19Z's; N00210's has no file at all
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not gzip")
    row = only_row(make_client(globe=globe), text)
    assert row["label"] == "no track on disk" and row["emergency_flags"] == [] and row["emergency_runs"] == []
    assert row["no_approach_logged"] is False and row["squawks"] == []
    assert (row["lowest_ft"], row["highest_ft"], row["label_airport"]) == (None, None, None)
    # with no track the link keeps to the day it was first heard, over the session as stored
    assert "showTrace=2026-10-17" in row["tar1090_url"]


MALFORMED = {         # well-formed JSON, gzipped, that is not a readsb trace: iter_trace_file raises on each
    "empty object": b"{}",
    "list": b"[]",
    "no timestamp": json.dumps({"icao": "4077d3", "trace": []}).encode(),
}


@pytest.mark.parametrize("body", MALFORMED.values(), ids=MALFORMED.keys())
def test_one_malformed_track_file_leaves_the_other_rows_unaffected(tmp_path, make_client, body, caplog):
    """A row whose file can't be read still shows. A file of the wrong shape must not fail all of the rows."""
    flown = [point(1792238400.0 + 60.0 * i, alt=8000 - 120 * i) for i in range(61)]
    globe = tmp_path / "globe"
    bad = globe / "2026/10/17/traces/d3/trace_full_4077d3.json"           # VIR19Z's
    bad.parent.mkdir(parents=True)
    bad.write_bytes(gzip.compress(body))
    write_trace(globe, "2026/10/17/traces/ee", "c0ffee", flown)
    c = make_client(globe=globe, extra=[("VIR19A", "c0ffee", flown[0].t, flown[-1].t)])
    with caplog.at_level(logging.WARNING):
        r = c.get("/api/search", params={"q": "VIR19"})
    assert r.status_code == 200, r.text
    rows = {row["callsign"]: row for row in r.json()["rows"]}
    assert set(rows) == {"VIR19Z", "VIR19A"}
    assert rows["VIR19Z"]["label"] == "no track on disk" and rows["VIR19Z"]["no_approach_logged"] is False
    assert rows["VIR19A"]["label"] == "arrival KSQL"
    assert any("4077d3" in rec.getMessage() for rec in caplog.records if rec.levelno == logging.WARNING)
    # the net that keeps this a 200 is broad, so a coding bug behind it must leave a traceback in the journal
    caught = [rec for rec in caplog.records if rec.name == "adsb_approach.web.app" and "4077d3" in rec.getMessage()]
    assert caught and all(rec.exc_info and rec.exc_info[0] is not None for rec in caught)


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": "  "}, {"q": "V"}, {"q": "VIR19Z", "cursor": "junk"},
                                    {"q": "VIR19Z", "limit": "0"}, {"q": "VIR19Z", "since": "yesterday"},
                                    {"q": "VIR19Z", "airport": "KSFO"}])
def test_bad_requests_are_400(client, params):
    r = client.get("/api/search", params=params)
    assert r.status_code == 400 and r.json()["detail"]


def test_text_ending_in_the_last_code_point_is_400(client):
    r = client.get("/api/search?q=A%F4%8F%BF%BF")           # "A" and U+10FFFF
    assert r.status_code == 400 and "can't be searched" in r.json()["detail"]


def test_text_ending_just_below_the_surrogates_still_answers(client):
    r = client.get("/api/search?q=A%ED%9F%BF")               # "A" and U+D7FF: one above it is a lone surrogate
    assert r.status_code == 200 and r.json()["rows"] == []


def test_the_search_page_is_served(client):
    r = client.get("/search")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"


def test_an_arrival_row_passes_through_with_its_registration(client):
    rows = client.get("/api/search", params={"q": "UAL2177"}).json()["rows"]
    arrival = next(r for r in rows if r["kind"] == "arrival")             # the replay also holds a session with none
    assert {"arrival_id", "attempts", "attention", "emergency", "flight_id", "registration"} <= set(arrival)
    assert "label" not in arrival and "tar1090_url" not in arrival     # the approach drawer builds an arrival's own


def test_pages_and_counts_carry_through(make_client):
    t0 = 1792000000.0
    sessions = [("PAGE01", f"fa00{i:02x}", t0 + 1000.0 * i, t0 + 1000.0 * i + 500.0) for i in range(3)]
    c = make_client(extra=sessions)
    first = c.get("/api/search", params={"q": "PAGE", "limit": "2"}).json()
    assert [r["first_seen"] for r in first["rows"]] == [t0 + 2000.0, t0 + 1000.0]
    assert first["counts"] == {"flights": 3, "with_approach": 0} and first["next_cursor"]
    assert {r["label"] for r in first["rows"]} == {"no track on disk"}
    second = c.get("/api/search", params={"q": "PAGE", "limit": "2", "cursor": first["next_cursor"]}).json()
    assert [r["first_seen"] for r in second["rows"]] == [t0] and second["next_cursor"] is None


def test_the_newest_recent_session_of_a_hex_reads_on_past_its_last_seen(tmp_path, make_client):
    """A session still open when it was last stored has points after last_seen; an older session of the same hex does
    not (it ended when the newer one began)."""
    now = time.time()
    older, newer = (now - 3000.0, now - 2400.0), (now - 1200.0, now - 900.0)
    write_trace(tmp_path / "live", "ee", "c0ffee", [point(now - 3000.0 + 60.0 * i) for i in range(50)])
    c = make_client(globe=tmp_path / "empty", live=tmp_path / "live",
                    extra=[("OPEN1", "c0ffee", *older), ("OPEN1", "c0ffee", *newer)])
    old, new = sorted(c.get("/api/search", params={"q": "OPEN1"}).json()["rows"], key=lambda r: r["first_seen"])
    assert new["newest"] is True and new["last_seen"] + 300.0 < new["heard_to"] <= now
    assert old["newest"] is False and old["heard_to"] == pytest.approx(old["last_seen"], abs=1.0)


def test_a_quiet_session_is_not_read_past_its_last_seen(tmp_path, make_client):
    now = time.time()
    write_trace(tmp_path / "live", "ee", "c0ffee", [point(now - 3 * 7200.0 + 60.0 * i) for i in range(200)])
    last = now - 2 * 7200.0                                     # newest, but heard more than SESSION_GAP_S ago
    c = make_client(globe=tmp_path / "empty", live=tmp_path / "live",
                    extra=[("QUIET1", "c0ffee", last - 600.0, last)])
    row = only_row(c, "QUIET1")
    assert row["newest"] is True and row["heard_to"] == pytest.approx(row["last_seen"], abs=1.0)


def test_a_hex_with_several_sessions_in_a_day_reads_its_track_file_once(tmp_path, make_client, monkeypatch):
    flown = [point(1792238400.0 + 60.0 * i, alt=8000 - 120 * i) for i in range(61)]
    globe = tmp_path / "globe"
    write_trace(globe, "2026/10/17/traces/ee", "c0ffee", flown)
    sessions = [("HEXONE", "c0ffee", flown[0].t, flown[19].t), ("HEXTWO", "c0ffee", flown[20].t, flown[39].t),
                ("HEXTHR", "c0ffee", flown[40].t, flown[60].t)]
    c = make_client(globe=globe, extra=sessions)
    reads = []
    real = tracks.iter_trace_file
    monkeypatch.setattr(tracks, "iter_trace_file", lambda path: reads.append(path) or real(path))
    r = c.get("/api/search", params={"q": "C0FFEE"})
    assert r.status_code == 200 and len(r.json()["rows"]) == 3
    assert all(row["label"] != "no track on disk" for row in r.json()["rows"])
    assert len(reads) == 1


def test_a_directory_that_cannot_be_entered_is_a_missing_track_not_an_error(client, monkeypatch):
    real = Path.is_file

    def is_file(self, *args, **kwargs):
        if self.name == "trace_full_4077d3.json":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)
    assert only_row(client, "VIR19Z")["label"] == "no track on disk"
    assert only_row(client, "N00210")["label"] == "arrival KSQL"          # the other flights still read


def test_an_empty_live_directory_setting_turns_the_fallback_off(tmp_path, make_client, monkeypatch):
    """"" is "off", not the working directory, which is where Path("") would look."""
    first = FLIGHTS[0][3]
    write_trace(tmp_path, "d3", "4077d3", [point(first + 60.0 * i, alt=500 + 10 * i) for i in range(10)])
    monkeypatch.chdir(tmp_path)
    row = only_row(make_client(globe=tmp_path / "empty", live=""), "VIR19Z")
    assert row["label"] == "no track on disk"
    # and a live directory that is set does read the same file, so the test can fail
    (tmp_path / "live").mkdir(exist_ok=True)
    write_trace(tmp_path / "live", "d3", "4077d3", [point(first + 60.0 * i, alt=500 + 10 * i) for i in range(10)])
    row = only_row(make_client(globe=tmp_path / "empty", live=tmp_path / "live"), "VIR19Z")
    assert row["label"] != "no track on disk"


def test_the_search_labels_a_path_approach_from_the_configuration(tmp_path, replay_db, example_cfg):
    db = copy_db(replay_db, tmp_path / "path-search.sqlite")
    w = LiveWriter(db)
    try:
        fid = w.flight_row("407699", "VIR19Z", first_seen=w.t0, last_seen=w.t0 + 400, reg="G-VPRD")
        aid = w.open("407699", "VIR19Z", flight_id=fid, dt=200, runway="28R", path="quiet-bridge")
        w.close_attempt(aid, "407699", Outcome.LANDED, dt=300, references=["quiet-bridge"])
    finally:
        w.close()
    rows = TestClient(create_app(example_cfg, db)).get("/api/search", params={"q": "VIR19Z"}).json()["rows"]
    [row] = [r for r in rows if r["kind"] == "arrival"]
    assert row["attempts"][0]["path_label"] == "Quiet Bridge"
    assert row["attempts"][0]["references"] == [{"name": "quiet-bridge", "label": "Quiet Bridge"}]

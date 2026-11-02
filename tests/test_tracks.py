"""Track summaries: a session's label, lowest and highest points and emergency runs, read from readsb's globe_history
trace files. tests/fixtures/tracks holds twelve real ones recorded by a local receiver."""
from __future__ import annotations

import inspect
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from adsb_approach import tracks
from adsb_approach.model import Airport, Position
from adsb_approach.tracker import Tracker
from adsb_approach.tracks import (SESSION_GAP_S, EmergencyRun, Place, load_places, read_places, session_positions,
                                  summarize)
from tests.traces import trace_body

FIX = Path(__file__).parent / "fixtures" / "tracks"
MIDNIGHT = 1792713600.0         # 2026-10-23 00:00:00Z

REAL = [       # hex, first_seen, last_seen, label, lowest_ft, lowest_nm, highest_ft, squawks
    ("4077d3", 1792275517.54, 1792276598.399, "arrival KSFO", 575, 2.9, 20350, ("1715",)),
    ("0010fd", 1792199408.388, 1792204091.658, "arrival KSQL", 600, 1.8, 3000, ("7600",)),
    ("a1f1a0", 1792708539.907, 1792709575.246, "departure KSFO", 1075, 1.7, 26025, ("3243",)),
    ("001113", 1792708088.282, 1792709180.25, "departure KPAO", 300, 0.4, 6000, ("4267",)),
    ("a8469e", 1792708526.452, 1792709842.99, "overflight FL350", 35000, None, 35000, ("3550",)),
    ("00106f", 1792708456.716, 1792709114.592, "on ground KSFO", 0, 0.5, 0, ()),
    ("001048", 1792708527.273, 1792710113.388, "transit, lowest 3,500 ft near KCCR", 3475, 2.3, 6025, ("4232",)),
    ("ac7686", 1792708559.048, 1792714429.195, "overflight FL337", 10025, None, 33700, ("7236",)),
    ("a224df", 1791224714.483, 1791230635.889, "departure KSQL", 0, 0.2, 4500, ()),
]
BY_HEX = {case[0]: case for case in REAL}


@pytest.fixture(scope="module")
def places() -> list[Place]:
    return read_places(FIX / "places.csv")


def real_summary(hexid: str, places: list[Place]):
    _, first, last, *_ = BY_HEX[hexid]
    return summarize(session_positions(hexid, first, last, globe_dir=FIX, live_dir=None), places)


def point(t: float, alt: int | None = 3000, *, hexid: str = "0010fd", on_ground: bool = False,
          squawk: str | None = None, emergency: str | None = None, lat: float = 37.5, lon: float = -122.2) -> Position:
    return Position(t=t, hex=hexid, lat=lat, lon=lon, alt_baro=alt, on_ground=on_ground, alt_geom=None, gs=120.0,
                    track=90.0, baro_rate=None, geom_rate=None, callsign=None, squawk=squawk, category=None,
                    nav_qnh=None, nav_altitude_mcp=None, nav_modes=frozenset(), source="adsb_icao",
                    emergency=emergency)


def write_trace(root: Path, relative: str, hexid: str, positions: list[Position]) -> Path:
    path = root / relative / f"trace_full_{hexid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(trace_body(hexid, positions))
    return path


@pytest.mark.parametrize("hexid,first,last,label,lowest_ft,lowest_nm,highest_ft,squawks", REAL,
                         ids=[case[0] for case in REAL])
def test_labels_from_real_tracks(places, hexid, first, last, label, lowest_ft, lowest_nm, highest_ft, squawks):
    got = summarize(session_positions(hexid, first, last, globe_dir=FIX, live_dir=None), places)
    assert (got.label, got.lowest_ft, got.lowest_nm, got.highest_ft, got.squawks) == (
        label, lowest_ft, lowest_nm, highest_ft, squawks)


def test_the_key_time_is_the_low_point_for_a_landing_or_takeoff_and_the_start_otherwise(places):
    arrival, overflight = real_summary("4077d3", places), real_summary("a8469e", places)
    assert arrival.first_t < arrival.key_t <= arrival.last_t
    assert overflight.key_t == overflight.first_t
    assert (arrival.first_t, arrival.last_t) == pytest.approx((1792275517.54, 1792276598.399), abs=2.0)


def test_a_nordo_squawk_latches_one_run(places):
    got = real_summary("0010fd", places).emergency
    assert len(got) == 1
    run = got[0]
    assert (run.flag, run.code, run.alt_ft) == ("EMERGENCY_NORDO", "7600", 2600)
    assert run.start == pytest.approx(1792203289.99, abs=0.01)
    assert run.end == pytest.approx(1792203422.92, abs=0.01)


def test_a_single_snapshot_emergency_does_not_latch(places):
    assert real_summary("a224df", places).emergency == ()


def test_a_session_across_utc_midnight_reads_both_day_files(places):
    _, first, last, *_ = BY_HEX["ac7686"]
    got = session_positions("ac7686", first, last, globe_dir=FIX, live_dir=None)
    assert sum(1 for p in got if p.t >= MIDNIGHT) == 1
    assert got[-1].t == pytest.approx(1792714427.74, abs=0.01)


def deny(monkeypatch, *endings: str) -> None:
    """Make Path.is_file raise PermissionError, as it does under a directory the service cannot enter, for the paths
    that end in any of `endings`."""
    real = Path.is_file

    def is_file(self, *args, **kwargs):
        if self.as_posix().endswith(endings):
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)


def test_a_day_file_in_a_directory_that_cannot_be_entered_counts_as_missing(monkeypatch):
    _, first, last, *_ = BY_HEX["ac7686"]
    whole = session_positions("ac7686", first, last, globe_dir=FIX, live_dir=None)
    deny(monkeypatch, "2026/10/23/traces/86/trace_full_ac7686.json")
    got = session_positions("ac7686", first, last, globe_dir=FIX, live_dir=None)
    assert got and got == [p for p in whole if p.t < MIDNIGHT]
    deny(monkeypatch, "trace_full_ac7686.json")
    assert session_positions("ac7686", first, last, globe_dir=FIX, live_dir=None) == []


def test_a_live_file_in_a_directory_that_cannot_be_entered_counts_as_missing(tmp_path, monkeypatch):
    start = MIDNIGHT + 3600.0
    write_trace(tmp_path / "live", "fd", "0010fd", [point(start + 60.0 * i) for i in range(6)])
    (tmp_path / "globe").mkdir()
    deny(monkeypatch, "live/fd/trace_full_0010fd.json")
    got = session_positions("0010fd", start, start + 300.0, globe_dir=tmp_path / "globe", live_dir=tmp_path / "live")
    assert got == []


def test_the_live_file_covers_a_day_globe_history_lacks(tmp_path):
    start = MIDNIGHT + 3600.0
    seen = [point(start + 60.0 * i, alt=2000 + i) for i in range(6)]
    write_trace(tmp_path / "live", "fd", "0010fd", seen)
    (tmp_path / "globe").mkdir()
    got = session_positions("0010fd", start, start + 300.0, globe_dir=tmp_path / "globe", live_dir=tmp_path / "live")
    assert [(p.t, p.alt_baro) for p in got] == [(p.t, p.alt_baro) for p in seen]


def test_the_live_file_is_not_read_when_globe_history_has_the_day(tmp_path):
    _, first, last, *_ = BY_HEX["0010fd"]
    expected = session_positions("0010fd", first, last, globe_dir=FIX, live_dir=None)
    assert expected
    write_trace(tmp_path, "fd", "0010fd", [point(first + 60.0 * i, alt=9000) for i in range(5)])
    assert session_positions("0010fd", first, last, globe_dir=FIX, live_dir=tmp_path) == expected


def test_the_live_file_adds_only_the_days_globe_history_lacks(tmp_path):
    """The live file reaches back about 24 h, so it repeats the day globe_history has: no point may come twice."""
    times = [MIDNIGHT - 600.0 + 120.0 * i for i in range(11)]            # 23:50Z to 00:10Z
    globe_day = [point(t) for t in times if t < MIDNIGHT]
    write_trace(tmp_path / "globe", "2026/10/22/traces/fd", "0010fd", globe_day)
    write_trace(tmp_path / "live", "fd", "0010fd", [point(t) for t in times])
    got = session_positions("0010fd", times[0], times[-1], globe_dir=tmp_path / "globe", live_dir=tmp_path / "live")
    assert [p.t for p in got] == times


def count_reads(monkeypatch) -> list[Path]:
    """The files iter_trace_file is asked to read, in order, as session_positions calls it."""
    reads: list[Path] = []
    real = tracks.iter_trace_file

    def counting(path):
        reads.append(Path(path))
        return real(path)

    monkeypatch.setattr(tracks, "iter_trace_file", counting)
    return reads


def test_two_sessions_of_one_hex_share_a_cache_and_read_the_file_once(monkeypatch):
    _, first, last, *_ = BY_HEX["0010fd"]
    mid = (first + last) / 2
    sessions = [(first, mid), (mid + 1.0, last)]            # one hex, one UTC day, two sessions
    reads = count_reads(monkeypatch)
    plain = [session_positions("0010fd", a, b, globe_dir=FIX, live_dir=None) for a, b in sessions]
    assert len(reads) == 2 and all(plain)
    reads.clear()
    cache: dict[Path, list[Position]] = {}
    cached = [session_positions("0010fd", a, b, globe_dir=FIX, live_dir=None, cache=cache) for a, b in sessions]
    assert len(reads) == 1 and list(cache) == reads
    assert cached == plain


def test_a_cached_live_file_still_gives_each_session_only_its_own_missing_days(tmp_path, monkeypatch):
    """The cache holds the whole file, not what the first session kept of it: each session keeps from the live file the
    days globe_history lacks for it, and these two lack different days."""
    times = [MIDNIGHT - 600.0 + 120.0 * i for i in range(11)]            # 23:50Z to 00:10Z
    live = write_trace(tmp_path / "live", "fd", "0010fd", [point(t) for t in times])
    (tmp_path / "globe").mkdir()
    args = dict(globe_dir=tmp_path / "globe", live_dir=tmp_path / "live")
    sessions = [(times[0], times[4]), (times[6], times[-1])]          # the 22nd, then the 23rd
    plain = [session_positions("0010fd", a, b, **args) for a, b in sessions]
    assert [[p.t for p in got] for got in plain] == [times[:5], times[6:]]
    reads = count_reads(monkeypatch)
    cache: dict[Path, list[Position]] = {}
    cached = [session_positions("0010fd", a, b, cache=cache, **args) for a, b in sessions]
    assert cached == plain and reads == [live]


def test_an_open_session_reads_to_its_first_long_gap(tmp_path):
    start = MIDNIGHT + 3600.0
    last_seen = start + 300.0
    inside = [start + 60.0 * i for i in range(6)]
    extra = [last_seen + 600.0, last_seen + 1200.0, last_seen + 1200.0 + 7000.0]     # each gap under SESSION_GAP_S
    after_the_gap = [extra[-1] + 3 * 3600.0 + 60.0 * i for i in range(1, 4)]
    write_trace(tmp_path / "live", "fd", "0010fd", [point(t) for t in inside + extra + after_the_gap])
    (tmp_path / "globe").mkdir()
    args = dict(globe_dir=tmp_path / "globe", live_dir=tmp_path / "live")
    open_session = session_positions("0010fd", start, last_seen, open_ended=True, now=after_the_gap[-1] + 60.0, **args)
    assert [p.t for p in open_session] == inside + extra
    closed = session_positions("0010fd", start, last_seen, **args)
    assert [p.t for p in closed] == inside


def test_the_session_gap_is_the_trackers():
    assert SESSION_GAP_S == inspect.signature(Tracker.__init__).parameters["session_gap_s"].default


@pytest.mark.parametrize("hexid", ["../../etc", "ZZZZZZ", "0010f", "0010fdd", "0010FD", "0010fd/", "~~0010fd", ""])
def test_no_path_is_built_from_a_hex_that_is_not_one(monkeypatch, caplog, hexid):
    """The hex goes into a file name: whatever isn't six lowercase hex digits (with readsb's "~" for a non-ICAO address)
    gives no track and reads no file, even where a file would be found."""
    def fail(path):
        raise AssertionError(f"read {path}")

    monkeypatch.setattr(tracks, "iter_trace_file", fail)
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    with caplog.at_level(logging.WARNING, logger="adsb_approach.tracks"):
        got = session_positions(hexid, MIDNIGHT, MIDNIGHT + 300.0, globe_dir=FIX, live_dir=FIX)
    assert got == []
    assert [r.levelno for r in caplog.records] == [logging.WARNING] and repr(hexid) in caplog.text


def test_a_non_icao_hex_with_readsbs_tilde_is_read(tmp_path):
    start = MIDNIGHT + 3600.0
    seen = [point(start + 60.0 * i, hexid="~0010fd") for i in range(4)]
    write_trace(tmp_path, "2026/10/23/traces/fd", "~0010fd", seen)
    got = session_positions("~0010fd", start, start + 180.0, globe_dir=tmp_path, live_dir=None)
    assert [p.t for p in got] == [p.t for p in seen]


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_corrupt_files_give_no_track(tmp_path, places, damage):
    if damage == "corrupt":
        path = tmp_path / "2026/10/17/traces/fd/trace_full_0010fd.json"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not gzip")
    got = session_positions("0010fd", 1792199408.388, 1792204091.658, globe_dir=tmp_path, live_dir=None)
    assert got == []
    summary = summarize(got, places)
    assert (summary.kind, summary.label) == ("no track", "no track on disk")
    assert summary.emergency == () and summary.squawks == ()


def test_squawk_run_ends_at_the_last_report_before_another_squawk():
    flight = [point(10.0 * i, 2000, squawk="7700") for i in range(8)]          # held 70 s
    flight += [point(80.0, 2000, squawk="1200"), point(90.0, 2000, squawk="7700")]
    got = summarize(flight, []).emergency
    assert got == (EmergencyRun("EMERGENCY_GENERAL", "7700", start=0.0, end=70.0, alt_ft=2000),)


def test_an_ads_b_state_latches_under_its_own_name():
    flight = [point(10.0 * i, 1500 + 100 * i, squawk="1200", emergency="nordo" if i >= 2 else "none") for i in range(9)]
    got = summarize(flight, []).emergency
    assert got == (EmergencyRun("EMERGENCY_NORDO", "nordo", start=20.0, end=80.0, alt_ft=1700),)


def test_a_state_that_latched_first_keeps_the_run_when_the_squawk_came_later():
    flight = [point(10.0 * i, 2000, squawk="1200" if i < 2 else "7600", emergency="nordo") for i in range(5)]
    got = summarize(flight, []).emergency
    assert [(r.flag, r.code, r.start) for r in got] == [("EMERGENCY_NORDO", "nordo", 0.0)]


def test_a_squawk_and_a_state_that_start_together_give_one_run_under_the_squawk():
    flight = [point(10.0 * i, 2000, squawk="7600", emergency="nordo") for i in range(5)]
    assert [(r.flag, r.code) for r in summarize(flight, []).emergency] == [("EMERGENCY_NORDO", "7600")]


def test_a_ground_point_counts_as_zero_feet_and_a_missing_altitude_is_ignored():
    flight = [point(0.0, None), point(100.0, None, on_ground=True), point(200.0, 800), point(300.0, None)]
    got = summarize(flight, [Place("KXYZ", "KXYZ", 37.5, -122.2)])
    assert (got.lowest_ft, got.highest_ft, got.kind, got.airport) == (0, 800, "departure", "KXYZ")


def test_the_nearest_place_and_the_first_of_tied_lowest_points_decide_the_label():
    places = [Place("FAR", "Far", 38.5, -122.2), Place("NEAR", "Near", 37.51, -122.2)]
    flight = [point(0.0, 5000), point(100.0, 900, lat=37.5), point(200.0, 900, lat=38.5), point(300.0, 5000)]
    got = summarize(flight, places)
    assert (got.airport, got.lowest_ft, got.lowest_nm, got.key_t) == ("NEAR", 900, 0.6, 100.0)


def test_a_low_point_in_the_middle_of_the_session_is_a_low_pass():
    places = [Place("KXYZ", "KXYZ", 37.5, -122.2)]
    flight = [point(0.0, 4000), point(500.0, 500), point(1000.0, 4000)]
    assert summarize(flight, places).label == "low pass KXYZ"


def test_a_low_point_far_from_every_place_is_a_transit_and_a_track_without_places_still_labels():
    far = [Place("KXYZ", "KXYZ", 39.5, -122.2)]
    flight = [point(0.0, 4000), point(500.0, 1480), point(1000.0, 4000)]
    assert summarize(flight, far).label == "transit, lowest 1,500 ft near KXYZ"
    assert summarize(flight, []).label == "transit, lowest 1,500 ft"


def test_read_places_reads_the_nearby_airports_csv(tmp_path):
    path = tmp_path / "places.csv"
    path.write_text("ident,name,type,lat,lon\nKSFO,San Francisco Intl,large_airport,37.6189,-122.375\n")
    assert read_places(path) == [Place("KSFO", "San Francisco Intl", 37.6189, -122.375)]


def configured(icao: str, lat: float, lon: float) -> Airport:
    return Airport(icao=icao, name=f"{icao} field", enabled=True, lat=lat, lon=lon, elevation_ft=10.0, metar=icao,
                   geoid_offset_ft=-100.0, declination_deg=13.0)


def test_load_places_adds_configured_airports_and_tolerates_a_missing_file(tmp_path):
    airports = {"KSFO": configured("KSFO", 37.6189, -122.375), "KOAK": configured("KOAK", 37.7213, -122.2208),
                "KSJC": configured("KSJC", 37.3626, -121.9291), "KNUQ": configured("KNUQ", 37.4161, -122.049)}
    assert load_places(tmp_path / "missing.csv", airports) == [
        Place("KSFO", "KSFO field", 37.6189, -122.375), Place("KOAK", "KOAK field", 37.7213, -122.2208),
        Place("KSJC", "KSJC field", 37.3626, -121.9291), Place("KNUQ", "KNUQ field", 37.4161, -122.049)]
    listed = load_places(FIX / "places.csv", airports)
    assert len(listed) == 20 and listed == read_places(FIX / "places.csv")      # all four are already listed: none added
    short = tmp_path / "short.csv"
    short.write_text("ident,name,type,lat,lon\nKSFO,San Francisco Intl,large_airport,37.6189,-122.375\n")
    assert [p.ident for p in load_places(short, airports)] == ["KSFO", "KOAK", "KSJC", "KNUQ"]
    assert load_places(short, airports)[0].name == "San Francisco Intl"         # the list's own entry wins


def test_load_places_falls_back_when_the_list_cannot_be_looked_at(monkeypatch):
    airports = {"KSFO": configured("KSFO", 37.6189, -122.375), "KOAK": configured("KOAK", 37.7213, -122.2208)}
    deny(monkeypatch, "places.csv")
    assert load_places(FIX / "places.csv", airports) == [
        Place("KSFO", "KSFO field", 37.6189, -122.375), Place("KOAK", "KOAK field", 37.7213, -122.2208)]


@pytest.mark.parametrize("body", [
    b"ident,name,type,lat,lon\nKSFO,San Francisco Intl,large_airport,abc,-122.375\n",      # a lat that is not a number
    b"ident,name,type,lat\nKSFO,San Francisco Intl,large_airport,37.6189\n",               # a missing column
    b"ident,name,type,lat,lon\nKSFO,San Francisco Intl\n",                                 # a row cut short
    b"ident,name,type,lat,lon\nKSFO,San Francisco Intl,large_airport,37.6,\xff\n",          # not UTF-8
], ids=["bad lat", "missing column", "short row", "not utf-8"])
def test_load_places_falls_back_on_a_malformed_file(tmp_path, caplog, body):
    """The list is optional, and create_app calls load_places: a bad file must not stop the web service."""
    airports = {"KSFO": configured("KSFO", 37.6189, -122.375), "KOAK": configured("KOAK", 37.7213, -122.2208)}
    bad = tmp_path / "bad.csv"
    bad.write_bytes(body)
    with caplog.at_level(logging.WARNING, logger="adsb_approach.tracks"):
        places = load_places(bad, airports)
    assert places == [Place("KSFO", "KSFO field", 37.6189, -122.375), Place("KOAK", "KOAK field", 37.7213, -122.2208)]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and str(bad) in warnings[0].getMessage()
    with pytest.raises(Exception):                  # read_places itself still raises: build-nearby and tests want it
        read_places(bad)


KXYZ = [Place("KXYZ", "KXYZ", 37.5, -122.2)]
BONEYARD = Place("US-3395", "Boneyard Airstrip", 37.66337, -122.02126)       # as config/nearby_airports.csv has it


def test_a_session_heard_for_a_minute_is_heard_briefly(places):
    """N00210, 2026-09-21 02:48Z: 60 s and three points, 2,000 then 1,900 ft over Fremont. Its last point was the lowest,
    which made it an "arrival" at a strip 7.5 nm away; that much track cannot say which way it was going."""
    got = summarize(session_positions("0010fd", 1789958916.457, 1789958976.931, globe_dir=FIX, live_dir=None),
                    [*places, BONEYARD])
    assert (got.kind, got.label, got.lowest_ft, got.lowest_nm) == ("brief", "heard briefly at 1,900 ft near US-3395",
                                                                   1900, 7.5)
    assert got.key_t == got.first_t


def test_an_on_ground_flag_at_altitude_does_not_make_the_lowest_point(places):
    """N00210, 2026-09-20 from 13:47 PDT: its ADS-B reported on_ground at 2,325 and 6,050 ft geometric over Fremont,
    which made those points the "lowest" and the takeoff a "departure KPAO". It left San Carlos."""
    got = summarize(session_positions("0010fd", 1789937232.569, 1789938579.67, globe_dir=FIX, live_dir=None), places)
    assert (got.label, got.lowest_ft) == ("departure KSQL", 200)


@pytest.mark.parametrize("flight,label", [
    ([point(0.0, 900)], "heard briefly at 900 ft near KXYZ"),                                       # one point
    ([point(0.0, 1500), point(600.0, 900)], "heard briefly at 900 ft near KXYZ"),                   # two, ten minutes apart
    ([point(0.0, 1500), point(60.0, 900), point(119.0, 1200)], "heard briefly at 900 ft near KXYZ"),  # under two minutes
    ([point(0.0, 35000), point(30.0, 35000)], "heard briefly at FL350"),                             # high: no place
    ([point(0.0, 1500, lat=39.5), point(30.0, 1400, lat=39.5)], "heard briefly at 1,400 ft"),        # no place near
    ([point(0.0, None, on_ground=True), point(12.0, 300)], "heard briefly on the ground near KXYZ"),  # lowest on the ground
    ([point(0.0, None), point(30.0, None)], "heard briefly"),                                      # no altitude at all
    ([point(0.0, 12000), point(30.0, 12500)], "heard briefly at FL120"),                            # FL from HIGH_FT, as overflights
    ([point(0.0, -40), point(30.0, 300)], "heard briefly at 0 ft near KXYZ"),                       # below 0 ft is not the ground
])
def test_too_little_track_is_heard_briefly(flight, label):
    assert summarize(flight, KXYZ).label == label


def test_three_points_over_two_minutes_are_enough_for_a_direction():
    flight = [point(0.0, 1500), point(60.0, 1200), point(120.0, 900)]
    assert summarize(flight, KXYZ).label == "arrival KXYZ"


def test_a_short_session_on_the_ground_is_still_on_the_ground():
    assert summarize([point(0.0, None, on_ground=True)], KXYZ).label == "on ground KXYZ"


@pytest.mark.parametrize("alt_geom,lowest_ft", [(None, 0), (400, 0), (2325, 2325)])
def test_an_on_ground_flag_is_believed_only_near_the_ground(alt_geom, lowest_ft):
    flight = [point(0.0, 3000), replace(point(300.0, None, on_ground=True), alt_geom=alt_geom), point(600.0, 3000)]
    assert summarize(flight, KXYZ).lowest_ft == lowest_ft

import pytest

from adsb_approach.model import Outcome
from adsb_approach.tracks import TrackSummary
from adsb_approach.store.db import Store
from adsb_approach.web.arrivals import DEFAULT_LIMIT
from adsb_approach.web.db import snapshot
from adsb_approach.web.search import (SearchQuery, decode_search_cursor, encode_search_cursor, list_search, match_sql,
                                      page_sql)
from tests.webdb import LiveWriter


@pytest.fixture
def writer(tmp_path):
    """A fresh database, so a test can count exactly what it wrote; the replayed fixture already holds sessions."""
    db = tmp_path / "s.sqlite"
    store = Store(db)
    store.migrate()
    store.close()
    w = LiveWriter(db)
    yield w
    w.close()


def session(w, hexid, callsign, first, last, **kw):
    """A flights row from `first` to `last` seconds after the run's start."""
    return w.flight_row(hexid, callsign, first_seen=w.t0 + first, last_seen=w.t0 + last, **kw)


def search(w, text, *, since=None, until=None, limit=DEFAULT_LIMIT, cursor=None, labels=None):
    params = {"q": text}
    if since is not None:
        params["since"] = str(since)
    if until is not None:
        params["until"] = str(until)
    with snapshot(w.store.path) as conn:
        return list_search(conn, SearchQuery.parse(params), limit=limit, cursor=decode_search_cursor(cursor),
                           labels=labels)


def every_page(w, text, *, limit, **kw):
    rows, cursor = [], None
    while True:
        page = search(w, text, limit=limit, cursor=cursor, **kw)
        rows += page["rows"]
        cursor = page["next_cursor"]
        if cursor is None:
            return rows


def row_time(row):
    return row["started_at"] if row["kind"] == "arrival" else row["first_seen"]


def test_callsign_prefix_ignores_case_and_spaces(writer):
    w = writer
    first = session(w, "407699", "VIR19Z", 100, 200)
    second = session(w, "407699", "VIR19Z", 300, 400)
    third = session(w, "407699", "VIR19Z", 500, 600)
    session(w, "407700", "VIR20V", 700, 800)
    page = search(w, " vir19 ")
    assert [(r["kind"], r["flight_id"]) for r in page["rows"]] == [("flight", third), ("flight", second),
                                                                   ("flight", first)]
    assert page["counts"] == {"flights": 3, "with_approach": 0}
    assert page["next_cursor"] is None


def test_registration_matches_with_or_without_hyphen(writer):
    w = writer
    vprd = session(w, "406123", "VIR19Z", 100, 200, reg="G-VPRD")
    ga = session(w, "0010fd", "SWA123", 300, 400, reg="N00210")
    for text in ("GVPR", "g-vpr", "G-VPRD", "GVPRD"):
        assert [r["flight_id"] for r in search(w, text)["rows"]] == [vprd], text
    assert [r["flight_id"] for r in search(w, "N002")["rows"]] == [ga]
    assert search(w, "G-VPRE")["rows"] == []


def test_six_hex_digits_also_match_the_hex(writer):
    w = writer
    fid = session(w, "0010fd", "N00210", 100, 200)
    assert [r["flight_id"] for r in search(w, "0010FD")["rows"]] == [fid]
    assert [r["flight_id"] for r in search(w, "0010fd")["rows"]] == [fid]
    assert search(w, "0010F")["rows"] == []


def test_six_hex_digits_match_both_hex_and_callsign_prefix(writer):
    """BAE146 is a hex and also the start of a callsign; each session is found by the one branch that fits it."""
    w = writer
    by_hex = session(w, "bae146", "N0123AB", 100, 200)
    by_callsign = session(w, "a00001", "BAE1460", 300, 400)
    session(w, "a00002", "BAE1", 500, 600)                          # neither: a callsign shorter than the text
    assert sorted(r["flight_id"] for r in search(w, "BAE146")["rows"]) == sorted([by_hex, by_callsign])
    assert search(w, "BAE146")["counts"] == {"flights": 2, "with_approach": 0}


def test_a_hex_whose_callsign_changed_matches_by_each_sessions_own_values(writer):
    """A callsign belongs to a session, not to the airframe: the same hex flew as VIR19Z and later as VIR20V."""
    w = writer
    first = session(w, "407699", "VIR19Z", 100, 200)
    second = session(w, "407699", "VIR20V", 300, 400)
    assert [r["flight_id"] for r in search(w, "VIR19")["rows"]] == [first]
    assert [r["flight_id"] for r in search(w, "VIR20")["rows"]] == [second]
    assert [r["flight_id"] for r in search(w, "407699")["rows"]] == [second, first]       # newest first


def test_wildcards_and_quotes_are_literal(writer):
    w = writer
    session(w, "407699", "VIR19Z", 100, 200, reg="G-VPRD")
    session(w, "0010fd", "N00210", 300, 400, reg="N00210")
    for text in ("%%", "__", "VIR%", "VIR_9", "VIR'19"):       # each would match VIR19Z were it read as a LIKE pattern
        page = search(w, text)
        assert page["rows"] == [] and page["counts"] == {"flights": 0, "with_approach": 0}


def test_a_session_with_an_approach_gives_its_arrival_rows_not_a_flight_row(writer):
    w = writer
    fid = session(w, "406123", "VIR19Z", 100, 400, reg="G-VPRD")
    aid = w.open("406123", "VIR19Z", flight_id=fid, dt=200)
    w.close_attempt(aid, "406123", Outcome.LANDED, dt=300)
    page = search(w, "VIR19")
    [row] = page["rows"]
    assert row["kind"] == "arrival" and row["arrival_id"] == aid and row["registration"] == "G-VPRD"
    assert row["flight_id"] == fid and row["hex"] == "406123" and row["attempts"][0]["outcome"] == "landed"
    assert page["counts"] == {"flights": 1, "with_approach": 1}


def test_a_search_result_carries_the_path_label_as_the_log_does(writer):
    w = writer
    fid = session(w, "406123", "VIR19Z", 100, 400)
    aid = w.open("406123", "VIR19Z", flight_id=fid, dt=200, runway="28R", path="quiet-bridge")
    w.close_attempt(aid, "406123", Outcome.LANDED, dt=300, references=["quiet-bridge", None])
    labels = {("KSFO", "28R", "quiet-bridge"): "Quiet Bridge", ("KSFO", "28R", None): "ILS"}
    [row] = search(w, "VIR19", labels=labels)["rows"]
    [attempt] = row["attempts"]
    assert attempt["path"] == "quiet-bridge" and attempt["path_label"] == "Quiet Bridge"
    assert attempt["references"] == [{"name": "quiet-bridge", "label": "Quiet Bridge"}, {"name": None, "label": "ILS"}]
    [bare] = search(w, "VIR19")["rows"]                                          # without labels, as before this feature
    assert bare["attempts"][0]["path_label"] == "quiet-bridge"


def test_a_session_with_a_go_around_gives_one_arrival_row_not_one_per_attempt(writer):
    w = writer
    fid = session(w, "a44540", "UAL2177", 100, 600)
    first = w.open("a44540", "UAL2177", flight_id=fid, dt=200)
    w.close_attempt(first, "a44540", Outcome.GO_AROUND, dt=260)
    second = w.open("a44540", "UAL2177", flight_id=fid, dt=300, arrival_id=first, attempt_no=2, prev_approach_id=first)
    w.close_attempt(second, "a44540", Outcome.LANDED, dt=400)
    [row] = search(w, "UAL2177")["rows"]
    assert row["kind"] == "arrival" and row["arrival_id"] == first
    assert [a["approach_id"] for a in row["attempts"]] == [first, second]


def test_window_keeps_sessions_that_overlap_it(writer):
    w = writer
    session(w, "000001", "SWA1A", 100, 200)
    middle = session(w, "000002", "SWA1B", 300, 400)
    last = session(w, "000003", "SWA1C", 500, 600)
    page = search(w, "SWA1", since=w.t0 + 350, until=w.t0 + 550)
    assert [r["flight_id"] for r in page["rows"]] == [last, middle]
    assert page["counts"]["flights"] == 2


def test_window_includes_the_instant_a_session_was_last_heard_and_excludes_the_one_it_began(writer):
    w = writer
    ends_at_since = session(w, "000001", "SWA1A", 100, 300)
    session(w, "000002", "SWA1B", 600, 700)                    # first heard at `until`
    assert [r["flight_id"] for r in search(w, "SWA1", since=w.t0 + 300, until=w.t0 + 600)["rows"]] == [ends_at_since]


def test_pages_cover_every_row_once_newest_first(writer):
    w = writer
    ids = [session(w, f"{i:06x}", f"VIR{i}", 100 * i, 100 * i + 50) for i in range(1, 8)]
    first = search(w, "VIR", limit=2)
    assert len(first["rows"]) == 2 and first["next_cursor"] is not None and first["counts"]["flights"] == 7
    rows = every_page(w, "VIR", limit=2)
    assert [r["flight_id"] for r in rows] == ids[::-1]
    times = [row_time(r) for r in rows]
    assert all(a > b for a, b in zip(times, times[1:]))


def test_rows_with_the_same_time_page_without_loss_or_repeat(writer):
    w = writer
    # a flight row and an arrival whose first attempt began the instant the flight row's session did
    plain = session(w, "000001", "SWA1A", 500, 600)
    other = session(w, "000002", "SWA1B", 100, 700)
    aid = w.open("000002", "SWA1B", flight_id=other, dt=500)
    w.close_attempt(aid, "000002", Outcome.LANDED, dt=550)
    expected = [("arrival", aid), ("flight", plain)]
    assert [(r["kind"], r["arrival_id"] if r["kind"] == "arrival" else r["flight_id"])
            for r in every_page(w, "SWA1", limit=1)] == expected
    assert [(r["kind"], r["arrival_id"] if r["kind"] == "arrival" else r["flight_id"])
            for r in search(w, "SWA1")["rows"]] == expected


def test_short_text_is_one_bounded_page(writer):
    w = writer
    for i in range(1, 121):
        session(w, f"{i:06x}", f"UAL{i}", 10 * i, 10 * i + 5)
    page = search(w, "UA", limit=50)
    assert len(page["rows"]) == 50 and page["next_cursor"] is not None
    assert page["counts"]["flights"] == 120


def test_newest_marks_only_the_hex_latest_session(writer):
    w = writer
    earlier = session(w, "407699", "VIR19Z", 100, 200)
    later = session(w, "407699", "VIR19Z", 300, 400)
    rows = {r["flight_id"]: r for r in search(w, "VIR19")["rows"]}
    assert (rows[earlier]["newest"], rows[later]["newest"]) == (False, True)


def test_a_flight_row_carries_the_session_fields(writer):
    w = writer
    fid = session(w, "406123", "VIR19Z", 100, 200, reg="G-VPRD", type_code="A35K", traffic_class="airline")
    [row] = search(w, "VIR19Z")["rows"]
    assert row == {"kind": "flight", "flight_id": fid, "hex": "406123", "callsign": "VIR19Z", "registration": "G-VPRD",
                   "type_code": "A35K", "traffic_class": "airline", "first_seen": w.t0 + 100,
                   "last_seen": w.t0 + 200, "newest": True}


def test_empty_text_is_an_error():
    with pytest.raises(ValueError, match="q: search text is required"):
        SearchQuery.parse({"q": "  "})
    with pytest.raises(ValueError, match="q: search text is required"):
        SearchQuery.parse({})


def test_one_character_is_too_short():
    for text in ("N", " n "):
        with pytest.raises(ValueError, match="q: type at least 2 characters"):
            SearchQuery.parse({"q": text})
    assert SearchQuery.parse({"q": "N1"}) == SearchQuery("N1")


def test_a_hyphen_does_not_count_toward_the_two_characters():
    """The registration branch matches on the text with its hyphens removed, so `N-` would prefix-match every US
    registration through the key `N`; the floor applies to what is matched, not to what was typed."""
    for text in ("N-", "-N", "--", " n- "):
        with pytest.raises(ValueError, match="q: type at least 2 characters"):
            SearchQuery.parse({"q": text})
    assert SearchQuery.parse({"q": "G-V"}) == SearchQuery("G-V")
    assert SearchQuery.parse({"q": "N-1"}) == SearchQuery("N-1")


def test_a_character_beyond_the_bmp_is_refused():
    """_after adds one to the last character, and chr() refuses to go past U+10FFFF; no callsign, registration or hex
    has a character above U+FFFF, so the text is refused before any query is built."""
    for text in ("A\U0010ffff", "\U0001f600X", " ab\U00010000 "):
        with pytest.raises(ValueError, match="q: search text has a character that can't be searched"):
            SearchQuery.parse({"q": text})
    assert SearchQuery.parse({"q": "A\uffff"}) == SearchQuery("A\uffff")        # the last BMP character still works


def test_the_text_after_a_character_just_below_the_surrogates_is_not_a_surrogate():
    """U+D7FF + 1 is U+D800, a lone surrogate that sqlite3 cannot encode; the bound uses U+E000 instead."""
    where, args = match_sql(SearchQuery.parse({"q": "A\ud7ff"}))
    assert args[:2] == ["A\ud7ff", "A\ue000"]
    args[1].encode("utf-8")


def test_only_q_since_and_until_are_accepted():
    with pytest.raises(ValueError, match="unknown parameter 'x'"):
        SearchQuery.parse({"q": "VIR", "x": "1"})
    assert SearchQuery.parse({"q": " vir19 ", "since": "10", "until": "20.5"}) == SearchQuery("VIR19", 10.0, 20.5)
    with pytest.raises(ValueError, match="since: 'soon' is not a number"):
        SearchQuery.parse({"q": "VIR", "since": "soon"})


def test_a_cursor_round_trips_and_a_malformed_one_is_an_error():
    assert decode_search_cursor(encode_search_cursor(1789240100.5, "arrival", 42)) == (1789240100.5, "arrival", 42)
    assert decode_search_cursor(None) is None and decode_search_cursor("  ") is None
    for text in ("garbage", "1.0:arrival", "1.0:arrival:x", "x:arrival:1", "1.0:other:1"):
        with pytest.raises(ValueError, match="cursor: .* is not a cursor from this API"):
            decode_search_cursor(text)


def plan(w, sql, args):
    with snapshot(w.store.path) as conn:
        return " ".join(r["detail"] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, args))


def test_matching_uses_the_indexes(writer):
    w = writer
    query = SearchQuery.parse({"q": "GVPR"})
    where, args = match_sql(query)
    matching = plan(w, f"SELECT f.id FROM flights f WHERE {where}", args)
    assert "flights_callsign" in matching and "flights_reg_key" in matching
    where, args = match_sql(SearchQuery.parse({"q": "0010FD"}))
    assert "flights_hex" in plan(w, f"SELECT f.id FROM flights f WHERE {where}", args)
    sql, args = page_sql(query, None, 50)
    assert "approaches_flight" in plan(w, sql, args)


@pytest.mark.parametrize("kind,missed", [("arrival", True), ("brief", False)])
def test_only_an_arrival_at_a_scored_airport_is_marked_missed(kind, missed):
    """A session heard too briefly to say which way it went is never called a missed arrival (a one-minute track at
    1,900 ft had once been called an "arrival")."""
    from adsb_approach.web.search import grey_row
    row = {"kind": "flight", "flight_id": 1, "hex": "0010fd", "first_seen": 1789958916.0, "last_seen": 1789958977.0}
    summary = TrackSummary(kind, "KSFO", 900, 1.0, 1200, 1789958916.0, 1789958916.0, 1789958977.0, (), ())
    got = grey_row(row, summary, tar1090="http://pi/tar1090/", globe_url=None, configured={"KSFO"})
    assert got["no_approach_logged"] is missed

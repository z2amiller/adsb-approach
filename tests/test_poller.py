"""The live poller: re-api find_hex requests for the aircraft on the watch list, with backoff, and the positions they
bring. The replies are synthetic (tests/reapi.py); a stand-in HTTP host answers the real fetch, and a hand-moved clock
and a fake fetch drive the poller's steps."""
from __future__ import annotations

import asyncio
import http.server
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, replace

import pytest

from adsb_approach import __version__
from adsb_approach.config import Config, load_config
from adsb_approach.model import Position
from adsb_approach.poller import (API_KEY_HEADER, IDLE_S, ORIGIN, USER_AGENT, PollError, Poller, WatchList, fetch_reply,
                                  poll_url, positions_from_reply, read_api_key, refused, retry_after_s, run_poller)
from adsb_approach.store.db import Store, current_poll_pause
from tests.conftest import ROOT, T0, make_sfo_28r, synthetic_track
from tests.reapi import reapi_reply, repeated_fix_replies

NOW = T0 + 600.0


def fix(hexid: str, t: float) -> Position:
    """One airborne position of `hexid` at `t`."""
    return replace(synthetic_track(make_sfo_28r(), hexid=hexid, start_nm=3.0, end_nm=2.9)[0], t=t)


# --- the HTTP side -------------------------------------------------------------------------------------------------

ODD_RETRY_AFTER = {"sup2": "\u00b2", "bigyear": "Thu, 01 Jan 99999999999999999999 00:00:00 GMT"}


@pytest.fixture
def reapi_host():
    """A stand-in for re-api. /status/<code>/ answers with that status, /html/ with an HTML page, /not-object/,
    /no-now/, /no-aircraft/, /text-now/ and /aircraft-object/ with 200 and JSON that is not a reply, /slow/ stalls for
    a second, and any other path with a reply. Every request's path, User-Agent and x-api-key is recorded."""
    reply = reapi_reply([fix("a1b2c3", NOW - 1.0), fix("abc123", NOW - 2.0)], NOW)
    pages = {"/html/": b"<html><body>Verify you are human</body></html>", "/not-object/": b"[]",
             "/no-now/": b'{"aircraft": []}', "/no-aircraft/": b'{"now": 1.0}',
             "/text-now/": b'{"now": "1.0", "aircraft": []}', "/aircraft-object/": b'{"now": 1.0, "aircraft": {}}',
             "/huge-now/": b'{"now": 1' + b"9" * 400 + b', "aircraft": []}',
             "/v2-huge-now/": b'{"ac": [], "msg": "No error", "now": 1' + b"9" * 400 + b'}',
             "/v2-error/": b'{"ac": [], "msg": "Rate limited", "now": 1792800000000}',
             "/v2-seconds/": b'{"ac": [], "msg": "No error", "now": 1792800000.5}',
             "/v2-null-ac/": b'{"ac": null, "msg": "No error", "now": 1792800000000}',
             "/ctime-only/": b'{"ctime": 1, "now": 5}'}
    requests: list[tuple[str, str | None, str | None]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("User-Agent"), self.headers.get(API_KEY_HEADER)))
            try:
                prefix = "/" + self.path.split("/")[1] + "/"
                if prefix == "/status/":                     # /status/<code>/[<Retry-After>|-/[<reset>/]]
                    parts = self.path.split("/")
                    self.send_response(int(parts[2]))
                    if len(parts) > 4 and parts[3] not in ("", "-"):
                        self.send_header("Retry-After", ODD_RETRY_AFTER.get(parts[3], parts[3]))
                    if len(parts) > 5 and parts[4]:
                        self.send_header("x-ratelimit-reset", parts[4])
                    self.end_headers()
                    return
                if prefix in ("/v2/", "/v2-empty/"):          # adsb.lol's v2 shape: `ac`, and `now` in milliseconds
                    v2 = {"ac": [] if prefix == "/v2-empty/" else reply["aircraft"], "msg": "No error",
                          "now": round(NOW * 1000), "total": 0 if prefix == "/v2-empty/" else 2, "ctime": 0,
                          "ptime": 0}
                    body = json.dumps(v2).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if prefix == "/slow/":
                    time.sleep(1.0)
                body = pages.get(prefix) or json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass                                   # the client gave up first

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", reply, requests
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def redirecting_host():
    """A host that answers every request with a 302 to a second host on another port, which records what it is asked
    and would answer with a reply. Yields the redirecting host's URL and the second host's requests."""
    asked: list[tuple[str, str | None]] = []
    servers: list[http.server.ThreadingHTTPServer] = []

    def serve(handler: type[http.server.BaseHTTPRequestHandler]) -> str:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            asked.append((self.path, self.headers.get(API_KEY_HEADER)))
            body = json.dumps(reapi_reply([], NOW)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    target = serve(Target)

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", target + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    try:
        yield serve(Redirector), asked
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_a_redirect_is_not_followed_so_the_key_never_reaches_another_host(redirecting_host):
    """urllib would follow a 3xx and send the x-api-key header on to wherever it points; a redirect fails its own poll
    as any status that is not a refusal does, and nothing is asked of the host it names."""
    base, asked = redirecting_host
    with pytest.raises(PollError, match="HTTP 302") as caught:
        fetch_reply(poll_url(base, ["a1b2c3"]), 5.0, "k-secret")
    assert (caught.value.status, caught.value.refused) == (302, False)
    assert asked == []


def test_fetch_reply_sends_the_user_agent_and_the_key_and_returns_the_reply(reapi_host):
    base, reply, requests = reapi_host
    url = poll_url(base + "/", ["abc123", "a1b2c3"])
    assert url == base + "/?find_hex=a1b2c3,abc123"                    # sorted, with no doubled slash
    assert fetch_reply(url, 5.0, "k-1") == reply
    assert requests == [("/?find_hex=a1b2c3,abc123", USER_AGENT, "k-1")]
    assert USER_AGENT == f"adsb-approach/{__version__} (+https://github.com/z2amiller/adsb-approach)"
    assert API_KEY_HEADER == "x-api-key"


def test_fetch_reply_without_a_key_sends_no_key_header(reapi_host):
    base, reply, requests = reapi_host
    assert fetch_reply(poll_url(base, ["a1b2c3"]), 5.0, None) == reply
    assert requests == [("/?find_hex=a1b2c3", USER_AGENT, None)]


@pytest.mark.parametrize("status, pauses", [(401, True), (403, True), (429, True), (500, True), (503, True),
                                            (400, False), (404, False)])
def test_a_refusal_raises_with_its_status(reapi_host, status, pauses):
    base = reapi_host[0]
    with pytest.raises(PollError, match=f"HTTP {status}") as caught:
        fetch_reply(poll_url(f"{base}/status/{status}", ["a1b2c3"]), 5.0, None)
    assert (caught.value.status, caught.value.refused) == (status, pauses)
    assert refused(status) is pauses


def test_refused_is_false_without_a_status():
    assert refused(None) is False and refused(200) is False and refused(499) is False and refused(500) is True


def test_a_stalled_host_raises_a_network_error_without_status(reapi_host):
    with pytest.raises(PollError) as caught:
        fetch_reply(poll_url(reapi_host[0] + "/slow", ["a1b2c3"]), 0.2, None)
    assert (caught.value.status, caught.value.refused) == (None, False)


@pytest.mark.parametrize("page", ["html", "not-object", "no-now", "no-aircraft", "text-now", "aircraft-object"])
def test_a_reply_that_is_not_re_api_json_fails_without_refusal(reapi_host, page):
    """A 200 that is a verification page, or JSON without a numeric `now` or a list `aircraft`, fails the poll as a
    network error does."""
    with pytest.raises(PollError, match="not a re-api reply") as caught:
        fetch_reply(poll_url(f"{reapi_host[0]}/{page}", ["a1b2c3"]), 5.0, None)
    assert (caught.value.status, caught.value.refused) == (None, False)


def test_a_poll_error_keeps_its_status_and_refusal():
    plain, pausing = PollError("TimeoutError: timed out"), PollError("HTTP 429", 429, True)
    assert (str(plain), plain.status, plain.refused) == ("TimeoutError: timed out", None, False)
    assert (str(pausing), pausing.status, pausing.refused) == ("HTTP 429", 429, True)


def test_read_api_key_strips_the_file_and_an_unreadable_one_pauses_polling(tmp_path):
    key = tmp_path / "key"
    key.write_text("  k-7\n")
    assert read_api_key("") is None and read_api_key(str(key)) == "k-7"
    key.write_text(" \n")
    with pytest.raises(PollError, match=f"^api_key_file: {key} holds no key$") as empty:
        read_api_key(str(key))
    with pytest.raises(PollError, match="^api_key_file: FileNotFoundError") as missing:
        read_api_key(str(tmp_path / "absent"))
    assert [(e.value.status, e.value.refused) for e in (empty, missing)] == [(None, True)] * 2


def test_a_key_file_that_is_not_utf_8_names_its_path(tmp_path):
    """A UnicodeDecodeError says nothing of the file it came from, so the error adds the path (an OSError's message
    carries it already); the `api_key_file: ` prefix is what the page's banner keys on."""
    key = tmp_path / "key"
    key.write_bytes(b"k-\xff\xfe")
    with pytest.raises(PollError, match=f"^api_key_file: {key}: UnicodeDecodeError: ") as caught:
        read_api_key(str(key))
    assert (caught.value.status, caught.value.refused) == (None, True)


# --- reading a reply -----------------------------------------------------------------------------------------------

def test_positions_from_reply_shift_label_and_skip():
    """A position seen 1.4 s before the reply's `now` of 1000 is at 998.6, and 0.10 s earlier on the receiver's
    clock."""
    here = {"lat": 37.6, "lon": -122.3, "alt_baro": 1500, "gs": 140.0, "track": 280.0, "type": "adsb_icao"}
    reply = {"now": 1000.0, "aircraft": [
        {"hex": "ABC123", "seen_pos": 1.4, "flight": "UAL2177 ", **here},
        {"hex": "~a35f3f", "seen_pos": 0.5, **here},                       # a non-ICAO address
        {"hex": "def456", "seen_pos": 0.5, "alt_baro": 900},               # no position
        {"hex": "012345", "seen_pos": 2.0, **here, "alt_baro": "ground"},
        "not an aircraft"]}
    got = positions_from_reply(reply, clock_offset_s=0.10)
    assert [(p.hex, p.origin, p.on_ground, p.callsign) for p in got] == [
        ("abc123", "adsb.lol", False, "UAL2177"), ("012345", "adsb.lol", True, None)]
    assert [p.t for p in got] == [pytest.approx(998.5), pytest.approx(997.9)]
    assert ORIGIN == "adsb.lol" and got[0].alt_baro == 1500 and got[0].source == "adsb_icao"
    assert positions_from_reply({"now": 5.0, "aircraft": []}, clock_offset_s=0.1) == []
    assert positions_from_reply(reply, clock_offset_s=0.0)[0].t == pytest.approx(998.6)


def test_a_fix_repeated_in_later_replies_keeps_one_time(tmp_path):
    """re-api prints `now` and `seen_pos` to the millisecond, and at the epoch a float step is 2.4e-7 s, so the same fix
    read against each poll's own `now` comes out a step apart unless its time goes back to the millisecond. Forty fixes,
    each repeated in four polls about 2 s apart: one time per fix, and the store keeps each once."""
    store = Store(tmp_path / "repeats.sqlite")
    store.migrate()
    run = store.new_run("live", "test", external_merge=True)
    times: list[float] = []
    new_per_poll: list[int] = []
    for reply in repeated_fix_replies(fixes=40, polls_per_fix=4):
        (p,) = positions_from_reply(reply, clock_offset_s=0.10)
        poll_id = store.record_poll(run_id=run, requested_at=reply["now"], arrived_at=reply["now"], asked=1, returned=1,
                                    http_status=200, error=None, refused=False)
        new_per_poll.append(store.insert_external_positions(poll_id, reply["now"], [p]))
        times.append(p.t)
    store.close()
    assert [len(set(times[i:i + 4])) for i in range(0, len(times), 4)] == [1] * 40
    assert len(set(times)) == 40 and times == sorted(times)
    assert new_per_poll == [1, 0, 0, 0] * 40


# --- the watch list ------------------------------------------------------------------------------------------------

def started(w: WatchList, hexid: str, approach_id: int, t: float) -> None:
    w.on_event("approach.started", t, approach_id, 100 + approach_id, {"hex": hexid, "runway": "28R"})


def completed(w: WatchList, hexid: str, approach_id: int, t: float, outcome: str) -> None:
    w.on_event("approach.completed", t, approach_id, 100 + approach_id, {"hex": hexid, "outcome": outcome})


def test_the_watch_list_follows_starts_and_closes():
    w = WatchList(1200.0)
    assert w.hexes(T0) == []
    started(w, "b2", 2, T0)
    started(w, "a1", 1, T0 + 5)
    assert w.hexes(T0 + 10) == ["a1", "b2"]                                   # sorted
    completed(w, "a1", 1, T0 + 60, "landed")
    assert w.hexes(T0 + 60) == ["b2"]
    # a go-around is watched on for watch_max_s, to the second
    started(w, "a1", 3, T0 + 100)
    completed(w, "a1", 3, T0 + 160, "go_around")
    assert w.hexes(T0 + 160) == ["a1", "b2"] and w.hexes(T0 + 160 + 1200) == ["a1", "b2"]
    assert w.hexes(T0 + 160 + 1200.5) == ["b2"]
    assert w.hexes(T0 + 161) == ["b2"]                                        # pruned: it does not come back
    # a touch-and-go too, and a new start inside the window keeps the aircraft with no deadline
    completed(w, "b2", 2, T0 + 300, "touch_and_go")
    assert w.hexes(T0 + 301) == ["b2"]
    started(w, "b2", 4, T0 + 400)
    assert w.hexes(T0 + 300 + 1200 + 1) == ["b2"] and w.hexes(T0 + 1_000_000) == ["b2"]
    completed(w, "b2", 4, T0 + 1_000_001, "landed")
    assert w.hexes(T0 + 1_000_001) == []


def test_a_new_start_ends_the_go_arounds_deadline_for_good():
    w = WatchList(1200.0)
    started(w, "a1", 1, T0)
    completed(w, "a1", 1, T0 + 60, "go_around")                               # watched until T0 + 1260
    started(w, "a1", 2, T0 + 100)
    completed(w, "a1", 2, T0 + 200, "landed")
    assert w.hexes(T0 + 201) == []


@pytest.mark.parametrize("outcome", ["landed", "lost", "lost_on_final", "abandoned", "runway_change"])
def test_only_a_go_around_or_touch_and_go_keeps_the_watch_after_the_close(outcome):
    w = WatchList(1200.0)
    started(w, "a1", 1, T0)
    completed(w, "a1", 1, T0 + 60, outcome)
    assert w.hexes(T0 + 60) == []


def test_the_watch_list_keeps_an_aircraft_across_a_runway_change_and_ignores_strangers():
    """The runway change closes #1 as runway_change and starts #2 in the same breath."""
    w = WatchList(1200.0)
    started(w, "a1", 1, T0)
    completed(w, "a1", 1, T0 + 60, "runway_change")
    started(w, "a1", 2, T0 + 60)
    assert w.hexes(T0 + 61) == ["a1"] and w.hexes(T0 + 60 + 3600) == ["a1"]
    started(w, "a1", 2, T0 + 70)                                              # a repeated start
    assert w.hexes(T0 + 71) == ["a1"]
    completed(w, "ffffff", 9, T0 + 80, "landed")                              # an aircraft never held
    completed(w, "a1", 7, T0 + 81, "landed")                                  # an attempt of a held one never opened
    assert w.hexes(T0 + 82) == ["a1"]
    completed(w, "a1", 2, T0 + 90, "landed")
    assert w.hexes(T0 + 91) == []


def test_the_watch_list_ignores_events_it_does_not_follow():
    w = WatchList(1200.0)
    w.on_event("approach.runway_change", T0, 1, 101, {"hex": "a1"})
    w.on_event("approach.go_around", T0, 1, 101, {"hex": "a1", "outcome": "go_around"})
    assert w.hexes(T0) == []


def test_an_aircraft_is_quiet_once_the_receiver_has_not_placed_it_for_quiet_s():
    """The start is a receiver position, so a new watch begins heard; only the receiver's own positions count."""
    w = WatchList(1200.0)
    started(w, "a1", 1, T0)
    started(w, "b2", 2, T0)
    assert w.quiet_hexes(T0 + 8.0, 8.0) == []                                 # not more than 8 s yet
    assert w.quiet_hexes(T0 + 8.5, 8.0) == ["a1", "b2"]
    w.heard(fix("b2", T0 + 10.0))
    assert w.quiet_hexes(T0 + 12.0, 8.0) == ["a1"] and w.hexes(T0 + 12.0) == ["a1", "b2"]
    w.heard(replace(fix("a1", T0 + 11.0), origin=ORIGIN))                     # adsb.lol's position is not the receiver's
    assert w.quiet_hexes(T0 + 12.0, 8.0) == ["a1"]
    w.heard(fix("b2", T0 + 5.0))                                              # an older position moves nothing back
    assert w.quiet_hexes(T0 + 18.0, 8.0) == ["a1"] and w.quiet_hexes(T0 + 18.5, 8.0) == ["a1", "b2"]


def test_a_go_around_stays_quiet_or_heard_through_its_watch_and_a_stranger_is_never_kept():
    w = WatchList(1200.0)
    started(w, "a1", 1, T0)
    w.heard(fix("a1", T0 + 50.0))
    completed(w, "a1", 1, T0 + 60.0, "go_around")
    assert w.quiet_hexes(T0 + 60.0, 8.0) == ["a1"]                            # 10 s since the receiver placed it
    w.heard(fix("a1", T0 + 70.0))                                             # the climb-out, heard again
    assert w.quiet_hexes(T0 + 75.0, 8.0) == []
    w.heard(fix("c3", T0 + 70.0))                                             # never watched: not kept
    started(w, "c3", 2, T0 + 100.0)
    assert w.quiet_hexes(T0 + 108.5, 8.0) == ["a1", "c3"]                     # c3 heard at its start, not before
    completed(w, "c3", 2, T0 + 110.0, "landed")
    started(w, "c3", 3, T0 + 200.0)                                           # a new watch starts heard again
    assert w.quiet_hexes(T0 + 205.0, 8.0) == ["a1"]


def test_quiet_s_zero_asks_about_every_watched_aircraft():
    w = WatchList(1200.0)
    started(w, "a1", 1, T0)
    w.heard(fix("a1", T0 + 1.0))
    assert w.quiet_hexes(T0 + 1.0, 0.0) == ["a1"] == w.hexes(T0 + 1.0)


# --- the poller ----------------------------------------------------------------------------------------------------

class Clock:
    """A clock the test moves by hand."""

    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeReapi:
    """A fake fetch: each call takes the next reply, a dict or an exception, and records its URL, time, timeout and
    key. A call with no reply left fails the test."""

    def __init__(self, clock: Clock) -> None:
        self.clock, self.replies = clock, []
        self.calls: list[tuple[str, float, float]] = []
        self.keys: list[str | None] = []

    def __call__(self, url: str, timeout_s: float, api_key: str | None) -> dict:
        self.calls.append((url, self.clock.t, timeout_s))
        self.keys.append(api_key)
        assert self.replies, f"unexpected request: {url}"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@dataclass
class Rig:
    cfg: Config
    store: Store
    run: int
    clock: Clock
    fetch: FakeReapi
    buffer: deque
    poller: Poller

    def watch(self, *hexes: str) -> None:
        for i, hexid in enumerate(hexes):
            self.poller.watch.on_event("approach.started", self.clock.t, 1 + i, 1 + i, {"hex": hexid})

    def reply(self, *hexes: str) -> None:
        """Queue a reply holding a position of each hex, seen a second before it was sent."""
        self.fetch.replies.append(reapi_reply([fix(h, self.clock.t - 1.0) for h in hexes], self.clock.t))

    def step(self) -> float:
        return asyncio.run(self.poller.step())

    def polls(self) -> list:
        return list(self.store.conn.execute("SELECT * FROM external_polls ORDER BY id"))


@pytest.fixture
def rig(request, tmp_path):
    """A poller on the example configuration. It asks about every watched aircraft (quiet_s = 0) unless a test sets
    `quiet_s` with `@pytest.mark.parametrize("rig", [<quiet_s>], indirect=True)`."""
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    cfg.external.quiet_s = getattr(request, "param", 0.0)
    store = Store(tmp_path / "poller.sqlite")
    store.migrate()
    run = store.new_run("live", "test", external_merge=True)
    clock = Clock(NOW)
    fetch, buffer = FakeReapi(clock), deque()
    yield Rig(cfg, store, run, clock, fetch, buffer, Poller(cfg, store, run, buffer, fetch=fetch, now=clock))
    store.close()


def test_the_poller_watches_for_watch_max_min_after_a_go_around(rig):
    assert rig.poller.watch.watch_max_s == rig.cfg.external.watch_max_min * 60.0 == 1200.0


def test_nothing_is_polled_while_the_watch_list_is_empty(rig):
    assert rig.step() == IDLE_S == 1.0
    assert rig.fetch.calls == [] and rig.polls() == []


@pytest.mark.parametrize("rig", [8.0], indirect=True)
def test_only_the_aircraft_the_receiver_has_lost_are_asked_about(rig):
    rig.watch("a1", "b2")                                                     # both started, so heard, at NOW
    rig.clock.t = NOW + 9.0
    rig.poller.watch.heard(fix("b2", NOW + 8.0))
    rig.reply("a1", "b2")
    assert rig.step() == 0.0
    assert [url for url, _, _ in rig.fetch.calls] == ["https://re-api.adsb.lol/?find_hex=a1"]
    assert [p.hex for p in rig.buffer] == ["a1"]                              # b2 was not asked about, so not kept
    assert [row["asked"] for row in rig.polls()] == [1]


@pytest.mark.parametrize("rig", [8.0], indirect=True)
def test_no_request_goes_out_while_the_receiver_hears_every_watched_aircraft(rig):
    rig.watch("a1")
    for t in (NOW + 2.0, NOW + 6.0, NOW + 10.0):
        rig.clock.t = t
        rig.poller.watch.heard(fix("a1", t - 0.5))
        assert rig.step() == IDLE_S
    assert rig.fetch.calls == [] and rig.polls() == []
    rig.clock.t = NOW + 18.6                                                  # 9.1 s after the receiver's last
    rig.reply("a1")
    assert rig.step() == 0.0 and len(rig.fetch.calls) == 1


def test_a_poll_asks_for_the_watched_aircraft_and_buffers_and_stores_their_positions(rig, caplog):
    rig.watch("abc123", "a1b2c3")
    rig.reply("a1b2c3", "abc123")
    assert rig.step() == 0.0
    assert rig.fetch.calls == [(poll_url(rig.cfg.external.url, ["a1b2c3", "abc123"]), NOW, rig.cfg.external.timeout_s)]
    assert rig.fetch.calls[0][0].endswith("/?find_hex=a1b2c3,abc123") and rig.fetch.keys == [None]
    assert [(p.hex, p.origin) for p in rig.buffer] == [("a1b2c3", "adsb.lol"), ("abc123", "adsb.lol")]
    assert [p.t for p in rig.buffer] == [pytest.approx(NOW - 1.1)] * 2          # seen a second before, less 0.10
    (row,) = rig.polls()
    assert (row["run_id"], row["asked"], row["returned"], row["http_status"], row["error"], row["refused"]) == (
        rig.run, 2, 2, 200, None, 0)
    assert (row["requested_at"], row["arrived_at"]) == (NOW, NOW)
    stored = list(rig.store.conn.execute(
        "SELECT hex, t, poll_id, arrived_at, source FROM external_positions ORDER BY hex"))
    assert [(r["hex"], r["poll_id"], r["arrived_at"], r["source"]) for r in stored] == [
        ("a1b2c3", row["id"], NOW, "adsb_icao"), ("abc123", row["id"], NOW, "adsb_icao")]
    assert [r["t"] for r in stored] == [pytest.approx(NOW - 1.1)] * 2
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_only_the_aircraft_asked_about_are_kept(rig):
    """A replay drops an unwatched aircraft's stored positions at the watch list, so live must not take one either:
    a reply holding an aircraft nobody asked for (the service answers more than it was asked) is read, not kept."""
    rig.watch("a1b2c3")
    rig.reply("a1b2c3", "abc123")
    rig.step()
    assert [p.hex for p in rig.buffer] == ["a1b2c3"]
    (row,) = rig.polls()
    assert (row["asked"], row["returned"]) == (1, 2)
    assert [r["hex"] for r in rig.store.conn.execute("SELECT hex FROM external_positions")] == ["a1b2c3"]


def test_a_repeated_position_is_buffered_again_but_stored_once(rig):
    """The newer-than rule drops the repeat downstream; the store keeps what the live run was offered once."""
    rig.watch("a1b2c3")
    rig.fetch.replies.append(reapi_reply([fix("a1b2c3", NOW - 1.0)], NOW))
    rig.step()
    rig.clock.t += 2.0
    rig.fetch.replies.append(reapi_reply([fix("a1b2c3", NOW - 1.0)], NOW + 2.0))
    rig.step()
    assert len(rig.buffer) == 2 and len(rig.polls()) == 2
    assert rig.store.conn.execute("SELECT COUNT(*) FROM external_positions").fetchone()[0] == 1


def test_a_reply_with_nothing_in_it_is_a_poll_that_returned_none(rig):
    rig.watch("a1b2c3")
    rig.fetch.replies.append({"now": NOW, "aircraft": [], "resultCount": 0, "ptime": 0.1})
    assert rig.step() == 0.0
    assert [(r["asked"], r["returned"], r["http_status"], r["refused"]) for r in rig.polls()] == [(1, 0, 200, 0)]
    assert list(rig.buffer) == []


def test_polls_are_poll_interval_s_apart(rig):
    rig.watch("a1b2c3")
    rig.reply("a1b2c3")
    rig.step()
    rig.clock.t += 0.5
    assert rig.step() == pytest.approx(1.5) and len(rig.fetch.calls) == 1
    rig.clock.t += 1.5
    rig.reply("a1b2c3")
    assert rig.step() == 0.0 and [t for _, t, _ in rig.fetch.calls] == [NOW, NOW + 2.0]


def test_a_poll_that_runs_long_is_followed_straight_away_not_overlapped(rig):
    rig.watch("a1b2c3")
    rig.fetch.replies.append(reapi_reply([fix("a1b2c3", NOW - 1.0)], NOW))
    rig.step()
    rig.clock.t += 7.0                                  # the next step comes long after the interval
    rig.reply("a1b2c3")
    assert rig.step() == 0.0 and len(rig.fetch.calls) == 2


def test_a_refusal_pauses_polling_for_backoff_min(rig, caplog):
    rig.watch("a1b2c3")
    rig.fetch.replies.append(PollError("HTTP 429", 429, True))
    with caplog.at_level(logging.WARNING, logger="adsb_approach.poller"):
        assert rig.step() == 0.0
    (row,) = rig.polls()
    assert (row["asked"], row["returned"], row["http_status"], row["error"], row["refused"]) == (
        1, None, 429, "HTTP 429", 1)
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "poller: paused for 15 min: HTTP 429"]
    for dt in (2.0, 60.0, 600.0):
        rig.clock.t = NOW + dt
        assert rig.step() == IDLE_S
    rig.clock.t = NOW + 899.5
    assert rig.step() == pytest.approx(0.5)
    assert len(rig.fetch.calls) == 1
    rig.clock.t = NOW + 900.0
    rig.reply("a1b2c3")
    assert rig.step() == 0.0 and len(rig.fetch.calls) == 2
    assert current_poll_pause(rig.store.conn, 900.0, NOW + 901.0) is None             # a 200 ended it


@pytest.mark.parametrize("value, expected", [
    ("37", 37.0), ("0", 0.0), (" 120 ", 120.0), ("99999", 3600.0),           # delta-seconds, capped at an hour
    ("Thu, 01 Jan 1970 00:01:30 GMT", 90.0), ("Thu, 01 Jan 1970 00:00:00 GMT", 0.0),   # an HTTP-date, at now = 0
    ("soon", None), ("-5", None), ("1.5", None), ("", None), (None, None),
    ("Wed, 31 Dec 1969 23:59:50 GMT", 0.0),                                    # a date already past
    ("Thu, 01 Jan 1970 02:00:00 GMT", 3600.0),                                 # a date over an hour ahead: capped
    ("Thu Jan  1 00:01:30 1970", 90.0),                                        # the asctime form: no zone, read as GMT
    ("\u00b2", None), ("1\u00b3", None),                                       # Latin-1 superscripts pass isdigit()
    ("Thu, 01 Jan 99999999999999999999 00:00:00 GMT", None),                   # these two overflow
    ("Thu, 01 Jan 1970 00:00:00 +9999999999999999999999", None),
])
def test_retry_after_reads_seconds_or_a_date(value, expected):
    assert retry_after_s(value, 0.0) == expected


@pytest.mark.parametrize("base, expected", [
    ("https://re-api.adsb.lol/", "https://re-api.adsb.lol/?find_hex=a1b2c3,abc123"),
    ("https://re-api.adsb.lol", "https://re-api.adsb.lol/?find_hex=a1b2c3,abc123"),
    ("https://api.adsb.lol/v2/hex/", "https://api.adsb.lol/v2/hex/a1b2c3,abc123"),
    ("https://api.adsb.lol/v2/hex", "https://api.adsb.lol/v2/hex/a1b2c3,abc123"),
])
def test_a_v2_hex_url_asks_by_path_and_any_other_by_find_hex(base, expected):
    assert poll_url(base, ["abc123", "a1b2c3"]) == expected


def test_a_v2_reply_reads_as_a_re_api_one(reapi_host):
    base, _, _ = reapi_host
    reply = fetch_reply(base + "/v2/hex/a1b2c3", 5.0, None)
    assert reply["now"] == pytest.approx(NOW) and [a["hex"] for a in reply["aircraft"]] == ["a1b2c3", "abc123"]
    positions = positions_from_reply(reply, clock_offset_s=0.10)
    assert [round(p.t, 3) for p in positions] == [round(NOW - 1.0 - 0.10, 3), round(NOW - 2.0 - 0.10, 3)]
    empty = fetch_reply(base + "/v2-empty/", 5.0, None)
    assert empty["aircraft"] == [] and empty["now"] == pytest.approx(NOW)


@pytest.mark.parametrize("odd", sorted(ODD_RETRY_AFTER))
def test_an_unreadable_retry_after_still_refuses_and_falls_back(reapi_host, odd):
    base, _, _ = reapi_host
    with pytest.raises(PollError) as told:
        fetch_reply(base + f"/status/429/{odd}/", 5.0, None)
    assert (told.value.status, told.value.refused, told.value.retry_after_s, str(told.value)) == (
        429, True, None, "HTTP 429")


@pytest.mark.parametrize("page", ["huge-now", "v2-huge-now", "v2-error", "v2-seconds", "v2-null-ac", "ctime-only"])
def test_a_body_that_only_looks_like_a_reply_fails_its_poll(reapi_host, page):
    base, _, _ = reapi_host
    with pytest.raises(PollError) as told:
        fetch_reply(f"{base}/{page}/", 5.0, None)
    assert (told.value.status, told.value.refused) == (None, False) and str(told.value).startswith("not a re-api reply")


def test_retry_after_wins_over_the_rate_limit_reset(reapi_host):
    base, _, _ = reapi_host
    with pytest.raises(PollError) as told:
        fetch_reply(base + "/status/429/37/30/", 5.0, None)
    assert (told.value.retry_after_s, str(told.value)) == (37.0, "HTTP 429 (Retry-After 37 s)")
    with pytest.raises(PollError) as fell:
        fetch_reply(base + "/status/429/sup2/30/", 5.0, None)          # unreadable Retry-After: the reset decides
    assert (fell.value.retry_after_s, str(fell.value)) == (30.0, "HTTP 429 (x-ratelimit-reset 30 s)")


def test_a_refusal_falls_back_to_the_rate_limit_reset(reapi_host):
    base, _, _ = reapi_host
    with pytest.raises(PollError) as told:
        fetch_reply(base + "/status/429/-/30/", 5.0, None)          # no Retry-After, x-ratelimit-reset 30
    assert (told.value.retry_after_s, str(told.value)) == (30.0, "HTTP 429 (x-ratelimit-reset 30 s)")


def test_a_refusal_carries_its_retry_after(reapi_host):
    base, _, _ = reapi_host
    with pytest.raises(PollError) as told:
        fetch_reply(base + "/status/429/37/", 5.0, None)
    assert (told.value.status, told.value.refused, told.value.retry_after_s, str(told.value)) == (
        429, True, 37.0, "HTTP 429 (Retry-After 37 s)")
    with pytest.raises(PollError) as untold:
        fetch_reply(base + "/status/429/", 5.0, None)
    assert (untold.value.retry_after_s, str(untold.value)) == (None, "HTTP 429")


def test_a_refusal_with_retry_after_pauses_only_that_long(rig, caplog):
    rig.watch("a1b2c3")
    rig.fetch.replies.append(PollError("HTTP 429 (Retry-After 60 s)", 429, True, retry_after_s=60.0))
    with caplog.at_level(logging.WARNING, logger="adsb_approach.poller"):
        assert rig.step() == 0.0
    (row,) = rig.polls()
    assert (row["http_status"], row["refused"], row["retry_after_s"]) == (429, 1, 60.0)
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "poller: paused for 60 s as re-api asked: HTTP 429 (Retry-After 60 s)"]
    rig.clock.t = NOW + 59.5
    assert rig.step() == pytest.approx(0.5) and len(rig.fetch.calls) == 1
    rig.clock.t = NOW + 60.0
    rig.reply("a1b2c3")
    assert rig.step() == 0.0 and len(rig.fetch.calls) == 2


def test_a_401_says_the_key_was_refused(rig, caplog):
    rig.watch("a1b2c3")
    rig.fetch.replies.append(PollError("HTTP 401", 401, True))
    with caplog.at_level(logging.WARNING, logger="adsb_approach.poller"):
        rig.step()
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "poller: paused for 15 min: HTTP 401, the key was refused"]


def test_a_timeout_skips_one_poll_without_pausing(rig, caplog):
    rig.watch("a1b2c3")
    rig.fetch.replies.append(PollError("TimeoutError: timed out"))
    with caplog.at_level(logging.WARNING, logger="adsb_approach.poller"):
        assert rig.step() == 0.0
    (row,) = rig.polls()
    assert (row["http_status"], row["error"], row["refused"], row["returned"]) == (
        None, "TimeoutError: timed out", 0, None)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "paused" in r.getMessage()]
    assert current_poll_pause(rig.store.conn, 900.0, NOW + 1.0) is None
    rig.clock.t += 0.5
    assert rig.step() == pytest.approx(1.5)                                          # the interval still holds
    rig.clock.t += 1.5
    rig.reply("a1b2c3")
    assert rig.step() == 0.0 and len(rig.fetch.calls) == 2
    assert [r["http_status"] for r in rig.polls()] == [None, 200]


def test_an_outage_is_logged_once_at_info_and_its_end_once(rig, caplog):
    """Every poll that fails is in external_polls; the log says an outage began and that it ended, not every skipped
    poll (a night of timeouts would write some 40,000 lines)."""
    rig.watch("a1b2c3")
    rig.fetch.replies.extend(PollError("TimeoutError: timed out") for _ in range(3))
    with caplog.at_level(logging.DEBUG, logger="adsb_approach.poller"):
        for _ in range(3):
            rig.step()
            rig.clock.t += 2.0
        failed = [(r.levelno, r.getMessage()) for r in caplog.records]
        assert failed == [(logging.INFO, "poller: poll failed, the next one goes on: TimeoutError: timed out"),
                          (logging.DEBUG, "poller: poll failed, the next one goes on: TimeoutError: timed out"),
                          (logging.DEBUG, "poller: poll failed, the next one goes on: TimeoutError: timed out")]
        caplog.clear()
        rig.reply("a1b2c3")
        rig.step()
        rig.clock.t += 2.0
        rig.reply("a1b2c3")
        rig.step()
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.INFO, "poller: polls succeed again after 3 failed")]
    assert [r["http_status"] for r in rig.polls()] == [None, None, None, 200, 200]          # every poll is recorded


def test_a_second_outage_is_logged_again(rig, caplog):
    rig.watch("a1b2c3")
    with caplog.at_level(logging.INFO, logger="adsb_approach.poller"):
        for outcome in (PollError("TimeoutError: timed out"), None, PollError("HTTP 404", 404)):
            if outcome is None:
                rig.reply("a1b2c3")
            else:
                rig.fetch.replies.append(outcome)
            rig.step()
            rig.clock.t += 2.0
    assert [r.getMessage() for r in caplog.records] == [
        "poller: poll failed, the next one goes on: TimeoutError: timed out",
        "poller: polls succeed again after 1 failed",
        "poller: poll failed, the next one goes on: HTTP 404"]


def test_a_poll_that_succeeds_after_a_pause_says_so(rig, caplog):
    rig.watch("a1b2c3")
    rig.fetch.replies.append(PollError("HTTP 429", 429, True))
    with caplog.at_level(logging.INFO, logger="adsb_approach.poller"):
        rig.step()
        rig.clock.t += 900.0
        rig.reply("a1b2c3")
        rig.step()
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "poller: paused for 15 min: HTTP 429"),
        (logging.INFO, "poller: polls succeed again after 1 failed")]


def test_an_unreadable_key_file_pauses_polling_without_a_request(rig, tmp_path, caplog):
    rig.cfg.external.api_key_file = str(tmp_path / "absent.key")
    rig.watch("a1b2c3", "abc123")
    with caplog.at_level(logging.WARNING, logger="adsb_approach.poller"):
        assert rig.step() == 0.0
    assert rig.fetch.calls == []
    (row,) = rig.polls()
    assert (row["asked"], row["returned"], row["http_status"], row["refused"]) == (2, None, None, 1)
    assert row["error"].startswith("api_key_file: FileNotFoundError")
    assert any(r.levelno == logging.WARNING and "paused for 15 min: api_key_file: FileNotFoundError" in r.getMessage()
               for r in caplog.records)
    rig.clock.t += 60.0
    assert rig.step() == IDLE_S and rig.fetch.calls == []


def test_the_key_file_is_read_at_every_poll_and_its_key_sent(rig, tmp_path):
    key = tmp_path / "adsb.key"
    key.write_text("k-1\n")
    rig.cfg.external.api_key_file = str(key)
    rig.watch("a1b2c3")
    rig.reply("a1b2c3")
    rig.step()
    key.write_text("k-2\n")
    rig.clock.t += 2.0
    rig.reply("a1b2c3")
    rig.step()
    assert rig.fetch.keys == ["k-1", "k-2"]


def test_a_pause_left_by_an_earlier_run_still_holds(rig):
    """The pause is read from external_polls, not held in memory, so a restarted collector keeps it."""
    earlier = rig.store.new_run("live", "test", external_merge=True)
    rig.store.record_poll(run_id=earlier, requested_at=NOW - 100.0, arrived_at=NOW - 99.0, asked=1, returned=None,
                          http_status=429, error="HTTP 429", refused=True)
    rig.watch("a1b2c3")
    assert rig.step() == IDLE_S and rig.fetch.calls == []
    rig.clock.t = NOW - 99.0 + 900.0 - 0.25
    assert rig.step() == pytest.approx(0.25) and rig.fetch.calls == []
    rig.clock.t = NOW - 99.0 + 900.0
    rig.reply("a1b2c3")
    assert rig.step() == 0.0 and len(rig.fetch.calls) == 1


def test_the_pause_does_not_show_while_nothing_is_watched(rig):
    rig.store.record_poll(run_id=rig.run, requested_at=NOW - 2.0, arrived_at=NOW - 1.0, asked=1, returned=None,
                          http_status=503, error="HTTP 503", refused=True)
    assert rig.step() == IDLE_S                                                      # idle, not "paused for 15 min"


def test_a_go_around_keeps_the_poll_going_after_the_close_until_the_cap(rig):
    rig.watch("a1b2c3")
    rig.poller.watch.on_event("approach.completed", NOW, 1, 1, {"hex": "a1b2c3", "outcome": "go_around"})
    rig.clock.t = NOW + 1200.0
    rig.reply("a1b2c3")
    assert rig.step() == 0.0 and len(rig.fetch.calls) == 1
    rig.clock.t = NOW + 1200.5
    assert rig.step() == IDLE_S and len(rig.fetch.calls) == 1


# --- the task ------------------------------------------------------------------------------------------------------

def test_run_poller_survives_a_step_that_raises(rig, caplog):
    """A bug in a step is logged, the poller waits poll_interval_s and goes on."""
    results = [RuntimeError("boom"), RuntimeError("bang"), 0.5, 0.0, 0.0]
    sleeps: list[float] = []

    async def step() -> float:
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 4:
            raise asyncio.CancelledError

    rig.poller.step = step
    with caplog.at_level(logging.ERROR, logger="adsb_approach.poller"), pytest.raises(asyncio.CancelledError):
        asyncio.run(run_poller(rig.poller, sleep=sleep))
    assert sleeps == [2.0, 2.0, 0.5, 0.0] and sleeps[0] == rig.cfg.external.poll_interval_s
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert [r.exc_info[1].args[0] for r in errors] == ["boom", "bang"]
    assert results == [0.0]                                                          # the fifth step never came

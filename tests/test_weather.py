import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from adsb_approach.weather import WeatherTable, parse_awc_json, poll_metars

FIXTURE = Path(__file__).parent / "fixtures" / "metar-20260912.json"


def test_parse_fixture():
    obs = parse_awc_json(json.loads(FIXTURE.read_text()))
    assert len(obs) == 84
    assert {o.airport for o in obs} == {"KSFO", "KOAK", "KNUQ", "KSJC"}
    sfo = [o for o in obs if o.airport == "KSFO" and o.observed_at == 1789239360][0]
    assert sfo.altim_hpa == 1015.3
    assert sfo.wind_dir == 280 and sfo.wind_kt == 14 and sfo.gust_kt == 24
    assert sfo.raw.startswith("METAR KSFO 121856Z")


def test_lookup_latest_at_or_before():
    wt = WeatherTable()
    wt.extend(parse_awc_json(json.loads(FIXTURE.read_text())))
    w = wt.at("KSFO", 1789239911)            # 19:05:11Z
    assert w is not None and w.observed_at == 1789239360
    assert wt.at("KSFO", 1789239360).observed_at == 1789239360   # at the observation time itself
    assert wt.at("KSFO", 1789239359) is not None and wt.at("KSFO", 1789239359).observed_at < 1789239360
    assert wt.at("KSFO", 1789100000) is None  # before the first observation
    assert wt.at("KXYZ", 1789239911) is None


def test_variable_wind_parses_to_none():
    obs = parse_awc_json([{"icaoId": "KNUQ", "obsTime": 1789245300, "altim": 1014.3, "temp": 23, "dewp": 13,
                           "wdir": "VRB", "wspd": 3, "wgst": None, "rawOb": "METAR KNUQ 122035Z VRB03KT"}])
    assert obs[0].wind_dir is None and obs[0].wind_kt == 3 and obs[0].gust_kt is None


def test_malformed_items_are_skipped_and_a_non_list_is_rejected():
    good = {"icaoId": "KSFO", "obsTime": 1789239360, "altim": 1015.3, "rawOb": "METAR KSFO 121856Z"}
    assert len(parse_awc_json([good, "junk", {"icaoId": "KSFO", "obsTime": "soon"}, {"icaoId": "KOAK"}])) == 1
    with pytest.raises(ValueError):
        parse_awc_json({"error": "rate limited"})


def test_prune_keeps_each_airports_latest_observation():
    obs = parse_awc_json(json.loads(FIXTURE.read_text()))
    wt = WeatherTable()
    wt.extend(obs)
    latest = {a: wt.latest(a) for a in wt.airports()}
    assert wt.prune(max(o.observed_at for o in obs) + 3600) == len(obs) - 4
    assert {a: wt.latest(a) for a in wt.airports()} == latest and wt.at("KSFO", 1789239911) is None
    assert wt.prune(0.0) == 0


def test_metar_poller_keeps_the_last_observations_when_a_fetch_fails():
    sfo = sorted((o for o in parse_awc_json(json.loads(FIXTURE.read_text())) if o.airport == "KSFO"), key=lambda o: o.observed_at)
    table, stored, seen = WeatherTable(), [], []
    replies = [[sfo[-2]], OSError("aviationweather.gov timed out"), [sfo[-1], sfo[-2]]]

    def fetch(ids):
        seen.append((ids, table.latest("KSFO")))
        reply = replies[min(len(seen), len(replies)) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def main():
        task = asyncio.create_task(poll_metars(table, ["KSFO"], 0.01, fetch=fetch, on_new=stored.append,
                                               now=lambda: sfo[-1].observed_at))
        while len(stored) < 2:
            await asyncio.sleep(0.005)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert stored == [sfo[-2], sfo[-1]]                     # each observation reported once, when it first arrives
    assert seen[2] == (["KSFO"], sfo[-2])                   # the failed fetch left the last observation in place
    assert table.latest("KSFO") is sfo[-1]


def test_a_failed_fetch_retries_after_the_short_delay_not_the_full_interval():
    """A stale altimeter setting can flip the 100 ft touchdown rule, so a failed fetch must not wait out the whole
    (10 minute default) interval before trying again."""
    table = WeatherTable()
    calls, delays = {"n": 0}, []

    def fetch(ids):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("aviationweather.gov timed out")
        return []

    async def sleep(s):
        delays.append(s)
        if len(delays) == 2:
            raise asyncio.CancelledError            # stop the loop once both delays are recorded

    async def main():
        with contextlib.suppress(asyncio.CancelledError):
            await poll_metars(table, ["KSFO"], 600.0, fetch=fetch, sleep=sleep)

    asyncio.run(main())
    assert delays == [60.0, 600.0]                          # the retry is short; the next poll waits the full interval

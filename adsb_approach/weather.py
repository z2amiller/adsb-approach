"""METAR observations: parsing the aviationweather.gov JSON, time lookup, fetching and live polling."""
from __future__ import annotations

import asyncio
import bisect
import json
import logging
import time
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Iterable

from .model import Weather

log = logging.getLogger(__name__)
AWC_URL = "https://aviationweather.gov/api/data/metar"
WEATHER_KEEP_S = 6 * 3600.0         # METAR history the live poller keeps in memory
RETRY_AFTER_FAILURE_S = 60.0        # a failed fetch retries soon rather than waiting out the full interval


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    f = _num(v)
    return None if f is None else int(round(f))


def parse_awc_json(items: list[dict]) -> list[Weather]:
    if not isinstance(items, list):
        raise ValueError(f"expected a JSON list of METARs, got {type(items).__name__}")
    out: list[Weather] = []
    for it in items:
        t = _num(it.get("obsTime")) if isinstance(it, dict) else None
        if t is None or not it.get("icaoId"):
            continue
        out.append(Weather(
            airport=str(it["icaoId"]).upper(),
            observed_at=t,
            altim_hpa=_num(it.get("altim")),
            temp_c=_num(it.get("temp")),
            dewp_c=_num(it.get("dewp")),
            wind_dir=_int(it.get("wdir")),          # "VRB" -> None
            wind_kt=_int(it.get("wspd")),
            gust_kt=_int(it.get("wgst")),
            raw=str(it.get("rawOb") or ""),
        ))
    return out


class WeatherTable:
    """Per-airport observations sorted by time; `at()` returns the latest at or before t."""

    def __init__(self) -> None:
        self._obs: dict[str, list[Weather]] = {}
        self._keys: dict[str, list[float]] = {}

    def add(self, w: Weather) -> bool:
        """Insert w, replacing an observation for the same airport and time; True when that time was new."""
        obs = self._obs.setdefault(w.airport, [])
        keys = self._keys.setdefault(w.airport, [])
        i = bisect.bisect_left(keys, w.observed_at)
        if i < len(keys) and keys[i] == w.observed_at:
            obs[i] = w
            return False
        keys.insert(i, w.observed_at)
        obs.insert(i, w)
        return True

    def extend(self, ws: Iterable[Weather]) -> None:
        for w in sorted(ws, key=lambda o: o.observed_at):   # the API lists newest first
            self.add(w)

    def at(self, airport: str, t: float) -> Weather | None:
        keys = self._keys.get(airport)
        if not keys:
            return None
        i = bisect.bisect_right(keys, t)
        return None if i == 0 else self._obs[airport][i - 1]

    def latest(self, airport: str) -> Weather | None:
        obs = self._obs.get(airport)
        return obs[-1] if obs else None

    def airports(self) -> list[str]:
        return sorted(self._obs)

    def prune(self, before: float) -> int:
        """Forget observations older than `before`, except each airport's latest, which scoring may still fall back on.
        Returns how many went."""
        dropped = 0
        for airport, keys in self._keys.items():
            n = min(bisect.bisect_left(keys, before), len(keys) - 1)
            if n > 0:
                del keys[:n]
                del self._obs[airport][:n]
                dropped += n
        return dropped


def fetch_metar_raw(ids: list[str], hours: int = 0, timeout: float = 10.0) -> list[dict]:
    """Raises OSError (network, HTTP status, timeout), http.client.HTTPException, or ValueError (bad JSON)."""
    params = {"ids": ",".join(ids), "format": "json"}
    if hours:
        params["hours"] = str(hours)
    url = AWC_URL + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body.strip() else []   # 204 No Content: no observations for these ids


def fetch_metars(ids: list[str], hours: int = 0, timeout: float = 10.0) -> list[Weather]:
    return parse_awc_json(fetch_metar_raw(ids, hours, timeout))


async def poll_metars(table: WeatherTable, ids: list[str], interval_s: float, *,
                      fetch: Callable[[list[str]], list[Weather]] = fetch_metars,
                      on_new: Callable[[Weather], object] | None = None,
                      now: Callable[[], float] = time.time,
                      sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Fetch the latest METAR for `ids` into `table` every interval_s until cancelled, in a worker thread so the event
    loop keeps running. A failed fetch is logged, the table keeps its last observations, which scoring uses until the
    QNH and temperature age limits run out, and the next attempt follows after RETRY_AFTER_FAILURE_S rather than the
    full interval, since a live approach cannot see a fresh altimeter setting until one arrives. `on_new` receives
    each observation the table did not hold yet."""
    while True:
        try:
            observations = await asyncio.to_thread(fetch, ids)
        except Exception as exc:              # OSError, http.client.HTTPException, ValueError: keep what the table has
            log.warning("METAR fetch for %s failed, keeping the last observations: %s", ",".join(ids), exc)
            await sleep(min(interval_s, RETRY_AFTER_FAILURE_S))
            continue
        for w in observations:
            if table.add(w) and on_new is not None:
                on_new(w)
        table.prune(now() - WEATHER_KEEP_S)
        await sleep(interval_s)

"""Synthetic replies from adsb.lol's re-api, built from Positions: no fetched data."""
from __future__ import annotations

import json
import random

from adsb_approach.model import Position
from adsb_approach.sources.base import readsb_object


def reapi_object(p: Position, now: float) -> dict:
    """One entry of a reply's `aircraft` list as re-api sends it: the JSON-port object without its own `now`, and a
    `seen_pos` that puts the position at p.t when read against the reply's `now`, to the millisecond as re-api
    prints it."""
    obj = readsb_object(p)
    del obj["now"]
    obj["seen_pos"] = round(now - p.t, 3)
    return obj


def reapi_reply(positions: list[Position], now: float) -> dict:
    """A whole reply, as the poller's fetch returns it."""
    return {"now": now, "aircraft": [reapi_object(p, now) for p in positions], "resultCount": len(positions),
            "ptime": 0.1}


EPOCH_MS = 1_792_800_000_000        # a time of the order the live service sees: float steps of 2.4e-7 s


def repeated_fix_replies(fixes: int = 40, polls_per_fix: int = 4, *, seed: int = 7) -> list[dict]:
    """The replies of a poll every ~2 s that watches one aircraft report a new fix every `polls_per_fix` polls: each
    fix appears in `polls_per_fix` replies in a row, its `seen_pos` growing by what the polls are apart. Built as JSON
    text printed to the millisecond, as re-api prints `now` and `seen_pos`, and parsed back, so the floats are the
    ones the poller reads off the wire. The polls are a few ms off two seconds apart, as real ones are."""
    rng = random.Random(seed)
    replies, now_ms = [], EPOCH_MS
    for i in range(fixes):
        fix_ms = EPOCH_MS + polls_per_fix * 2000 * i - rng.randrange(100, 1900)
        for _ in range(polls_per_fix):
            replies.append(json.loads(
                '{"now":%.3f,"aircraft":[{"hex":"a1b2c3","lat":37.6,"lon":-122.3,"alt_baro":1500,"gs":140.0,'
                '"track":280.0,"type":"adsb_icao","seen_pos":%.3f}],"resultCount":1,"ptime":0.1}'
                % (now_ms / 1000, (now_ms - fix_ms) / 1000)))
            now_ms += 2000 + rng.randrange(0, 40)
    return replies

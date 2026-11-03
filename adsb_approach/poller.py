"""The live poller: while an aircraft is on the watch list it asks adsb.lol's re-api for its position every
poll_interval_s, and hands what comes back to the collector's buffer, stamped with the origin "adsb.lol" and moved onto
the receiver's clock. Failures stay in this module's task: a refusal pauses polling for as long as its Retry-After asks,
else backoff_min (the pause is read back from external_polls, so a restart keeps it), and anything else skips one
poll."""
from __future__ import annotations

import asyncio
import datetime
import email.utils
import heapq
import http.client
import json
import logging
import math
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import replace
from pathlib import Path

from . import __version__
from .config import Config
from .model import Outcome, Position
from .sources.base import position_from_readsb
from .store.db import Store, current_poll_pause

ORIGIN = "adsb.lol"                                    # Position.origin of a polled position
USER_AGENT = f"adsb-approach/{__version__} (+https://github.com/z2amiller/adsb-approach)"
API_KEY_HEADER = "x-api-key"
IDLE_S = 1.0                  # the longest the poller sleeps before it looks at the watch list and the pause again
# the outcomes after which the next attempt may follow, so the aircraft stays watched
KEEP_WATCHING = frozenset({Outcome.GO_AROUND.value, Outcome.TOUCH_AND_GO.value})

log = logging.getLogger(__name__)


def refused(status: int | None) -> bool:
    """The statuses that pause polling, 401, 403, 429 and every 5xx. None, a failure without an answer, and every other
    status skip only their own poll."""
    return status is not None and (status in (401, 403, 429) or status >= 500)


class PollError(Exception):
    """A poll that failed: `status` is the HTTP status of an answer (None for a timeout, a network error, a reply that
    isn't re-api's, or a key file that couldn't be read), `refused` whether it pauses the polls after it, and
    `retry_after_s` how long the answer's Retry-After asked to wait (None without one)."""

    def __init__(self, message: str, status: int | None = None, refused: bool = False,
                 retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.status, self.refused, self.retry_after_s = status, refused, retry_after_s


RETRY_AFTER_MAX_S = 3600.0     # a Retry-After longer than this is taken as this: an hour is already a long silence


def retry_after_s(value: str | None, now: float) -> float | None:
    """Seconds a Retry-After header asks to wait (RFC 9110 §10.2.3): whole delta-seconds or an HTTP-date (one
    without a zone, the asctime form, read as GMT as the RFC says), a date in the past as 0, and either capped at
    RETRY_AFTER_MAX_S. None for a missing or unreadable header, which leaves the pause to [external] backoff_min. It
    never raises: headers arrive decoded as Latin-1, where str.isdigit also accepts superscript digits, and an absurd
    date overflows."""
    if value is None:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return min(float(value), RETRY_AFTER_MAX_S)
    try:
        when = email.utils.parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        return min(max(when.timestamp() - now, 0.0), RETRY_AFTER_MAX_S)
    except (TypeError, ValueError, OverflowError):
        return None


def read_api_key(path: str) -> str | None:
    """The key in `path`, without surrounding whitespace, or None for an empty path. A file that can't be read, or
    holds no key, raises a refused PollError without a status: nothing is sent without the key the configuration
    names, and polling pauses as it does after a refusal."""
    if not path:
        return None
    try:
        key = Path(path).read_text().strip()
    except UnicodeDecodeError as exc:                          # unlike an OSError's, its message doesn't name the file
        raise PollError(f"api_key_file: {path}: {type(exc).__name__}: {exc}", refused=True) from None
    except OSError as exc:
        raise PollError(f"api_key_file: {type(exc).__name__}: {exc}", refused=True) from None
    if not key:
        raise PollError(f"api_key_file: {path} holds no key", refused=True)
    return key


def poll_url(base: str, hexes: Iterable[str]) -> str:
    """The query for `hexes`, in sorted order so one watch list always asks the same way. A base ending in /v2/hex is
    adsb.lol's keyed v2 API, which takes the hexes comma-separated in the path and passes them to re-api's find_hex; any
    other base is re-api itself, asked `?find_hex=`."""
    base = base.rstrip("/")
    joined = ",".join(sorted(hexes))
    return f"{base}/{joined}" if base.endswith("/v2/hex") else f"{base}/?find_hex={joined}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies a request's headers, the key among them, onto the request a 3xx names, whatever host that is. With
    no new request to make, the 3xx falls through to the HTTPError it would be."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch_reply(url: str, timeout_s: float, api_key: str | None) -> dict:
    """GET one re-api reply with this program's User-Agent, and `api_key` in API_KEY_HEADER when there is one. A
    redirect is never followed, so the key goes to the configured host alone. Raises PollError, with the status of a
    3xx, 4xx or 5xx answer, or without one for a timeout, a network error, or a 200 that isn't a re-api reply (an HTML
    verification page, JSON without `now` or `aircraft`): that fails its own poll like a network error, never
    pausing. It blocks: run it in a worker thread."""
    headers = {"User-Agent": USER_AGENT} | ({API_KEY_HEADER: api_key} if api_key else {})
    request = urllib.request.Request(url, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout_s) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        exc.close()                                            # an HTTPError holds the connection until closed
        # Retry-After when there is one; else x-ratelimit-reset, the seconds until the window resets, which adsb.lol's
        # v2 API sends on keyless answers
        wait, said = None, ""
        for header in ("Retry-After", "x-ratelimit-reset"):
            wait = retry_after_s(exc.headers.get(header) if exc.headers else None, time.time())
            if wait is not None:
                said = f" ({header} {wait:g} s)"
                break
        raise PollError(f"HTTP {exc.code}{said}", exc.code, refused(exc.code), wait) from None
    except (OSError, http.client.HTTPException) as exc:        # URLError, timeouts, resets
        raise PollError(f"{type(exc).__name__}: {exc}") from None
    try:
        reply = _as_reapi(json.loads(body))
    except (ValueError, RecursionError) as exc:                # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise PollError(f"not a re-api reply: {type(exc).__name__}: {exc}") from None
    now = reply.get("now") if isinstance(reply, dict) else None
    if (isinstance(now, bool) or not isinstance(now, (int, float)) or not _finite(now)
            or not isinstance(reply.get("aircraft"), list)):
        raise PollError("not a re-api reply: no numeric `now` and list `aircraft`")
    return reply


V2_NOW_MS = (1e11, 1e14)       # a v2 `now` must look like epoch milliseconds (1973 to 5138): seconds would be 1970


def _finite(x: int | float) -> bool:
    """math.isfinite that never raises: an int too large for a float (a 400-digit `now`) is not finite here."""
    try:
        return math.isfinite(x)
    except OverflowError:
        return False


def _as_reapi(reply: object) -> object:
    """adsb.lol's v2 reply (the `jv2` shape: `ac`, and `now` in milliseconds) as re-api's own (`aircraft`, `now` in
    seconds), so everything after the fetch reads one shape; a body with `ac` is v2. Anything else is returned as it
    came. A v2 body whose `msg` is not "No error" (an error that still parses) fails its poll like a network error,
    and so does a `now` that isn't plausibly milliseconds: both would otherwise read as a successful empty poll."""
    if not isinstance(reply, dict) or "aircraft" in reply or "ac" not in reply:
        return reply
    msg = reply.get("msg")
    if msg is not None and msg != "No error":
        raise PollError(f"not a re-api reply: v2 says {str(msg)[:80]!r}")
    now = reply.get("now")
    if isinstance(now, (int, float)) and not isinstance(now, bool) and V2_NOW_MS[0] < now < V2_NOW_MS[1]:
        now = now / 1000.0
    else:
        now = None                                        # the check after this refuses the reply
    return {"now": now, "aircraft": reply.get("ac"), "resultCount": reply.get("total")}


def positions_from_reply(reply: dict, *, clock_offset_s: float) -> list[Position]:
    """A reply's positions as the pipeline takes them: each entry read against the reply's `now`, moved clock_offset_s
    earlier onto the receiver's clock, rounded to the millisecond, and labelled ORIGIN. Entries without a position, and
    `~`-prefixed non-ICAO addresses, which the matcher never tracks, are skipped; hexes come back lower case."""
    out: list[Position] = []
    for obj in reply["aircraft"]:
        p = position_from_readsb(obj, now=reply["now"]) if isinstance(obj, dict) else None
        if p is None or p.hex.startswith("~"):
            continue
        # re-api prints `now` and `seen_pos` as whole milliseconds, but the subtraction in float differs between polls
        # by a step (2.4e-7 s at the epoch) for one fix; rounding back to the millisecond gives a fix repeated in later
        # polls one time, so the store's (hex, t) key and the pipeline's newer-than rule see it as the same position.
        out.append(replace(p, t=round(p.t - clock_offset_s, 3), origin=ORIGIN))
    return out


class WatchList:
    """The aircraft to ask re-api about, kept from the matcher's events: an aircraft is watched while it has an approach
    open, and after a go-around or touch-and-go closes it for `watch_max_s` more, or until its next attempt starts,
    whichever comes first. A runway change closes one attempt and opens the next in one breath, so the aircraft never
    leaves the list. A go_around or touch_and_go completion sets that deadline even for an aircraft the list never
    held. It also keeps when the receiver last placed each watched aircraft (heard), so the poller can ask about only
    the ones the receiver has lost (quiet_hexes)."""

    def __init__(self, watch_max_s: float) -> None:
        self.watch_max_s = watch_max_s
        self._open: dict[str, set[int | None]] = {}      # hex -> the ids of its open approaches
        self._until: dict[str, float] = {}               # hex -> when its watch after a go-around ends
        self._heard: dict[str, float] = {}               # hex -> t of the receiver's newest position of it, while watched

    def on_event(self, kind: str, t: float, approach_id: int | None, flight_id: int | None, payload: dict) -> None:
        """EventBus subscriber. Events about other things change nothing, and nor does any other kind of completion
        for an aircraft the list never held; a go-around's or touch-and-go's watches it all the same. A start comes from
        a receiver position, so the aircraft it puts on the list is heard at `t`."""
        if kind == "approach.started":
            hexid = payload["hex"]
            self._open.setdefault(hexid, set()).add(approach_id)
            self._until.pop(hexid, None)
            self._heard[hexid] = max(self._heard.get(hexid, t), t)
        elif kind == "approach.completed":
            hexid = payload["hex"]
            open_ids = self._open.get(hexid, set())
            open_ids.discard(approach_id)
            if not open_ids:
                self._open.pop(hexid, None)
            if payload.get("outcome") in KEEP_WATCHING:
                self._until[hexid] = t + self.watch_max_s
            elif hexid not in self._open and hexid not in self._until:
                self._heard.pop(hexid, None)

    def heard(self, pos: Position) -> None:
        """The receiver placed `pos.hex` at `pos.t`: kept for a watched aircraft (an older position moves nothing back),
        ignored for any other and for adsb.lol's own positions."""
        if pos.origin == ORIGIN or (pos.hex not in self._open and pos.hex not in self._until):
            return
        self._heard[pos.hex] = max(self._heard.get(pos.hex, pos.t), pos.t)

    def hexes(self, now: float) -> list[str]:
        """The aircraft to ask about at `now`, sorted, with every watch that ended before `now` forgotten."""
        self._until = {h: until for h, until in self._until.items() if until >= now}
        watched = self._open.keys() | self._until.keys()
        self._heard = {h: t for h, t in self._heard.items() if h in watched}
        return sorted(watched)

    def quiet_hexes(self, now: float, quiet_s: float) -> list[str]:
        """The watched aircraft the receiver has given no position of for more than `quiet_s` at `now`, sorted; with
        quiet_s 0, every watched aircraft."""
        hexes = self.hexes(now)
        if quiet_s <= 0:
            return hexes
        return [h for h in hexes if now - self._heard.get(h, -math.inf) > quiet_s]


def replay_merge(local: Iterable[Position], external: Iterable[tuple[float, Position]],
                 watch: WatchList) -> Iterator[Position]:
    """A replay's stream with a live run's stored adsb.lol positions merged back in, as the live run merged them. A
    local position's key is its `t` and an external one's the `arrived_at` the collector stored, so the two come out in
    the order the pipeline saw them; both inputs must already be in key order, and a tie keeps local first (heapq.merge
    is stable). An external position is yielded only if its hex is on `watch` at that moment. The check follows the
    merge for that reason: `watch` is fed by the replay pipeline's events, and an approach starts or ends on a local
    position yielded earlier, which a check made as the external position was read, ahead of that local one, would not
    yet have seen."""
    keyed = heapq.merge(((p.t, p, False) for p in local), ((arrived_at, p, True) for arrived_at, p in external),
                        key=lambda item: item[0])
    for key, p, is_external in keyed:
        if not is_external or p.hex in watch.hexes(key):
            yield p


class Poller:
    """The poller task of `run`. A step sends at most one request, for the watched aircraft the receiver has lost
    (quiet_s): none while there are none or a pause from external_polls lasts, and none sooner than poll_interval_s
    after the last, so polls never overlap. The collector tells the watch list what the receiver places. The request runs
    in a worker thread; the rest runs on the event loop through the collector's own Store, so each poll's row and
    positions commit on their own, outside the drain's batch. Replays build no Poller, so they never fetch."""

    def __init__(self, cfg: Config, store: Store, run_id: int, buffer: deque[Position], *,
                 fetch: Callable[[str, float, str | None], dict] = fetch_reply,
                 now: Callable[[], float] = time.time) -> None:
        self.cfg, self.ext, self.store, self.run_id, self.buffer = cfg, cfg.external, store, run_id, buffer
        self.fetch, self.now = fetch, now
        self.watch = WatchList(self.ext.watch_max_min * 60.0)
        self.last_request_at = -math.inf
        self._failed_in_a_row = 0      # polls that failed since the last one that did not, for the log's sake

    async def step(self) -> float:
        """Poll if a poll is due and allowed, and return how long to sleep before the next step."""
        now = self.now()
        hexes = self.watch.quiet_hexes(now, self.ext.quiet_s)
        if not hexes:
            return IDLE_S
        pause = current_poll_pause(self.store.conn, self.ext.backoff_min * 60.0, now)
        if pause is not None:
            return min(pause.until - now, IDLE_S)
        wait = self.last_request_at + self.ext.poll_interval_s - now
        if wait > 0:
            return wait
        await self._poll(hexes)
        return 0.0

    async def _poll(self, hexes: list[str]) -> None:
        url = poll_url(self.ext.url, hexes)

        def fetch() -> dict:
            return self.fetch(url, self.ext.timeout_s, read_api_key(self.ext.api_key_file))

        requested_at = self.last_request_at = self.now()
        try:
            reply = await asyncio.to_thread(fetch)
        except PollError as exc:
            self._failed(len(hexes), requested_at, exc)
            return
        arrived_at = self.now()
        asked = set(hexes)             # a replay merges only the watched, so live keeps no other aircraft either
        positions = [p for p in positions_from_reply(reply, clock_offset_s=self.ext.clock_offset_s) if p.hex in asked]
        poll_id = self.store.record_poll(run_id=self.run_id, requested_at=requested_at, arrived_at=arrived_at,
                                         asked=len(hexes), returned=len(reply["aircraft"]), http_status=200, error=None,
                                         refused=False)
        self.store.insert_external_positions(poll_id, arrived_at, positions)
        self.buffer.extend(positions)
        if self._failed_in_a_row:
            log.info("poller: polls succeed again after %d failed", self._failed_in_a_row)
            self._failed_in_a_row = 0

    def _failed(self, asked: int, requested_at: float, exc: PollError) -> None:
        """Record the failed poll. A refused one pauses the polls after it (current_poll_pause) and says so in a
        warning. Any other skips only this poll: it is logged at info if the poll before it succeeded, so the log says
        an outage began, and at debug otherwise, since an outage would repeat it every poll (external_polls keeps
        them all); the next poll that succeeds says it ended."""
        self.store.record_poll(run_id=self.run_id, requested_at=requested_at, arrived_at=self.now(), asked=asked,
                               returned=None, http_status=exc.status, error=str(exc), refused=exc.refused,
                               retry_after_s=exc.retry_after_s)
        first = self._failed_in_a_row == 0
        self._failed_in_a_row += 1
        if exc.refused and exc.retry_after_s == 0:
            log.warning("poller: refused, and re-api asked for no wait: %s", exc)
        elif exc.refused and exc.retry_after_s is not None:
            log.warning("poller: paused for %g s as re-api asked: %s%s", exc.retry_after_s, exc,
                        ", the key was refused" if exc.status == 401 else "")
        elif exc.refused:
            log.warning("poller: paused for %g min: %s%s", self.ext.backoff_min, exc,
                        ", the key was refused" if exc.status == 401 else "")
        else:
            log.log(logging.INFO if first else logging.DEBUG, "poller: poll failed, the next one goes on: %s", exc)


async def run_poller(poller: Poller, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """The poller task: step, then sleep as long as the step says, until cancelled. A step that raises is logged and
    the poller waits poll_interval_s before the next, so a bug here never ends the task (and with it the run)."""
    while True:
        try:
            delay = await poller.step()
        except Exception:
            log.exception("poller: a step failed; polling goes on")
            delay = poller.cfg.external.poll_interval_s
        await sleep(delay)

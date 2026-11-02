"""The live collector: readsb JSON port → pipeline → SQLite in one-second batches, METAR polling, health."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import resource
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import Config
from .fleet import FleetNorms, Rebuild, load_norms, rebuild
from .model import Position, Weather
from .pipeline import Pipeline, PipelineStats, load_weather
from .poller import Poller, fetch_reply, run_poller
from .sources.readsb_json import FeedStats, readsb_json_positions
from .store.db import Store
from .typedb import TypeDb
from .weather import WeatherTable, fetch_metars, poll_metars

log = logging.getLogger(__name__)
COMMIT_INTERVAL_S = 1.0
HEALTH_INTERVAL_S = 60.0
FLEET_BUSY_TIMEOUT_S = 30.0       # the rebuild's own connection waits this long for the collector's own batch
# A drain that re-takes the write lock at once leaves a waiting writer no turn at all: measured on two connections,
# one committing once a second and re-beginning immediately, the other doing what reflag does per approach with a
# 30 s busy timeout, it got 0 of 20 writes in, "database is locked" after 31.1 s. A 150 ms lock-free gap after each
# drain let it do all 20 in 0.6 s.
MIN_LOCK_FREE_S = 0.15
MAX_BUFFERED_POSITIONS = 20000    # roughly a minute of a busy feed; past this, drain at once instead of waiting for the tick


def rss_mb() -> float:
    """Resident memory in MB: current where /proc has it (Linux), else the peak so far (macOS)."""
    try:
        with open("/proc/self/statm") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except (OSError, ValueError, IndexError):
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss          # bytes on macOS, kilobytes on Linux
        return peak / 1e6 if sys.platform == "darwin" else peak / 1e3


def metar_age_s(weather: WeatherTable, ids: list[str], now: float) -> float | None:
    """Age of the stalest of these airports' latest METARs; None while any of them has none."""
    ages: list[float] = []
    for ident in ids:
        latest = weather.latest(ident)
        if latest is None:
            return None
        ages.append(now - latest.observed_at)
    return max(ages) if ages else None


def health_line(stats: FeedStats, positions_per_s: float, open_attempts: int, metar_age: float | None,
                rss: float | None, merge: PipelineStats | None = None) -> str:
    """The 60 s health log line. A run that merges adsb.lol passes its pipeline's `merge` stats, and the line ends with
    what the "newer than" rule has done with each origin's positions since the run began: adsb.lol's taken and dropped,
    and the receiver's dropped for being older than an adsb.lol one."""
    age = stats.last_line_age_s()
    line = ("health: last line " + ("never" if age is None else f"{age:.1f} s ago")
            + f", {positions_per_s:.1f} positions/s, {open_attempts} open attempts, METAR age "
            + ("none" if metar_age is None else f"{metar_age / 60:.0f} min")
            + ", RSS " + ("unknown" if rss is None else f"{rss:.0f} MB"))
    if merge is not None:
        line += (f", adsb.lol kept {merge.external_kept}, dropped {merge.external_dropped}, "
                 f"local dropped {merge.external_local_dropped}")
    return line


def _load_typedb(path: str | None) -> TypeDb | None:
    if path is None:
        return None
    if not Path(path).exists():
        log.warning("type database %s is missing: flights get traffic class 'other' until fetch-typedb writes it", path)
        return None
    return TypeDb.load(path)


def _drain(pipe: Pipeline, store: Store, buffer: deque[Position]) -> None:
    """Process everything buffered right now, in arrival order, as one commit; an empty buffer opens no transaction.
    Taking the positions out before processing them -- instead of popping them one at a time -- means _ingest can
    keep appending, untouched, for the whole of this call: nothing here awaits, so nothing else runs in between."""
    if not buffer:
        return
    taken = list(buffer)
    buffer.clear()
    store.begin_batch()
    for pos in taken:
        pipe.process(pos)
    store.commit_batch()


async def _ingest(cfg: Config, stats: FeedStats, backoff_initial_s: float, buffer: deque[Position],
                  overflow: asyncio.Event) -> None:
    """Take positions off the feed into `buffer` as fast as they arrive, touching neither the store nor the
    pipeline: _drain_every is the only writer of the pipeline's rows, so the write lock is held only for the burst of
    a drain, never for however long the feed takes between positions. The poller's own short writes, a poll's row and
    its positions, also go through this connection, each committing on its own between drains."""
    positions = readsb_json_positions(cfg.source.host, cfg.source.port, stats, backoff_initial_s=backoff_initial_s)
    async with contextlib.aclosing(positions):
        async for pos in positions:
            buffer.append(pos)
            if len(buffer) > MAX_BUFFERED_POSITIONS and not overflow.is_set():
                log.warning("position buffer at %d, over the %d cap: draining early", len(buffer), MAX_BUFFERED_POSITIONS)
                overflow.set()


async def _drain_every(pipe: Pipeline, store: Store, buffer: deque[Position], overflow: asyncio.Event,
                       interval_s: float, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Drain the buffer once every interval_s, or at once when _ingest signals overflow. A lock-free gap of at least
    MIN_LOCK_FREE_S always follows a drain -- one that ran long or one overflow triggered early included -- since
    that gap, not the interval between ticks, is what gives a waiting writer its turn (see MIN_LOCK_FREE_S)."""
    while True:
        try:
            await asyncio.wait_for(overflow.wait(), timeout=interval_s)
        except TimeoutError:
            pass
        overflow.clear()
        _drain(pipe, store, buffer)
        await sleep(MIN_LOCK_FREE_S)


async def _report_health(pipe: Pipeline, stats: FeedStats, metar_ids: list[str], interval_s: float) -> None:
    positions, since = stats.positions, time.monotonic()
    while True:
        await asyncio.sleep(interval_s)
        now = time.monotonic()
        rate = (stats.positions - positions) / max(now - since, 1e-9)
        positions, since = stats.positions, now
        log.info(health_line(stats, rate, len(pipe.matcher.live()), metar_age_s(pipe.matcher.weather, metar_ids, time.time()),
                             rss_mb(), pipe.stats if pipe.cfg.external.enabled else None))


def rebuild_fleet(cfg: Config) -> Rebuild:
    """One rebuild, on its own connection: a Store belongs to the thread that made it, and this runs in a worker
    thread so the event loop keeps taking positions."""
    store = Store(cfg.service.db_path, busy_timeout_s=FLEET_BUSY_TIMEOUT_S)
    try:
        return rebuild(store, cfg)
    finally:
        store.close()


def fleet_first_delay(built_at: float | None, interval_s: float, *, now: float) -> float | None:
    """The worker's first-rebuild delay, from the snapshot `run_live` loaded at startup: 0.0 when it is empty or older
    than interval_s, so a fresh install or a collector that was down a while does not wait a full interval for its first
    rebuild; None (rebuild_fleet_every's own interval_s) otherwise, so a restart during a config fiddle pays nothing."""
    return 0.0 if built_at is None or (now - built_at) > interval_s else None


async def rebuild_fleet_every(cfg: Config, pipe: Pipeline, interval_s: float, *,
                              first_delay_s: float | None = None) -> None:
    """Rebuild the norms every interval_s and swap the new snapshot into the scorer. The first rebuild waits
    first_delay_s (interval_s itself when not given); every rebuild after it waits the full interval. A rebuild that
    fails is logged and the previous snapshot stays in use: the collector never stops for one, and the next interval
    tries again."""
    delay = interval_s if first_delay_s is None else first_delay_s
    while True:
        await asyncio.sleep(delay)
        delay = interval_s
        try:
            result = await asyncio.to_thread(rebuild_fleet, cfg)
        except Exception as exc:
            log.warning("fleet rebuild failed, keeping the norms in use: %s", exc)
            continue
        pipe.scorer.set_norms(result.norms)
        log.info("%s", result.text())


def _open_run(cfg: Config, store: Store, metar_path: str | Path | None) -> tuple[Pipeline, WeatherTable, FleetNorms]:
    """Everything run_live does to the database and the pipeline before any task starts, in this order: migrate; close
    the approaches a crashed run left open as interrupted; load the METARs observed within qnh_max_age_min, so an
    approach already close to the ground does not get QNH_ASSUMED just because the process restarted; open the new
    run; load the fleet norms. Returns the run's pipeline, its weather table and the norms loaded."""
    store.migrate()
    closed, runs = store.sweep_interrupted()
    if closed or runs:
        log.warning("closed %d approaches left open by %d interrupted live runs", closed, runs)
    weather = load_weather(metar_path, store)
    weather.extend(store.weather_since(time.time() - cfg.service.qnh_max_age_min * 60.0))
    pipe = Pipeline(cfg, store, run_kind="live", source_desc=f"readsb-json:{cfg.source.host}:{cfg.source.port}",
                    typedb=_load_typedb(cfg.service.typedb_path), weather=weather,
                    external_merge=cfg.external.enabled)
    # The norms the last rebuild left, so a restart judges gates at once; empty, and every delta null, until the
    # first rebuild has run. Its built_at also decides the worker's first-rebuild delay in run_live: a stale or empty
    # snapshot is rebuilt at once, a fresh one waits.
    norms = load_norms(store)
    pipe.scorer.set_norms(norms)
    log.info("run %d: reading %s:%s into %s", pipe.run_id, cfg.source.host, cfg.source.port, cfg.service.db_path)
    return pipe, weather, norms


async def _wait_then_cancel(stop: asyncio.Event, tasks: list[asyncio.Task]) -> asyncio.Task | None:
    """Runs nothing itself: waits until `stop` is set or one of `tasks` ends, then cancels everything and waits for it
    all to unwind. Returns the task that ended on its own, None when it was `stop`."""
    stopping = asyncio.create_task(stop.wait(), name="stop")
    done, _ = await asyncio.wait([stopping, *tasks], return_when=asyncio.FIRST_COMPLETED)
    failed = next((t for t in tasks if t in done), None)          # these tasks never return: one that ended failed
    for t in (stopping, *tasks):
        t.cancel()
    await asyncio.gather(stopping, *tasks, return_exceptions=True)
    return failed


def _end_run(pipe: Pipeline, store: Store, buffer: deque[Position],
             failed: asyncio.Task | None) -> BaseException | None:
    """Close the run once its tasks are gone, and return the error to raise. After a clean stop (`failed` is None),
    drain whatever is still buffered, finish the run and commit, and return None. After a failed task, finish the run
    with the task's exception in its note, and return that exception (a RuntimeError when the task returned instead of
    raising)."""
    if failed is None:
        _drain(pipe, store, buffer)     # a clean stop loses nothing buffered; a crash or kill may lose this much
        pipe.finish(error=None)
        store.commit_batch()
        return None
    error = failed.exception() or RuntimeError(f"the {failed.get_name()} task ended")
    note = f"{type(error).__name__}: {error}"
    # A failing finish() or final commit must not replace the task's own error, but it must not be silently
    # swallowed either.
    log.error("run %d: %s failed", pipe.run_id, failed.get_name(), exc_info=error)
    try:
        pipe.finish(error=note)
    except Exception:
        log.exception("run %d: pipe.finish() failed while ending a failed run", pipe.run_id)
    try:
        store.commit_batch()
    except Exception:
        log.exception("run %d: final commit failed while ending a failed run", pipe.run_id)
    return error


async def run_live(cfg: Config, *, stop: asyncio.Event, metar_path: str | Path | None = None,
                   fetch: Callable[[list[str]], list[Weather]] = fetch_metars, stats: FeedStats | None = None,
                   health_interval_s: float = HEALTH_INTERVAL_S, backoff_initial_s: float = 1.0,
                   reply_fetch: Callable[[str, float, str | None], dict] = fetch_reply) -> int:
    """Collect from readsb's JSON port into a new live run until `stop` is set, and return the run's id.

    At start the database is migrated, the approaches a crashed run left open are closed as interrupted, and METARs
    observed within qnh_max_age_min are reloaded so an approach already close to the ground does not get
    QNH_ASSUMED just because the process restarted. Writes commit once a second. With [external] enabled, the run
    merges adsb.lol's positions for the aircraft it is following, polled with `reply_fetch` (poller.Poller), and is
    marked as having done so. When `stop` is set, attempts in progress close as at the end of a stream and the run ends.
    If a task fails instead, the run ends the same way with the error in its note, and the error is raised."""
    if cfg.source.kind != "readsb-json":
        raise ValueError(f"[source] kind {cfg.source.kind!r}: the live service reads readsb-json only")
    stats = stats if stats is not None else FeedStats()
    store = Store(cfg.service.db_path)
    try:
        pipe, weather, norms = _open_run(cfg, store, metar_path)
        metar_ids = sorted({a.metar for a in cfg.enabled_airports()})
        buffer: deque[Position] = deque()
        overflow = asyncio.Event()
        tasks = [asyncio.create_task(_ingest(cfg, stats, backoff_initial_s, buffer, overflow), name="ingest"),
                 asyncio.create_task(_drain_every(pipe, store, buffer, overflow, COMMIT_INTERVAL_S), name="drain"),
                 asyncio.create_task(_report_health(pipe, stats, metar_ids, health_interval_s), name="health")]
        if cfg.service.metar_interval_s > 0 and metar_ids:
            tasks.append(asyncio.create_task(poll_metars(weather, metar_ids, cfg.service.metar_interval_s, fetch=fetch,
                                                         on_new=store.insert_weather), name="metar"))
        if cfg.fleet.rebuild_interval_min > 0:
            interval_s = cfg.fleet.rebuild_interval_min * 60.0
            first_delay_s = fleet_first_delay(norms.built_at, interval_s, now=time.time())
            tasks.append(asyncio.create_task(rebuild_fleet_every(cfg, pipe, interval_s, first_delay_s=first_delay_s),
                                             name="fleet"))
        if cfg.external.enabled:
            # The poller fills the same buffer _ingest does, and its stores commit on their own on the event loop, so a
            # poll can never land inside a drain's batch: _drain has no await in it, and must not gain one.
            poller = Poller(cfg, store, pipe.run_id, buffer, fetch=reply_fetch)
            pipe.events.subscribe(poller.watch.on_event)
            tasks.append(asyncio.create_task(run_poller(poller), name="external"))
            log.info("run %d: merging adsb.lol positions from %s every %g s, %s", pipe.run_id, cfg.external.url,
                     cfg.external.poll_interval_s,
                     f"with the key in {cfg.external.api_key_file}" if cfg.external.api_key_file else "with no key")
        failed = await _wait_then_cancel(stop, tasks)
        error = _end_run(pipe, store, buffer, failed)
        log.info("run %d ended: %d positions, %d approaches", pipe.run_id, stats.positions, pipe.stats.closed)
        if error is not None:
            raise error
        return pipe.run_id
    finally:
        store.close()

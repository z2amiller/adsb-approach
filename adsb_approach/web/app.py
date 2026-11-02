"""The web page and its read-only API, served by `adsb-approach web`."""
from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from .. import __version__
from ..config import Config
from ..model import Flags, Outcome, Position
from ..reference import reference_labels
from ..tracks import SESSION_GAP_S, load_places, session_positions, summarize
from .arrivals import ATTENTION_FLAGS, ATTENTION_OUTCOMES, ArrivalFilter, decode_cursor, list_arrivals, parse_limit
from .db import DatabaseUnavailable, snapshot
from .detail import approach_detail, tar1090_base
from .live import health_report, live_cards
from .search import SearchQuery, decode_search_cursor, grey_row, list_search
from .stats import parse_days, stats_payload
from .stream import EventBroker, event_stream

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class RevalidatedStaticFiles(StaticFiles):
    """Static files a browser checks for changes on every load, so the page never runs old scripts after an update."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class EventStreamResponse(StreamingResponse):
    """An event stream that ends as soon as its page goes away. Under ASGI spec 2.4, which uvicorn 0.32 declares,
    Starlette 0.46 notices a departed client only by send() raising OSError, and uvicorn 0.32's send() drops the message
    instead; so this listens for http.disconnect, as Starlette does for older specs, or a closed page's stream and its
    subscriber would last until the service stops."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with anyio.create_task_group() as tg:
            async def respond() -> None:
                await self.stream_response(send)
                tg.cancel_scope.cancel()

            tg.start_soon(respond)
            await self.listen_for_disconnect(receive)
            tg.cancel_scope.cancel()


def create_app(cfg: Config, db_path: str | Path | None = None, *, poll_interval_s: float = 1.0) -> FastAPI:
    """The app for a configuration. It reads `db_path` (default [service] db_path) and never writes to it."""
    db = Path(db_path if db_path is not None else cfg.service.db_path)
    broker = EventBroker(db, poll_interval_s=poll_interval_s)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        broker.start()
        try:
            yield
        finally:
            await broker.stop()

    # The flight search's inputs, fixed for the life of the service: the places a track can be near, where readsb's
    # trace files are ("" for the live directory means none: Path("") would be the working directory), and the airports
    # whose arrivals the matcher is expected to log, so a grey row can tell a miss from an arrival it never scores.
    places = load_places(cfg.service.nearby_airports_path, cfg.airports)
    globe_dir = Path(cfg.service.globe_history_dir)
    live_dir = Path(cfg.service.live_traces_dir) if cfg.service.live_traces_dir else None
    scored = frozenset(apt.icao for apt in cfg.enabled_airports())
    # The label of every reference an attempt can name, from the configuration the service started with: the log and
    # the flight search show a path by its configured label, and the stored name for one no longer configured.
    labels = reference_labels(cfg.airports)

    app = FastAPI(title="adsb-approach", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.broker = broker

    @app.exception_handler(DatabaseUnavailable)
    async def database_unavailable(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": f"database unavailable: {exc}"}, status_code=503)

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return {
            "version": __version__,
            "airports": [{"icao": apt.icao, "name": apt.name, "metar": apt.metar,
                          "runways": [{"ident": r.ident, "glide_path_deg": r.glide_path_deg, "tch_ft": r.tch_ft,
                                       "threshold_elev_ft": r.threshold_elev_ft, "course_true_deg": r.course_true_deg,
                                       "ils": r.ils, "reference_source": r.reference_source}
                                      for r in apt.runways.values() if r.enabled]}
                         for apt in cfg.enabled_airports()],
            "outcomes": [o.value for o in Outcome],
            "flags": [f.name for f in Flags],
            "attention": {"outcomes": [o.value for o in ATTENTION_OUTCOMES],
                          "flags": [f.name for f in Flags if f in ATTENTION_FLAGS]},
            # The configured GA type codes, so the page can filter live SSE events for GA client-side
            # (applyLiveEvent) the way it already does for rotorcraft -- /api/live is filtered server-side, but the
            # event stream, unlike /api/live, is not.
            "ga_types": sorted(cfg.type_classes.ga),
        }

    @app.get("/api/health")
    def get_health() -> dict[str, Any]:
        report = health_report(db, time.time(), cfg.external)
        report["stream"] = {"clients": broker.clients, "error": broker.error, "last_poll_at": broker.last_poll_at}
        return report

    @app.get("/api/live")
    def get_live(request: Request) -> dict[str, Any]:
        rotorcraft = request.query_params.get("rotorcraft", "").strip() == "1"
        ga = request.query_params.get("ga", "").strip() == "1"
        with snapshot(db) as conn:
            return live_cards(conn, rotorcraft=rotorcraft, ga=ga, ga_types=cfg.type_classes.ga)

    @app.get("/api/arrivals")
    def get_arrivals(request: Request) -> dict[str, Any]:
        params = dict(request.query_params)
        try:
            limit = parse_limit(params.pop("limit", None))
            cursor = decode_cursor(params.pop("cursor", None))
            filt = ArrivalFilter.parse(params, ga_types=cfg.type_classes.ga)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        with snapshot(db) as conn:
            return list_arrivals(conn, filt, limit=limit, cursor=cursor, labels=labels)

    @app.get("/api/search")
    def get_search(request: Request) -> dict[str, Any]:
        """Find a flight: the sessions matching `q`, newest first. A session with approach rows
        comes back as its arrivals, as /api/arrivals gives them; one without, as a grey row, which carries what its
        readsb track says (grey_row). The tracks are read after the snapshot closes, so that a slow disk never holds a
        database read open."""
        params = dict(request.query_params)
        try:
            limit = parse_limit(params.pop("limit", None))
            cursor = decode_search_cursor(params.pop("cursor", None))
            query = SearchQuery.parse(params)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        base = tar1090_base(cfg.service.tar1090_url, request.url.hostname, request.url.scheme)
        now = time.time()
        with snapshot(db) as conn:
            page = list_search(conn, query, limit=limit, cursor=cursor, labels=labels)
        parsed: dict[Path, list[Position]] = {}     # this request's track files: a hex's sessions share theirs

        def grey(row: dict[str, Any]) -> dict[str, Any]:
            # A session still going when it was last stored runs on past last_seen: only the hex's newest can be.
            open_ended = row["newest"] and now - row["last_seen"] < SESSION_GAP_S
            try:
                positions = session_positions(row["hex"], row["first_seen"], row["last_seen"], globe_dir=globe_dir,
                                              live_dir=live_dir, open_ended=open_ended, now=now, cache=parsed)
                summary = summarize(positions, places)
            except Exception as exc:        # a file of the wrong shape: iter_trace_file raises; this row has no track
                log.warning("%s: track not used: %s: %s", row["hex"], type(exc).__name__, exc, exc_info=True)
                summary = summarize([], places)
            return grey_row(row, summary, tar1090=base, globe_url=cfg.external.globe_url, configured=scored)

        page["rows"] = [grey(row) if row["kind"] == "flight" else row for row in page["rows"]]
        return page

    @app.get("/api/stats")
    def get_stats(request: Request) -> dict[str, Any]:
        """The coverage page's data. One read, so its four parts agree with each other."""
        try:
            days = parse_days(dict(request.query_params))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        with snapshot(db) as conn:
            return stats_payload(conn, cfg, days=days, now=time.time())

    @app.get("/api/approaches/{approach_id}")
    def get_approach(approach_id: int, request: Request) -> dict[str, Any]:
        base = tar1090_base(cfg.service.tar1090_url, request.url.hostname, request.url.scheme)
        with snapshot(db) as conn:
            found = approach_detail(conn, approach_id, cfg, base, time.time())
        if found is None:
            raise HTTPException(status_code=404, detail=f"no approach {approach_id}")
        return found

    @app.get("/api/events")
    async def get_events(request: Request, after: int | None = None) -> StreamingResponse:
        """Server-sent events from after `after`, or from after the Last-Event-ID header when a browser reconnects."""
        header = request.headers.get("last-event-id")
        if header is not None:
            try:
                after = int(header)
            except ValueError:
                raise HTTPException(status_code=400, detail="Last-Event-ID must be an event id") from None
        return EventStreamResponse(event_stream(broker, after), media_type="text/event-stream",
                                   headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/coverage", include_in_schema=False)
    def coverage_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "coverage.html", headers={"Cache-Control": "no-cache"})

    @app.get("/search", include_in_schema=False)
    def search_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "search.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", RevalidatedStaticFiles(directory=STATIC_DIR), name="static")
    return app

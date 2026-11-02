"""uvicorn serving the web app (`adsb-approach web`)."""
from __future__ import annotations

from pathlib import Path

import uvicorn

from ..config import Config
from .app import create_app

GRACEFUL_SHUTDOWN_S = 3         # on SIGTERM, the longest uvicorn waits for open requests before cancelling them


def build_server(cfg: Config, *, db_path: str | Path | None = None, bind: str | None = None, port: int | None = None,
                 poll_interval_s: float = 1.0) -> uvicorn.Server:
    """A uvicorn server for the app on [service] bind and port unless given. The access log is off, so the journal
    holds uvicorn's start, stop and error lines and the app's warnings. loop="asyncio" and http="h11" pin the same
    event loop and HTTP implementation used in development and the tests: Debian's uvicorn package depends on
    uvloop, so "auto" would pick that instead on a systemd install running on the distribution's packages."""
    app = create_app(cfg, db_path, poll_interval_s=poll_interval_s)
    config = uvicorn.Config(app, host=cfg.service.bind if bind is None else bind,
                            port=cfg.service.port if port is None else port, log_level=cfg.service.log_level,
                            access_log=False, timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S, lifespan="on",
                            loop="asyncio", http="h11")
    server = uvicorn.Server(config)
    app.state.broker.stopping = lambda: server.should_exit     # event streams end within a poll of SIGTERM
    return server

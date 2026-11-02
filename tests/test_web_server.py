"""The web app under a real uvicorn: FastAPI's test client waits for a response to end, and an event stream never does."""
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

from adsb_approach.web.server import build_server
from tests.conftest import ROOT
from tests.webdb import LiveWriter, copy_db


@contextmanager
def serving(cfg, db):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = build_server(cfg, db_path=db, poll_interval_s=0.05)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    try:
        yield sock.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


class Frames:
    """Reads a server-sent events response one frame at a time; "" once the stream has ended."""

    def __init__(self, port: int, path: str, headers: dict[str, str] | None = None) -> None:
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.conn.request("GET", path, headers=headers or {})
        self.response = self.conn.getresponse()

    def next(self) -> str:
        lines = []
        while True:
            line = self.response.readline().decode()
            if line in ("", "\n"):
                return "\n".join(lines)
            lines.append(line.removesuffix("\n"))

    def close(self) -> None:
        self.conn.close()


def get_json(port: int, path: str) -> dict:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path)
        return json.loads(conn.getresponse().read())
    finally:
        conn.close()


def test_build_server_pins_the_event_loop_and_http_implementation(example_cfg, replay_db):
    """Debian's uvicorn package depends on uvloop, so "auto" would pick a different event loop and HTTP implementation
    on a Debian host than in development and the tests."""
    server = build_server(example_cfg, db_path=replay_db)
    assert server.config.loop == "asyncio"
    assert server.config.http == "h11"


def test_the_stream_catches_up_then_follows_the_collector(tmp_path, replay_db, example_cfg):
    db = copy_db(replay_db, tmp_path / "live.sqlite")
    w = LiveWriter(db)
    try:
        newest = w.store.conn.execute("SELECT MAX(id) FROM events").fetchone()[0]
        with serving(example_cfg, db) as port:
            frames = Frames(port, "/api/events", {"Last-Event-ID": str(newest - 2)})
            try:
                assert frames.response.status == 200
                assert frames.response.getheader("content-type", "").startswith("text/event-stream")
                assert frames.next() == ": connected"
                assert [f.split("\n")[0] for f in (frames.next(), frames.next())] == [f"id: {newest - 1}", f"id: {newest}"]
                aid = w.open("aaa001", "UAL1", along_nm=5.0)
                started = frames.next()
                assert started.startswith(f"id: {newest + 1}\n")
                event = json.loads(started.split("data: ", 1)[1])
                assert (event["kind"], event["approach_id"], event["payload"]["arrival_id"]) == ("approach.started", aid, aid)
            finally:
                frames.close()
    finally:
        w.close()


def test_a_page_ahead_of_the_database_is_told_to_reload(replay_db, example_cfg):
    with serving(example_cfg, replay_db) as port:
        frames = Frames(port, "/api/events?after=999999999")
        try:
            assert frames.next() == ": connected"
            assert frames.next() == "event: reload\ndata: {}"
            assert frames.next() == ""                                     # the stream has ended
        finally:
            frames.close()


def test_a_page_that_goes_away_is_no_longer_a_client(replay_db, example_cfg):
    """uvicorn 0.32 drops what is sent to a client that has gone, rather than raising, so the stream must notice the
    disconnect itself: otherwise every closed page keeps its stream and its subscriber until the service stops."""
    with serving(example_cfg, replay_db) as port:
        frames = Frames(port, "/api/events")
        assert frames.next() == ": connected"
        assert get_json(port, "/api/health")["stream"]["clients"] == 1
        frames.close()
        deadline = time.monotonic() + 5
        while (clients := get_json(port, "/api/health")["stream"]["clients"]) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert clients == 0


def test_web_serves_until_sigterm_even_with_a_page_connected(tmp_path, replay_db):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("ADSB_APPROACH_")}
    log = (tmp_path / "web.log").open("w")
    proc = subprocess.Popen([sys.executable, "-m", "adsb_approach.cli", "web", "--config",
                             str(ROOT / "config" / "adsb-approach.example.toml"), "--db", str(replay_db),
                             "--bind", "127.0.0.1", "--port", str(port)], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                conn.request("GET", "/api/health")
                health = json.loads(conn.getresponse().read())
                conn.close()
                break
            except OSError:
                assert proc.poll() is None and time.monotonic() < deadline, (tmp_path / "web.log").read_text()
                time.sleep(0.1)
        assert health["collector"]["state"] == "none"
        frames = Frames(port, "/api/events")
        assert frames.next() == ": connected"
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        # uvicorn re-raises the signal once it has shut down, so the process ends by SIGTERM: a clean stop to systemd
        assert proc.wait(timeout=15) == -signal.SIGTERM, (tmp_path / "web.log").read_text()
        assert time.monotonic() - started < 10
        text = (tmp_path / "web.log").read_text()
        assert "Finished server process" in text and "graceful shutdown exceeded" not in text      # the stream ended itself
        frames.close()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        log.close()

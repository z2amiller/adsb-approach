"""Contract test: the poller's own query and parsing against a real readsb's --net-api-port.

re-api is readsb's API port, so a throwaway readsb fed a slice of a recorded Beast stream answers the same
`find_hex` query the poller sends to adsb.lol. The test is opt-in: it skips unless `readsb` is on the PATH and
ADSB_APPROACH_BEAST_CORPUS names a readsb Beast dump, raw or zstd-compressed (a `.zst` needs `zstd` too). On a host
that records its receiver's Beast stream it runs without uv or the repository's conftest. The path below is an
example of the layout a readsb `--dump-beast` archive can have, one zstd file per hour in a directory per day:

    ADSB_APPROACH_BEAST_CORPUS=/var/beast_dump/<day>/<hour>Z.zst PYTHONPATH=. \
        python3 -m pytest -q --noconftest -p no:cacheprovider tests/test_readsb_contract.py

readsb stamps every position with its arrival time, not the dump's own times (`seen_pos` is the same few seconds for
all), so this checks the query and the parsing and never timing, which the synthetic tests cover. The corpus is read
in place and nothing from it is kept.
"""
from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from typing import NoReturn

import pytest

from adsb_approach.poller import PollError, fetch_reply, poll_url, positions_from_reply

CORPUS = os.environ.get("ADSB_APPROACH_BEAST_CORPUS", "")
BEAST_BYTES = 8_000_000      # the slice sent; one observed run: about 3 s of readsb's work gave 131 aircraft
STARTUP_S = 20.0             # readsb's two ports must accept within this long
SETTLE_S = 3.0               # after the slice is sent, readsb needs about this long to decode it
AIRCRAFT_S = 30.0            # then aircraft with a position must be listed within this long
WANT_HEXES = 3
CLOCK_OFFSET_S = 0.10


def _skip_reason() -> str | None:
    if shutil.which("readsb") is None:
        return "readsb is not on the PATH"
    if not os.path.isfile(CORPUS):
        return f"ADSB_APPROACH_BEAST_CORPUS does not name a readsb Beast dump (it is {CORPUS!r})"
    if CORPUS.endswith(".zst") and shutil.which("zstd") is None:
        return "zstd is not on the PATH, and the corpus is compressed"
    return None


SKIP = _skip_reason()
pytestmark = pytest.mark.skipif(SKIP is not None, reason=SKIP or "")


def _free_ports(n: int) -> list[int]:
    """n distinct free loopback ports: all are held until every one is chosen, so none repeats."""
    socks = [socket.socket() for _ in range(n)]
    try:
        for s in socks:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


class _Readsb:
    """A throwaway readsb, as an unprivileged user on high ports (net-only, so no receiver is needed). Its output goes
    to a file rather than a pipe, since a pipe nobody reads would stall readsb once full, and the tail of it goes into
    every failure message."""

    def __init__(self, beast_port: int, api_port: int) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self._log_path = os.path.join(self._dir.name, "readsb.log")
        command = ["readsb", "--net-only", f"--net-bi-port={beast_port}", f"--net-api-port={api_port}",
                   "--write-json-every=1", "--quiet"]
        self.command = " ".join(command)
        try:
            with open(self._log_path, "wb") as sink:
                self.proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT)
        except BaseException:
            self._dir.cleanup()
            raise

    def tail(self, lines: int = 15) -> str:
        """The last lines readsb printed, leaving out its repeated, harmless `codepoint` lines (counted instead)."""
        with open(self._log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            text = f.read().decode(errors="replace")
        kept = [line for line in text.splitlines() if "codepoint" not in line]
        noise = len(text.splitlines()) - len(kept)
        return "\n".join(kept[-lines:]) + (f"\n(and {noise} harmless `codepoint` lines)" if noise else "")

    def fail(self, message: str) -> NoReturn:
        status = self.proc.poll()
        pytest.fail(f"{message}\n--- {self.command}: {'still running' if status is None else f'exited {status}'}, "
                    f"its output ---\n{self.tail() or '(nothing)'}", pytrace=False)

    def connect(self, port: int, what: str) -> socket.socket:
        """A connection to one of readsb's ports, retried until it accepts, readsb exits or STARTUP_S passes."""
        deadline = time.monotonic() + STARTUP_S
        while True:
            if self.proc.poll() is not None:
                self.fail(f"readsb exited before its {what} port {port} accepted a connection")
            try:
                return socket.create_connection(("127.0.0.1", port), timeout=1.0)
            except OSError as exc:
                if time.monotonic() > deadline:
                    self.fail(f"readsb's {what} port {port} did not accept a connection in {STARTUP_S:g} s: {exc}")
                time.sleep(0.2)

    def stop(self) -> None:
        """Stop the readsb this test started, by its own handle, and remove its log."""
        try:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        finally:
            self._dir.cleanup()


def _beast_slice(path: str, size: int = BEAST_BYTES) -> tuple[bytes, str]:
    """The first `size` bytes of the dump, decompressed when it is a `.zst`, and what zstd said on stderr. Only the
    slice is ever held: zstd is stopped once it has been read, whatever the hour's length."""
    if not path.endswith(".zst"):
        with open(path, "rb") as f:
            return f.read(size), ""
    zstd = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        data = zstd.stdout.read(size)
    finally:
        zstd.stdout.close()
        zstd.terminate()
        try:
            zstd.wait(10)
        except subprocess.TimeoutExpired:
            zstd.kill()
            zstd.wait(10)
    with zstd.stderr:
        return data, zstd.stderr.read().decode(errors="replace").strip()


def _icao_hexes_with_position(readsb: _Readsb, base: str) -> list[str]:
    """Up to WANT_HEXES aircraft from `GET /?all_with_pos`, asked until some are listed or AIRCRAFT_S passes. Hexes
    that start with `~` are non-ICAO addresses, which the poller never asks about."""
    deadline = time.monotonic() + AIRCRAFT_S
    while True:
        try:
            with urllib.request.urlopen(f"{base}?all_with_pos", timeout=10.0) as resp:
                listing = json.load(resp)
            listed = listing["aircraft"]
            hexes = [o["hex"] for o in listed if isinstance(o.get("hex"), str) and not o["hex"].startswith("~")]
            problem = f"it listed {len(listed)} aircraft with a position, {len(hexes)} of them ICAO"
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError) as exc:
            hexes, problem = [], f"the request failed: {type(exc).__name__}: {exc}"
        if hexes:
            return hexes[:WANT_HEXES]
        if time.monotonic() > deadline:
            readsb.fail(f"GET {base}?all_with_pos listed no ICAO aircraft within {AIRCRAFT_S:g} s of the slice "
                        f"({problem})")
        time.sleep(0.5)


def test_the_poller_reads_a_real_readsb_api():
    beast_port, api_port = _free_ports(2)
    base = f"http://127.0.0.1:{api_port}/"
    readsb = _Readsb(beast_port, api_port)
    beast = None
    try:
        readsb.connect(api_port, "API").close()
        beast = readsb.connect(beast_port, "Beast-in")

        data, zstd_error = _beast_slice(CORPUS)
        assert data, f"{CORPUS} gave no bytes to send" + (f"; zstd said: {zstd_error}" if zstd_error else "")
        beast.settimeout(60.0)
        try:
            beast.sendall(data)
        except OSError as exc:
            readsb.fail(f"sending {len(data)} bytes of {CORPUS} to the Beast-in port failed: {exc}")
        time.sleep(SETTLE_S)

        hexes = _icao_hexes_with_position(readsb, base)
        url = poll_url(base, hexes)
        try:
            reply = fetch_reply(url, 10.0, None)
        except PollError as exc:
            readsb.fail(f"fetch_reply({url}) failed: {exc} (status {exc.status})")

        assert {"aircraft", "now", "ptime", "resultCount"} <= set(reply), f"the reply's keys are {sorted(reply)}"
        positions = positions_from_reply(reply, clock_offset_s=CLOCK_OFFSET_S)
        assert sorted(p.hex for p in positions) == sorted(h.lower() for h in hexes), (
            f"asked for {sorted(hexes)}; the reply held {sorted(o.get('hex') for o in reply['aircraft'])} "
            f"and gave positions for {sorted(p.hex for p in positions)}")
        by_hex = {o["hex"].lower(): o for o in reply["aircraft"]}
        for p in positions:
            obj = by_hex[p.hex]
            assert p.origin == "adsb.lol"
            assert "seen_pos" in obj, f"{p.hex} came back without seen_pos: {obj}"
            assert p.t == pytest.approx(reply["now"] - obj["seen_pos"] - CLOCK_OFFSET_S, abs=1e-6)
            assert -90.0 <= p.lat <= 90.0 and -180.0 <= p.lon <= 180.0, f"{p.hex} is at {p.lat}, {p.lon}"
    finally:
        if beast is not None:
            beast.close()
        readsb.stop()

"""Command-line entry point: adsb-approach <command> [options]."""
from __future__ import annotations

import argparse
import datetime
import math
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def _cifp_source(path: str) -> Generator[tuple[Path, str], None, None]:
    """Yield (text file path, tag); the tag is the file stem. A zip's FAACIFP member is copied into a temporary
    directory that is deleted when the block exits."""
    p = Path(path)
    if p.suffix.lower() != ".zip":
        yield p, p.stem
        return
    with zipfile.ZipFile(p) as zf, tempfile.TemporaryDirectory(prefix="adsb-approach-cifp-") as tmp:
        member = next((n for n in zf.namelist() if Path(n).name.upper().startswith("FAACIFP")), None)
        if member is None:
            raise ValueError(f"{p}: no FAACIFP file inside")
        text = Path(tmp) / Path(member).name
        with zf.open(member) as src, text.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        yield text, p.stem


def cmd_build_config(args: argparse.Namespace) -> int:
    from .refdata.build import build_all
    with _cifp_source(args.cifp) as (src, tag):
        written = build_all([a.strip().upper() for a in args.airports.split(",")], src, args.out,
                            ourairports_dir=args.ourairports, overrides_dir=args.overrides,
                            cifp_tag=args.cifp_tag or tag)
    for p in written:
        print(p)
    return 0


def _numbers(option: str, text: str, count: int, shape: str) -> list[float]:
    """`count` comma-separated numbers; ValueError, naming `option` and its `shape`, when `text` is anything else."""
    try:
        values = [float(part) for part in text.split(",")]
    except ValueError:
        values = []
    if len(values) != count or not all(math.isfinite(v) for v in values):
        raise ValueError(f"fit-path: {option} takes {shape}, not {text!r}")
    for lat, lon in zip(values[0::2], values[1::2]):
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise ValueError(f"fit-path: {option} {lat:g},{lon:g} is not a latitude and longitude in degrees")
    return values


def _iso_date(text: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a date, YYYY-MM-DD") from None


def _fit_seed(args: argparse.Namespace) -> tuple[tuple[tuple[float, float], tuple[float, float]], str]:
    """The (seed, description) the fit-path options give: two points, or a navaid and a true radial outbound from it."""
    from .fitpath import navaid_seed
    if args.navaid is None:
        if args.radial_true is not None:
            raise ValueError("fit-path: --radial-true goes with --navaid")
        lat1, lon1, lat2, lon2 = _numbers("--seed", args.seed, 4, "LAT,LON,LAT,LON")
        return ((lat1, lon1), (lat2, lon2)), f"seed line {lat1:.6f},{lon1:.6f} to {lat2:.6f},{lon2:.6f}"
    if args.radial_true is None:
        raise ValueError("fit-path: --navaid needs --radial-true")
    lat, lon = _numbers("--navaid", args.navaid, 2, "LAT,LON")
    try:
        radial = float(args.radial_true)
    except ValueError:
        radial = math.nan
    if not math.isfinite(radial):
        raise ValueError(f"fit-path: --radial-true must be a number of degrees true, not {args.radial_true!r}")
    return navaid_seed(lat, lon, radial), f"navaid {lat:.6f},{lon:.6f} radial {radial % 360.0:.1f} true outbound"


def cmd_fit_path(args: argparse.Namespace) -> int:
    """Print the `[[runways."R".paths]]` table fitted to the flights that followed a seed, then the bins it came from.
    Nothing is written: the table is pasted into config/overrides/<icao>.toml by hand."""
    import logging
    from .config import load_config
    from .fitpath import fit_path, fit_source, format_bins, toml_snippet, trace_files_between
    from .reference import PATH_NAME_RE
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")     # a file skipped, a seed doubted
    try:
        if not PATH_NAME_RE.fullmatch(args.name):          # the rule reference.validate_path will apply, before a 70 s scan
            raise ValueError(f"fit-path: --name must match [a-z0-9-]+ (it is stored on approaches and used in URLs), "
                             f"not {args.name!r}")
        if not args.label.strip():
            raise ValueError("fit-path: --label must say what to show on the page")
        seed, seed_text = _fit_seed(args)
        if args.from_ > args.to:
            raise ValueError(f"fit-path: --from {args.from_} is after --to {args.to}")
        cfg = load_config(args.config)
        files = trace_files_between(args.globe_history, args.from_, args.to)
        if not files:
            raise ValueError(f"fit-path: no trace files under {args.globe_history} for {args.from_}..{args.to}")
        airport, runway = args.airport.upper(), args.runway.upper()
        result = fit_path(cfg, airport, runway, seed, files)
    except ValueError as exc:
        print(f"adsb-approach: {exc}", file=sys.stderr)
        return 1
    source = fit_source(result, seed=seed_text, first=args.from_, last=args.to)
    print(toml_snippet(result, runway=runway, name=args.name, label=args.label, source=source))
    print(format_bins(result), end="")
    return 0


NEARBY_COLUMNS: tuple[str, ...] = ("ident", "name", "type", "lat", "lon")     # str keys, to match parse_nearby's rows


def cmd_build_nearby(args: argparse.Namespace) -> int:
    """Write the nearby-airports list: the OurAirports airports within --radius-nm of any airport file in
    --airports-dir (the same airports `build-config` writes and the service scores)."""
    import csv
    from .config import load_airport_file
    from .refdata.ourairports import parse_nearby
    files = sorted(Path(args.airports_dir).glob("*.toml"))
    if not files:
        raise ValueError(f"{args.airports_dir}: no airport files to centre the list on")
    airports = [load_airport_file(f) for f in files]
    rows = parse_nearby(Path(args.ourairports) / "airports.csv", [(a.lat, a.lon) for a in airports], args.radius_nm)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, NEARBY_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} airports to {out}")
    return 0


TYPEDB_URL = "https://raw.githubusercontent.com/wiedehopf/tar1090-db/csv/aircraft.csv.gz"


def cmd_migrate(args: argparse.Namespace) -> int:
    from .store.db import Store
    Store(args.db).migrate()
    print(f"schema up to date: {args.db}")
    return 0


def cmd_fetch_typedb(args: argparse.Namespace) -> int:
    """Download into a temporary file beside --out, check that it decompresses to the end, then rename it over --out:
    an interrupted or truncated download leaves the previous file untouched and no partial file behind."""
    import gzip
    import os
    import urllib.request
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".part", dir=out.parent)
    part = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh, urllib.request.urlopen(TYPEDB_URL, timeout=60) as resp:
            shutil.copyfileobj(resp, fh, 1 << 20)
        with gzip.open(part, "rb") as check:
            while check.read(1 << 20):
                pass
        part.chmod(0o644)                   # mkstemp creates it private; the service account must be able to read it
        os.replace(part, out)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    print(f"{out} ({out.stat().st_size} bytes)")
    return 0


def cmd_fetch_metar(args: argparse.Namespace) -> int:
    import json
    from .weather import fetch_metar_raw
    items = fetch_metar_raw([i.strip().upper() for i in args.ids.split(",")], hours=args.hours)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(items))
    print(f"{out}: {len(items)} observations")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from .config import load_config
    from .pipeline import format_stats, replay
    cfg = load_config(args.config)
    run_id, stats = replay(cfg, kind=args.kind, path=args.from_, db_path=args.db, run_tag=args.run_tag,
                           metar_path=args.metar, typedb_path=args.typedb, external_from=args.external_from)
    print(format_stats(stats))
    print(f"run {run_id} written to {args.db}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    import asyncio
    import logging
    import os
    import signal
    from .config import load_config
    from .service import run_live
    cfg = load_config(args.config, env=os.environ)
    logging.basicConfig(level=cfg.service.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    async def main() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        return await run_live(cfg, stop=stop, metar_path=args.metar)

    asyncio.run(main())
    return 0


BUSY_TIMEOUT_S = 30.0       # how long the two maintenance commands wait for a running collector's one-second batch


def _maintenance_store(args: argparse.Namespace):
    """The database fleet-rebuild and reflag work on: --db, else [service] db_path, opened with a busy timeout so a
    write waits for a running collector's batch instead of failing at once."""
    import os
    from .config import load_config
    from .store.db import Store
    cfg = load_config(args.config, env=os.environ)
    store = Store(args.db or cfg.service.db_path, busy_timeout_s=BUSY_TIMEOUT_S)
    store.migrate()
    return cfg, store


def cmd_fleet_rebuild(args: argparse.Namespace) -> int:
    from .fleet import rebuild
    cfg, store = _maintenance_store(args)
    try:
        result = rebuild(store, cfg, include_replays=args.include_replays)
    finally:
        store.close()
    print(result.text())
    return 0


def cmd_reflag(args: argparse.Namespace) -> int:
    import time
    from .fleet import DAY_S, reflag
    cfg, store = _maintenance_store(args)
    try:
        since = 0.0 if args.all else time.time() - cfg.fleet.window_days * DAY_S
        result = reflag(store, cfg, since=since, include_replays=args.include_replays)
    finally:
        store.close()
    print(result.text())
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    import logging
    import os
    from .config import load_config
    from .web.server import build_server
    cfg = load_config(args.config, env=os.environ)
    logging.basicConfig(level=cfg.service.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = build_server(cfg, db_path=args.db, bind=args.bind, port=args.port)
    server.run()
    return 0 if server.started else 1       # uvicorn returns without starting when the app's startup fails


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="adsb-approach")
    sub = parser.add_subparsers(dest="command", required=True)

    bc = sub.add_parser("build-config", help="generate config/airports/*.toml from CIFP (+ OurAirports fallback)")
    bc.add_argument("--cifp", required=True, help="CIFP zip, FAACIFP18 file, or a subset text file")
    bc.add_argument("--airports", required=True, help="comma-separated ICAO idents")
    bc.add_argument("--out", required=True)
    bc.add_argument("--ourairports", help="directory with airports.csv and runways.csv")
    bc.add_argument("--overrides", help="directory with <icao>.toml override files")
    bc.add_argument("--cifp-tag", help="reference tag, default: CIFP file stem")
    bc.set_defaults(func=cmd_build_config)

    fp = sub.add_parser("fit-path", help="propose a procedure path from recorded flights; prints a TOML table, writes nothing")
    fp.add_argument("--config", required=True)
    fp.add_argument("--airport", required=True, help="ICAO ident, e.g. KSFO")
    fp.add_argument("--runway", required=True, help="runway end, e.g. 28R")
    seed = fp.add_mutually_exclusive_group(required=True)
    seed.add_argument("--seed", metavar="LAT,LON,LAT,LON", help="two points on the line the flights follow")
    seed.add_argument("--navaid", metavar="LAT,LON", help="a navaid whose radial the flights follow; needs --radial-true")
    fp.add_argument("--radial-true", help="degrees true, outbound from --navaid (the radial plus declination)")
    fp.add_argument("--from", dest="from_", type=_iso_date, required=True, metavar="YYYY-MM-DD", help="first UTC day")
    fp.add_argument("--to", type=_iso_date, required=True, metavar="YYYY-MM-DD", help="last UTC day, included")
    fp.add_argument("--globe-history", required=True, help="readsb's globe_history directory (YYYY/MM/DD/traces/...)")
    fp.add_argument("--name", required=True, help="the path's name: [a-z0-9-]+, stored on approaches")
    fp.add_argument("--label", required=True, help="what the page shows, e.g. \"Quiet Bridge\"")
    fp.set_defaults(func=cmd_fit_path)

    bn = sub.add_parser("build-nearby", help="list the OurAirports airports near the configured ones, for the flight search")
    bn.add_argument("--ourairports", required=True, help="directory with airports.csv")
    bn.add_argument("--airports-dir", required=True, help="directory of airport files (config/airports)")
    bn.add_argument("--out", required=True)
    bn.add_argument("--radius-nm", type=float, default=150.0, help="how far from an airport file's airport, default 150")
    bn.set_defaults(func=cmd_build_nearby)

    mg = sub.add_parser("migrate", help="create or upgrade the SQLite schema")
    mg.add_argument("--db", required=True)
    mg.set_defaults(func=cmd_migrate)

    ft = sub.add_parser("fetch-typedb", help="download the tar1090-db aircraft.csv.gz")
    ft.add_argument("--out", required=True)
    ft.set_defaults(func=cmd_fetch_typedb)

    fm = sub.add_parser("fetch-metar", help="save aviationweather.gov METAR JSON for --ids")
    fm.add_argument("--ids", required=True)
    fm.add_argument("--hours", type=int, default=0)
    fm.add_argument("--out", required=True)
    fm.set_defaults(func=cmd_fetch_metar)

    rp = sub.add_parser("replay", help="run recorded positions through the pipeline into a tagged run")
    rp.add_argument("--config", required=True)
    rp.add_argument("--kind", choices=["chunks", "jsonl", "traces"], required=True)
    rp.add_argument("--from", dest="from_", required=True)
    rp.add_argument("--db", required=True)
    rp.add_argument("--run-tag", required=True)
    rp.add_argument("--metar", help="aviationweather.gov JSON file (from fetch-metar)")
    rp.add_argument("--typedb", help="aircraft.csv.gz; default from [service].typedb_path")
    rp.add_argument("--external-from", metavar="DB",
                    help="a collector's database: merge the adsb.lol positions it stored, as the live run did")
    rp.set_defaults(func=cmd_replay)

    rn = sub.add_parser("run", help="score approaches live from readsb's JSON port until SIGTERM or SIGINT")
    rn.add_argument("--config", required=True)
    rn.add_argument("--metar", help="aviationweather.gov JSON to preload (from fetch-metar)")
    rn.set_defaults(func=cmd_run)

    fr = sub.add_parser("fleet-rebuild", help="rebuild fleet_stats and coverage_daily over the norm window")
    fr.add_argument("--config", required=True)
    fr.add_argument("--db", help="database to rebuild; default [service].db_path")
    fr.add_argument("--include-replays", action="store_true",
                    help="also read replay runs, for a database of replays rather than a collector's")
    fr.set_defaults(func=cmd_fleet_rebuild)

    rf = sub.add_parser("reflag", help="give the stored approaches the fleet deltas, FAST, SLOW and their norm's key")
    rf.add_argument("--config", required=True)
    rf.add_argument("--db", help="database to reflag; default [service].db_path")
    rf.add_argument("--all", action="store_true", help="every stored approach, not only the last window_days")
    rf.add_argument("--include-replays", action="store_true",
                    help="also reflag replay approaches, for a database of replays rather than a collector's")
    rf.set_defaults(func=cmd_reflag)

    wb = sub.add_parser("web", help="serve the read-only web page and its API until SIGTERM or SIGINT")
    wb.add_argument("--config", required=True)
    wb.add_argument("--db", help="database to read; default [service].db_path")
    wb.add_argument("--bind", help="address to listen on; default [service].bind")
    wb.add_argument("--port", type=int, help="port to listen on; default [service].port")
    wb.set_defaults(func=cmd_web)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

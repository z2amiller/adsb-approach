"""fit-path: a procedure path proposed from recorded flights. Every track is synthetic.

The geometry is KSFO 28R with a seed that sits 6,000 ft right of the centreline at 10 nm and meets it at the threshold
(600 ft per nm). The synthetic flights follow the seed in to a turn at 5.0 nm, fly straight to a join on the centreline
1.2 nm out, and ride the centreline from there; ILS flights ride the centreline throughout."""
from __future__ import annotations

import gzip
import json
import logging
import math
import random
import tomllib
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from adsb_approach import fitpath as fp
from adsb_approach.cli import main
from adsb_approach.config import Config, airport_from_dict
from adsb_approach.fitpath import FitResult, fit_path, toml_snippet, trace_files_between
from adsb_approach.geometry import NM_FT, bearing_deg, destination, runway_frame
from adsb_approach.model import Position, ProcedurePath, RunwayEnd
from adsb_approach.reference import Reference
from tests.conftest import ROOT, T0, path_track
from tests.traces import trace_body

EXAMPLE_CONFIG = ROOT / "config" / "adsb-approach.example.toml"
DAY = f"{datetime.fromtimestamp(T0, timezone.utc):%Y-%m-%d}"       # the day path_track puts its flights on
SEED_SLOPE_FT_PER_NM = 600.0            # the seed's offset to the right of the centreline per nm of distance out
TURN_NM, JOIN_NM = 5.0, 1.2
START_NM = 12.0                         # where the synthetic flights come into the data, in nm still to fly
END_NM = 0.4


def at(rwy: RunwayEnd, along_nm: float, cross_ft: float) -> tuple[float, float]:
    """The position along_nm out on the extended centreline and cross_ft to the right of it."""
    lat, lon = destination(rwy.threshold_lat, rwy.threshold_lon, (rwy.course_true_deg + 180) % 360, along_nm * NM_FT)
    return destination(lat, lon, (rwy.course_true_deg + 90) % 360, cross_ft)


def on_seed(rwy: RunwayEnd, along_nm: float) -> tuple[float, float]:
    return at(rwy, along_nm, along_nm * SEED_SLOPE_FT_PER_NM)


def seed_line(rwy: RunwayEnd) -> tuple[tuple[float, float], tuple[float, float]]:
    return on_seed(rwy, 20.0), on_seed(rwy, 0.0)


@pytest.fixture
def rwy(example_cfg: Config) -> RunwayEnd:
    return example_cfg.airports["KSFO"].runways["28R"]


def quiet_path(rwy: RunwayEnd, *, intercept_ft: float = 0.0) -> Reference:
    """The path the synthetic flights fly: down the seed to the turn, then straight to the join. With `intercept_ft` the
    flights come onto the seed from that far to its right, between 12.6 and 10 nm."""
    first = on_seed(rwy, 13.0) if not intercept_ft else at(rwy, 12.6, 12.6 * SEED_SLOPE_FT_PER_NM + intercept_ft)
    points = (first, *([on_seed(rwy, 10.0)] if intercept_ft else []), on_seed(rwy, TURN_NM), at(rwy, JOIN_NM, 0.0))
    return Reference(rwy, ProcedurePath("synthetic", "Synthetic", "test", points))


def path_through(rwy: RunwayEnd, *vertices: tuple[float, float]) -> Reference:
    """A path through (along_nm, cross_ft) vertices, far end first."""
    return Reference(rwy, ProcedurePath("synthetic", "Synthetic", "test", tuple(at(rwy, a, c) for a, c in vertices)))


def write_trace(root: Path, hexid: str, positions: list[Position]) -> str:
    """The trace file readsb would keep for these positions: <root>/YYYY/MM/DD/traces/<last two hex digits>/trace_full_<hex>.json."""
    day = datetime.fromtimestamp(positions[0].t, timezone.utc)
    folder = root / f"{day:%Y/%m/%d}" / "traces" / hexid[-2:]
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"trace_full_{hexid}.json"
    out.write_bytes(trace_body(hexid, positions))
    return str(out)


def hexid(n: int) -> str:
    return f"{0xA00000 + n:06x}"


def quiet_flights(root: Path, rwy: RunwayEnd, n: int, *, start_nm: float = START_NM, end_nm: float = END_NM,
                  first: int = 0, seed: int = 1, intercept_ft: float = 0.0, ref: Reference | None = None) -> list[str]:
    """n flights down the synthetic path (or `ref`), each a constant distance (up to 60 ft) to one side of it."""
    rng = random.Random(seed)
    ref = ref or quiet_path(rwy, intercept_ft=intercept_ft)
    return [write_trace(root, hexid(first + i),
                        path_track(ref, start_dtg_nm=start_nm, end_dtg_nm=end_nm, cross_ft=rng.uniform(-60.0, 60.0),
                                   hexid=hexid(first + i)))
            for i in range(n)]


def ils_track(rwy: RunwayEnd, *, cross_ft: float = 0.0, n: int = 0) -> list[Position]:
    return path_track(Reference(rwy), start_dtg_nm=START_NM, end_dtg_nm=END_NM, cross_ft=cross_ft, hexid=hexid(500 + n))


def ils_flights(root: Path, rwy: RunwayEnd, n: int, *, seed: int = 2) -> list[str]:
    rng = random.Random(seed)
    return [write_trace(root, hexid(500 + i), ils_track(rwy, cross_ft=rng.uniform(-60.0, 60.0), n=i)) for i in range(n)]


def fit(cfg: Config, rwy: RunwayEnd, files: list[str]) -> FitResult:
    return fit_path(cfg, "KSFO", "28R", seed_line(rwy), files)


def selected(cfg: Config, rwy: RunwayEnd, files: list[str]) -> int:
    """How many of `files` fit-path selects, whether or not that is enough to fit a path."""
    try:
        return fit(cfg, rwy, files).flights
    except ValueError as exc:
        assert str(exc).startswith("fit-path: only ") and str(exc).endswith(" flights followed the seed"), exc
        return int(str(exc).split()[2])


@pytest.fixture
def fitted(tmp_path: Path, example_cfg: Config, rwy: RunwayEnd) -> FitResult:
    return fit(example_cfg, rwy, quiet_flights(tmp_path, rwy, 30) + ils_flights(tmp_path, rwy, 10))


def test_fit_finds_the_turn_and_join(fitted: FitResult, rwy: RunwayEnd):
    assert fitted.flights == 30
    assert fitted.turn_nm == pytest.approx(TURN_NM, abs=0.3)
    assert fitted.join_nm == pytest.approx(JOIN_NM, abs=0.3)
    far, turn, join = fitted.points
    for (lat, lon), nm in ((turn, fitted.turn_nm), (join, fitted.join_nm)):
        assert runway_frame(rwy, lat, lon)[0] / NM_FT == pytest.approx(nm, abs=0.01)
    assert runway_frame(rwy, *turn)[1] == pytest.approx(fitted.turn_nm * SEED_SLOPE_FT_PER_NM, abs=10.0)     # on the seed
    assert abs(runway_frame(rwy, *join)[1]) < 1.0                                                           # on the centreline
    far_along, far_cross = runway_frame(rwy, *far)
    assert far_cross == pytest.approx(far_along / NM_FT * SEED_SLOPE_FT_PER_NM, abs=10.0)    # the far end is on the seed
    assert far_along / NM_FT == pytest.approx(START_NM, abs=0.01)             # the far edge of the farthest covered bin


def test_the_bins_run_from_the_far_end_with_their_spread(fitted: FitResult):
    assert [b[0] for b in fitted.bins] == sorted((b[0] for b in fitted.bins), reverse=True)
    for _, median, p25, p75 in fitted.bins:
        assert p25 <= median <= p75
        assert p75 - p25 < 130.0                                              # the flights are within ±60 ft of one path
    far = fitted.bins[0]
    assert START_NM - 0.5 <= far[0] <= START_NM
    assert abs(far[1]) < 60.0                                                 # on the seed, within the noise
    just_past_the_turn = next(b for b in fitted.bins if b[0] == pytest.approx(3.75))
    assert just_past_the_turn[1] < -200.0                                     # left of the seed once past the turn


def test_ils_flights_are_not_selected(tmp_path: Path, example_cfg: Config, rwy: RunwayEnd, fitted: FitResult):
    """The centreline comes within 0.3 nm of this seed from 3 nm in, so an ILS flight does fly near it for 2 nm, but only
    after its short final begins at about 4.5 nm: too late to count."""
    k = SEED_SLOPE_FT_PER_NM / NM_FT
    near = [along / NM_FT for p in ils_track(rwy) for along, cross in [runway_frame(rwy, p.lat, p.lon)]
            if abs(cross - k * along) / math.hypot(1.0, k) <= 0.3 * NM_FT and along > 0]
    assert max(near) - min(near) > 2.0                                        # the premise: the final 2.7 nm hug the seed
    assert fitted.flights == 30                                               # the 10 ILS flights beside them were left out
    with pytest.raises(ValueError, match=r"^fit-path: only 0 flights followed the seed$"):
        fit(example_cfg, rwy, ils_flights(tmp_path / "ils-only", rwy, 12))


def test_snippet_loads(fitted: FitResult):
    snippet = toml_snippet(fitted, runway="28R", name="synthetic-path", label="Synthetic path",
                           source='fitted "to" 30 synthetic flights, with a back\\slash')
    assert snippet.startswith('[[runways."28R".paths]]\n')
    airport = (ROOT / "config" / "airports" / "ksfo.toml").read_text()
    merged = airport_from_dict(tomllib.loads(airport + "\n" + snippet))       # appends to 28R's existing [[...paths]]
    assert [p.name for p in merged.runways["28R"].paths] == ["quiet-bridge", "synthetic-path"]
    path = merged.runways["28R"].paths[-1]
    assert path.label == "Synthetic path"
    assert path.source == 'fitted "to" 30 synthetic flights, with a back\\slash'
    assert len(path.points) == 3
    for loaded, fitted_point in zip(path.points, fitted.points):
        assert loaded == pytest.approx(fitted_point, abs=1e-6)


def test_too_few_flights_is_an_error(tmp_path: Path, example_cfg: Config, rwy: RunwayEnd):
    files = quiet_flights(tmp_path, rwy, 9) + ils_flights(tmp_path, rwy, 10)
    with pytest.raises(ValueError, match=r"^fit-path: only 9 flights followed the seed$"):
        fit(example_cfg, rwy, files)
    assert fit(example_cfg, rwy, files + quiet_flights(tmp_path, rwy, 1, first=100)).flights == 10      # ten are enough


def test_flights_that_come_onto_the_seed_from_the_side_do_not_hide_the_turn(
        tmp_path: Path, example_cfg: Config, rwy: RunwayEnd, caplog):
    """A flight's run starts where it enters the seed's corridor, so the farthest bins hold flights 1,400 ft off the seed
    that are on their way onto it. The turn is the departure after the seed has been followed, not that one, and a few
    bins like them are not a seed the flights stray from."""
    files = quiet_flights(tmp_path, rwy, 30, start_nm=12.4, intercept_ft=1500.0)
    with caplog.at_level(logging.WARNING, logger="adsb_approach.fitpath"):
        result = fit(example_cfg, rwy, files)
    assert caplog.records == []
    assert result.bins[0][1] > 1000.0                                         # the premise: the far bin is well off the seed
    assert result.turn_nm == pytest.approx(TURN_NM, abs=0.3)
    assert result.join_nm == pytest.approx(JOIN_NM, abs=0.3)


def test_the_seed_must_be_flown_for_2_nm_before_the_short_final(tmp_path: Path, example_cfg: Config, rwy: RunwayEnd):
    """The short final begins at about 3.1 nm on this path, where it comes within 1,500 ft of the centreline: flights that
    come into the data at 4.0 nm have flown under 1 nm of the seed by then, those from 6.0 nm more than 2 nm."""
    assert selected(example_cfg, rwy, quiet_flights(tmp_path / "late", rwy, 12, start_nm=4.0)) == 0
    assert selected(example_cfg, rwy, quiet_flights(tmp_path / "early", rwy, 12, start_nm=6.0)) == 12


def test_a_flight_must_reach_a_short_final(tmp_path: Path, example_cfg: Config, rwy: RunwayEnd):
    assert selected(example_cfg, rwy, quiet_flights(tmp_path, rwy, 12, end_nm=6.0)) == 0


def test_a_flight_too_high_on_final_is_not_selected(tmp_path: Path, example_cfg: Config, rwy: RunwayEnd):
    ref = quiet_path(rwy)
    files = [write_trace(tmp_path, hexid(i), [replace(p, alt_baro=p.alt_baro + 2000) if p.alt_baro is not None else p
                                              for p in path_track(ref, start_dtg_nm=START_NM, end_dtg_nm=END_NM,
                                                                  hexid=hexid(i))])
             for i in range(12)]
    assert selected(example_cfg, rwy, files) == 0


def test_a_flight_on_another_heading_is_not_selected(tmp_path: Path, example_cfg: Config, rwy: RunwayEnd):
    ref = quiet_path(rwy)
    files = [write_trace(tmp_path, hexid(i), [replace(p, track=(rwy.course_true_deg + 30.0) % 360.0)
                                              for p in path_track(ref, start_dtg_nm=START_NM, end_dtg_nm=END_NM,
                                                                  hexid=hexid(i))])
             for i in range(12)]
    assert selected(example_cfg, rwy, files) == 0


def test_an_unknown_airport_or_runway_is_an_error(example_cfg: Config, rwy: RunwayEnd):
    with pytest.raises(ValueError, match=r"fit-path: no airport KXXX"):
        fit_path(example_cfg, "KXXX", "28R", seed_line(rwy), [])
    with pytest.raises(ValueError, match=r"fit-path: KSFO has no runway 99X"):
        fit_path(example_cfg, "KSFO", "99X", seed_line(rwy), [])


def test_a_seed_across_the_approach_is_an_error(example_cfg: Config, rwy: RunwayEnd):
    across = (at(rwy, 5.0, -3000.0), at(rwy, 5.0, 3000.0))
    with pytest.raises(ValueError, match=r"seed must run along the approach"):
        fit_path(example_cfg, "KSFO", "28R", across, [])


def test_trace_files_between_reads_the_days_asked_for(tmp_path: Path):
    for day in ("2026/09/12", "2026/09/13", "2026/09/14", "2026/09/15"):
        folder = tmp_path / day / "traces" / "00"
        folder.mkdir(parents=True)
        (folder / "trace_full_a00000.json").write_bytes(b"{}")
    (tmp_path / "2026/09/13/traces/00/trace_full_a00001.json.readsb_tmp").write_bytes(b"")
    found = trace_files_between(tmp_path, date(2026, 9, 13), date(2026, 9, 14))
    assert [Path(f).relative_to(tmp_path).parts[:3] for f in found] == [("2026", "09", "13"), ("2026", "09", "14")]
    assert trace_files_between(tmp_path, date(2026, 10, 1), date(2026, 10, 2)) == []


# --- the estimator, on bins ----------------------------------------------------------------------------------------

SEED = fp._Seed(slope=SEED_SLOPE_FT_PER_NM / NM_FT, offset_ft=0.0)       # the synthetic seed in the runway's frame
Bins = list[tuple[float, float, float, float]]


def seed_y(along_nm: float) -> float:
    return along_nm * SEED_SLOPE_FT_PER_NM


def profile_bins(*, turn_nm: float = TURN_NM, join_nm: float = JOIN_NM, far_nm: float = 19.75, last_nm: float = 0.75,
                 medians: dict[float, float] | None = None) -> Bins:
    """The bins a long run of flights along the synthetic path would leave, far end first: the median at each bin
    centre is the path's offset from the seed there, with those in `medians` replaced."""
    bins: Bins = []
    x = far_nm
    while x >= last_nm - 1e-9:
        y = seed_y(x) if x >= turn_nm else seed_y(turn_nm) * (x - join_nm) / (turn_nm - join_nm) if x >= join_nm else 0.0
        d = (medians or {}).get(x, SEED.offset(x * NM_FT, y))
        bins.append((x, d, d - 20.0, d + 20.0))
        x -= BIN_STEP
    return bins


BIN_STEP = 0.5


def leg_bins(*, slope: float, at_4_75: float = -210.0, last_nm: float = 1.75, y_offset: float | None = None) -> Bins:
    """Bins on the seed (offset 0) from the far end to 5.25 nm, then a straight leg of `slope` ft per nm whose median is
    `at_4_75` at 4.75 nm. With `y_offset` the leg is instead parallel to the centreline, that many ft right of it."""
    bins = [b for b in profile_bins(medians={x: 0.0 for x in (5.25 + 0.5 * i for i in range(30))}) if b[0] >= 5.25]
    x = 4.75
    while x >= last_nm - 1e-9:
        d = SEED.offset(x * NM_FT, y_offset) if y_offset is not None else at_4_75 + slope * (x - 4.75)
        bins.append((x, d, d - 20.0, d + 20.0))
        x -= BIN_STEP
    return bins


@pytest.mark.parametrize("stray_nm, median", [(18.25, -210.0), (12.25, -205.0), (8.25, -201.0), (12.25, 400.0)])
def test_one_stray_bin_far_out_does_not_move_the_turn(stray_nm: float, median: float):
    """The leg is the run of bins more than 200 ft off the seed that ends at the join: a single bin further out that
    strays past 200 ft does not start it."""
    assert fp._turn_and_join(SEED, profile_bins()) == pytest.approx((TURN_NM, JOIN_NM), abs=0.01)
    turn, join = fp._turn_and_join(SEED, profile_bins(medians={stray_nm: median}))
    assert turn == pytest.approx(TURN_NM, abs=0.05)
    assert join == pytest.approx(JOIN_NM, abs=0.05)


def test_a_detour_early_in_the_first_leg_does_not_move_the_turn(tmp_path: Path, example_cfg: Config, rwy: RunwayEnd):
    """Flights that all swing 300 ft left of the seed around 12 nm and come back: two bins past 200 ft, far from the turn."""
    jog = path_through(rwy, (14.5, seed_y(14.5)), (12.25, seed_y(12.25) - 300.0), (11.0, seed_y(11.0)),
                       (TURN_NM, seed_y(TURN_NM)), (JOIN_NM, 0.0))
    result = fit(example_cfg, rwy, quiet_flights(tmp_path, rwy, 30, start_nm=14.0, ref=jog))
    assert min(b[1] for b in result.bins if 11.5 < b[0] < 13.0) < -200.0                   # the premise: the swing shows
    assert result.turn_nm == pytest.approx(TURN_NM, abs=0.3)
    assert result.join_nm == pytest.approx(JOIN_NM, abs=0.3)


def test_the_leg_to_the_join_is_found_as_long_as_it_leaves_the_seed_steeply_enough():
    just_enough = leg_bins(slope=fp.MIN_LEG_SLOPE_FT_PER_NM * 1.2)
    turn, join = fp._turn_and_join(SEED, just_enough)
    assert 0.0 < join < turn < 20.0


def test_a_leg_that_leaves_the_seed_too_slowly_places_no_turn():
    """At 20 ft per nm a 50 ft change in a median moves the turn 2.5 nm: here it would sit 15 nm out."""
    with pytest.raises(ValueError, match=r"fit-path: the leg to the join leaves the seed by only 20 ft per nm"):
        fp._turn_and_join(SEED, leg_bins(slope=20.0))


def test_a_median_that_never_comes_within_200_ft_of_the_seed_is_an_error():
    bins = [(x, -300.0, -320.0, -280.0) for x, *_ in profile_bins()]
    with pytest.raises(ValueError, match=r"fit-path: the median is never within 200 ft of the seed"):
        fp._turn_and_join(SEED, bins)


def test_a_median_that_never_leaves_the_seed_is_an_error():
    bins = [(x, 0.0, -20.0, 20.0) for x, *_ in profile_bins()]
    with pytest.raises(ValueError, match=r"fit-path: the median never leaves the seed by more than 200 ft"):
        fp._turn_and_join(SEED, bins)


def test_a_leg_of_one_bin_is_too_few_to_fit():
    bins = leg_bins(slope=170.0, last_nm=4.75)
    with pytest.raises(ValueError, match=r"fit-path: the leg to the join holds 1 bin; it takes 2 to fit"):
        fp._turn_and_join(SEED, bins)


def test_medians_on_the_centreline_from_the_far_end_are_an_error():
    bins = profile_bins(turn_nm=25.0, join_nm=24.0)
    with pytest.raises(ValueError, match=r"fit-path: the median is on the centreline from the farthest bin"):
        fp._turn_and_join(SEED, bins)


def test_a_leg_parallel_to_the_centreline_never_joins_it():
    with pytest.raises(ValueError, match=r"fit-path: the leg to the join runs parallel to the centreline"):
        fp._turn_and_join(SEED, leg_bins(slope=0.0, y_offset=500.0))


def test_a_leg_that_would_join_behind_the_threshold_is_not_a_path():
    """A leg bearing away from the seed on its right meets the seed 7.7 nm out and the centreline behind the threshold."""
    bins = [b for b in leg_bins(slope=-100.0, at_4_75=300.0)]
    bins = [(x, d if x >= 5.25 else 300.0 - 100.0 * (x - 4.75), d - 20.0, d + 20.0) for x, d, *_ in bins]
    with pytest.raises(ValueError, match=r"fit-path: the flights put the turn .* the join .*: that is no path that turns, then joins"):
        fp._turn_and_join(SEED, bins)


def test_no_bin_holding_ten_flights_is_an_error():
    with pytest.raises(ValueError, match=r"fit-path: no 0.5 nm bin holds 10 of the 12 flights"):
        fp._bin_table({3: [1.0] * 9, 4: [2.0] * 9}, flights=12)


def test_a_join_too_far_out_breaks_the_rules_for_a_path(tmp_path: Path, example_cfg: Config, rwy: RunwayEnd):
    """Flights that leave the seed at 14 nm and join the centreline 7 nm out: a path may join at 5 nm at the most."""
    early = path_through(rwy, (19.5, seed_y(19.5)), (14.0, seed_y(14.0)), (7.0, 0.0))
    with pytest.raises(ValueError, match=r"fit-path: the fitted path breaks the rules for a path: .*within 5 nm of the threshold"):
        fit(example_cfg, rwy, quiet_flights(tmp_path, rwy, 30, start_nm=18.0, ref=early))


def test_a_seed_half_a_degree_off_is_an_error_not_a_path(tmp_path: Path, example_cfg: Config, rwy: RunwayEnd):
    """Rotated about the threshold by 0.5 degrees the seed is 53 ft per nm off the flights: the medians are never within 200 ft."""
    off = (at(rwy, 20.0, 20.0 * (SEED_SLOPE_FT_PER_NM + 53.0)), at(rwy, 0.0, 0.0))
    with pytest.raises(ValueError, match=r"fit-path: the median is never within 200 ft of the seed"):
        fit_path(example_cfg, "KSFO", "28R", off, quiet_flights(tmp_path, rwy, 30))


def test_a_seed_a_little_off_the_flown_line_warns(tmp_path: Path, example_cfg: Config, rwy: RunwayEnd, caplog):
    """30 ft per nm off: the bins by the turn are within 200 ft of it, but over the first leg the medians typically sit
    300+ ft from it."""
    files = quiet_flights(tmp_path, rwy, 30, start_nm=18.0)
    off = (at(rwy, 20.0, 20.0 * (SEED_SLOPE_FT_PER_NM + 30.0)), at(rwy, 0.0, 0.0))
    with caplog.at_level(logging.WARNING, logger="adsb_approach.fitpath"):
        result = fit_path(example_cfg, "KSFO", "28R", off, files)
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1 and "first leg" in warnings[0] and "check the seed" in warnings[0], warnings
    assert result.flights == 30
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="adsb_approach.fitpath"):
        fit(example_cfg, rwy, files)                                       # the seed the flights flew: nothing to say
    assert caplog.records == []


def tamper(path: str, change) -> None:
    """Rewrite a trace file as plain JSON after `change(dict)` (json.dumps writes Infinity for an infinite float)."""
    body = json.loads(gzip.decompress(Path(path).read_bytes()))
    change(body)
    Path(path).write_text(json.dumps(body))


@pytest.mark.parametrize("name, change", [
    ("timestamp-text", lambda b: b.update(timestamp="x")),
    ("timestamp-null", lambda b: b.update(timestamp=None)),
    ("dict-row", lambda b: b["trace"].insert(1, {"1": 2})),
    ("no-icao", lambda b: b.pop("icao")),
    ("trace-not-a-list", lambda b: b.update(trace={"a": 1})),
    ("infinite-latitude", lambda b: b["trace"].insert(5, [1.0, float("inf"), float("inf"), 1000, 150, 290, 0])),
])
def test_a_malformed_trace_file_is_skipped_with_a_warning_that_names_it(
        tmp_path: Path, example_cfg: Config, rwy: RunwayEnd, caplog, name: str, change):
    files = quiet_flights(tmp_path, rwy, 31)
    tamper(files[-1], change)
    with caplog.at_level(logging.WARNING, logger="adsb_approach.fitpath"):
        result = fit(example_cfg, rwy, files)
    assert result.flights == 30
    assert [r for r in caplog.records if files[-1] in r.getMessage() and "skipped" in r.getMessage()], caplog.text


# --- the command ---------------------------------------------------------------------------------------------------

def fit_path_args(tmp_path: Path, *seed: str) -> list[str]:
    return ["fit-path", "--config", str(EXAMPLE_CONFIG), "--airport", "KSFO", "--runway", "28R", *seed,
            "--from", DAY, "--to", DAY, "--globe-history", str(tmp_path), "--name", "synthetic-path",
            "--label", "Synthetic path"]


def seed_option(rwy: RunwayEnd) -> list[str]:
    (lat1, lon1), (lat2, lon2) = seed_line(rwy)
    return ["--seed", f"{lat1:.6f},{lon1:.6f},{lat2:.6f},{lon2:.6f}"]


def test_the_command_prints_the_snippet_and_the_bins_and_writes_nothing(capsys, tmp_path: Path, rwy: RunwayEnd):
    quiet_flights(tmp_path, rwy, 30)
    ils_flights(tmp_path, rwy, 10)
    before = sorted(tmp_path.rglob("*"))
    assert main(fit_path_args(tmp_path, *seed_option(rwy))) == 0
    out, err = capsys.readouterr()
    assert err == ""
    assert sorted(tmp_path.rglob("*")) == before
    path = tomllib.loads(out)["runways"]["28R"]["paths"][0]                  # the whole output is TOML: the bins are comments
    assert (path["name"], path["label"]) == ("synthetic-path", "Synthetic path")
    assert len(path["points"]) == 3
    assert "30 flights" in path["source"] and f"{DAY}..{DAY}" in path["source"] and "(fit-path)" in path["source"]
    table = [line for line in out.splitlines() if line.startswith("#")]
    assert any("along_nm" in line and "p25" in line and "p75" in line for line in table)
    assert sum(1 for line in table if line.lstrip("# ")[:1].isdigit()) >= 20      # a row for each covered bin


def test_the_command_takes_a_navaid_and_a_radial(capsys, tmp_path: Path, rwy: RunwayEnd):
    quiet_flights(tmp_path, rwy, 30)
    (far_lat, far_lon), (nav_lat, nav_lon) = seed_line(rwy)
    radial = bearing_deg(nav_lat, nav_lon, far_lat, far_lon)
    assert main(fit_path_args(tmp_path, "--navaid", f"{nav_lat:.6f},{nav_lon:.6f}", "--radial-true", f"{radial:.3f}")) == 0
    path = tomllib.loads(capsys.readouterr().out)["runways"]["28R"]["paths"][0]
    assert len(path["points"]) == 3
    assert f"radial {radial:.1f}" in path["source"]


@pytest.mark.parametrize("seed, message", [
    (["--navaid", "37.6,-122.3"], "--navaid needs --radial-true"),
    (["--seed", "37.6,-122.3,37.5,-122.1", "--radial-true", "112"], "--radial-true goes with --navaid"),
    (["--seed", "37.6,-122.3,37.5"], "--seed takes LAT,LON,LAT,LON"),
    (["--navaid", "37.6,-122.3", "--radial-true", "north"], "--radial-true must be a number"),
])
def test_the_command_explains_a_bad_seed(capsys, tmp_path: Path, seed: list[str], message: str):
    assert main(fit_path_args(tmp_path, *seed)) == 1
    out, err = capsys.readouterr()
    assert out == "" and message in err and "Traceback" not in err


def test_the_command_needs_one_seed(tmp_path: Path):
    with pytest.raises(SystemExit):
        main(fit_path_args(tmp_path))


def test_the_command_reports_too_few_flights_without_a_traceback(capsys, tmp_path: Path, rwy: RunwayEnd):
    quiet_flights(tmp_path, rwy, 4)
    assert main(fit_path_args(tmp_path, *seed_option(rwy))) == 1
    out, err = capsys.readouterr()
    assert out == "" and "fit-path: only 4 flights followed the seed" in err and "Traceback" not in err


@pytest.mark.parametrize("option, value, message", [
    ("--name", "Quiet Bridge", "--name must match [a-z0-9-]+"),
    ("--name", "", "--name must match [a-z0-9-]+"),
    ("--label", "   ", "--label must say what to show on the page"),
])
def test_the_command_checks_the_name_and_label_before_scanning(capsys, tmp_path: Path, rwy: RunwayEnd, option: str, value: str, message: str):
    """The globe_history directory is empty: had the command got as far as the scan, it would say there are no traces."""
    args = fit_path_args(tmp_path, *seed_option(rwy))
    args[args.index(option) + 1] = value
    assert main(args) == 1
    out, err = capsys.readouterr()
    assert out == "" and message in err and "no trace files" not in err

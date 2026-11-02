import sqlite3
import statistics
from pathlib import Path

import pytest

from adsb_approach.config import load_config
from adsb_approach.model import Flags, flag_names
from adsb_approach.pipeline import replay
from tests.conftest import FIXTURES, ROOT

CHUNKS = FIXTURES / "raw" / "tar1090-chunks-20260912"
PRESUMED_AND_CONFIRMED = ["GATE500_UNOBSERVED", "PRESUMED_LANDED", "LANDING_CONFIRMED"]
WALL_CLOCK_COLUMNS = {"approaches": {"created_at"}}


def run_golden(db: Path):
    cfg = load_config(ROOT / "config" / "adsb-approach.example.toml")
    return replay(cfg, kind="chunks", path=CHUNKS, db_path=db, run_tag="golden",
                  metar_path=FIXTURES / "metar-20260912.json", typedb_path=FIXTURES / "typedb-sample.csv")


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    db = tmp_path_factory.mktemp("golden") / "g.sqlite"
    run_id, stats = run_golden(db)
    return db, run_id, stats


def rows(db: Path, sql: str, *args):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def approach(db: Path, hexid: str, airport: str, runway: str, established_at: float) -> sqlite3.Row:
    """The one approach by hexid to airport/runway established within two minutes of established_at."""
    found = rows(db, "SELECT * FROM approaches WHERE hex=? AND airport=? AND runway=? AND ABS(established_at - ?) < 120",
                 hexid, airport, runway, established_at)
    assert len(found) == 1, (hexid, airport, runway, len(found))
    return found[0]


def gate(db: Path, approach_id: int, gate_ft: int) -> sqlite3.Row:
    return rows(db, "SELECT * FROM approach_gates WHERE approach_id=? AND gate_ft=?", approach_id, gate_ft)[0]


def test_every_attempt_closed(golden):
    db, run_id, stats = golden
    assert stats.positions > 60000 and stats.opened == stats.closed
    assert rows(db, "SELECT COUNT(*) AS c FROM approaches WHERE outcome IS NULL")[0]["c"] == 0


def test_sfo_volume_and_coverage(golden):
    db, _, _ = golden
    sfo = rows(db, "SELECT * FROM approaches WHERE airport='KSFO' AND runway IN ('28L','28R')")
    assert len(sfo) >= 80
    assert {r["runway"] for r in sfo} == {"28L", "28R"}
    landed_like = [r for r in sfo if r["outcome"] in ("landed", "lost_on_final")]
    assert len(landed_like) / len(sfo) >= 0.5
    assert 400 < statistics.median(r["lowest_hat_ft"] for r in landed_like) < 1400
    assert all(Flags.PRESUMED_LANDED in Flags(r["flags"]) for r in landed_like if r["outcome"] == "lost_on_final")


def test_ual2177_go_around_chain(golden):
    db, _, stats = golden
    a = rows(db, "SELECT * FROM approaches WHERE hex='a44540' ORDER BY established_at")
    assert [r["outcome"] for r in a] == ["go_around", "lost_on_final"]
    assert Flags.INFERRED_GO_AROUND in Flags(a[0]["flags"]) and Flags.PRESUMED_LANDED not in Flags(a[0]["flags"])
    assert abs(a[0]["ended_at"] - 1789226778) < 30
    assert -90 < gate(db, a[0]["id"], 1000)["gp_dev_ft"] < -10          # observed -48 ft at 3.29 nm
    assert a[1]["arrival_id"] == a[0]["id"] and a[1]["attempt_no"] == 2 and a[1]["prev_approach_id"] == a[0]["id"]
    assert a[0]["next_approach_id"] == a[1]["id"]
    assert 3.0 < a[1]["closest_along_nm"] < 3.8 and 900 < a[1]["lowest_hat_ft"] < 1250
    assert a[1]["landed_confirmed_by"] == "departure" and Flags.LANDING_CONFIRMED in Flags(a[1]["flags"])
    assert any(g["hex"] == "a44540" for g in stats.go_arounds) or stats.by_outcome["go_around"] >= 1


def test_ual2432_sidestep(golden):
    db, _, _ = golden
    a = rows(db, "SELECT * FROM approaches WHERE hex='ac2706' ORDER BY established_at")
    assert len(a) >= 2 and a[0]["runway"] == "28L" and a[0]["outcome"] == "runway_change"
    assert a[1]["runway"] == "28R" and Flags.SIDESTEP in Flags(a[1]["flags"]) and a[1]["arrival_id"] == a[0]["id"]


def test_runway_changes_are_the_five_sidesteps(golden):
    # 28L -> 28R sidesteps confirmed from their point tracks during review; any other runway change is spurious.
    db, _, stats = golden
    changed = {r["hex"] for r in rows(db, "SELECT hex FROM approaches WHERE outcome='runway_change'")}
    assert changed == {"a7ccce", "ab0f89", "a4b5b6", "ac2706", "a4dfd2"}
    assert sorted(r["hex"] for r in stats.runway_changes) == sorted(changed)


def test_clean_28l_landing(golden):
    # DAL1053, A220-300, 16:17Z. Observed: gate 1000 at 3.20 nm, -19.9 ft (-0.15 dot), 0.1 ft left, 124 kt with a
    # 12 kt headwind; whole final rms 25.5 ft vertical and 9.3 ft lateral, 2.90 deg against the 2.85 deg glide slope.
    db, _, _ = golden
    a = approach(db, "a37265", "KSFO", "28L", 1789229842.4)
    assert a["outcome"] == "lost_on_final" and a["landed_confirmed_by"] == "departure"
    assert flag_names(a["flags"]) == PRESUMED_AND_CONFIRMED
    g = gate(db, a["id"], 1000)
    assert g["observed"] == 1 and 2.9 < g["along_nm"] < 3.5
    assert -60 < g["gp_dev_ft"] < 20 and abs(g["gp_dots"]) < 0.5 and abs(g["loc_dev_ft"]) < 60
    assert 110 < g["gs"] < 140 and 5 < g["gs_corr"] - g["gs"] < 20
    assert gate(db, a["id"], 500)["observed"] == 0
    assert a["gp_dev_rms_ft"] < 60 and a["loc_dev_rms_ft"] < 40 and 2.6 < a["descent_angle_deg"] < 3.2


def test_clean_28r_landing(golden):
    # SKW5293, E175, 13:57Z. Observed: gate 1000 at 3.01 nm, -12.6 ft (-0.11 dot), 0.3 ft right, 138 kt; whole final
    # mean -16.8 ft, rms 19.1 ft vertical and 8.0 ft lateral, 3.03 deg against the 3.00 deg glide slope.
    db, _, _ = golden
    a = approach(db, "ac53be", "KSFO", "28R", 1789221400.7)
    assert a["outcome"] == "lost_on_final" and a["landed_confirmed_by"] == "departure"
    assert flag_names(a["flags"]) == PRESUMED_AND_CONFIRMED
    g = gate(db, a["id"], 1000)
    assert g["observed"] == 1 and 2.7 < g["along_nm"] < 3.3
    assert -55 < g["gp_dev_ft"] < 30 and abs(g["gp_dots"]) < 0.5 and abs(g["loc_dev_ft"]) < 60
    assert 120 < g["gs"] < 155
    assert -60 < a["gp_dev_mean_ft"] < 25 and a["gp_dev_rms_ft"] < 60 and a["loc_dev_rms_ft"] < 40
    assert 2.7 < a["descent_angle_deg"] < 3.3


def test_oak_30_arrival(golden):
    # FDX5044, B777F, 12:56Z. Observed: gate 1000 at 2.98 nm, -2.3 ft, 3.2 ft left, 154 kt; whole final mean +0.3 ft,
    # rms 5.1 ft vertical and 12.6 ft lateral, 2.99 deg against the 3.00 deg glide slope.
    db, _, _ = golden
    a = approach(db, "abbe23", "KOAK", "30", 1789217752.6)
    assert a["outcome"] == "lost_on_final" and a["landed_confirmed_by"] == "departure"
    assert flag_names(a["flags"]) == PRESUMED_AND_CONFIRMED
    g = gate(db, a["id"], 1000)
    assert g["observed"] == 1 and 2.7 < g["along_nm"] < 3.3
    assert -40 < g["gp_dev_ft"] < 40 and abs(g["loc_dev_ft"]) < 60 and 140 < g["gs"] < 170
    assert abs(a["gp_dev_mean_ft"]) < 40 and a["gp_dev_rms_ft"] < 40 and a["loc_dev_rms_ft"] < 40
    assert 2.7 < a["descent_angle_deg"] < 3.3


def test_nuq_32l_arrival(golden):
    # CNS18, PC-24, 14:44Z, on the 3.5 deg path to 32L and tracked down to 90 ft at 0.14 nm. Observed: gate 1000 at
    # 2.55 nm, -1.2 ft, 47 ft left; gate 500 at 1.23 nm, -12.1 ft, 3.4 ft left; 3.46 deg. It closes as `landed`
    # because its last points were under 100 ft in the runway footprint, only 10 ft inside that limit, so
    # lost_on_final is accepted too.
    db, _, _ = golden
    a = approach(db, "a316ef", "KNUQ", "32L", 1789224265.6)
    assert a["outcome"] in ("landed", "lost_on_final") and a["lowest_hat_ft"] < 250 and a["closest_along_nm"] < 0.5
    unstable = Flags.HIGH | Flags.LOW | Flags.OFF_COURSE | Flags.HIGH_SINK | Flags.UNSTABLE_1000 | Flags.UNSTABLE_500
    assert not Flags(a["flags"]) & (unstable | Flags.GATE1000_UNOBSERVED | Flags.GATE500_UNOBSERVED)
    g1000, g500 = gate(db, a["id"], 1000), gate(db, a["id"], 500)
    assert 2.3 < g1000["along_nm"] < 2.8 and -40 < g1000["gp_dev_ft"] < 40 and abs(g1000["loc_dev_ft"]) < 150
    assert 1.0 < g500["along_nm"] < 1.45 and -50 < g500["gp_dev_ft"] < 30 and abs(g500["loc_dev_ft"]) < 100
    assert 3.2 < a["descent_angle_deg"] < 3.8


def test_sjc_partial_coverage(golden):
    db, _, _ = golden
    sjc = rows(db, "SELECT * FROM approaches WHERE airport='KSJC'")
    assert len(sjc) >= 2
    lost = [r for r in sjc if r["outcome"] in ("lost", "lost_on_final")]
    assert len(lost) / len(sjc) >= 0.8
    assert statistics.median(r["lowest_hat_ft"] for r in sjc) > 900
    unobserved = Flags.GATE1000_UNOBSERVED | Flags.GATE500_UNOBSERVED
    assert all((Flags(r["flags"]) & unobserved) == unobserved for r in sjc)     # tracked from ~5 nm: no gate reached


def test_fleet_sits_near_the_published_paths(golden):
    # Median whole-final vertical deviation of the landings per runway, observed: KSFO 28L -16 ft (67), 28R -5 ft (36),
    # KOAK 30 +3 ft (27). An altimetry or datum error (QNH sign, geoid offset: about 100 ft) moves them past 50 ft.
    db, _, _ = golden
    for airport, runway in (("KSFO", "28L"), ("KSFO", "28R"), ("KOAK", "30")):
        devs = [r["gp_dev_mean_ft"] for r in rows(db, "SELECT gp_dev_mean_ft FROM approaches WHERE airport=? AND runway=? "
                                                      "AND outcome IN ('landed', 'lost_on_final') AND gp_dev_mean_ft IS NOT NULL",
                                                  airport, runway)]
        assert len(devs) >= 15 and abs(statistics.median(devs)) < 50, (airport, runway, len(devs))


def test_quiet_bridge_arrivals_are_recorded_on_28r(golden):
    # The chunk replay carries the committed Quiet Bridge path (config/airports/ksfo.toml). Its flights are recorded as
    # 28R approaches measured against the path, so their lateral numbers are small where, measured from the centreline,
    # they were thousands of feet. Prototype: 77 rows (72 lost_on_final, 4 lost, 1 go-around), median |loc dev| at the
    # 1000 ft gate 32 ft (p90 204 ft).
    db, _, _ = golden
    on_path = rows(db, "SELECT * FROM approaches WHERE path = 'quiet-bridge'")
    assert len(on_path) >= 70
    assert {(r["airport"], r["runway"]) for r in on_path} == {("KSFO", "28R")}
    gates = [gate(db, r["id"], 1000) for r in on_path]
    locs = [abs(g["loc_dev_ft"]) for g in gates if g["observed"]]
    assert len(locs) >= 30 and statistics.median(locs) < 100            # 46 reach the gate; the rest end above 1000 ft
    # SWA1368 went around from a Quiet Bridge approach to 28R and landed on 28L: one arrival, two attempts
    [first] = rows(db, "SELECT * FROM approaches WHERE hex='abb930' AND runway='28R' AND outcome='go_around'")
    assert first["path"] == "quiet-bridge"
    [second] = rows(db, "SELECT * FROM approaches WHERE hex='abb930' AND runway='28L' AND attempt_no=2")
    assert second["arrival_id"] == first["arrival_id"] and second["prev_approach_id"] == first["id"]
    assert second["arrival_id"] == first["id"] and first["attempt_no"] == 1


def table(db: Path, name: str, order_by: str) -> list[tuple]:
    """Every row of a table in a stable order, without its wall-clock columns."""
    conn = sqlite3.connect(db)
    try:
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({name})") if c[1] not in WALL_CLOCK_COLUMNS.get(name, set())]
        return conn.execute(f"SELECT {', '.join(cols)} FROM {name} ORDER BY {order_by}").fetchall()
    finally:
        conn.close()


def test_replay_is_deterministic(golden, tmp_path):
    db1, _, _ = golden
    db2 = tmp_path / "again.sqlite"
    run_golden(db2)
    for name, order_by in (("approaches", "id"), ("approach_points", "approach_id, t"), ("approach_gates", "approach_id, gate_ft"),
                           ("approach_bins", "approach_id, bin_nm"), ("flights", "id"), ("events", "id")):
        first, again = table(db1, name, order_by), table(db2, name, order_by)
        assert first and first == again, name

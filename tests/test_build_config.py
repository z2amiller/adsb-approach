import tomllib
from pathlib import Path

import pytest

from adsb_approach.config import load_airport_file
from adsb_approach.geometry import bearing_deg, track_diff_deg
from adsb_approach.refdata.build import build_airport, build_all, opposite_ident, to_toml
from adsb_approach.refdata.cifp import parse_cifp
from adsb_approach.refdata.ourairports import parse_airports, parse_runways
from tests.conftest import QUIET_BRIDGE

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def cifp():
    return parse_cifp(FIX / "cifp" / "FAACIFP18-subset.txt")


def test_to_toml_round_trips_nested_tables():
    d = {"airport": {"icao": "KSFO", "enabled": True, "lat": 37.5, "n": 3, "parallel_groups": [["28L", "28R"]],
                     "note": 'say "hi"'},
         "runways": {"28R": {"tch_ft": 55, "ils": None, "glide_path_deg": 3.0}}}
    text = to_toml(d)
    back = tomllib.loads(text)
    assert back["airport"]["parallel_groups"] == [["28L", "28R"]] and back["airport"]["note"] == 'say "hi"'
    assert back["runways"]["28R"]["tch_ft"] == 55 and "ils" not in back["runways"]["28R"]
    assert '[runways."28R"]' in text


def test_ksfo_from_cifp(cifp):
    d = build_airport("KSFO", cifp, None, None, cifp_tag="CIFP_260903")
    apt = d["airport"]
    assert apt["declination_deg"] == 14.0 and apt["name"] == "SAN FRANCISCO INTL"
    assert -109 < apt["geoid_offset_ft"] < -105
    assert ["28L", "28R"] in apt["parallel_groups"] and ["01L", "01R"] in apt["parallel_groups"]
    r = d["runways"]["28R"]
    assert (r["glide_path_deg"], r["tch_ft"], r["loc_width_deg"]) == (3.0, 55, 3.29)
    assert r["threshold_lat"] == pytest.approx(37.613919, abs=1e-6)
    assert (r["threshold_elev_ft"], r["threshold_hae_m"], r["displaced_threshold_ft"], r["runway_length_ft"]) == (13, -28.7, 300, 11870)
    # 28R landing threshold 37.613919, -122.358058 -> 10L landing threshold 37.628739, -122.393392 (CIFP PG records):
    # initial great-circle bearing 297.914; the PG runway bearing, 284 magnetic + 14 E, gave 298.0
    assert r["course_true_deg"] == pytest.approx(297.914, abs=0.002)
    assert r["localizer_lat"] == pytest.approx(37.629544, abs=1e-5)
    assert r["ils"] == "IGWQ 111.70 CAT 3" and r["reference_source"] == "cifp:CIFP_260903"
    assert d["runways"]["28L"]["glide_path_deg"] == 2.85 and d["runways"]["28L"]["tch_ft"] == 53
    r01l = d["runways"]["01L"]                    # no ILS, no path point, no straight-in procedure
    assert r01l["glide_path_deg"] == 3.0 and r01l["tch_ft"] == 50
    assert r01l["reference_source"] == "cifp:CIFP_260903+assumed"
    assert "localizer_lat" not in r01l


def test_knuq_lpv_and_localizer_only(cifp):
    d = build_airport("KNUQ", cifp, None, None, cifp_tag="CIFP_260903")
    r32l = d["runways"]["32L"]
    assert r32l["glide_path_deg"] == 3.5 and r32l["tch_ft"] == 55.0 and r32l["reference_source"] == "cifp:CIFP_260903"
    r14l = d["runways"]["14L"]                    # localizer without glide slope: LPV path point supplies the angle
    assert r14l["loc_width_deg"] == 3.97 and r14l["glide_path_deg"] == 3.0 and r14l["ils"].startswith("IMNQ")


def test_ourairports_fallback_when_missing_from_cifp(cifp):
    oa_r = parse_runways(FIX / "ourairports" / "runways.csv")
    oa_a = parse_airports(FIX / "ourairports" / "airports.csv")
    only_nuq = parse_cifp(FIX / "cifp" / "FAACIFP18-subset.txt", airports={"KNUQ"})
    d = build_airport("KSFO", only_nuq, oa_r, oa_a, cifp_tag="CIFP_260903")
    r = d["runways"]["28R"]
    assert r["reference_source"] == "ourairports+assumed" and r["glide_path_deg"] == 3.0
    assert r["threshold_lat"] == pytest.approx(37.613919, abs=2e-5)   # end moved 300 ft along 298 deg
    assert d["airport"]["name"].startswith("San Francisco") and d["airport"]["geoid_offset_ft"] == 0.0


def test_build_all_writes_loadable_files(tmp_path, cifp):
    paths = build_all(["KSFO", "KOAK", "KNUQ", "KSJC"], FIX / "cifp" / "FAACIFP18-subset.txt", tmp_path,
                      ourairports_dir=FIX / "ourairports", cifp_tag="CIFP_260903")
    assert sorted(p.name for p in paths) == ["knuq.toml", "koak.toml", "ksfo.toml", "ksjc.toml"]
    apt = load_airport_file(tmp_path / "ksfo.toml")
    assert apt.runways["28R"].loc_width_deg == 3.29 and apt.runways["28R"].parallel_group == ("28L", "28R")
    assert load_airport_file(tmp_path / "koak.toml").runways["12"].glide_path_deg == 2.75


def test_overrides_are_applied(tmp_path, cifp):
    (tmp_path / "ksfo.toml").write_text('[airport]\nenabled = false\n[runways."28R"]\ntch_ft = 60\n')
    paths = build_all(["KSFO"], FIX / "cifp" / "FAACIFP18-subset.txt", tmp_path / "out", overrides_dir=tmp_path,
                      cifp_tag="CIFP_260903")
    apt = load_airport_file(paths[0])
    assert apt.enabled is False and apt.runways["28R"].tch_ft == 60 and apt.runways["28L"].tch_ft == 53


def test_a_typo_in_an_overrides_file_is_rejected(tmp_path):
    (tmp_path / "ksfo.toml").write_text('[runways."28R"]\ntch = 60\n')
    with pytest.raises(ValueError, match=r"ksfo\.toml: runway KSFO/28R: unknown key 'tch' \(did you mean 'tch_ft'\?\)"):
        build_all(["KSFO"], FIX / "cifp" / "FAACIFP18-subset.txt", tmp_path / "out", overrides_dir=tmp_path,
                  cifp_tag="CIFP_260903")


def test_course_is_the_bearing_to_the_opposite_threshold(tmp_path):
    paths = build_all(["KSFO", "KOAK", "KNUQ", "KSJC"], FIX / "cifp" / "FAACIFP18-subset.txt", tmp_path,
                      ourairports_dir=FIX / "ourairports", cifp_tag="CIFP_260903")
    checked = 0
    for path in paths:
        apt = load_airport_file(path)
        for ident, r in apt.runways.items():
            o = apt.runways[opposite_ident(ident)]
            b = bearing_deg(r.threshold_lat, r.threshold_lon, o.threshold_lat, o.threshold_lon)
            assert abs(track_diff_deg(r.course_true_deg, b)) < 0.01, (apt.icao, ident, r.course_true_deg, b)
            checked += 1
    assert checked == 26                          # KSFO 8, KOAK 8, KNUQ 4, KSJC 6 (11/29 from OurAirports)
    # KNUQ 32R landing threshold 37.406039, -122.042847 -> 14L landing threshold 37.428922, -122.054606: 337.802;
    # the PG runway bearing, 321 magnetic + 16 E, gave 337.0, which puts the centreline 850 ft off at 10 nm
    assert load_airport_file(tmp_path / "knuq.toml").runways["32R"].course_true_deg == pytest.approx(337.802, abs=0.002)


def test_course_falls_back_to_the_pg_bearing_without_the_opposite_end():
    data = parse_cifp(FIX / "cifp" / "FAACIFP18-subset.txt", airports={"KSFO"})
    del data.runways[("KSFO", "10L")]
    d = build_airport("KSFO", data, None, None, cifp_tag="CIFP_260903")
    assert "10L" not in d["runways"] and d["runways"]["28R"]["course_true_deg"] == 298.0   # 284 magnetic + 14 E


def test_course_comes_from_the_runway_number_when_the_pg_bearing_is_blank():
    # private strips such as 13FD carry no PG bearing; without the opposite end the course was the declination alone
    data = parse_cifp(FIX / "cifp" / "FAACIFP18-subset.txt", airports={"KSFO"})
    del data.runways[("KSFO", "10L")]
    data.runways[("KSFO", "28R")].mag_bearing_deg = None
    d = build_airport("KSFO", data, None, None, cifp_tag="CIFP_260903")
    assert d["runways"]["28R"]["course_true_deg"] == 294.0          # runway 28: 280 magnetic + 14 E


def test_ksfo_idents_are_the_cifp_ends_once(cifp):
    oa_r = parse_runways(FIX / "ourairports" / "runways.csv")
    oa_a = parse_airports(FIX / "ourairports" / "airports.csv")
    d = build_airport("KSFO", cifp, oa_r, oa_a, cifp_tag="CIFP_260903")
    cifp_idents = sorted(ident for icao, ident in cifp.runways if icao == "KSFO")
    assert sorted(d["runways"]) == cifp_idents == ["01L", "01R", "10L", "10R", "19L", "19R", "28L", "28R"]
    assert d["airport"]["parallel_groups"] == [["01L", "01R"], ["10L", "10R"], ["19L", "19R"], ["28L", "28R"]]
    assert all(r["reference_source"].startswith("cifp:") for r in d["runways"].values())   # OurAirports' 1L/1R are 01L/01R


def test_closed_ourairports_runway_is_emitted_disabled(cifp):
    # KSJC 11/29 is closed in OurAirports and absent from CIFP: keep it visible but never match approaches to it
    d = build_airport("KSJC", cifp, parse_runways(FIX / "ourairports" / "runways.csv"),
                      parse_airports(FIX / "ourairports" / "airports.csv"), cifp_tag="CIFP_260903")
    enabled = {ident: r["enabled"] for ident, r in d["runways"].items()}
    assert enabled == {"11": False, "12L": True, "12R": True, "29": False, "30L": True, "30R": True}


def test_ourairports_fallback_for_a_field_filed_under_another_ident(tmp_path):
    # CIFP spells a small field 00C; OurAirports files it and its runways under K00C, with local_code 00C
    from adsb_approach.refdata.ourairports import load_ourairports
    (tmp_path / "airports.csv").write_text(
        "id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,continent,iso_country,iso_region,municipality,"
        "scheduled_service,icao_code,iata_code,gps_code,local_code,home_link,wikipedia_link,keywords\n"
        "9001,K00C,small_airport,Test Field,37.2000,-107.8700,6684,NA,US,US-CO,Town,no,,,K00C,00C,,,\n")
    (tmp_path / "runways.csv").write_text(
        "id,airport_ref,airport_ident,length_ft,width_ft,surface,lighted,closed,le_ident,le_latitude_deg,"
        "le_longitude_deg,le_elevation_ft,le_heading_degT,le_displaced_threshold_ft,he_ident,he_latitude_deg,"
        "he_longitude_deg,he_elevation_ft,he_heading_degT,he_displaced_threshold_ft\n"
        "9101,9001,K00C,4960,50,ASP,0,0,1,37.1930,-107.8700,,0.0,,19,37.2066,-107.8700,6690,180.0,\n")
    oa_runways, oa_airports = load_ourairports(tmp_path, {"00C"})
    empty = parse_cifp(FIX / "cifp" / "FAACIFP18-subset.txt", airports={"00C"})
    d = build_airport("00C", empty, oa_runways, oa_airports, cifp_tag="CIFP_260903")
    assert d["airport"]["name"] == "Test Field" and sorted(d["runways"]) == ["01", "19"]
    assert d["runways"]["01"]["threshold_elev_ft"] == 6684          # blank in runways.csv: the airport's elevation
    assert d["runways"]["19"]["threshold_elev_ft"] == 6690
    assert d["runways"]["01"]["reference_source"] == "ourairports+assumed"


def _write_ourairports(dir_path: Path, airports: list[str], runways: list[str]) -> None:
    (dir_path / "airports.csv").write_text(
        "id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,continent,iso_country,iso_region,municipality,"
        "scheduled_service,icao_code,iata_code,gps_code,local_code,home_link,wikipedia_link,keywords\n"
        + "".join(row + "\n" for row in airports))
    (dir_path / "runways.csv").write_text(
        "id,airport_ref,airport_ident,length_ft,width_ft,surface,lighted,closed,le_ident,le_latitude_deg,"
        "le_longitude_deg,le_elevation_ft,le_heading_degT,le_displaced_threshold_ft,he_ident,he_latitude_deg,"
        "he_longitude_deg,he_elevation_ft,he_heading_degT,he_displaced_threshold_ft\n"
        + "".join(row + "\n" for row in runways))


def test_ourairports_runways_far_from_the_cifp_airport_belong_to_another_field(tmp_path):
    # OurAirports reuses codes across countries: two fields answer local_code KSFO here, a Hong Kong strip filed first. On
    # the full download the file-first match gave CIFP OR9 (Lord Flat, Oregon) the runways of Balad, Iraq, 5,917 nm away.
    from adsb_approach.refdata.ourairports import load_ourairports
    _write_ourairports(tmp_path,
                       ["9001,HK-0106,small_airport,Far Strip,22.2000,114.2000,10,AS,HK,HK-U-A,Town,no,,,,KSFO,,,",
                        "9002,US-0002,small_airport,Near Field,37.6200,-122.3700,13,NA,US,US-CA,Town,no,,,,KSFO,,,"],
                       ["9101,9001,HK-0106,3000,60,ASP,0,0,13,22.2000,114.2000,10,130.0,,31,22.1900,114.2100,10,310.0,"])
    oa_runways, oa_airports = load_ourairports(tmp_path, {"KSFO"})
    cifp = parse_cifp(FIX / "cifp" / "FAACIFP18-subset.txt", airports={"KSFO"})
    cifp_only = build_airport("KSFO", cifp, None, None, cifp_tag="CIFP_260903")
    assert build_airport("KSFO", cifp, oa_runways, oa_airports, cifp_tag="CIFP_260903")["runways"] == cifp_only["runways"]


def test_an_ourairports_field_answering_two_requested_idents_lends_its_runways_to_each(tmp_path):
    # OurAirports keeps a retired FAA ident as the ident of a field now coded otherwise (its row 1TS9 answers CIFP 1TS9 and
    # 9S1), so two requested idents can match one row. Only the ident matched last received the row's runways: here KSFO
    # lost its own 13/31 to KSJC, 26 nm away.
    from adsb_approach.refdata.ourairports import load_ourairports
    _write_ourairports(tmp_path,
                       ["9001,KSFO,large_airport,San Francisco,37.6188,-122.3754,13,NA,US,US-CA,Town,yes,KSFO,SFO,KSFO,KSJC,,,"],
                       ["9101,9001,KSFO,3000,60,ASP,0,0,13,37.6250,-122.3900,13,130.0,,31,37.6150,-122.3800,13,310.0,"])
    oa_runways, oa_airports = load_ourairports(tmp_path, {"KSFO", "KSJC"})
    cifp = parse_cifp(FIX / "cifp" / "FAACIFP18-subset.txt", airports={"KSFO", "KSJC"})
    sfo = build_airport("KSFO", cifp, oa_runways, oa_airports, cifp_tag="CIFP_260903")
    sjc = build_airport("KSJC", cifp, oa_runways, oa_airports, cifp_tag="CIFP_260903")
    assert {"13", "31"} <= set(sfo["runways"]) and not {"13", "31"} & set(sjc["runways"])


def test_build_config_from_a_zip_leaves_no_temporary_files(tmp_path, monkeypatch):
    import filecmp
    import tempfile
    import zipfile

    from adsb_approach.cli import main

    expected = build_all(["KSFO", "KOAK", "KNUQ", "KSJC"], FIX / "cifp" / "FAACIFP18-subset.txt", tmp_path / "expected",
                         ourairports_dir=FIX / "ourairports", cifp_tag="CIFP_260903")
    cifp_zip = tmp_path / "CIFP_260903.zip"
    with zipfile.ZipFile(cifp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(FIX / "cifp" / "FAACIFP18-subset.txt", "FAACIFP18")
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    assert main(["build-config", "--cifp", str(cifp_zip), "--airports", "KSFO,KOAK,KNUQ,KSJC",
                 "--out", str(tmp_path / "out"), "--ourairports", str(FIX / "ourairports")]) == 0
    assert list(scratch.iterdir()) == []                          # the extracted FAACIFP18 is gone
    names = sorted(p.name for p in expected)                      # and the zip was read, tagged with its stem
    assert filecmp.cmpfiles(tmp_path / "out", tmp_path / "expected", names, shallow=False)[0] == names


# --- procedure paths ----------------------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def _paths_toml(ident: str = "28R") -> str:
    """An overrides file adding QUIET_BRIDGE to a runway, spelt the way config/overrides/ksfo.toml spells it."""
    pts = ", ".join(f"[{la!r}, {lo!r}]" for la, lo in QUIET_BRIDGE.points)
    return (f'[[runways."{ident}".paths]]\nname = "{QUIET_BRIDGE.name}"\nlabel = "{QUIET_BRIDGE.label}"\n'
            f'source = "{QUIET_BRIDGE.source}"\npoints = [{pts}]\n')


def test_to_toml_writes_paths_as_arrays_of_tables():
    paths = [{"name": "quiet-bridge", "label": 'Quiet "Bridge"', "source": "test",
              "points": [[37.488179, -121.966495], [37.582913, -122.259838], [37.60534, -122.337623]]},
             {"name": "second", "label": "Second", "source": "test", "points": [[37.6, -122.3], [37.605, -122.337]]}]
    d = {"airport": {"icao": "KSFO", "lat": 37.5},
         "runways": {"28L": {"tch_ft": 53, "glide_path_deg": 2.85},
                     "28R": {"tch_ft": 55, "ils": None, "paths": paths, "glide_path_deg": 3.0},
                     "10L": {"tch_ft": 50}}}
    text = to_toml(d)
    expected = {"airport": {"icao": "KSFO", "lat": 37.5},
                "runways": {"28L": {"tch_ft": 53, "glide_path_deg": 2.85},
                            "28R": {"tch_ft": 55, "paths": paths, "glide_path_deg": 3.0}, "10L": {"tch_ft": 50}}}
    assert tomllib.loads(text) == expected
    lines = text.splitlines()
    assert lines.count('[[runways."28R".paths]]') == 2 and sum(ln.startswith("[runways.") for ln in lines) == 3
    table = lines.index('[runways."28R"]')
    assert lines.index('[[runways."28R".paths]]') > lines.index('glide_path_deg = 3.0', table)     # after the scalars
    assert lines.index('[runways."10L"]') > max(i for i, ln in enumerate(lines) if ln.startswith("points = "))
    assert "points = [[37.488179, -121.966495], [37.582913, -122.259838], [37.60534, -122.337623]]" in lines


def test_overrides_add_paths_to_the_generated_file(tmp_path):
    overrides = tmp_path / "overrides"
    overrides.mkdir()
    (overrides / "ksfo.toml").write_text(_paths_toml())
    paths = build_all(["KSFO"], FIX / "cifp" / "FAACIFP18-subset.txt", tmp_path / "out", overrides_dir=overrides,
                      cifp_tag="CIFP_260903")
    apt = load_airport_file(paths[0])
    assert apt.runways["28R"].paths == (QUIET_BRIDGE,)
    assert all(not r.paths for ident, r in apt.runways.items() if ident != "28R")
    assert apt.runways["28R"].tch_ft == 55 and apt.runways["28R"].parallel_group == ("28L", "28R")      # the rest is unchanged


def test_a_bad_path_in_an_overrides_file_is_rejected(tmp_path):
    overrides = tmp_path / "overrides"
    overrides.mkdir()
    (overrides / "ksfo.toml").write_text(_paths_toml().replace("quiet-bridge", "Quiet Bridge"))
    with pytest.raises(ValueError, match=r"ksfo\.toml: .*name must match"):
        build_all(["KSFO"], FIX / "cifp" / "FAACIFP18-subset.txt", tmp_path / "out", overrides_dir=overrides,
                  cifp_tag="CIFP_260903")


def _paths_by_runway(path: Path) -> dict[str, list]:
    runways = tomllib.loads(path.read_text()).get("runways", {})
    return {ident: rd["paths"] for ident, rd in runways.items() if rd.get("paths")}


def path_mismatches(airports_dir: Path, overrides_dir: Path) -> list[str]:
    """Where the generated airport files and the overrides files disagree about procedure paths, in both directions: a
    runway with paths in one and none in the other, or with different ones. An overrides file may hold no paths at all
    (hand corrections only), and an airport file without paths needs no overrides file."""
    names = sorted({p.name for p in airports_dir.glob("*.toml")} | {p.name for p in overrides_dir.glob("*.toml")})
    out: list[str] = []
    for name in names:
        generated = _paths_by_runway(airports_dir / name) if (airports_dir / name).exists() else {}
        wanted = _paths_by_runway(overrides_dir / name) if (overrides_dir / name).exists() else {}
        out += [f"{name} runway {ident}: airport file has {generated.get(ident)}, overrides file has {wanted.get(ident)}"
                for ident in sorted(generated.keys() | wanted.keys()) if generated.get(ident) != wanted.get(ident)]
    return out


def test_committed_airport_files_carry_their_overrides_paths():
    assert path_mismatches(ROOT / "config" / "airports", ROOT / "config" / "overrides") == []
    assert _paths_by_runway(ROOT / "config" / "airports" / "ksfo.toml").keys() == {"28R"}     # the Quiet Bridge entry


def test_path_mismatches_are_found_in_both_directions(tmp_path):
    airports, overrides = tmp_path / "airports", tmp_path / "overrides"
    airports.mkdir()
    overrides.mkdir()
    (airports / "ksfo.toml").write_text(_paths_toml())
    (overrides / "ksfo.toml").write_text(_paths_toml())
    assert path_mismatches(airports, overrides) == []
    (airports / "koak.toml").write_text(_paths_toml("12"))                    # paths in the airport file, no overrides file
    assert len(path_mismatches(airports, overrides)) == 1
    (overrides / "koak.toml").write_text('[runways."12"]\ntch_ft = 60\n')       # an overrides file with corrections only
    assert len(path_mismatches(airports, overrides)) == 1
    (overrides / "koak.toml").write_text(_paths_toml("12"))
    assert path_mismatches(airports, overrides) == []
    (overrides / "ksjc.toml").write_text(_paths_toml("30L"))                  # paths in the overrides file, none generated
    assert len(path_mismatches(airports, overrides)) == 1
    (overrides / "ksjc.toml").write_text('[runways."30L"]\ntch_ft = 60\n')       # allowed: no paths to carry
    assert path_mismatches(airports, overrides) == []
    (airports / "ksfo.toml").write_text(_paths_toml().replace("Quiet Bridge", "Another"))          # a field differs
    assert len(path_mismatches(airports, overrides)) == 1

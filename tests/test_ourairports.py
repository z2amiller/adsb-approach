from pathlib import Path

import pytest

from adsb_approach.geometry import NM_FT, destination
from adsb_approach.refdata.ourairports import parse_airports, parse_nearby, parse_runways
from adsb_approach.tracks import read_places

FIX = Path(__file__).parent / "fixtures" / "ourairports"


def test_runway_ends():
    ends = parse_runways(FIX / "runways.csv")
    r = ends[("KSFO", "28R")]
    assert (r.lat, r.lon) == (37.613538, -122.35716)
    assert (r.elev_ft, r.heading_true_deg, r.displaced_ft, r.length_ft, r.closed) == (13, 298.0, 300, 11870, False)
    assert len([k for k in ends if k[0] == "KSFO"]) == 8
    assert ends[("KSFO", "10L")].displaced_ft == 0
    assert ends[("KSJC", "11")].closed and ends[("KSJC", "29")].closed and not ends[("KSJC", "12L")].closed


def test_airports_and_filter():
    apts = parse_airports(FIX / "airports.csv", airports={"KSFO", "KNUQ"})
    assert set(apts) == {"KSFO", "KNUQ"}
    assert "San Francisco" in apts["KSFO"].name
    assert apts["KSFO"].elev_ft == 13
    assert parse_runways(FIX / "runways.csv", airports={"KNUQ"}).keys() >= {("KNUQ", "32L"), ("KNUQ", "14R")}


def test_a_requested_ident_matches_ident_then_gps_code_then_local_code(tmp_path):
    header = ("id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,continent,iso_country,iso_region,municipality,"
              "scheduled_service,icao_code,iata_code,gps_code,local_code,home_link,wikipedia_link,keywords\n")
    rows = ["1,K00C,small_airport,By local code,37.0,-107.0,6684,NA,US,US-CO,Town,no,,,K00C,00C,,,",
            "2,X00C,small_airport,By gps code,38.0,-108.0,100,NA,US,US-CO,Town,no,,,00C,,,,",
            "3,KSFO,large_airport,By ident,37.6,-122.4,13,NA,US,US-CA,Town,no,KSFO,SFO,KSFO,SFO,,,",
            "4,US-0001,small_airport,Shadow,37.5,-122.5,5,NA,US,US-CA,Town,no,,,,KSFO,,,"]
    (tmp_path / "airports.csv").write_text(header + "\n".join(rows) + "\n")
    apts = parse_airports(tmp_path / "airports.csv", airports={"00C", "KSFO"})
    assert (apts["00C"].name, apts["00C"].oa_ident) == ("By gps code", "X00C")
    assert (apts["KSFO"].name, apts["KSFO"].oa_ident) == ("By ident", "KSFO")


def test_parse_nearby_keeps_airport_types_within_the_radius(tmp_path):
    header = ("id,ident,type,name,latitude_deg,longitude_deg,elevation_ft,continent,iso_country,iso_region,municipality,"
              "scheduled_service,icao_code,iata_code,gps_code,local_code,home_link,wikipedia_link,keywords\n")
    centre = (37.6, -122.4)

    def row(ident: str, kind: str, nm: float) -> str:
        lat, lon = destination(*centre, 90.0, nm * NM_FT)
        return f"1,{ident},{kind},Field {ident},{lat:.5f},{lon:.5f},10,NA,US,US-CA,Town,no,,,{ident},,,,"

    rows = [row("KNEAR", "small_airport", 10), row("KHELI", "heliport", 5), row("KSHUT", "closed", 5),
            row("KFAR", "medium_airport", 200), row("KBIG", "large_airport", 20)]
    (tmp_path / "airports.csv").write_text(header + "\n".join(rows) + "\n")
    found = parse_nearby(tmp_path / "airports.csv", [centre])
    assert [r["ident"] for r in found] == ["KBIG", "KNEAR"]                    # sorted by ident; heliport, closed, far gone
    near = found[1]
    assert (near["name"], near["type"]) == ("Field KNEAR", "small_airport")
    assert set(near) == {"ident", "name", "type", "lat", "lon"} and isinstance(near["lat"], float)
    assert [r["ident"] for r in parse_nearby(tmp_path / "airports.csv", [centre], radius_nm=250)] == ["KBIG", "KFAR", "KNEAR"]
    far_centre = destination(*centre, 90.0, 200 * NM_FT)                       # a second centre at KFAR brings KFAR in
    assert [r["ident"] for r in parse_nearby(tmp_path / "airports.csv", [centre, far_centre])] == ["KBIG", "KFAR", "KNEAR"]


def test_build_nearby_writes_the_csv_that_read_places_reads(tmp_path, capsys):
    from adsb_approach.cli import main
    out = tmp_path / "out" / "nearby.csv"
    assert main(["build-nearby", "--ourairports", str(FIX), "--airports-dir", str(FIX.parents[2] / "config" / "airports"),
                 "--out", str(out)]) == 0
    assert capsys.readouterr().out == f"wrote 4 airports to {out}\n"
    assert out.read_text().splitlines()[0] == "ident,name,type,lat,lon"
    places = read_places(out)
    assert [p.ident for p in places] == ["KNUQ", "KOAK", "KSFO", "KSJC"]
    assert "San Francisco" in places[2].name and (places[2].lat, places[2].lon) == (37.619806, -122.374821)
    with pytest.raises(ValueError, match="no airport files"):
        main(["build-nearby", "--ourairports", str(FIX), "--airports-dir", str(tmp_path), "--out", str(out)])

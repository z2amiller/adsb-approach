from pathlib import Path

import pytest

from adsb_approach.refdata.cifp import parse_cifp, parse_coord, procedure_runway

SUBSET = Path(__file__).parent / "fixtures" / "cifp" / "FAACIFP18-subset.txt"


@pytest.fixture(scope="module")
def cifp():
    return parse_cifp(SUBSET)


def test_parse_coord_both_precisions():
    assert parse_coord("N37365011") == pytest.approx(37.613919, abs=1e-6)
    assert parse_coord("W122212901") == pytest.approx(-122.358058, abs=1e-6)
    assert parse_coord("N3736501050") == pytest.approx(37.6139181, abs=1e-6)
    assert parse_coord("W12221290075") == pytest.approx(-122.3580576, abs=1e-6)


def test_procedure_runway():
    assert procedure_runway("I28R") == "28R"
    assert procedure_runway("R28RZ") == "28R"
    assert procedure_runway("H10RZ") == "10R"
    assert procedure_runway("R10L") == "10L"
    assert procedure_runway("VDMA") is None
    assert procedure_runway("R30-Y") == "30" and procedure_runway("RNV-A") is None


def test_airport_records(cifp):
    a = cifp.airports["KSFO"]
    assert a.name == "SAN FRANCISCO INTL"
    assert a.declination_deg == 14.0
    assert a.elev_ft == 13
    assert a.lat == pytest.approx(37.618806, abs=1e-5)
    assert a.lon == pytest.approx(-122.375417, abs=1e-5)
    assert cifp.airports["KSJC"].declination_deg == 13.0


def test_runway_28r(cifp):
    r = cifp.runways[("KSFO", "28R")]
    assert r.lat == pytest.approx(37.613919, abs=1e-6)
    assert r.lon == pytest.approx(-122.358058, abs=1e-6)
    assert (r.length_ft, r.mag_bearing_deg, r.hae_m, r.elev_ft) == (11870, 284.0, -28.7, 13)
    assert (r.displaced_ft, r.tch_ft, r.width_ft, r.tch_source) == (300, 55, 200, "I")
    assert (r.loc_ident, r.ils_cat) == ("IGWQ", "3")
    assert len([k for k in cifp.runways if k[0] == "KSFO"]) == 8
    assert cifp.runways[("KSFO", "01L")].loc_ident is None
    assert cifp.runways[("KNUQ", "32L")].tch_source == "R"


def test_localizers(cifp):
    loc = cifp.localizers[("KSFO", "28R")]
    assert (loc.ident, loc.cat, loc.freq_mhz) == ("IGWQ", "3", 111.70)
    assert loc.lat == pytest.approx(37.629544, abs=1e-5)
    assert loc.mag_bearing_deg == 283.8
    assert (loc.width_deg, loc.gs_angle_deg, loc.tch_ft, loc.declination_deg) == (3.29, 3.00, 55, 14.0)
    assert loc.gs_from_threshold_ft == 1065
    assert loc.gs_lat == pytest.approx(37.614278, abs=1e-5)
    assert cifp.localizers[("KSFO", "28L")].gs_angle_deg == 2.85
    assert cifp.localizers[("KSFO", "28L")].tch_ft == 53
    knuq14l = cifp.localizers[("KNUQ", "14L")]
    assert knuq14l.gs_angle_deg is None and knuq14l.gs_lat is None and knuq14l.width_deg == 3.97
    assert cifp.localizers[("KOAK", "12")].gs_angle_deg == 2.75


# CIFP_260903 KSNA 20R: the runway, its ILS ISNA and the offset LDA IOJW on the same runway.
KSNA_20R = [
    "SUSAP KSNAK2GRW20R   0057001960 N33405394W117515423         -0022400041000051150IISNA1    IOJWA           "
    "                 213452203",
    "SUSAP KSNAK2IISNA1   011175RW20RN33395449W1175231891959N33404758W1175202281100 08870590300E01205100042    "
    "                 213472211",
    "SUSAP KSNAK2IIOJWA   010830RW20RN33405647W1175147651829                   6187-    0600   E0120           "
    "                 213462211",
]


def test_duplicate_localizer_keeps_the_runway_aligned_one(tmp_path):
    rwy, ils, lda = KSNA_20R
    for order in ([rwy, ils, lda], [rwy, lda, ils]):
        path = tmp_path / "ksna-20r.txt"
        path.write_text("\n".join(order) + "\n")
        assert parse_cifp(path).localizers[("KSNA", "20R")].ident == "ISNA"


def test_path_points(cifp):
    pps = {p.procedure: p for p in cifp.path_points[("KSFO", "28R")]}
    p = pps["R28RZ"]
    assert (p.gpa_deg, p.ltp_hae_m, p.course_width_m, p.length_offset_m) == (3.0, -28.7, 106.75, 0)
    assert p.tch_ft == pytest.approx(54.8)
    assert p.ltp_lat == pytest.approx(37.613918, abs=1e-5)
    assert cifp.path_points[("KNUQ", "32L")][0].gpa_deg == 3.5
    assert cifp.path_points[("KSFO", "28L")][0].gpa_deg == 2.85


def test_approach_vertical_angles(cifp):
    angles = {a.procedure: a.vertical_angle_deg for a in cifp.approach_angles[("KSFO", "28L")]}
    assert angles["I28L"] == 2.85 and angles["R28L"] == 2.85
    angles_r = {a.procedure: a.vertical_angle_deg for a in cifp.approach_angles[("KSFO", "28R")]}
    assert angles_r["I28R"] == 3.0 and angles_r["R28RZ"] == 3.0


# CIFP_260903: 0S9 RNV-A is a circling procedure whose missed-approach point is RW09 (-420); KSQL R30-Y, RNAV (GPS) Y
# RWY 30, carries its -305 on a leg to a named fix.
PF_RUNWAY_ATTRIBUTION = [
    "SUSAP 0S9 K1FRNV-A R      030RW09 K1PG0GY M 031TF                                   00152             -420"
    "          B PC   819002306",
    "SUSAP KSQLK2FR30-Y R      030KIZEMK2PC0EY M 031TF                                   00098             -305"
    "          B PS   227932407",
]


def test_vertical_angles_come_from_runway_procedures(tmp_path):
    path = tmp_path / "pf.txt"
    path.write_text("\n".join(PF_RUNWAY_ATTRIBUTION) + "\n")
    d = parse_cifp(path)
    assert ("0S9", "09") not in d.approach_angles
    assert [(a.procedure, a.vertical_angle_deg) for a in d.approach_angles[("KSQL", "30")]] == [("R30-Y", 3.05)]


def test_airport_filter():
    d = parse_cifp(SUBSET, airports={"KNUQ"})
    assert set(k[0] for k in d.runways) == {"KNUQ"} and len(d.runways) == 4
    assert set(d.airports) == {"KNUQ"}


# CIFP_260903 KGUC H06 (RNAV (RNP) RWY 6) final legs. The W continuation record's fourth RNP value, A112,
# reaches columns 103-106, which hold the vertical angle on a primary record.
KGUC_H06_FINAL = [
    "SUSAP KGUCK2FH06   H      020OSDUEK2PC1E  F 051TF                                 + 10300                 "
    "RW06  K2PGA FS   065732411",
    "SUSAP KGUCK2FH06   H      020OSDUEK2PC2W                                                A031A262A021A112  "
    "            FS   065741310",
    "SUSAP KGUCK2FH06   H      030RW06 K2PG0G  M 031TF                                   07707             -320"
    "          A FS   065752411",
]


def test_continuation_records_are_skipped(tmp_path):
    assert all(len(line) == 132 for line in KGUC_H06_FINAL)
    path = tmp_path / "kguc-h06.txt"
    path.write_text("\n".join(KGUC_H06_FINAL) + "\n")
    angles = parse_cifp(path).approach_angles[("KGUC", "06")]
    assert [(a.procedure, a.vertical_angle_deg) for a in angles] == [("H06", 3.2)]


# CIFP_260903 KGPI 30 runway legs: VOR/DME RWY 30 codes " 000" (no vertical angle), RNAV (GPS) RWY 30 codes -321.
KGPI_30_RUNWAY_LEGS = [
    "SUSAP KGPIK1FD30   D      030RW30 K1PG0GY M    CF FCA K1      3155006531550065D     03001              000"
    "          0  S   038352310",
    "SUSAP KGPIK1FR30   R      030RW30 K1PG0GY M 031TF                                   03015             -321"
    "          B PS   039452310",
]


def test_zero_vertical_angle_is_not_an_angle(tmp_path):
    path = tmp_path / "kgpi-30.txt"
    path.write_text("\n".join(KGPI_30_RUNWAY_LEGS) + "\n")
    angles = parse_cifp(path).approach_angles[("KGPI", "30")]
    assert [(a.procedure, a.vertical_angle_deg) for a in angles] == [("R30", 3.21)]

from pathlib import Path

from adsb_approach.config import TypeClasses
from adsb_approach.fleet import keys_for
from adsb_approach.typedb import TypeDb, classify

ROOT = Path(__file__).resolve().parents[1]


def classes() -> TypeClasses:
    return TypeClasses.load(ROOT / "config" / "type_classes.toml")


def test_load_and_lookup():
    db = TypeDb.load(ROOT / "tests" / "fixtures" / "typedb-sample.csv")
    assert len(db) == 2                     # the fixture's comment lines are skipped
    info = db.get("a44540")
    assert (info.reg, info.type_code, info.desc) == ("N37415", "B39M", "BOEING 737 MAX 9")
    assert db.get("A44540") is info
    assert db.get("zzzzzz") is None
    assert (db.get("000001").type_code, db.get("000001").flags) == ("C56X", None)


def test_classify_rules():
    c = classes()
    assert classify("UAL2177", "B39M", "A3", c) == "airline"
    assert classify("UAL39", None, "A5", c) == "airline"        # airline callsign + heavy category, unknown type
    assert classify("SWA123", "B738", None, c) == "airline"
    assert classify("EJA123", "C56X", "A2", c) == "bizjet"      # bizjet type wins over airline-shaped callsign
    assert classify("N00249", "C172", "A1", c) == "other"
    assert classify("N00373", None, "A1", c) == "other"
    assert classify(None, "B738", None, c) == "other"           # no callsign, no class claim
    assert classify(" UAL2177 ", "B39M", "A3", c) == "airline"
    assert classify("QFX800", "GL7T", "A3", c) == "bizjet"      # Global 7500 in the 2026-09-12 capture: A3 and an airline-shaped callsign
    assert classify("JOLLY93", "H60", "A7", c) == "rotorcraft"
    assert classify("EJA123", "C56X", "A7", c) == "rotorcraft"  # rotorcraft category wins even over a bizjet type
    assert classify("UAL2177", "B39M", "A7", c) == "rotorcraft"  # ...and over an airline-shaped callsign and airliner type


def test_a_ga_type_code_still_classifies_as_other_and_keeps_its_fleet_key():
    """The GA list is a page filter only. It must never reach classify() or keys_for(), so a type code the list names is
    still traffic_class "other" (classify() does not consult TypeClasses.ga at all) and still forms the same fleet key
    chain as before the list existed -- the proof that scoring did not move."""
    c = classes()
    assert "C172" in c.ga                                            # the shipped list covers this type
    assert classify("N00249", "C172", "A1", c) == "other"
    assert keys_for("C172", "other") == [("type", "C172"), ("class", "other"), ("*", "*")]

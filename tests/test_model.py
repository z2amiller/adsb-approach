from adsb_approach.model import EMERGENCY_MASK, LATCH_MASK, Flags, Outcome, NORMAL_LANDING_MASK, Position, flag_names, is_normal_landing


def test_flag_names_lists_only_set_bits():
    f = Flags.HIGH | Flags.UNSTABLE_1000
    assert flag_names(f) == ["HIGH", "UNSTABLE_1000"]
    assert flag_names(Flags.NONE) == []
    assert flag_names(int(Flags.HIGH | Flags.LOW)) == ["HIGH", "LOW"]      # raw INTEGER from SQLite


def test_flags_round_trip_through_int():
    f = Flags.SIDESTEP | Flags.PRESUMED_LANDED
    assert Flags(int(f)) == f
    assert int(Flags.TEMP_UNCORRECTED) == 1 << 20


def test_outcome_values_are_db_strings():
    assert Outcome.LOST_ON_FINAL.value == "lost_on_final"
    assert Outcome("go_around") is Outcome.GO_AROUND
    assert f"{Outcome.LANDED}" == "landed" and str(Outcome.GO_AROUND) == "go_around"


def test_normal_landing_definition():
    assert is_normal_landing(Outcome.LANDED, Flags.NONE)
    assert is_normal_landing(Outcome.LOST_ON_FINAL, Flags.PRESUMED_LANDED | Flags.GATE500_UNOBSERVED)
    assert is_normal_landing(Outcome.LOST_ON_FINAL, Flags.PRESUMED_LANDED | Flags.QNH_ASSUMED | Flags.TEMP_UNCORRECTED)
    assert not is_normal_landing(Outcome.LANDED, Flags.HIGH)
    assert not is_normal_landing(Outcome.GO_AROUND, Flags.NONE)
    assert NORMAL_LANDING_MASK & Flags.UNSTABLE_1000 == Flags.NONE


def test_vertical_rate_prefers_baro_then_geom():
    def pos(baro, geom):
        return Position(t=0.0, hex="a44540", lat=37.6, lon=-122.4, alt_baro=1000, on_ground=False, alt_geom=None,
                        gs=150.0, track=298.0, baro_rate=baro, geom_rate=geom, callsign=None, squawk=None,
                        category=None, nav_qnh=None, nav_altitude_mcp=None, nav_modes=frozenset(), source="adsb_icao")
    assert pos(-800, -700).vertical_rate == -800
    assert pos(None, -700).vertical_rate == -700
    assert pos(None, None).vertical_rate is None


def test_the_external_tail_flag_marks_where_points_came_from_not_how_it_was_flown():
    assert int(Flags.EXTERNAL_TAIL) == 1 << 23 and flag_names(Flags.EXTERNAL_TAIL) == ["EXTERNAL_TAIL"]
    assert is_normal_landing(Outcome.LANDED, Flags.LANDING_CONFIRMED | Flags.EXTERNAL_TAIL)


def test_the_external_outcome_flag_marks_a_tail_decided_verdict():
    assert int(Flags.EXTERNAL_OUTCOME) == 1 << 24 and flag_names(Flags.EXTERNAL_OUTCOME) == ["EXTERNAL_OUTCOME"]
    assert is_normal_landing(Outcome.LANDED, Flags.LANDING_CONFIRMED | Flags.EXTERNAL_TAIL | Flags.EXTERNAL_OUTCOME)


def test_the_external_gaps_flag_marks_filled_gaps_apart_from_the_tail():
    assert int(Flags.EXTERNAL_GAPS) == 1 << 25 and flag_names(Flags.EXTERNAL_GAPS) == ["EXTERNAL_GAPS"]
    assert Flags.EXTERNAL_GAPS & (Flags.EXTERNAL_TAIL | Flags.EXTERNAL_OUTCOME) == Flags.NONE
    assert NORMAL_LANDING_MASK & Flags.EXTERNAL_GAPS
    assert is_normal_landing(Outcome.LOST_ON_FINAL, Flags.PRESUMED_LANDED | Flags.EXTERNAL_GAPS)
    assert is_normal_landing(Outcome.LANDED, Flags.LANDING_CONFIRMED | Flags.EXTERNAL_TAIL | Flags.EXTERNAL_OUTCOME
                             | Flags.EXTERNAL_GAPS)


def test_the_emergency_flags_and_masks_match_the_contract():
    """Bits 26-30, and the two masks committed to model.py before either half started: the page is built against these
    names, so they must never move."""
    assert (int(Flags.EMERGENCY_GENERAL), int(Flags.EMERGENCY_NORDO), int(Flags.EMERGENCY_UNLAWFUL),
            int(Flags.EMERGENCY_DOWNED), int(Flags.MINIMUM_FUEL)) == (1 << 26, 1 << 27, 1 << 28, 1 << 29, 1 << 30)
    assert EMERGENCY_MASK == (Flags.EMERGENCY_GENERAL | Flags.EMERGENCY_NORDO | Flags.EMERGENCY_UNLAWFUL
                              | Flags.EMERGENCY_DOWNED)
    assert LATCH_MASK == EMERGENCY_MASK | Flags.MINIMUM_FUEL
    assert flag_names(EMERGENCY_MASK) == ["EMERGENCY_GENERAL", "EMERGENCY_NORDO", "EMERGENCY_UNLAWFUL", "EMERGENCY_DOWNED"]
    # An emergency landing is not a normal one: none of the five joins NORMAL_LANDING_MASK.
    assert NORMAL_LANDING_MASK & LATCH_MASK == Flags.NONE
    assert not is_normal_landing(Outcome.LANDED, Flags.EMERGENCY_GENERAL)
    assert not is_normal_landing(Outcome.LOST_ON_FINAL, Flags.PRESUMED_LANDED | Flags.MINIMUM_FUEL)

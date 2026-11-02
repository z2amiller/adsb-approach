"""EmergencyWatch: the two independent 30 s runs and the latch."""
from adsb_approach.emergency import LATCH_S, EmergencyWatch
from adsb_approach.model import Flags


def test_29s_does_not_latch_30s_does():
    w = EmergencyWatch()
    assert w.observe("7700", None, 0.0) == Flags.NONE
    assert w.observe("7700", None, 29.0) == Flags.NONE and w.latched == Flags.NONE
    assert w.observe("7700", None, 30.0) == Flags.EMERGENCY_GENERAL
    assert w.latched == Flags.EMERGENCY_GENERAL


def test_a_5s_dial_through_between_ordinary_codes_does_not_latch():
    w = EmergencyWatch()
    w.observe("1200", None, 0.0)                              # an ordinary code never starts a run
    assert w.observe("7700", None, 10.0) == Flags.NONE        # the dial-through starts one
    assert w.observe("1200", None, 15.0) == Flags.NONE        # 5 s later, back to ordinary: the run ends
    assert w.observe("1200", None, 40.0) == Flags.NONE        # long past 30 s from the dial-through: it never latches
    assert w.latched == Flags.NONE


def test_a_report_without_a_squawk_does_not_end_a_squawk_run():
    w = EmergencyWatch()
    w.observe("7700", None, 0.0)
    assert w.observe(None, None, 15.0) == Flags.NONE                    # missing squawk changes nothing
    assert w.observe("7700", None, 30.0) == Flags.EMERGENCY_GENERAL      # the run is still timed from t=0


def test_an_ads_b_none_does_not_end_a_squawk_run():
    """The two sources are independent: a transponder can squawk 7700 while ADS-B still reads none."""
    w = EmergencyWatch()
    w.observe("7700", "none", 0.0)
    assert w.observe("7700", "none", 15.0) == Flags.NONE
    assert w.observe("7700", "none", 30.0) == Flags.EMERGENCY_GENERAL


def test_squawk_and_ads_b_state_can_latch_independently_in_one_watch():
    w = EmergencyWatch()
    w.observe("7600", "general", 0.0)
    assert w.observe("7600", "general", 30.0) == Flags.EMERGENCY_NORDO | Flags.EMERGENCY_GENERAL
    assert w.latched == (Flags.EMERGENCY_NORDO | Flags.EMERGENCY_GENERAL)


def test_each_state_maps_per_the_table():
    for value, flag in (("general", Flags.EMERGENCY_GENERAL), ("nordo", Flags.EMERGENCY_NORDO),
                        ("unlawful", Flags.EMERGENCY_UNLAWFUL), ("downed", Flags.EMERGENCY_DOWNED),
                        ("minfuel", Flags.MINIMUM_FUEL)):
        w = EmergencyWatch()
        w.observe(None, value, 0.0)
        assert w.observe(None, value, LATCH_S) == flag, value


def test_lifeguard_and_reserved_map_to_nothing_and_never_latch():
    for value in ("lifeguard", "reserved"):
        w = EmergencyWatch()
        w.observe(None, value, 0.0)
        assert w.observe(None, value, LATCH_S) == Flags.NONE and w.latched == Flags.NONE, value


def test_lifeguard_ends_an_ongoing_state_run_without_inheriting_its_start():
    """Lifeguard is not merely unmapped: it ends whatever ADS-B run is running, the same as any other value the table
    gives no flag."""
    w = EmergencyWatch()
    w.observe(None, "general", 0.0)
    assert w.observe(None, "lifeguard", 15.0) == Flags.NONE          # ends the "general" run
    assert w.observe(None, "general", 45.0) == Flags.NONE            # a fresh run, timed from 45, not yet 30 s old
    assert w.latched == Flags.NONE


def test_latching_is_once_per_flag_per_flight():
    w = EmergencyWatch()
    w.observe("7700", None, 0.0)
    assert w.observe("7700", None, 30.0) == Flags.EMERGENCY_GENERAL
    assert w.observe("7700", None, 31.0) == Flags.NONE           # continuing the same run: nothing new latches
    w.observe("1200", None, 40.0)                                # the code changes: the run ends
    w.observe("7700", None, 45.0)                                # and a fresh run for the same code starts again
    assert w.observe("7700", None, 75.0) == Flags.NONE           # already latched: no second report of it


def test_switching_directly_between_two_emergency_codes_restarts_the_timer():
    w = EmergencyWatch()
    w.observe("7600", None, 0.0)
    assert w.observe("7700", None, 20.0) == Flags.NONE           # switched at 20 s, short of NORDO's own 30 s
    assert w.observe("7700", None, 30.0) == Flags.NONE           # only 10 s into the new (GENERAL) run
    assert w.observe("7700", None, 50.0) == Flags.EMERGENCY_GENERAL
    assert w.latched == Flags.EMERGENCY_GENERAL                  # NORDO's run was abandoned, never latched


def test_a_report_earlier_than_the_runs_start_neither_latches_nor_breaks_it():
    """positions can arrive slightly out of order; a late-delivered but chronologically earlier report must be inert
    for the run it would otherwise have ended or (implausibly) pre-latched."""
    w = EmergencyWatch()
    w.observe("7700", None, 100.0)
    assert w.observe("1200", None, 90.0) == Flags.NONE          # earlier than the run's start: must not end it
    assert w.observe("7700", None, 130.0) == Flags.EMERGENCY_GENERAL   # the run survived, still timed from t=100
    assert w.latched == Flags.EMERGENCY_GENERAL


def test_latched_at_remembers_each_flags_own_run_start_not_its_latch_time():
    """The matcher's path 4 (stamping a run that straddled a close) tells such a run from one that only starts afterward
    by comparing this against the closed attempt's ended_at, so it needs the run's start, not the moment 30 s later when
    the flag actually latched."""
    w = EmergencyWatch()
    w.observe("7700", None, 100.0)
    assert w.observe("7700", None, 130.0) == Flags.EMERGENCY_GENERAL
    assert w.latched_at == {Flags.EMERGENCY_GENERAL: 100.0}
    # already latched: neither source's own further runs of the same flag touch its recorded start
    w.observe("1200", None, 140.0)
    w.observe("7700", None, 150.0)
    assert w.observe("7700", None, 200.0) == Flags.NONE
    assert w.latched_at == {Flags.EMERGENCY_GENERAL: 100.0}

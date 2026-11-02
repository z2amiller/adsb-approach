import tomllib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adsb_approach.config import (
    RETIRED_EXTERNAL_KEYS, CoverageCfg, CoverageEvent, ExternalCfg, config_hash, load_airport_file, load_config, zone_of,
)
from adsb_approach.geometry import NM_FT, destination
from adsb_approach.model import ProcedurePath
from tests.conftest import QUIET_BRIDGE, make_sfo_28r

KSFO_MIN = '''
[airport]
icao = "KSFO"
name = "SAN FRANCISCO INTL"
enabled = true
lat = 37.618806
lon = -122.375417
elevation_ft = 13
metar = "KSFO"
declination_deg = 14.0
geoid_offset_ft = -107.2
parallel_groups = [["28L", "28R"]]

[runways."28R"]
enabled = true
threshold_lat = 37.613919
threshold_lon = -122.358058
threshold_elev_ft = 13
threshold_hae_m = -28.7
course_true_deg = 298.0
runway_length_ft = 11870
displaced_threshold_ft = 300
localizer_lat = 37.629544
localizer_lon = -122.395311
localizer_offset_ft = 1000
loc_width_deg = 3.29
glide_path_deg = 3.0
tch_ft = 55
ils = "IGWQ 111.70 CAT 3"
reference_source = "cifp:CIFP_260903"

[runways."28L"]
enabled = true
threshold_lat = 37.612097
threshold_lon = -122.359264
threshold_elev_ft = 13
course_true_deg = 298.0
runway_length_ft = 11381
displaced_threshold_ft = 300
glide_path_deg = 2.85
tch_ft = 53
reference_source = "cifp:CIFP_260903"
'''


def write_cfg(tmp_path: Path, main: str) -> Path:
    (tmp_path / "airports").mkdir()
    (tmp_path / "airports" / "ksfo.toml").write_text(KSFO_MIN)
    p = tmp_path / "main.toml"
    p.write_text(main)
    return p


def test_defaults_and_airport_files(tmp_path):
    cfg = load_config(write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n'))
    assert cfg.detection.lost_timeout_s == 45 and cfg.detection.gates_ft == [1000, 500]
    assert cfg.stability.gp_dots_max == 1.0 and cfg.fleet.bin_nm == 0.5
    assert cfg.source.kind == "readsb-json" and cfg.source.port == 30047
    apt = cfg.airports["KSFO"]
    assert apt.declination_deg == 14.0 and apt.geoid_offset_ft == -107.2
    r = apt.runways["28R"]
    assert r.key == "KSFO/28R" and r.glide_path_deg == 3.0 and r.tch_ft == 55 and r.loc_width_deg == 3.29
    assert r.parallel_group == ("28L", "28R")
    r28l = apt.runways["28L"]
    assert r28l.localizer_lat is None and r28l.loc_width_deg is None and r28l.threshold_hae_m is None
    assert r28l.localizer_offset_ft == 1000.0     # default when absent
    assert cfg.service.db_path.endswith("approaches.sqlite")


def test_overrides_from_main_config(tmp_path):
    main = '''
[service]
airports_dir = "airports"
[detection]
lost_timeout_s = 60
[airports.KSFO]
enabled = false
[airports.KSFO.runways."28R"]
glide_path_deg = 3.1
'''
    cfg = load_config(write_cfg(tmp_path, main))
    assert cfg.detection.lost_timeout_s == 60
    assert cfg.airports["KSFO"].enabled is False
    assert cfg.airports["KSFO"].runways["28R"].glide_path_deg == 3.1
    assert cfg.airports["KSFO"].runways["28L"].glide_path_deg == 2.85


def test_relative_paths_resolve_against_config_file(tmp_path):
    cfg = load_config(write_cfg(tmp_path, '[service]\nairports_dir = "airports"\ndb_path = "x.sqlite"\n'))
    assert Path(cfg.service.db_path) == (tmp_path / "x.sqlite").resolve()


def test_example_config_loads():
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config" / "adsb-approach.example.toml")
    assert cfg.type_classes.is_airliner("B738") and cfg.type_classes.is_bizjet("GLF5")
    assert cfg.type_classes.approach_category("B77W") == "D"


def test_search_settings_default_and_resolve():
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config" / "adsb-approach.example.toml")
    assert cfg.service.globe_history_dir == "/var/globe_history"
    assert cfg.service.live_traces_dir == "/run/readsb/traces"
    assert Path(cfg.service.nearby_airports_path) == root / "config" / "nearby_airports.csv"


@pytest.mark.parametrize("override, message", [
    ('[airports.KSFO.runways."28R"]\nglide_path = 3.1\n',
     r"runway KSFO/28R: unknown key 'glide_path' \(did you mean 'glide_path_deg'\?\)"),
    ('[airports.KSFO]\nmetars = "KOAK"\n', r"\[airports\.KSFO\]: unknown key 'metars'"),
    ('[airports.KSFO]\ngeoid_offset_ft = -100.0\n', r"\[airports\.KSFO\]: 'geoid_offset_ft' cannot be overridden"),
    ('[airports.KSFO]\nenabled = "false"\n', r"\[airports\.KSFO\]: enabled must be true or false"),
    ('[airports.KSFO.runways."28R"]\nglide_path_deg = true\n',
     r"runway KSFO/28R: glide_path_deg must be a number, not True"),
    ('[airports.KSFO.runways."28R"]\nglide_path_deg = "steep"\n',
     r"runway KSFO/28R: glide_path_deg must be a number, not 'steep'"),
    ('[airports.KSFO]\nmetar = 5\n', r"\[airports\.KSFO\]: metar must be a string, not 5"),
    ('[airports.KSFO]\nname = 5\n', r"\[airports\.KSFO\]: name must be a string, not 5"),
])
def test_bad_overrides_name_the_file_airport_and_key(tmp_path, override, message):
    with pytest.raises(ValueError, match=r"main\.toml: " + message):
        load_config(write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n' + override))


def test_unknown_key_in_an_airport_file_names_the_file(tmp_path):
    main = write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n')
    (tmp_path / "airports" / "ksfo.toml").write_text(KSFO_MIN.replace("tch_ft = 53", "tch = 53"))
    with pytest.raises(ValueError, match=r"ksfo\.toml: runway KSFO/28L: unknown key 'tch'"):
        load_config(main)


def test_metar_and_name_can_be_overridden(tmp_path):
    cfg = load_config(write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n[airports.KSFO]\nmetar = "KOAK"\nname = "SFO"\n'))
    assert (cfg.airports["KSFO"].metar, cfg.airports["KSFO"].name) == ("KOAK", "SFO")


def test_no_airports_is_an_error(tmp_path):
    (tmp_path / "main.toml").write_text('[service]\nairports_dir = "missing"\n')
    with pytest.raises(ValueError, match=r"missing does not exist"):
        load_config(tmp_path / "main.toml")


def test_optional_paths_stay_unset_and_absolute_paths_are_kept(tmp_path):
    cfg = load_config(write_cfg(tmp_path, f'[service]\nairports_dir = "{tmp_path / "airports"}"\n'))
    assert cfg.service.typedb_path is None and cfg.service.type_classes_path is None
    assert cfg.service.airports_dir == str(tmp_path / "airports")


@pytest.mark.parametrize("main, message", [
    ('[service]\nairports_dir = "airports"\n[detecton]\nlost_timeout_s = 99\n',
     r"main\.toml: unknown key 'detecton' \(did you mean 'detection'\?\)"),
    ('[service]\nairports_dir = "airports"\n[detection]\nlost_timout_s = 99\n',
     r"main\.toml: \[detection\]: unknown key 'lost_timout_s' \(did you mean 'lost_timeout_s'\?\)"),
    ('detection = 5\n[service]\nairports_dir = "airports"\n', r"main\.toml: \[detection\] must be a table"),
])
def test_misspelt_sections_and_keys_name_the_file(tmp_path, main, message):
    with pytest.raises(ValueError, match=message):
        load_config(write_cfg(tmp_path, main))


@pytest.mark.parametrize("key, value, replacement", [
    ("established_min_s", 20, "established_window_s, in seconds; 30 by default"),
    ("runway_change_min_points", 3, "runway_change_min_s, in seconds rather than positions; 10 by default"),
])
def test_a_removed_detection_key_names_its_replacement(tmp_path, key, value, replacement):
    # Both keys were replaced. Their nearest remaining names read another unit, so a did-you-mean hint would suggest
    # keeping the old value under the wrong key.
    main = f'[service]\nairports_dir = "airports"\n[detection]\n{key} = {value}\n'
    with pytest.raises(ValueError) as err:
        load_config(write_cfg(tmp_path, main))
    assert str(err.value).endswith(f"main.toml: [detection]: unknown key {key!r} (removed: set {replacement})")


@pytest.mark.parametrize("section, line, message", [
    ("detection", 'lost_timeout_s = "45"', r"\[detection\]: lost_timeout_s must be a number, not '45'"),
    ("fleet", "min_samples = 5.5", r"\[fleet\]: min_samples must be a whole number, not 5\.5"),
    ("detection", 'gates_ft = [1000, "500"]', r"\[detection\]: gates_ft\[1\] must be a whole number, not '500'"),
    ("source", "port = true", r"\[source\]: port must be a whole number, not True"),
    ("service", "typedb_path = 5", r"\[service\]: typedb_path must be a string, not 5"),
    ("stability", "gp_dots_max = [1.0]", r"\[stability\]: gp_dots_max must be a number, not \[1\.0\]"),
])
def test_a_value_of_the_wrong_type_names_its_section_and_key(tmp_path, section, line, message):
    main = '[service]\nairports_dir = "airports"\n' + ("" if section == "service" else f"[{section}]\n") + line + "\n"
    with pytest.raises(ValueError, match=r"main\.toml: " + message):
        load_config(write_cfg(tmp_path, main))


@pytest.mark.parametrize("line, message", [
    ("gates_ft = [1000]",
     r"\[detection\]: gates_ft must include 1000 and 500 for coverage_daily's columns; missing 500, not \[1000\]"),
    ("gates_ft = [500]",
     r"\[detection\]: gates_ft must include 1000 and 500 for coverage_daily's columns; missing 1000, not \[500\]"),
    ("gates_ft = [2000, 250]", r"\[detection\]: gates_ft must include 1000 and 500 for coverage_daily's columns; "
                              r"missing 1000 and 500, not \[2000, 250\]"),
    ("gates_ft = []", r"\[detection\]: gates_ft must include 1000 and 500 for coverage_daily's columns; "
                      r"missing 1000 and 500, not \[\]"),
])
def test_gates_ft_must_still_name_the_two_coverage_gates(tmp_path, line, message):
    # coverage_daily has a column per gate (gate1000_observed_frac, gate500_observed_frac), not a row per gate, so
    # dropping either from gates_ft would leave that column NULL forever with no error.
    with pytest.raises(ValueError, match=r"main\.toml: " + message):
        load_config(write_cfg(tmp_path, f'[service]\nairports_dir = "airports"\n[detection]\n{line}\n'))


def test_further_gates_are_kept_and_their_order_is_the_scorers_business(tmp_path):
    main = write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n[detection]\ngates_ft = [1000, 500, 250]\n')
    assert load_config(main).detection.gates_ft == [1000, 500, 250]
    main.write_text('[service]\nairports_dir = "airports"\n[detection]\ngates_ft = [500, 1000]\n')
    assert load_config(main).detection.gates_ft == [500, 1000]


@pytest.mark.parametrize("section, line", [("source", "port = 70000"), ("service", "port = 0")])
def test_a_port_out_of_range_names_its_section(tmp_path, section, line):
    """Otherwise it loads and fails only when the service connects: asyncio raises OverflowError past 65535."""
    main = '[service]\nairports_dir = "airports"\n' + ("" if section == "service" else f"[{section}]\n") + line + "\n"
    with pytest.raises(ValueError, match=rf"main\.toml: \[{section}\]: port must be a port number from 1 to 65535"):
        load_config(write_cfg(tmp_path, main))


@pytest.mark.parametrize("line, message", [
    ('log_level = "verbose"', r"\[service\]: log_level must be one of .*, not 'verbose'"),
])
def test_a_bad_log_level_names_the_section_at_load(tmp_path, line, message):
    """Otherwise it loads and only crashes the service once logging.basicConfig runs (cli.py cmd_run), well after
    install.sh's config check has passed."""
    main = f'[service]\nairports_dir = "airports"\n{line}\n'
    with pytest.raises(ValueError, match=r"main\.toml: " + message):
        load_config(write_cfg(tmp_path, main))


def test_a_bad_source_kind_names_the_section_at_load(tmp_path):
    """Otherwise it loads and only crashes the live service once run_live starts, well after install.sh's config
    check has passed."""
    main = '[service]\nairports_dir = "airports"\n[source]\nkind = "aircraft-json"\n'
    with pytest.raises(ValueError, match=r"main\.toml: \[source\]: kind must be one of .*, not 'aircraft-json'"):
        load_config(write_cfg(tmp_path, main))


def test_whole_numbers_load_as_floats_where_a_number_is_expected(tmp_path):
    cfg = load_config(write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n[detection]\nlost_timeout_s = 60\n'))
    assert cfg.detection.lost_timeout_s == 60.0 and isinstance(cfg.detection.lost_timeout_s, float)
    assert cfg.fleet.min_samples == 30 and isinstance(cfg.fleet.min_samples, int)
    assert cfg.detection.gates_ft == [1000, 500]


def test_metar_idents_are_upper_cased(tmp_path):
    main = write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n')
    (tmp_path / "airports" / "ksfo.toml").write_text(KSFO_MIN.replace('metar = "KSFO"', 'metar = "ksfo"'))
    assert load_config(main).airports["KSFO"].metar == "KSFO"
    main.write_text('[service]\nairports_dir = "airports"\n[airports.KSFO]\nmetar = "koak"\n')
    assert load_config(main).airports["KSFO"].metar == "KOAK"


def test_type_classes_are_upper_cased_and_strict(tmp_path):
    from adsb_approach.config import TypeClasses
    good = tmp_path / "classes.toml"
    good.write_text('airliner = ["b738"]\nbizjet = [" c56x "]\nga = [" c172 "]\n[approach_category]\nb738 = "c"\n')
    tc = TypeClasses.load(good)
    assert tc.is_airliner("B738") and tc.is_bizjet("C56X") and tc.is_ga("C172") and tc.approach_category("b738") == "C"
    assert tc.is_ga("c172") and not tc.is_ga("B738") and not tc.is_ga(None)
    bad = tmp_path / "bad.toml"
    for text, message in (('airliners = ["B738"]\n', r"unknown key 'airliners' \(did you mean 'airliner'\?\)"),
                          ("airliner = [737]\n", r"airliner must be a list of type codes"),
                          ('bizjet = "C56X"\n', r"bizjet must be a list of type codes"),
                          ('ga = "C172"\n', r"ga must be a list of type codes"),
                          ("[approach_category]\nB738 = 3\n", r"\[approach_category\] must map type codes to category letters")):
        bad.write_text(text)
        with pytest.raises(ValueError, match=r"bad\.toml: " + message):
            TypeClasses.load(bad)


def test_a_ga_list_is_display_only_and_so_stays_out_of_the_config_hash(tmp_path):
    # The arrivals/live GA filter is a page filter, not a scoring input -- same precedent as [service] timezone and
    # [coverage] below: a configuration setting that changes no score and no outcome must not make config_hash claim a
    # different one.
    main = write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n')
    before = config_hash(load_config(main))
    classes = tmp_path / "classes.toml"
    classes.write_text('ga = ["C172", "C152"]\n')
    main.write_text('[service]\nairports_dir = "airports"\ntype_classes_path = "classes.toml"\n')
    after = load_config(main)
    assert after.type_classes.ga == frozenset({"C172", "C152"})     # sanity: the list actually loaded
    assert config_hash(after) == before


def test_the_environment_may_locate_the_receiver(tmp_path, monkeypatch):
    main = write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n[source]\nhost = "127.0.0.1"\n')
    env = {"ADSB_APPROACH_SOURCE_HOST": "192.0.2.10", "ADSB_APPROACH_SOURCE_PORT": " 30047 ",
           "ADSB_APPROACH_TAR1090_URL": "http://192.0.2.10/tar1090/", "HOME": "/home/someone"}
    cfg = load_config(main, env=env)
    assert (cfg.source.host, cfg.source.port, cfg.service.tar1090_url) == ("192.0.2.10", 30047, "http://192.0.2.10/tar1090/")
    monkeypatch.setenv("ADSB_APPROACH_SOURCE_HOST", "192.0.2.99")
    assert load_config(main).source.host == "127.0.0.1"          # only a caller that passes env reads it


@pytest.mark.parametrize("env, message", [
    ({"ADSB_APPROACH_SOURCE_HOTS": "192.0.2.10"},
     r"environment: unknown variable ADSB_APPROACH_SOURCE_HOTS \(did you mean ADSB_APPROACH_SOURCE_HOST\?\)"),
    ({"ADSB_APPROACH_SOURCE_PORT": "30047/tcp"}, r"environment: ADSB_APPROACH_SOURCE_PORT must be a whole number, not '30047/tcp'"),
    ({"ADSB_APPROACH_SOURCE_PORT": "+30047"}, r"environment: ADSB_APPROACH_SOURCE_PORT must be a whole number, not '\+30047'"),
    ({"ADSB_APPROACH_SOURCE_PORT": "30_047"}, r"environment: ADSB_APPROACH_SOURCE_PORT must be a whole number, not '30_047'"),
    ({"ADSB_APPROACH_SOURCE_PORT": "70000"}, r"environment: ADSB_APPROACH_SOURCE_PORT must be a port number from 1 to 65535, not '70000'"),
    ({"ADSB_APPROACH_SOURCE_PORT": "0"}, r"environment: ADSB_APPROACH_SOURCE_PORT must be a port number from 1 to 65535, not '0'"),
    ({"ADSB_APPROACH_TAR1090_URL": "  "}, r"environment: ADSB_APPROACH_TAR1090_URL is empty"),
])
def test_a_bad_location_variable_is_an_error(tmp_path, env, message):
    with pytest.raises(ValueError, match=message):
        load_config(write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n'), env=env)


def test_config_hash_follows_what_scores_depend_on():
    from dataclasses import replace
    from adsb_approach.config import config_hash
    example = Path(__file__).resolve().parents[1] / "config" / "adsb-approach.example.toml"
    h = config_hash(load_config(example))
    assert len(h) == 16 and h == config_hash(load_config(example))
    same = load_config(example)
    same.detection.lost_timeout_s = float(same.detection.lost_timeout_s)      # the TOML's 45 hashes like 45.0
    same.service.db_path, same.service.port, same.source.host = "/elsewhere.sqlite", 9999, "192.0.2.1"
    assert config_hash(same) == h
    threshold = load_config(example)
    threshold.detection.lost_timeout_s = 60
    runway = load_config(example)
    runway.airports["KSFO"].runways["28R"] = replace(runway.airports["KSFO"].runways["28R"], tch_ft=60.0)
    disabled = load_config(example)
    disabled.airports["KSJC"].enabled = False
    qnh = load_config(example)
    qnh.service.qnh_max_age_min = 60
    assert len({h, config_hash(threshold), config_hash(runway), config_hash(disabled), config_hash(qnh)}) == 5


def test_the_poller_is_off_by_default_and_its_section_changes_no_replay(tmp_path):
    main = write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n')
    off = load_config(main)
    assert off.external == ExternalCfg() and off.external.enabled is False
    main.write_text('[service]\nairports_dir = "airports"\n[external]\nenabled = true\npoll_interval_s = 10\n')
    on = load_config(main)
    assert on.external.enabled is True and on.external.poll_interval_s == 10.0 and isinstance(on.external.poll_interval_s, float)
    assert config_hash(on) == config_hash(off)


def test_the_poller_defaults_match_the_design():
    ext = ExternalCfg()
    assert (ext.url, ext.poll_interval_s, ext.clock_offset_s, ext.watch_max_min, ext.timeout_s, ext.backoff_min) == (
        "https://re-api.adsb.lol/", 2.0, 0.10, 20.0, 10.0, 15.0)


def test_the_example_config_loads_with_the_poller_off():
    example = Path(__file__).resolve().parents[1] / "config" / "adsb-approach.example.toml"
    cfg = load_config(example)
    assert cfg.external.enabled is False and cfg.external == ExternalCfg()
    named = RETIRED_EXTERNAL_KEYS & set(tomllib.loads(example.read_text())["external"])
    assert not named, f"the example's [external] table names retired keys: {sorted(named)}"


@pytest.mark.parametrize("key", ["trace_url", "delay_s", "retry_after_s", "min_interval_s", "max_age_min", "max_tail_min",
                                 "max_nm"])
def test_a_retired_external_key_is_named_and_called_retired(tmp_path, key):
    assert key in RETIRED_EXTERNAL_KEYS
    with pytest.raises(ValueError, match=rf"main\.toml: \[external\]: {key} is retired") as raised:
        load_config(write_cfg(tmp_path, f'[service]\nairports_dir = "airports"\n[external]\n{key} = 1\n'))
    assert "live polling" in str(raised.value)


@pytest.mark.parametrize("line, message", [
    ('enabled = "yes"', r"\[external\]: enabled must be true or false, not 'yes'"),
    ("watch_max = 20", r"\[external\]: unknown key 'watch_max' \(did you mean 'watch_max_min'\?\)"),
    ('globe_url = "ftp://globe.adsb.lol/"', r"\[external\]: globe_url must be empty, or an http\(s\) URL, not 'ftp://globe\.adsb\.lol/'"),
    ('globe_url = "globe.adsb.lol"', r"\[external\]: globe_url must be empty, or an http\(s\) URL, not 'globe\.adsb\.lol'"),
    ("poll_interval_s = 0", r"\[external\]: poll_interval_s must be greater than 0, not 0\.0"),
    ("watch_max_min = -1", r"\[external\]: watch_max_min must be greater than 0, not -1\.0"),
    ("clock_offset_s = -0.1", r"\[external\]: clock_offset_s must be 0 or more, not -0\.1"),
    ('url = "ftp://re-api.adsb.lol/"', r"\[external\]: url must be an http\(s\) URL, not 'ftp://re-api\.adsb\.lol/'"),
])
def test_a_bad_external_setting_names_its_section_and_key(tmp_path, line, message):
    with pytest.raises(ValueError, match=r"main\.toml: " + message):
        load_config(write_cfg(tmp_path, f'[service]\nairports_dir = "airports"\n[external]\n{line}\n'))


def test_globe_url_defaults_to_adsb_lol_and_empty_turns_the_drawers_second_link_off(tmp_path):
    main = write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n')
    assert load_config(main).external.globe_url == "https://globe.adsb.lol/"
    main.write_text('[service]\nairports_dir = "airports"\n[external]\nglobe_url = ""\n')
    assert load_config(main).external.globe_url == ""


def test_the_api_key_file_is_unset_by_default_resolves_against_the_config_and_stays_out_of_the_hash(tmp_path):
    main = write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n')
    before = load_config(main)
    assert before.external.api_key_file == ""
    main.write_text('[service]\nairports_dir = "airports"\n[external]\napi_key_file = "secrets/adsblol.key"\n')
    after = load_config(main)
    assert after.external.api_key_file == str((tmp_path / "secrets" / "adsblol.key").resolve())
    assert config_hash(after) == config_hash(before)


def test_the_globe_link_is_display_only_and_so_stays_out_of_the_config_hash(tmp_path):
    # globe_url only points the drawer's second link somewhere to look, the same precedent as type_classes.ga,
    # [service] timezone and [coverage] above: a setting that changes no score and no outcome must not make
    # config_hash claim a different configuration -- and [external] is excluded from it entirely already (it is a
    # network setting, like [service] tar1090_url), so this only has to show the field doesn't slip through that.
    main = write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n')
    before = config_hash(load_config(main))
    main.write_text('[service]\nairports_dir = "airports"\n[external]\nglobe_url = "https://example.org/globe/"\n')
    after = load_config(main)
    assert after.external.globe_url == "https://example.org/globe/"     # sanity: it actually loaded
    assert config_hash(after) == before


# --- [service] timezone -------------------------------------------------------------------------------------------

def test_the_timezone_is_the_hosts_zone_unless_the_configuration_names_one(tmp_path):
    main = write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n')
    plain = load_config(main)
    assert plain.service.timezone is None and zone_of(plain) is None        # None: datetime reads the host's own zone
    main.write_text('[service]\nairports_dir = "airports"\ntimezone = "America/Los_Angeles"\n')
    named = load_config(main)
    assert named.service.timezone == "America/Los_Angeles"
    zone = zone_of(named)
    assert zone is not None
    assert datetime.fromtimestamp(1789240000.0, zone).strftime("%Y-%m-%d %H:%M") == "2026-09-12 12:06"
    assert datetime.fromtimestamp(1789240000.0, timezone.utc).strftime("%Y-%m-%d %H:%M") == "2026-09-12 19:06"


@pytest.mark.parametrize("line, message", [
    ('timezone = "America/Nowhere"',
     r"\[service\]: timezone must be an IANA zone name such as 'America/Los_Angeles', not 'America/Nowhere'"),
    ('timezone = "/etc/localtime"', r"\[service\]: timezone must be an IANA zone name"),
    ("timezone = 7", r"\[service\]: timezone must be a string, not 7"),
])
def test_a_bad_timezone_names_its_section_and_key(tmp_path, line, message):
    with pytest.raises(ValueError, match=r"main\.toml: " + message):
        load_config(write_cfg(tmp_path, f'[service]\nairports_dir = "airports"\n{line}\n'))


def test_the_zone_cuts_the_coverage_days_and_so_stays_out_of_the_config_hash(tmp_path):
    main = write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n')
    before = config_hash(load_config(main))
    main.write_text('[service]\nairports_dir = "airports"\ntimezone = "Europe/London"\n')
    assert config_hash(load_config(main)) == before


def test_the_example_configuration_names_the_receivers_zone():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config" / "adsb-approach.example.toml")
    assert cfg.service.timezone == "America/Los_Angeles"


# --- [coverage] and its marks -------------------------------------------------------------------------------------

MARKS = ('[[coverage.events]]\nat = 2026-10-04\nlabel = "new antenna"\n'
         '[[coverage.events]]\nat = "2026-09-01"\nlabel = "  LNA in line  "\n')


def test_marks_are_dated_labels_in_date_order(tmp_path):
    cfg = load_config(write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n' + MARKS))
    assert cfg.coverage.events == (CoverageEvent(at="2026-09-01", label="LNA in line"),
                                   CoverageEvent(at="2026-10-04", label="new antenna"))


def test_a_configuration_with_no_coverage_section_has_no_marks(tmp_path):
    cfg = load_config(write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n'))
    assert cfg.coverage == CoverageCfg() and cfg.coverage.events == ()


@pytest.mark.parametrize("body, message", [
    ('[coverage]\nmark = []\n', r"\[coverage\]: unknown key 'mark'"),
    ('[coverage]\nevents = 5\n', r"\[coverage\]: events must be a list of \[\[coverage\.events\]\] tables"),
    ('[coverage]\nevents = [5]\n', r"\[coverage\]: events must be a list of \[\[coverage\.events\]\] tables"),
    ('[[coverage.events]]\nat = 2026-10-04\n', r"\[coverage\] events\[0\]: missing 'label'"),
    ('[[coverage.events]]\nlabel = "moved"\n', r"\[coverage\] events\[0\]: missing 'at'"),
    ('[[coverage.events]]\nat = 2026-10-04\nlabel = "x"\nwhen = 1\n',
     r"\[coverage\] events\[0\]: unknown key 'when'"),
    ('[[coverage.events]]\nat = "the fourth"\nlabel = "x"\n',
     r"\[coverage\] events\[0\]: at must be a date, written 2026-10-04, not 'the fourth'"),
    ('[[coverage.events]]\nat = 2026-10-04T12:00:00\nlabel = "x"\n',
     r"\[coverage\] events\[0\]: at must be a date, written 2026-10-04"),
    ('[[coverage.events]]\nat = "2026-13-04"\nlabel = "x"\n',
     r"\[coverage\] events\[0\]: at must be a date, written 2026-10-04, not '2026-13-04'"),
    ('[[coverage.events]]\nat = 2026-10-04\nlabel = "  "\n',
     r'\[coverage\] events\[0\]: label must say what changed, for example "new antenna"'),
    ('[[coverage.events]]\nat = 2026-10-04\nlabel = 5\n',
     r"\[coverage\] events\[0\]: label must say what changed"),
    ('[[coverage.events]]\nat = 2026-10-04\nlabel = "one"\n[[coverage.events]]\nat = 2026-10-04\nlabel = "two"\n',
     r"\[coverage\]: two marks on 2026-10-04"),
])
def test_a_bad_mark_names_the_section_and_the_mark(tmp_path, body, message):
    with pytest.raises(ValueError, match=message):
        load_config(write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n' + body))


def test_a_mark_annotates_the_coverage_page_and_so_stays_out_of_the_config_hash(tmp_path):
    main = write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n')
    before = config_hash(load_config(main))
    main.write_text('[service]\nairports_dir = "airports"\n' + MARKS)
    assert config_hash(load_config(main)) == before


def test_the_example_configuration_ships_no_marks():
    cfg = load_config(Path(__file__).resolve().parents[1] / "config" / "adsb-approach.example.toml")
    assert cfg.coverage == CoverageCfg()


# --- procedure paths ----------------------------------------------------------------------------------------------

def paths_airport(*blocks: str) -> str:
    return KSFO_MIN + "".join(blocks)


def path_block(*, name='"quiet-bridge"', label='"Quiet Bridge"', source='"test"', points=None) -> str:
    pts = QUIET_BRIDGE.points if points is None else points
    lines = [f"name = {name}"] if name else []
    lines += [f"label = {label}"] if label else []
    lines += [f"source = {source}"] if source else []
    lines.append("points = [" + ", ".join(f"[{la!r}, {lo!r}]" for la, lo in pts) + "]")
    return '\n[[runways."28R".paths]]\n' + "\n".join(lines) + "\n"


def load_airport_text(tmp_path: Path, text: str):
    p = tmp_path / "ksfo.toml"
    p.write_text(text)
    return load_airport_file(p)


def test_paths_load_from_the_airport_file(tmp_path):
    apt = load_airport_text(tmp_path, paths_airport(path_block(source='"SFO VOR R-095 inbound"')))
    assert apt.runways["28R"].paths == (replace(QUIET_BRIDGE, source="SFO VOR R-095 inbound"),)
    assert isinstance(apt.runways["28R"].paths[0], ProcedurePath) and apt.runways["28L"].paths == ()


def test_the_paths_of_a_runway_keep_their_file_order(tmp_path):
    apt = load_airport_text(tmp_path, paths_airport(path_block(name='"zulu"'), path_block(name='"alpha"')))
    assert [p.name for p in apt.runways["28R"].paths] == ["zulu", "alpha"]


def join_at(out_nm: float, off_ft: float):
    """QUIET_BRIDGE's points with the join moved to out_nm along 28R's extended centreline and off_ft to its right."""
    r = make_sfo_28r()
    join = destination(r.threshold_lat, r.threshold_lon, (r.course_true_deg + 180) % 360, out_nm * NM_FT)
    return [*QUIET_BRIDGE.points[:2], destination(*join, (r.course_true_deg + 90) % 360, off_ft)]


def quiet_bridge_points():
    return list(QUIET_BRIDGE.points)


@pytest.mark.parametrize("block, message", [
    (lambda: path_block(points=quiet_bridge_points()[:1]), "at least 2 points"),
    (lambda: path_block(points=[quiet_bridge_points()[0]] * 2 + quiet_bridge_points()[2:]), "zero-length leg"),
    (lambda: path_block(points=join_at(1.1, 200.0)), "join must lie on the centreline"),
    (lambda: path_block(points=join_at(6.0, 0.0)), "join must lie within 5 nm"),
    (lambda: path_block(name='"Quiet Bridge"'), "name"),
    (lambda: path_block() + path_block(label='"Again"'), "duplicate path"),
    (lambda: path_block(label=None), "label"),
    (lambda: path_block(source=None), "source"),
], ids=["one-point", "repeated-point", "join-off-centreline", "join-too-far-out", "bad-name", "duplicate-name",
        "no-label", "no-source"])
def test_path_rules(tmp_path, block, message):
    with pytest.raises(ValueError, match=message):
        load_airport_text(tmp_path, paths_airport(block()))


def test_paths_are_refused_in_main_config_overrides(tmp_path):
    main = '[service]\nairports_dir = "airports"\n[airports.KSFO.runways."28R"]\npaths = []\n'
    with pytest.raises(ValueError, match="build-config --overrides"):
        load_config(write_cfg(tmp_path, main))


def test_an_override_of_another_key_keeps_the_runways_paths(tmp_path):
    cfg_path = write_cfg(tmp_path, '[service]\nairports_dir = "airports"\n[airports.KSFO.runways."28R"]\ntch_ft = 60\n')
    (tmp_path / "airports" / "ksfo.toml").write_text(paths_airport(path_block()))
    r = load_config(cfg_path).airports["KSFO"].runways["28R"]
    assert r.tch_ft == 60 and [p.name for p in r.paths] == ["quiet-bridge"]


def test_paths_change_the_config_hash():
    example = Path(__file__).resolve().parents[1] / "config" / "adsb-approach.example.toml"
    base = load_config(example)
    with_path = load_config(example)
    runway = with_path.airports["KSFO"].runways["28R"]
    with_path.airports["KSFO"].runways["28R"] = replace(runway, paths=runway.paths + (replace(QUIET_BRIDGE, name="extra"),))
    assert config_hash(with_path) != config_hash(base) and config_hash(base) == config_hash(load_config(example))

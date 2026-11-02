"""deploy/install.sh, run unprivileged into a scratch directory with stand-ins for uv and systemctl, and the service
templates checked against what the program reads."""
import getpass
import os
import plistlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from adsb_approach.config import ENV_SETTINGS, load_config
from tests.conftest import ROOT

DEPLOY = ROOT / "deploy"


def _in_git_worktree(path: Path) -> bool:
    """install.sh copies files with `git ls-files`, so it (and these tests, which run it against ROOT) need a git
    checkout: a `git archive` extraction or a plain copy has no `.git` for git to find."""
    return subprocess.run(["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
                          capture_output=True, text=True).returncode == 0


pytestmark = [pytest.mark.skipif(shutil.which("bash") is None, reason="the installer is a bash script"),
             pytest.mark.skipif(not _in_git_worktree(ROOT), reason="install.sh needs a git checkout; this is not one")]


def stub(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def install(tmp_path: Path, *, fail_build: bool = False, fetch_typedb: bool = False, fastapi_importable: bool = True,
            lighttpd: bool = False, lighttpd_check_fails: bool = False
           ) -> tuple[subprocess.CompletedProcess, dict[str, Path]]:
    """Run install.sh with every location under tmp_path/root, recording the uv, systemctl and fetch-typedb calls in
    calls.log. fail_build: the build step fails, as it would with no network and a cold uv cache. fetch_typedb: the
    aircraft type database is "fetched" (logged, not a real download) rather than skipped. fastapi_importable: False
    makes $PYTHON unable to import fastapi or uvicorn (via -S, which drops site-packages but keeps the standard
    library and PYTHONPATH), as it would be on a target missing python3-fastapi. lighttpd: stand-ins for `lighttpd`,
    `lighty-enable-mod` and `lighty-disable-mod` are installed and conf-available/conf-enabled exist under root, as
    on a Pi with lighttpd; False (default) leaves them all missing, as on a target with none. lighttpd_check_fails:
    only with lighttpd=True; the `lighttpd -tt` stand-in exits non-zero, as a bad rendered site would on the real
    binary."""
    root, calls = tmp_path / "root", tmp_path / "calls.log"
    # PYTHONDONTWRITEBYTECODE: these shims re-exec the interpreter running pytest, whose editable install of
    # adsb-approach points at this checkout, so without it they would write __pycache__ into ROOT on every call.
    python = stub(tmp_path / "python-shim", f'export PYTHONDONTWRITEBYTECODE=1\nexec "{sys.executable}" "$@"\n')
    python_env = python if fastapi_importable else stub(
        tmp_path / "python-shim-no-fastapi", f'export PYTHONDONTWRITEBYTECODE=1\nexec "{sys.executable}" -S "$@"\n')
    # fetch-typedb is intercepted so tests never hit the real network; every other subcommand runs for real.
    cli = stub(tmp_path / "cli-shim",
              'if [ "$1" = fetch-typedb ]; then\n'
             f'  echo "adsb-approach $*" >> "{calls}"\n'
              '  exit 0\n'
              'fi\n'
              'export PYTHONDONTWRITEBYTECODE=1\n'
             f'exec "{sys.executable}" -m adsb_approach.cli "$@"\n')
    build_branch = ('  exit 1\n' if fail_build else
                    '  for src in "$@"; do :; done\n'          # uv build's last positional is the source directory
                    '  mkdir -p "$src/dist"\n'
                    '  touch "$src/dist/adsb_approach-0.0.0-py3-none-any.whl"\n')
    uv = stub(tmp_path / "uv", f'echo "uv $*" >> "{calls}"\n'
                               'if [ "$1" = venv ]; then\n'
                               '  for venv in "$@"; do :; done\n'
                               '  mkdir -p "$venv/bin"\n'
                               f'  cp "{python}" "$venv/bin/python"\n'
                               f'  cp "{cli}" "$venv/bin/adsb-approach"\n'
                               'elif [ "$1" = build ]; then\n'
                               f'{build_branch}'
                               'fi\n')
    systemctl = stub(tmp_path / "systemctl", f'echo "systemctl $*" >> "{calls}"\n')
    # lighttpd=False (the default): conf-available is never created, so install.sh's own "is lighttpd installed?"
    # check (a conf-available directory and a binary) sees it as absent, as on a target with no lighttpd.
    lighttpd_conf_available = root / "etc" / "lighttpd" / "conf-available"
    lighttpd_conf_enabled = root / "etc" / "lighttpd" / "conf-enabled"
    lighttpd_conf = root / "etc" / "lighttpd" / "lighttpd.conf"
    if lighttpd:
        lighttpd_conf_available.mkdir(parents=True, exist_ok=True)
        lighttpd_conf_enabled.mkdir(parents=True, exist_ok=True)
        lighttpd_conf.write_text("# stand-in lighttpd.conf\n")
        fail_branch = ('  echo "lighttpd: stand-in configuration test failure" >&2\n  exit 1\n' if lighttpd_check_fails
                       else '  exit 0\n')
        lighttpd_bin = stub(tmp_path / "lighttpd-bin", f'echo "lighttpd $*" >> "{calls}"\n'
                                                        'if [ "$1" = "-tt" ]; then\n'
                                                       f'{fail_branch}'
                                                        'fi\n')
        # a faithful-enough stand-in for lighty-enable-mod/lighty-disable-mod: it does not consult conf-available
        # (unlike the real tool), but it does leave a matching name in conf-enabled so a second run of install.sh's
        # own enabled-check (a glob on conf-enabled) sees the module as already enabled, as the real tool's symlink
        # would.
        lighty_enable = stub(tmp_path / "lighty-enable-mod", f'echo "lighty-enable-mod $*" >> "{calls}"\n'
                                                              'for m; do\n'
                                                             f'  : > "{lighttpd_conf_enabled}/$m.conf"\n'
                                                              'done\n')
        lighty_disable = stub(tmp_path / "lighty-disable-mod", f'echo "lighty-disable-mod $*" >> "{calls}"\n'
                                                                'for m; do\n'
                                                               f'  rm -f "{lighttpd_conf_enabled}"/*"$m".conf\n'
                                                                'done\n')
        lighttpd_path, lighty_enable_path, lighty_disable_path = str(lighttpd_bin), str(lighty_enable), str(lighty_disable)
    else:
        # left missing, as on a target with no lighttpd: never created, so neither "command -v" nor a direct call
        # can find them
        no_lighttpd = tmp_path / "no-lighttpd"
        lighttpd_path, lighty_enable_path, lighty_disable_path = (str(no_lighttpd / name) for name in
                                                                   ("lighttpd", "lighty-enable-mod", "lighty-disable-mod"))
    paths = {"PREFIX": root / "opt" / "adsb-approach", "ETC_DIR": root / "etc" / "adsb-approach",
             "DATA_DIR": root / "var" / "lib" / "adsb-approach", "ENV_FILE": root / "etc" / "default" / "adsb-approach",
             "UNIT_DIR": root / "etc" / "systemd" / "system", "LIGHTTPD_CONF_AVAILABLE_DIR": lighttpd_conf_available,
             "LIGHTTPD_CONF_ENABLED_DIR": lighttpd_conf_enabled, "LIGHTTPD_CONF": lighttpd_conf}
    env = {**{k: v for k, v in os.environ.items() if not k.startswith("ADSB_APPROACH_")},
           **{k: str(v) for k, v in paths.items()},
           "UV": str(uv), "SYSTEMCTL": str(systemctl),
           "PYTHON": sys.executable if fastapi_importable else str(python_env), "SERVICE_USER": getpass.getuser(),
           "FETCH_TYPEDB": "1" if fetch_typedb else "0",
           "LIGHTTPD": lighttpd_path, "LIGHTY_ENABLE_MOD": lighty_enable_path, "LIGHTY_DISABLE_MOD": lighty_disable_path}
    # run as the README runs it (sudo ... deploy/install.sh), so the committed file must be executable
    result = subprocess.run([str(DEPLOY / "install.sh")], env=env, capture_output=True, text=True)
    return result, {**paths, "calls": calls}


def test_in_git_worktree_detects_a_real_checkout_and_a_plain_directory(tmp_path):
    assert _in_git_worktree(ROOT) is True
    assert _in_git_worktree(tmp_path) is False               # tmp_path is never itself a git repository


def test_install_lays_out_the_service_and_runs_again_without_losing_edits(tmp_path):
    result, p = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    config, venv = p["ETC_DIR"] / "adsb-approach.toml", p["PREFIX"] / "venv"
    cfg = load_config(config)
    assert cfg.service.db_path == str(p["DATA_DIR"] / "approaches.sqlite")
    assert cfg.service.typedb_path == str(p["DATA_DIR"] / "aircraft.csv.gz")
    assert sorted(cfg.airports) == ["KNUQ", "KOAK", "KSFO", "KSJC"] and cfg.type_classes.is_airliner("B738")
    assert sqlite3.connect(p["DATA_DIR"] / "approaches.sqlite").execute("SELECT MAX(version) FROM schema_version").fetchone()[0] >= 1
    calls = p["calls"].read_text().splitlines()
    assert f"uv venv --clear --system-site-packages --python {sys.executable} {venv}" in calls
    install_calls = [c for c in calls if c.startswith(f"uv pip install --python {venv / 'bin' / 'python'} ")]
    assert len(install_calls) == 1
    installed_from = install_calls[0].rsplit(" ", 1)[1]
    assert installed_from != str(ROOT)     # a temporary copy of the tracked files, not the checkout itself
    assert calls[-5:] == ["systemctl daemon-reload", "systemctl enable adsb-approach.service",
                          "systemctl restart adsb-approach.service", "systemctl enable adsb-approach-web.service",
                          "systemctl restart adsb-approach-web.service"]
    unit = (p["UNIT_DIR"] / "adsb-approach.service").read_text()
    for line in ("After=network-online.target readsb.service", "Restart=always", "RestartSec=5",
                 f"EnvironmentFile=-{p['ENV_FILE']}", f"ExecStart={venv}/bin/adsb-approach run --config {config}",
                 f"User={getpass.getuser()}", f"ReadWritePaths={p['DATA_DIR']}"):
        assert line in unit.splitlines(), line
    assert not re.search(r"@[A-Z_]+@", unit)
    config.write_text(config.read_text() + "# a local edit\n")
    p["ENV_FILE"].write_text(p["ENV_FILE"].read_text().replace("127.0.0.1", "192.0.2.10"))
    again, _ = install(tmp_path)
    assert again.returncode == 0, again.stdout + again.stderr
    assert f"keeping {config}" in again.stdout and config.read_text().endswith("# a local edit\n")
    assert "ADSB_APPROACH_SOURCE_HOST=192.0.2.10" in p["ENV_FILE"].read_text()
    assert "systemctl stop adsb-approach.service" in p["calls"].read_text().splitlines()


def test_typedb_is_fetched_before_the_service_is_stopped_on_an_upgrade(tmp_path):
    """A first install has nothing running to stop, so it fetches after the venv is (re)built, same as before. An
    upgrade already has a working adsb-approach, so it fetches with that before stopping anything, rather than
    extending the outage by the download."""
    result, p = install(tmp_path, fetch_typedb=True)
    assert result.returncode == 0, result.stdout + result.stderr
    p["calls"].unlink()
    again, _ = install(tmp_path, fetch_typedb=True)
    assert again.returncode == 0, again.stdout + again.stderr
    calls = p["calls"].read_text().splitlines()
    fetch = next(i for i, c in enumerate(calls) if c.startswith("adsb-approach fetch-typedb"))
    stop = calls.index("systemctl stop adsb-approach.service")
    assert fetch < stop, calls


def test_the_unit_has_increasing_restart_delays(tmp_path):
    """A persistent failure must not restart every 5 s forever, reloading the ~220 MB type database each time
    (needs systemd 254 or later; Debian 13 ships a later one)."""
    result, p = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    unit = (p["UNIT_DIR"] / "adsb-approach.service").read_text().splitlines()
    assert "RestartSec=5" in unit and "RestartSteps=5" in unit and "RestartMaxDelaySec=300" in unit


def test_the_wheel_is_installed_with_compiled_bytecode(tmp_path):
    # ProtectSystem=strict makes the venv read-only at runtime, so bytecode must be compiled at install time.
    result, p = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    venv = p["PREFIX"] / "venv"
    install_calls = [c for c in p["calls"].read_text().splitlines()
                     if c.startswith(f"uv pip install --python {venv / 'bin' / 'python'} ")]
    assert len(install_calls) == 1
    assert "--compile-bytecode" in install_calls[0].split()


def test_install_refuses_a_database_inside_the_configuration_directory_before_stopping_anything(tmp_path):
    result, p = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    config = p["ETC_DIR"] / "adsb-approach.toml"
    config.write_text("".join(line for line in config.read_text().splitlines(keepends=True) if not line.startswith("db_path")))
    p["calls"].unlink()
    again, _ = install(tmp_path)
    assert again.returncode != 0 and "db_path resolves to" in again.stderr
    assert not p["calls"].exists()                              # neither uv nor systemctl was called


def test_install_puts_nearby_airports_beside_type_classes(tmp_path):
    result, p = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    installed = p["ETC_DIR"] / "nearby_airports.csv"
    assert installed.is_file() and stat.S_IMODE(installed.stat().st_mode) == 0o644
    assert installed.read_bytes() == (ROOT / "config" / "nearby_airports.csv").read_bytes()
    assert load_config(p["ETC_DIR"] / "adsb-approach.toml").service.nearby_airports_path == str(installed)


def test_a_failing_config_check_leaves_installed_reference_data_unchanged(tmp_path):
    result, p = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    airport_file = p["ETC_DIR"] / "airports" / "ksfo.toml"
    airport_file.write_text(airport_file.read_text() + "# a local edit\n")
    type_classes = p["ETC_DIR"] / "type_classes.toml"
    type_classes.write_text(type_classes.read_text() + "# a local edit\n")
    nearby = p["ETC_DIR"] / "nearby_airports.csv"
    nearby.write_text(nearby.read_text() + "KXXX,A local edit,small_airport,37.0,-122.0\n")
    config = p["ETC_DIR"] / "adsb-approach.toml"
    config.write_text("".join(line for line in config.read_text().splitlines(keepends=True) if not line.startswith("db_path")))
    again, _ = install(tmp_path)
    assert again.returncode != 0 and "db_path resolves to" in again.stderr
    assert airport_file.read_text().endswith("# a local edit\n")
    assert type_classes.read_text().endswith("# a local edit\n")
    assert nearby.read_text().endswith("KXXX,A local edit,small_airport,37.0,-122.0\n")


def test_a_failing_build_leaves_the_service_running_and_the_venv_untouched(tmp_path):
    result, p = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    venv_python = p["PREFIX"] / "venv" / "bin" / "python"
    before = venv_python.read_bytes()
    p["calls"].unlink()
    again, _ = install(tmp_path, fail_build=True)
    assert again.returncode != 0
    calls = p["calls"].read_text().splitlines() if p["calls"].exists() else []
    assert not any(c.startswith("systemctl") for c in calls)     # nothing was stopped
    assert not any(c.startswith("uv venv") for c in calls)       # ...and the venv was not recreated
    assert venv_python.read_bytes() == before                    # ...so it is untouched


def test_a_failing_build_leaves_installed_reference_data_unchanged(tmp_path):
    """The reference-data swap is only made final once the build succeeds, so a failed build never pairs new
    reference data with the old code that is still running."""
    result, p = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    airport_file = p["ETC_DIR"] / "airports" / "ksfo.toml"
    airport_file.write_text(airport_file.read_text() + "# a local edit\n")
    type_classes = p["ETC_DIR"] / "type_classes.toml"
    type_classes.write_text(type_classes.read_text() + "# a local edit\n")
    nearby = p["ETC_DIR"] / "nearby_airports.csv"
    nearby.write_text(nearby.read_text() + "KXXX,A local edit,small_airport,37.0,-122.0\n")
    again, _ = install(tmp_path, fail_build=True)
    assert again.returncode != 0
    assert airport_file.read_text().endswith("# a local edit\n")
    assert type_classes.read_text().endswith("# a local edit\n")
    assert nearby.read_text().endswith("KXXX,A local edit,small_airport,37.0,-122.0\n")
    assert not (p["ETC_DIR"] / "airports.old").exists() and not (p["ETC_DIR"] / "type_classes.toml.old").exists()
    assert not (p["ETC_DIR"] / "nearby_airports.csv.old").exists()


@pytest.mark.parametrize("failure, message", [
    ("config", "does not load; nothing was stopped"),
    ("build", "build failed; nothing was stopped"),
    ("fastapi", "sudo apt-get install --no-install-recommends python3-fastapi"),
])
def test_a_rollback_on_a_first_upgrade_still_reports_why(tmp_path, failure, message):
    """A first upgrade to the nearby-airports list has no nearby_airports.csv to put back. The rollback has to finish
    anyway: if it returned that missing file's failed test as its status, `set -e` would end the script inside the
    `|| { restore_reference_data; die ...; }` before die printed anything."""
    result, p = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    type_classes = p["ETC_DIR"] / "type_classes.toml"
    type_classes.write_text(type_classes.read_text() + "# a local edit\n")
    (p["ETC_DIR"] / "nearby_airports.csv").unlink()              # as installed before the list existed
    if failure == "config":
        config = p["ETC_DIR"] / "adsb-approach.toml"
        config.write_text("".join(line for line in config.read_text().splitlines(keepends=True)
                                  if not line.startswith("db_path")))
    again, _ = install(tmp_path, fail_build=failure == "build", fastapi_importable=failure != "fastapi")
    assert again.returncode != 0
    assert message in again.stderr, again.stderr
    assert type_classes.read_text().endswith("# a local edit\n")  # the rollback itself completed
    assert (p["ETC_DIR"] / "airports" / "ksfo.toml").is_file()
    assert not (p["ETC_DIR"] / "nearby_airports.csv").exists()


def test_missing_fastapi_or_uvicorn_stops_the_install_before_anything_stops(tmp_path):
    """The venv is built with --system-site-packages, so FastAPI and uvicorn must already be importable by $PYTHON
    (Debian's python3-fastapi) before anything is stopped."""
    result, p = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    venv_python = p["PREFIX"] / "venv" / "bin" / "python"
    before = venv_python.read_bytes()
    p["calls"].unlink()
    again, _ = install(tmp_path, fastapi_importable=False)
    assert again.returncode != 0
    assert "sudo apt-get install --no-install-recommends python3-fastapi" in again.stderr
    calls = p["calls"].read_text().splitlines() if p["calls"].exists() else []
    assert not any(c.startswith("systemctl") for c in calls)     # nothing was stopped
    assert not any(c.startswith("uv venv") for c in calls)       # ...and the venv was not recreated
    assert venv_python.read_bytes() == before                    # ...so it is untouched


def test_an_existing_directory_keeps_its_mode(tmp_path):
    result, p = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    prefix = p["PREFIX"]
    prefix.chmod(0o700)
    again, _ = install(tmp_path)
    assert again.returncode == 0, again.stdout + again.stderr
    assert stat.S_IMODE(prefix.stat().st_mode) == 0o700


def test_installed_environment_file_defines_no_variables(tmp_path):
    result, p = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    active = [line for line in p["ENV_FILE"].read_text().splitlines()
             if line.strip() and not line.strip().startswith("#")]
    assert active == []


def checkout_paths() -> set[Path]:
    """Every file under the checkout, skipping directories install.sh has no business touching."""
    skip = (ROOT / ".git", ROOT / ".venv", ROOT / ".pytest_cache")
    return {p for p in ROOT.rglob("*") if p.is_file() and not any(s in p.parents for s in skip)}


def test_install_leaves_the_checkout_unmodified(tmp_path):
    before = checkout_paths()
    result, _ = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    after = checkout_paths()
    assert after == before, f"new files in the checkout: {sorted(str(p) for p in after - before)}"


def test_launchd_plist_and_environment_file_carry_the_location_settings():
    text = (DEPLOY / "com.z2amiller.adsb-approach.plist").read_text()
    plist = plistlib.loads(text.replace("@REPO@", "/Users/someone/adsb-approach")
                           .replace("@LOGS@", "/Users/someone/Library/Logs").encode())
    assert plist["ProgramArguments"] == ["/Users/someone/adsb-approach/.venv/bin/adsb-approach", "run", "--config",
                                         "/Users/someone/adsb-approach/config/adsb-approach.toml"]
    assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True and plist["ThrottleInterval"] == 5
    assert set(plist["EnvironmentVariables"]) == set(ENV_SETTINGS)
    # the env file ships its examples commented out (deploy/adsb-approach.env), so a line may start with "# "
    names = {m.group(1) for line in (DEPLOY / "adsb-approach.env").read_text().splitlines()
             if (m := re.match(r"#?\s*([A-Z][A-Z0-9_]*)=", line.strip()))}
    assert names == set(ENV_SETTINGS)


def test_lighttpd_template_names_install_sh_and_renders_the_validated_config():
    """The config as checked on a target running lighttpd 1.4.79 on Debian 13: a redirect, the proxy to the web unit
    with map-urlpath so the app sees "/", and server.stream-response-body=2 so the live event stream is not buffered."""
    text = (DEPLOY / "lighttpd-adsb-approach.conf").read_text()
    header = text.splitlines()[0]
    assert header.startswith("#") and "install.sh" in header
    rendered = text.replace("@PORT@", "8090")
    assert not re.search(r"@[A-Z_]+@", rendered)
    body = "\n".join(rendered.splitlines()[1:]) + "\n"
    assert body == (
        '# adsb-approach web page at /adsb-approaches/, proxied to adsb-approach-web on 127.0.0.1:8090\n'
        'url.redirect += ( "^/adsb-approaches$" => "/adsb-approaches/" )\n'
        '$HTTP["url"] =~ "^/adsb-approaches/" {\n'
        '    proxy.server = ( "" => ( ( "host" => "127.0.0.1", "port" => 8090 ) ) )\n'
        '    proxy.header = ( "map-urlpath" => ( "/adsb-approaches/" => "/" ) )\n'
        '    server.stream-response-body = 2    # pass the live event stream through unbuffered\n'
        '}\n'
    )


def test_installed_files_name_their_own_template_not_the_edit_warning(tmp_path):
    """The template's own header (checked above) tells someone editing the checkout not to edit the installed copy;
    the installed copy needs the opposite: which template produced it, and that edits made there are lost on the
    next install. sed only rewrites the first line, so the rest of each rendered file is unaffected."""
    result, p = install(tmp_path, lighttpd=True)
    assert result.returncode == 0, result.stdout + result.stderr
    site = (p["LIGHTTPD_CONF_AVAILABLE_DIR"] / "89-adsb-approach.conf").read_text().splitlines()[0]
    assert site == ("# Written by install.sh from deploy/lighttpd-adsb-approach.conf; changes made here are "
                    "overwritten by the next install.")
    for unit in ("adsb-approach.service", "adsb-approach-web.service"):
        header = (p["UNIT_DIR"] / unit).read_text().splitlines()[0]
        assert header == f"# Written by install.sh from deploy/{unit}; changes made here are overwritten by the next install."


def test_the_web_service_only_reads_and_starts_after_the_collector(tmp_path):
    result, p = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    config, venv = p["ETC_DIR"] / "adsb-approach.toml", p["PREFIX"] / "venv"
    unit = (p["UNIT_DIR"] / "adsb-approach-web.service").read_text()
    lines = unit.splitlines()
    for line in ("After=network-online.target adsb-approach.service", "Restart=always", f"EnvironmentFile=-{p['ENV_FILE']}",
                 f"ExecStart={venv}/bin/adsb-approach web --config {config}", f"User={getpass.getuser()}",
                 "NoNewPrivileges=yes", "ProtectSystem=strict"):
        assert line in lines, line
    assert not any(line.startswith("ReadWritePaths") for line in lines)        # not even the database's directory
    assert not re.search(r"@[A-Z_]+@", unit)
    again, _ = install(tmp_path)
    assert again.returncode == 0, again.stdout + again.stderr
    assert [c for c in p["calls"].read_text().splitlines() if c.startswith("systemctl stop")] == [
        "systemctl stop adsb-approach-web.service", "systemctl stop adsb-approach.service"]


def test_the_web_unit_has_extra_hardening_for_a_lan_listener_with_no_login(tmp_path):
    """Unlike the collector, it listens on the LAN with no login, so it gets more than the shared baseline hardening
    (NoNewPrivileges, PrivateTmp, ProtectHome, ProtectSystem) both units already carry."""
    result, p = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = (p["UNIT_DIR"] / "adsb-approach-web.service").read_text().splitlines()
    for line in ("PrivateDevices=yes", "ProtectKernelTunables=yes", "ProtectKernelModules=yes",
                 "ProtectKernelLogs=yes", "ProtectControlGroups=yes",
                 "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX", "RestrictNamespaces=yes",
                 "LockPersonality=yes", "SystemCallFilter=@system-service",
                 "NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectHome=yes", "ProtectSystem=strict"):
        assert line in lines, line
    collector = (p["UNIT_DIR"] / "adsb-approach.service").read_text().splitlines()
    assert not any(line.startswith(("PrivateDevices", "ProtectKernelTunables", "ProtectKernelModules",
                                    "ProtectKernelLogs", "ProtectControlGroups", "RestrictAddressFamilies",
                                    "RestrictNamespaces", "LockPersonality", "SystemCallFilter"))
              for line in collector)


def test_the_venv_uses_system_site_packages_and_installs_only_the_wheel(tmp_path):
    """FastAPI, Starlette, Pydantic and uvicorn come from Debian's packages on the target, not PyPI: the venv sees
    them through --system-site-packages, and only the wheel itself is installed into it, offline and with --no-deps."""
    result, p = install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = p["calls"].read_text().splitlines()
    venv = p["PREFIX"] / "venv"
    assert f"uv venv --clear --system-site-packages --python {sys.executable} {venv}" in calls
    [final] = [c for c in calls if c.startswith(f"uv pip install --python {venv / 'bin' / 'python'} ")]
    words = final.split()
    assert "--offline" in words and "--no-deps" in words and "--compile-bytecode" in words
    assert words[-1].endswith(".whl")
    assert not any(c.startswith("uv export") for c in calls)          # no uv.lock export into a throwaway env
    assert not any("/deps/bin/python" in c for c in calls)             # ...and no throwaway env at all


def test_lighttpd_step_is_skipped_quietly_when_lighttpd_is_not_installed(tmp_path):
    result, p = install(tmp_path)                               # lighttpd=False: no conf-available, no binary
    assert result.returncode == 0, result.stdout + result.stderr
    assert "own port only" in result.stdout
    assert not p["LIGHTTPD_CONF_AVAILABLE_DIR"].exists()
    calls = p["calls"].read_text().splitlines()
    assert not any(c.startswith(("lighty-enable-mod", "lighty-disable-mod", "lighttpd ", "systemctl reload-or-restart"))
                  for c in calls)


def test_lighttpd_site_is_written_enabled_tested_and_reloaded_after_the_web_unit_starts(tmp_path):
    result, p = install(tmp_path, lighttpd=True)
    assert result.returncode == 0, result.stdout + result.stderr
    site = p["LIGHTTPD_CONF_AVAILABLE_DIR"] / "89-adsb-approach.conf"
    text = site.read_text()
    assert not re.search(r"@[A-Z_]+@", text)
    assert 'proxy.server = ( "" => ( ( "host" => "127.0.0.1", "port" => 8090 ) ) )' in text
    calls = p["calls"].read_text().splitlines()
    web_restart = calls.index("systemctl restart adsb-approach-web.service")
    enable = next(i for i, c in enumerate(calls) if c.startswith("lighty-enable-mod"))
    assert sorted(calls[enable].split()[1:]) == ["adsb-approach", "proxy"]      # both new on a first run
    check = next(i for i, c in enumerate(calls) if c.startswith("lighttpd -tt"))
    reload_ = calls.index("systemctl reload-or-restart lighttpd")
    assert web_restart < enable < check < reload_, calls
    assert "adsb-approach is behind lighttpd at /adsb-approaches/" in result.stdout


def test_lighttpd_second_run_is_idempotent(tmp_path):
    result, p = install(tmp_path, lighttpd=True)
    assert result.returncode == 0, result.stdout + result.stderr
    site_before = (p["LIGHTTPD_CONF_AVAILABLE_DIR"] / "89-adsb-approach.conf").read_text()
    p["calls"].unlink()
    again, _ = install(tmp_path, lighttpd=True)
    assert again.returncode == 0, again.stdout + again.stderr
    assert (p["LIGHTTPD_CONF_AVAILABLE_DIR"] / "89-adsb-approach.conf").read_text() == site_before
    calls = p["calls"].read_text().splitlines()
    assert not any(c.startswith(("lighty-enable-mod", "lighty-disable-mod")) for c in calls)  # already enabled
    assert calls.count("systemctl reload-or-restart lighttpd") == 1
    assert not (p["LIGHTTPD_CONF_AVAILABLE_DIR"] / "89-adsb-approach.conf.old").exists()


def test_a_failing_lighttpd_check_rolls_back_what_it_enabled_and_the_install_still_succeeds(tmp_path):
    """A first run: nothing was enabled before, so both modules are newly enabled and then must be newly disabled;
    there was no previous site file to restore, so the rendered one is removed instead."""
    result, p = install(tmp_path, lighttpd=True, lighttpd_check_fails=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (p["LIGHTTPD_CONF_AVAILABLE_DIR"] / "89-adsb-approach.conf").exists()
    assert not (p["LIGHTTPD_CONF_AVAILABLE_DIR"] / "89-adsb-approach.conf.old").exists()
    calls = p["calls"].read_text().splitlines()
    enabled = next(c for c in calls if c.startswith("lighty-enable-mod")).split()[1:]
    disabled = next(c for c in calls if c.startswith("lighty-disable-mod")).split()[1:]
    assert sorted(enabled) == sorted(disabled) == ["adsb-approach", "proxy"]
    assert not any(c.startswith("systemctl reload-or-restart") for c in calls)
    assert "lighttpd -tt" in result.stderr and "stand-in configuration test failure" in result.stderr


def test_a_failing_lighttpd_check_on_an_upgrade_restores_the_previous_site_and_disables_nothing_already_enabled(tmp_path):
    result, p = install(tmp_path, lighttpd=True)
    assert result.returncode == 0, result.stdout + result.stderr
    site = p["LIGHTTPD_CONF_AVAILABLE_DIR"] / "89-adsb-approach.conf"
    before = site.read_text()
    p["calls"].unlink()
    again, _ = install(tmp_path, lighttpd=True, lighttpd_check_fails=True)
    assert again.returncode == 0, again.stdout + again.stderr
    assert site.read_text() == before                                     # restored, not removed
    assert not (p["LIGHTTPD_CONF_AVAILABLE_DIR"] / "89-adsb-approach.conf.old").exists()
    calls = p["calls"].read_text().splitlines()
    assert not any(c.startswith(("lighty-enable-mod", "lighty-disable-mod")) for c in calls)  # nothing new to disable
    assert not any(c.startswith("systemctl reload-or-restart") for c in calls)

#!/usr/bin/env bash
# Install or update adsb-approach as two systemd services from the checkout this script lives in: the collector
# (adsb-approach.service) and the read-only web page (adsb-approach-web.service). Safe to run again: it keeps an
# existing configuration and environment file, refreshes the reference data, rebuilds the virtual environment,
# migrates the database, fetches the aircraft type database and restarts both services.
#
#   sudo UV="$(command -v uv)" deploy/install.sh
#
# [root] marks the steps that need root and [service user] the steps run as the service account. Every location
# below can be overridden from the environment; tests/test_deploy.py runs this script unprivileged into a scratch
# directory, with stand-ins for uv, systemctl, and (README: "Run it as a service") lighttpd.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd -P)"
PYTHON="${PYTHON:-/usr/bin/python3}"             # the host's Python: uv would otherwise fetch the 3.12 in .python-version
UV="${UV:-$(command -v uv || true)}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
SERVICE_USER="${SERVICE_USER:-adsb-approach}"
PREFIX="${PREFIX:-/opt/adsb-approach}"            # the virtual environment
ETC_DIR="${ETC_DIR:-/etc/adsb-approach}"          # adsb-approach.toml, airports/, type_classes.toml, nearby_airports.csv
DATA_DIR="${DATA_DIR:-/var/lib/adsb-approach}"    # approaches.sqlite, aircraft.csv.gz
ENV_FILE="${ENV_FILE:-/etc/default/adsb-approach}"
UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
FETCH_TYPEDB="${FETCH_TYPEDB:-1}"
LIGHTTPD="${LIGHTTPD:-lighttpd}"                                       # tar1090's web server, if present
LIGHTY_ENABLE_MOD="${LIGHTY_ENABLE_MOD:-lighty-enable-mod}"
LIGHTY_DISABLE_MOD="${LIGHTY_DISABLE_MOD:-lighty-disable-mod}"
LIGHTTPD_CONF="${LIGHTTPD_CONF:-/etc/lighttpd/lighttpd.conf}"
LIGHTTPD_CONF_AVAILABLE_DIR="${LIGHTTPD_CONF_AVAILABLE_DIR:-/etc/lighttpd/conf-available}"
LIGHTTPD_CONF_ENABLED_DIR="${LIGHTTPD_CONF_ENABLED_DIR:-/etc/lighttpd/conf-enabled}"

VENV="$PREFIX/venv"
CONFIG="$ETC_DIR/adsb-approach.toml"
UNITS=(adsb-approach.service adsb-approach-web.service)     # started in this order, stopped in reverse

say() { printf '==> %s\n' "$*"; }
warn() { printf 'install.sh: warning: %s\n' "$*" >&2; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
as_service_user() {
  if [ "$(id -u)" = 0 ] && [ "$SERVICE_USER" != root ]; then
    runuser -u "$SERVICE_USER" -- "$@"
  else
    "$@"
  fi
}
ensure_dir() {  # ensure_dir MODE DIR [OWNER GROUP]: created if missing; an existing directory's owner and mode are
                # left alone, since it may be shared with something else (e.g. a kept config's db_path)
  local mode="$1" dir="$2"
  if [ -d "$dir" ]; then
    return 0
  fi
  if [ "$#" -ge 4 ]; then
    install -d -m "$mode" -o "$3" -g "$4" "$dir"
  else
    install -d -m "$mode" "$dir"
  fi
}

if [ "$(id -u)" != 0 ] && [ "$SYSTEMCTL" = systemctl ]; then
  die "run it as root: sudo UV=\"\$(command -v uv)\" $0"
fi
if [ -z "$UV" ] || [ ! -x "$UV" ]; then
  die "uv not found: install it (https://docs.astral.sh/uv/) or pass UV=/path/to/uv"
fi
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 12))' || die "$PYTHON is older than Python 3.12"

# a scratch copy of the tracked files to build from, so build/, *.egg-info and bytecode never land in this checkout;
# removed whenever the script exits
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT
git -C "$SRC" ls-files -z | tar -C "$SRC" --null -T - -cf - | tar -C "$BUILD_DIR" -xf - \
  || die "could not copy the tracked files from $SRC; is it a git checkout?"

# [root] the service account: a system user that can write only to the data directory
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  say "creating system user $SERVICE_USER"
  useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

# [root] directories: only those missing are created; an existing one keeps its owner and mode. $ETC_DIR/airports
# is not among them: the reference data step below owns its full lifecycle so it can roll it back on failure.
for d in "$PREFIX" "$ETC_DIR" "$(dirname "$ENV_FILE")" "$UNIT_DIR"; do
  ensure_dir 0755 "$d"
done
ensure_dir 0750 "$DATA_DIR" "$SERVICE_USER" "$SERVICE_GROUP"

# [root] configuration, written once from the example with absolute data paths; an existing file is kept
if [ -e "$CONFIG" ]; then
  say "keeping $CONFIG"
else
  say "writing $CONFIG"
  sed -e "s|^db_path = .*|db_path = \"$DATA_DIR/approaches.sqlite\"|" \
      -e "s|^typedb_path = .*|typedb_path = \"$DATA_DIR/aircraft.csv.gz\"|" \
      "$SRC/config/adsb-approach.example.toml" > "$CONFIG.new"
  chmod 0644 "$CONFIG.new"
  mv "$CONFIG.new" "$CONFIG"
fi

# [root] location settings for the unit, written once; an existing file is kept
if [ -e "$ENV_FILE" ]; then
  say "keeping $ENV_FILE"
else
  say "writing $ENV_FILE"
  install -m 0644 "$SRC/deploy/adsb-approach.env" "$ENV_FILE"
fi

# [root] reference data from this checkout: staged as .new, swapped in, and the previous version kept as .old until
# the build below succeeds, so a failed check or a failed build leaves what was installed unchanged
say "copying airport files, type classes and the nearby-airports list into $ETC_DIR"
rm -rf "$ETC_DIR/airports.new" "$ETC_DIR/type_classes.toml.new" "$ETC_DIR/nearby_airports.csv.new"
mkdir -m 0755 -p "$ETC_DIR/airports.new"
install -m 0644 "$SRC"/config/airports/*.toml "$ETC_DIR/airports.new/"
install -m 0644 "$SRC/config/type_classes.toml" "$ETC_DIR/type_classes.toml.new"
install -m 0644 "$SRC/config/nearby_airports.csv" "$ETC_DIR/nearby_airports.csv.new"
rm -rf "$ETC_DIR/airports.old" "$ETC_DIR/type_classes.toml.old" "$ETC_DIR/nearby_airports.csv.old"
[ -e "$ETC_DIR/airports" ] && mv "$ETC_DIR/airports" "$ETC_DIR/airports.old"
[ -e "$ETC_DIR/type_classes.toml" ] && mv "$ETC_DIR/type_classes.toml" "$ETC_DIR/type_classes.toml.old"
[ -e "$ETC_DIR/nearby_airports.csv" ] && mv "$ETC_DIR/nearby_airports.csv" "$ETC_DIR/nearby_airports.csv.old"
mv "$ETC_DIR/airports.new" "$ETC_DIR/airports"
mv "$ETC_DIR/type_classes.toml.new" "$ETC_DIR/type_classes.toml"
mv "$ETC_DIR/nearby_airports.csv.new" "$ETC_DIR/nearby_airports.csv"

restore_reference_data() {  # undo the swap above: only safe to call before the .old backups are removed
  rm -rf "$ETC_DIR/airports" "$ETC_DIR/type_classes.toml" "$ETC_DIR/nearby_airports.csv"
  # `if`, not `[ -e ] && mv`: a missing .old (the first upgrade has no nearby_airports.csv.old) would make the last of
  # these, and so the function, return 1, and set -e would end the script before the caller's die printed anything
  if [ -e "$ETC_DIR/airports.old" ]; then mv "$ETC_DIR/airports.old" "$ETC_DIR/airports"; fi
  if [ -e "$ETC_DIR/type_classes.toml.old" ]; then mv "$ETC_DIR/type_classes.toml.old" "$ETC_DIR/type_classes.toml"; fi
  if [ -e "$ETC_DIR/nearby_airports.csv.old" ]; then mv "$ETC_DIR/nearby_airports.csv.old" "$ETC_DIR/nearby_airports.csv"; fi
}

# the configuration must load as the service will load it, environment file and new reference data included,
# before anything is stopped
paths="$(PYTHONPATH="$SRC" PYTHONDONTWRITEBYTECODE=1 "$PYTHON" - "$CONFIG" "$ENV_FILE" "$ETC_DIR" <<'PY'
import sys
from pathlib import Path

from adsb_approach.config import load_config

config, env_file, etc_dir = sys.argv[1:]
env = {}
for line in Path(env_file).read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
cfg = load_config(config, env=env)
db = Path(cfg.service.db_path).resolve()
if db.is_relative_to(Path(etc_dir).resolve()):
    sys.exit(f"{config}: db_path resolves to {db}, inside {etc_dir}; set an absolute db_path in a data directory")
print(f"{db}\t{cfg.service.typedb_path or ''}\t{cfg.service.port}")
PY
)" || {
  restore_reference_data
  die "$CONFIG does not load; nothing was stopped"
}
IFS=$'\t' read -r DB_PATH TYPEDB_PATH SERVICE_PORT <<< "$paths"

# [root] build the wheel before anything is stopped, so a failure here (typically: no network and a cold uv cache)
# leaves the running service, its virtual environment and its reference data untouched
say "building the wheel"
UV_PYTHON_DOWNLOADS=never "$UV" build --python "$PYTHON" --wheel "$BUILD_DIR" || { restore_reference_data; die "build failed; nothing was stopped"; }
WHEEL="$(ls "$BUILD_DIR"/dist/*.whl)"

# [root] the venv below is built with --system-site-packages, so FastAPI, Starlette, Pydantic and uvicorn must
# already be importable by $PYTHON, from Debian's packages, before anything is stopped
"$PYTHON" -c 'import fastapi, uvicorn' \
  || { restore_reference_data; die "$PYTHON cannot import fastapi and uvicorn; install them: sudo apt-get install --no-install-recommends python3-fastapi"; }
rm -rf "$ETC_DIR/airports.old" "$ETC_DIR/type_classes.toml.old" "$ETC_DIR/nearby_airports.csv.old"      # build and the dependency check succeeded: the swap above is final

# [service user] the aircraft type database, fetched before anything is stopped so the download never extends the
# outage: into a temporary file beside the old one, renamed over it once complete. An upgrade already has a working
# adsb-approach to fetch with; a first install does not, but then there is also nothing running to stop, so it is
# fetched after the venv below instead.
FETCHED_TYPEDB=0
if [ "$FETCH_TYPEDB" = 1 ] && [ -n "$TYPEDB_PATH" ] && [ -x "$VENV/bin/adsb-approach" ]; then
  say "fetching $TYPEDB_PATH"
  if as_service_user "$VENV/bin/adsb-approach" fetch-typedb --out "$TYPEDB_PATH"; then
    FETCHED_TYPEDB=1
  else
    say "fetch-typedb failed: the service runs without aircraft types until this script runs again"
  fi
fi

# [root] stop the services before their code is replaced, the page first
for ((i = ${#UNITS[@]} - 1; i >= 0; i--)); do
  if [ -e "$UNIT_DIR/${UNITS[i]}" ]; then
    say "stopping ${UNITS[i]}"
    "$SYSTEMCTL" stop "${UNITS[i]}" || true
  fi
done

# [root] the program: a fresh virtual environment on the host's Python, installing only the wheel built above
# offline; --system-site-packages gives it FastAPI, Starlette, Pydantic and uvicorn from Debian's packages rather
# than PyPI. Bytecode is compiled now, not at run time: ProtectSystem=strict makes the venv read-only once the unit
# starts.
say "installing $WHEEL into $VENV with $PYTHON"
UV_PYTHON_DOWNLOADS=never "$UV" venv --clear --system-site-packages --python "$PYTHON" "$VENV"
UV_PYTHON_DOWNLOADS=never "$UV" pip install --python "$VENV/bin/python" --offline --no-deps --compile-bytecode "$WHEEL"

# [service user] the database schema
say "migrating $DB_PATH"
ensure_dir 0750 "$(dirname "$DB_PATH")" "$SERVICE_USER" "$SERVICE_GROUP"
as_service_user "$VENV/bin/adsb-approach" migrate --db "$DB_PATH"

# [service user] the aircraft type database, first install only: an upgrade already fetched it above, before the
# service stopped
if [ "$FETCH_TYPEDB" = 1 ] && [ -n "$TYPEDB_PATH" ] && [ "$FETCHED_TYPEDB" = 0 ]; then
  say "fetching $TYPEDB_PATH"
  as_service_user "$VENV/bin/adsb-approach" fetch-typedb --out "$TYPEDB_PATH" \
    || say "fetch-typedb failed: the service runs without aircraft types until this script runs again"
fi

# [root] the units, from their templates with these paths filled in; then enable and (re)start the services, the
# collector first, since the page reads the database files the collector keeps open
for unit in "${UNITS[@]}"; do
  say "installing $UNIT_DIR/$unit"
  sed -e "1s|.*|# Written by install.sh from deploy/$unit; changes made here are overwritten by the next install.|" \
      -e "s|@USER@|$SERVICE_USER|g" -e "s|@GROUP@|$SERVICE_GROUP|g" -e "s|@VENV@|$VENV|g" -e "s|@CONFIG@|$CONFIG|g" \
      -e "s|@ENV_FILE@|$ENV_FILE|g" -e "s|@DATA_DIR@|$(dirname "$DB_PATH")|g" \
      "$SRC/deploy/$unit" > "$UNIT_DIR/$unit.new"
  chmod 0644 "$UNIT_DIR/$unit.new"
  mv "$UNIT_DIR/$unit.new" "$UNIT_DIR/$unit"
done
"$SYSTEMCTL" daemon-reload
for unit in "${UNITS[@]}"; do
  "$SYSTEMCTL" enable "$unit"
  "$SYSTEMCTL" restart "$unit"
done

# [root] the page behind lighttpd at /adsb-approaches/ (README: "Run it as a service"), proxied to the web unit
# above: skipped quietly when lighttpd is not installed. A lighttpd problem here must never take down the collector,
# the page's own port, or tar1090, so a failing configuration test rolls back what this step did and only warns;
# either way the install still finishes and exits 0.
if [ ! -d "$LIGHTTPD_CONF_AVAILABLE_DIR" ] || ! command -v "$LIGHTTPD" >/dev/null 2>&1; then
  say "lighttpd not found: adsb-approach stays reachable on its own port only"
else
  lighttpd_mod_enabled() {  # a conf-enabled file for $1 already exists, whatever numeric prefix lighty-enable-mod used
    local f
    for f in "$LIGHTTPD_CONF_ENABLED_DIR"/*"$1".conf; do
      [ -e "$f" ] && return 0
    done
    return 1
  }

  LIGHTTPD_SITE="$LIGHTTPD_CONF_AVAILABLE_DIR/89-adsb-approach.conf"
  say "writing $LIGHTTPD_SITE"
  rm -f "$LIGHTTPD_SITE.old"
  if [ -e "$LIGHTTPD_SITE" ]; then
    mv "$LIGHTTPD_SITE" "$LIGHTTPD_SITE.old"       # a hand-made or earlier copy: kept until the test below passes
  fi
  sed -e "1s|.*|# Written by install.sh from deploy/lighttpd-adsb-approach.conf; changes made here are overwritten by the next install.|" \
      -e "s|@PORT@|$SERVICE_PORT|g" "$SRC/deploy/lighttpd-adsb-approach.conf" > "$LIGHTTPD_SITE.new"
  chmod 0644 "$LIGHTTPD_SITE.new"
  mv "$LIGHTTPD_SITE.new" "$LIGHTTPD_SITE"

  to_enable=()
  for mod in proxy adsb-approach; do
    lighttpd_mod_enabled "$mod" || to_enable+=("$mod")
  done
  if [ "${#to_enable[@]}" -gt 0 ]; then
    say "enabling lighttpd module(s): ${to_enable[*]}"
    "$LIGHTY_ENABLE_MOD" "${to_enable[@]}"
  fi

  if lighttpd_check="$("$LIGHTTPD" -tt -f "$LIGHTTPD_CONF" 2>&1)"; then
    "$SYSTEMCTL" reload-or-restart lighttpd
    say "adsb-approach is behind lighttpd at /adsb-approaches/"
  else
    if [ "${#to_enable[@]}" -gt 0 ]; then
      "$LIGHTY_DISABLE_MOD" "${to_enable[@]}"
    fi
    rm -f "$LIGHTTPD_SITE"
    if [ -e "$LIGHTTPD_SITE.old" ]; then
      mv "$LIGHTTPD_SITE.old" "$LIGHTTPD_SITE"
    fi
    warn "lighttpd -tt -f $LIGHTTPD_CONF failed; adsb-approach stays reachable on its own port only. Output:"
    printf '%s\n' "$lighttpd_check" >&2
  fi
  rm -f "$LIGHTTPD_SITE.old"
fi

say "done; follow them with: journalctl -u adsb-approach -u adsb-approach-web -f"

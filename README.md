# adsb-approach

Scores approaches to configured runways from a [readsb](https://github.com/wiedehopf/readsb) ADS-B receiver feed.
Each approach gets an outcome (landed, lost on final, lost, go-around, touch-and-go, runway change or abandoned),
its deviation from the published glide path and localizer course at every point and at the 1,000 ft and 500 ft
gates, and stability flags. Attempts by one flight at one airport form an arrival, so a go-around followed by a
landing is one arrival with two attempts. Recorded data replays through the same pipeline.

**Status:** the scoring pipeline, the reference-data generator, `replay`, the live collector `run`, the fleet norms
and the read-only page `web` work. They are tested against eight hours of recorded data and a replayed live feed.

**Scope:** made for US airports. Glide paths, threshold crossing heights and localizer courses come from the FAA's
CIFP (see [Reference data](#reference-data)). An airport CIFP does not cover is built from OurAirports alone, with a
3° glide path and a 50 ft threshold crossing height assumed, so its deviations are only as good as that guess. Speeds
are ground speed corrected by the METAR wind, not indicated airspeed. How low an approach can be followed depends on
the receiver: an airport whose final approach is below the receiver's horizon is mostly scored lost on final.

## Requirements

- [uv](https://docs.astral.sh/uv/). On first use it installs the Python version pinned in `.python-version`
  (3.12) and the dependencies, FastAPI and uvicorn, which serve the web page. Python 3.12 to 3.14 are tested.
- [Node.js](https://nodejs.org/), optionally: the test suite runs the page's JavaScript unit tests with it, and skips
  them without it.
- Network access only for `fetch-typedb`, `fetch-metar` and the reference-data downloads below.

## Develop

```sh
uv sync
uv run pytest
```

The suite takes one to two minutes. The slowest tests are the golden replay of the 2026-09-12 fixtures, the deploy
tests and two rebuild-worker tests that wait about 5 s each.

One test is opt-in: `tests/test_readsb_contract.py` starts a throwaway `readsb` fed a slice of a recorded Beast dump
and runs the poller's own query and parsing against its API port. It skips unless `readsb` is on the PATH and
`ADSB_APPROACH_BEAST_CORPUS` names a dump, so it runs only where you have both; the file's docstring has the
command.

## Replay the 2026-09-12 fixtures

```sh
uv run adsb-approach replay --config config/adsb-approach.example.toml \
    --kind chunks --from tests/fixtures/raw/tar1090-chunks-20260912 \
    --metar tests/fixtures/metar-20260912.json --db data/replay.sqlite --run-tag fixtures
```

The command prints counts by runway and outcome, lists the go-arounds and runway changes, and writes a run to
`data/replay.sqlite`, creating `data/` if needed. The fixture holds eight hours of tar1090 history (11:31Z to
19:28Z) from a receiver in the San Francisco Bay Area. It includes United 2177's go-around at SFO (hex a44540,
15:27Z) and United 2432's sidestep from 28L to 28R (hex ac2706). Once `fetch-typedb` (below) has written
`data/aircraft.csv.gz`, replay also labels each flight with its aircraft type.

Other kinds of source:

- `--kind jsonl` reads lines captured from readsb's JSON position port (`--net-json-port`), plain or gzipped.
  `tests/fixtures/raw/jsonport-20260912-2011Z.jsonl.gz` holds 45 minutes from 20:11Z.
- `--kind traces` reads a readsb globe-history day directory (`--write-globe-history`), for example
  `/var/globe_history/2026/09/12`, with its `traces/<xx>/trace_full_<hex>.json` files.

Pass `--metar` whenever you have observations for the replayed period. Without them, heights assume the
standard pressure of 1013.25 hPa. That puts them tens of feet off, which can change an outcome near a
threshold. Every replay adds a run, and replaying into an existing database keeps the earlier runs. To look at
the results:

```sh
sqlite3 data/replay.sqlite "SELECT hex, callsign, airport, runway, outcome, lowest_hat_ft FROM approaches WHERE outcome = 'go_around'"
```

`flags` is a bitmask; `adsb_approach/model.py` names the bits.

**Emergency squawks and ADS-B emergency state**, latched onto a flight for the rest of its life once one has held for
30 s, and then stamped onto every attempt the flight closes, wherever it is on the approach when it latches. Never
cleared by any code path: a false positive is removed by hand in SQL.

- `EMERGENCY_GENERAL` -- squawk 7700, or ADS-B emergency state `general`.
- `EMERGENCY_NORDO` -- squawk 7600, or `nordo`: radio failure.
- `EMERGENCY_UNLAWFUL` -- squawk 7500, or `unlawful`: unlawful interference.
- `EMERGENCY_DOWNED` -- ADS-B `downed`; no squawk equivalent.
- `MINIMUM_FUEL` -- ADS-B `minfuel`: shown as attention, not drawn red like the four above.

## Collect live

`adsb-approach run` reads readsb's JSON position port (`--net-json-port`), scores approaches as they happen and
stores them in `[service] db_path`. It runs until it receives SIGTERM or SIGINT:

```sh
cp config/adsb-approach.example.toml config/adsb-approach.toml     # then set the receiver and the paths
uv run adsb-approach fetch-typedb --out data/aircraft.csv.gz
uv run adsb-approach run --config config/adsb-approach.toml
```

- **Weather.** It fetches METAR observations for the enabled airports every `metar_interval_s` (600 s). A failed
  fetch keeps the last observations. Once the altimeter setting is older than `qnh_max_age_min`, heights fall back
  to standard pressure and approaches carry `QNH_ASSUMED`; after `metar_max_age_min` they lose the temperature
  correction. `--metar FILE` preloads observations, and `metar_interval_s = 0` turns polling off.
- **Live merge from adsb.lol.** Off unless `[external] enabled = true`; the climb-out below is the one part that works
  without it. While an aircraft is on an approach, `run` asks adsb.lol's live service where it is and merges the
  answer into its own tracking as it arrives. An approach the receiver loses low is then followed to the ground, and
  landed or go-around is decided from the merged track.
  - **What it watches, and when:** an aircraft goes on the watch list when its approach is established, including the
    new attempt a runway change starts. Nothing is polled while the list is empty.
    - A landing is watched until it closes. The matcher closes a landing only once it has rolled out (touched down
      and at or below 30 kt), so it needs no rule of its own.
    - A go-around or touch-and-go is watched until its next attempt is established, which puts that attempt on the
      list in its own right, or until `watch_max_min` (20 min) after it closed, whichever comes first.
    - Anything else is watched until its attempt closes: lost, abandoned or a runway change.
    - A restart begins with an empty list: the next start closes the approaches the stop cut short as `lost` with
      `INTERRUPTED`, so none is left to watch.
  - **What it asks:** at most one request every `poll_interval_s` (2 s), to `[external] url`
    (`https://re-api.adsb.lol/`) as `?find_hex=<hex>,<hex>,...`. A request that runs long delays the next, so
    requests never overlap.
    - **Only the aircraft the receiver has lost:** a request asks about the watched aircraft the receiver has given no
      position of for more than `quiet_s` (8 s), all at once, and none goes out while the receiver places every
      watched aircraft. adsb.lol adds to a track only where the receiver has lost it, and a key's quota is counted in
      requests. `quiet_s = 0` asks about every watched aircraft. The collector tells the watch list of each position
      it processes, and a start counts as heard, since it comes from a receiver position.
    - **What that saves:** on three busy days of a receiver near four airports (2026-10-25 to 10-27), polling every
      watched aircraft at 8 s made about 7,200 requests a day. Asking only about aircraft quiet for 8 s would have
      made 13% fewer, and kept 95% of the airborne adsb.lol positions those approaches used. The saving is small
      because by day some watched aircraft is nearly always in its tail, below the receiver's coverage, which is
      exactly when adsb.lol is asked.
    - **Fitting a key's quota:** a free receiver key allowed 18,000 requests a week and 72,000 a month in November
      2026, about 2,400 a day. With `quiet_s = 8`, that receiver's traffic fits `poll_interval_s = 22`, about 2,300
      a day. At 12 s it would have needed some 4,200.
    - re-api is readsb's API port. adsb.lol serves it only to an address that feeds adsb.lol, and since 2026-10-28
      only with a key: keyless requests from the feeding address now answer 401 `api key required`.
    - **The v2 API instead:** a `url` ending in `/v2/hex/` (`https://api.adsb.lol/v2/hex/`) asks adsb.lol's keyed
      API, with the hexes comma-separated in the path. That API hands them to re-api's `find_hex` and answers in its
      v2 shape (`ac`, `now` in milliseconds), which the poller reads as re-api's. Tested 2026-10-24: the anonymous key
      is refused (403) by re-api but accepted here. Keyless, this API allows only 4 requests a second, 60 a minute
      and 500 a day, per its `x-ratelimit-*` headers. Keyed answers send no such headers, so the key page's usage
      figure is the only gauge.
    - **The key:** `api_key_file` names a file holding a key from <https://www.adsb.lol/mmb>, sent as the `x-api-key`
      header; empty sends none. re-api checks a key it is sent, so a wrong one is a 401. The file is read again at
      every poll, so a new key needs no restart, and the key never reaches a log.
    - **Its manners:** the User-Agent is `adsb-approach/<version> (+https://github.com/z2amiller/adsb-approach)`.
      `poll_interval_s` is the knob if adsb.lol objects to the load.
  - **How it merges:** the receiver's own positions and adsb.lol's go through one tracker, matcher and scorer.
    - **The clock:** adsb.lol stamps positions about 0.1 s later than the receiver, so each moves back by
      `clock_offset_s` (0.10 s) first.
    - **The "newer than" rule:** an adsb.lol position joins the stream only when it is newer than anything already
      seen for that aircraft, from either origin. That drops a position repeated across polls and adsb.lol's copies
      of the receiver's own. A local position older than an adsb.lol one already accepted is dropped too, so each
      aircraft's order stays monotonic. While the receiver hears the aircraft, adsb.lol adds little; when it goes
      quiet, adsb.lol carries the track.
    - **What it decides:** `lost_timeout_s` (45 s) applies to the merged stream, so an approach is lost only when
      neither origin has heard the aircraft for that long. adsb.lol's points count towards the gates, the stability
      flags, go-around detection and the outcome, as the receiver's own do. The coverage statistics keep to the
      receiver's own points.
  - **The labels:** every point keeps its `origin`, `local` or `adsb.lol`, and the drawer draws adsb.lol's as a lighter
    line. Three flags say what adsb.lol supplied.
    - `EXTERNAL_TAIL`: the approach's last point is adsb.lol's, and either the receiver had been silent for more than
      `lost_timeout_s` at the end, or the approach is a go-around or touch-and-go whose lowest point came after the
      receiver's last. A go-around closes only a few seconds into the climb, so the second case catches one the
      receiver lost low, where only adsb.lol saw the bottom and the climb, although the receiver's silence at the
      close was under `lost_timeout_s`.
    - `EXTERNAL_GAPS`: adsb.lol points sit inside the receiver's own track, or in an end stretch that is not a tail.
    - `EXTERNAL_OUTCOME`: adsb.lol's point decided the outcome of an approach that also has the tail. A landing so
      decided has `landed_confirmed_by = adsb.lol` and reads `landed 28L · adsb.lol`.
    - The coverage page's adsb.lol column counts `EXTERNAL_TAIL` only, for approaches from a run that merged (see
      Browse approaches).
  - **The climb-out, with the merge on or off:** a go-around's or touch-and-go's record continues after the close
    whether or not `enabled` is set: from the receiver's own positions alone when it is off, from both origins when
    it is on. It lasts until the aircraft has flown as far again as it was from the threshold when the go-around
    began, and at least 1 nm. It stops sooner when the next attempt is established, `[external] watch_max_min`
    passes after the close, or the aircraft goes unheard for more than `lost_timeout_s`, on or off alike; whatever
    reappears after such a gap is not added to it.
    - **For example:** a go-around begun 3 nm out is followed for 3 nm, one begun at 0.4 nm for 1 nm.
    - **For the charts only:** the points are stored with `charts_only` and are never scored, so no gate, flag,
      outcome or norm reads them, and the fleet rebuild and `reflag` leave them out.
    - A landing gets nothing after its close.
  - **When adsb.lol refuses or is down:** a failure never stops the collector, which carries on with the receiver
    alone, so approaches end as they would without the merge.
    - **Pauses:** a 401, 403, 429 or 5xx pauses polling, logged with the status. The pause lasts as long as the
      answer's `Retry-After` asks (seconds or a date, capped at an hour), and `backoff_min` (15 min) when it sends none.
      Keyless re-api answered 429 after about 60 requests at a 2 s interval and about 130 at 5 s (2026-10-24), before
      adsb.lol began requiring a key. With a key, set `poll_interval_s` from its quota, as above. A 401 says the key was
      refused. A key file that can't be read, or is empty, pauses the same way without sending a request,
      and its error names the file. The pause is read back from the database, so it survives a restart.
    - **Skips:** a timeout, a network error, any other status (a redirect included, which is never followed, so the
      key stays with the configured host), or a 200 that isn't a re-api reply (a verification page, JSON without `now`
      or `aircraft`) skips that one poll. Every one is recorded in `external_polls`; the log says at info that an
      outage began and, when polls succeed again, how many failed.
    - **Where it shows:** the log, in lines starting `poller:`; a banner on the page while polling is paused; and
      `/api/health` under `external`, with whether it is enabled, how many aircraft the last poll asked about, the
      last success, when and why a pause ends, and the failures of the last hour.
  - **What it stores:** every poll goes into `external_polls`, and each distinct position it returned into
    `external_positions`, whether or not the "newer than" rule took it. `adsb-approach replay ... --external-from
    <live database>` reads them back and merges them through the same watch list, in the order they arrived, so the
    replay lands what the live run landed. A run records whether it merged (`runs.external_merge`).
  - **The data:** what is fetched stays in the database. Don't publish it, and don't copy it into the repository or
    its test fixtures; the tests build synthetic replies. adsb.lol's docs license their data under ODbL 1.0, and
    publishing any of it would be a separate decision. Replays never fetch.
- **Fleet norms.** For each runway, the norm is the 25th, 50th and 75th percentiles of height and wind-corrected
  speed in each 0.5 nm bin, over the approaches of the last `window_days` (30) that reached the runway.
  - **The key:** the aircraft's own type once it has `min_samples` (30) approaches of its own in the window, else
    its traffic class, else every aircraft on that runway.
  - **When it runs:** every `rebuild_interval_min` (60) in a worker thread, and on demand with
    `adsb-approach fleet-rebuild --config FILE` — add `--include-replays` to build norms from a database of replays
    rather than a collector's. The first rebuild computes every coverage day of the window in one pass (3.0 s for
    five days and 2,302 approaches on a Mac); later ones skip the days that are finished.
  - **What it gives:** each observed gate a height and a speed delta from its norm, the `FAST` and `SLOW` flags
    beyond `[stability] fast_delta_kt` and `slow_delta_kt`, and the band the page draws behind the track. `FAST` and
    `SLOW` are raised only when the norm used is the aircraft's own type or its traffic class: an approach judged
    against the every-aircraft norm still gets its deltas and shows them, but is never flagged, since that norm's
    speed spread is about twice a single type's. The deltas are null until the first rebuild.
  - **Catching up what is already stored:** `adsb-approach reflag --config FILE` gives the approaches already
    stored their bins, deltas, flags and norm — `--all` for every one of them rather than just the last 30 days.
    Run `fleet-rebuild` first, then `reflag`; `reflag` with no norms built yet is a no-op. It never touches
    outcomes, points or the gates' measured values, and writes no events, so the page picks up the change on its
    next load.
  - **Watching it:** `/api/health` carries the norms' `built_at` and their age in seconds, so a rebuild that has
    quietly stopped is visible; a database with no norms built yet is normal and does not change the reported
    status.
- **Feed.** When the feed drops, it reconnects after 1 s, doubling the wait up to 30 s.
- **Stopping.** Writes commit once a second. On SIGTERM, approaches in progress close as at the end of a replay and
  the run ends. After a crash or a kill, the next start closes the approaches left open as `lost` with the
  `INTERRUPTED` flag.
- **Log.** One line per completed or reclassified approach, and a health line every minute: the age of the last line
  from readsb, positions per second, open attempts, the age of the stalest METAR and memory in use.

### Location settings from the environment

The settings that locate your receiver can come from the environment instead of the configuration file, so the
file can be shared. `run` and `web` read these variables at start; other commands ignore them.

| Variable | Replaces |
|---|---|
| `ADSB_APPROACH_SOURCE_HOST` | `[source] host` |
| `ADSB_APPROACH_SOURCE_PORT` | `[source] port` |
| `ADSB_APPROACH_TAR1090_URL` | `[service] tar1090_url` |

A variable is checked like the value it replaces. A port that is not a whole number, an empty value, or any other
variable starting with `ADSB_APPROACH_` stops the service with an error that names it.

## Browse approaches

`adsb-approach web` serves a read-only page on `[service] bind` and `port` (127.0.0.1:8090 in the example
configuration, so only the machine itself can reach it): the aircraft on final now, the log of arrivals with its
filters kept in the URL, and a drawer with each approach's charts, gates, METAR and a link to its trace in tar1090.
It reads the database the collector writes and never writes to it, so it runs beside `run`, or on its own over a
replay:

```sh
uv run adsb-approach web --config config/adsb-approach.example.toml --db data/replay.sqlite --bind 127.0.0.1
```

Then open http://127.0.0.1:8090/. `--db`, `--bind` and `--port` override the configuration. The page has no login:
anyone who can reach the port can read it. To reach it from the LAN directly, set `bind = "0.0.0.0"`, or put it behind
the proxy the install sets up (see Run it as a service), and keep it off the internet. "Open in tar1090" goes to
`[service] tar1090_url`, except that while that names 127.0.0.1, as in the example, it goes to `/tar1090/` on the host
you opened the page on. Beside it, "Open on adsb.lol" (labelled from `[external] globe_url`'s host,
`https://globe.adsb.lol/` by default) shows the same aircraft over the same timeframe on adsb.lol's own globe, for
comparing the receiver's own track — which can miss the low end of an approach — against adsb.lol's multi-feeder one;
an empty `globe_url` turns it off, independent of `[external] enabled`, which only switches the live merge. A banner
says when the database cannot be read, when the collector has stopped, and when nothing has been recorded for 30
minutes. Points from adsb.lol draw as a lighter solid line in the drawer's charts, and the legend says so. An outcome
adsb.lol decided reads, for example, `landed 28L · adsb.lol`. A note beside the "Open in tar1090" link says how many
of the approach's positions were merged live from adsb.lol, and a banner shows while polling is paused. Every page
ends with the notice adsb.lol's licence, the ODbL 1.0, asks of a public page drawn from its data (§4.3), in the
licence's own example wording.

An approach flown along a procedure path carries a path chip, "Quiet Bridge" or "Quiet Bridge → ILS" after a switch, on
its log row and, while it is on the path, on its live card. The drawer adds where the path came from and what its
cross-track chart is measured from. The log's Approach filter (All, Straight-in or Path; `approach_kind` in the URL)
narrows it to either kind.

The **coverage page**, at `/coverage` and linked from the header, is about the receiver rather than the flights:
one small chart per runway, busiest first, of the median height above the threshold at which the receiver still
saw an aircraft, day by day, with the day's approach count drawn as bars behind it. Under them, one before/after
table for each change recorded in `[[coverage.events]]`, and a stacked chart of how each airport's approaches
ended. It shows what the last fleet rebuild wrote — a day in progress appears when the rebuild next runs — and
every number comes from `coverage_daily` or a count over `approaches`, never from scoring points again. The
adsb.lol column is the share of a day's approaches that ended on adsb.lol's positions, the receiver having gone
quiet for more than `lost_timeout_s` or, for a go-around, before its lowest point (`EXTERNAL_TAIL`): it reads the
antenna's shortfall from the other side and should fall as the antenna improves.
`GET /api/stats?days=30` (or `days=all`) answers with the coverage days of the span, the daily outcome counts per
airport, the configured marks and a summary of the norms in force, from one read of the database.

The **Find a flight page**, at `/search` and linked from the header, looks up one aircraft's sessions by a callsign or
registration prefix (hyphens ignored, at least 2 characters, so `N-012` finds `N012AB`) or an exact 6-digit hex, newest
first, within a window from the last hour to all time, or a range of dates. A session with approaches shows them as the
arrivals log does. One without shows as a grey row, which reads the session's readsb trace for what the aircraft did:
where it was heard, its lowest and highest points, its squawks, and any emergency squawk or ADS-B emergency state it
held for 30 seconds, which turns the row red. A grey row that says "no approach logged" is an arrival at an airport this
analyser scores, which most likely means the matcher missed it; an arrival at any other field has no approach by design.
A session with fewer than 3 points, or heard for under 2 minutes, is labelled "heard briefly" with its lowest altitude
and nearest field rather than a direction, which that much track cannot show. An on-ground report counts only when the
geometric height agrees or is unknown, since some transponders report it in flight. The page reads `[service]
globe_history_dir` and `live_traces_dir` for those traces, and the nearby-airports list under Reference data gives the
labels. `GET /api/search?q=&since=&until=&limit=&cursor=` returns the same rows as JSON: `rows`, `counts` over every
match, and a `next_cursor` for the next page.

### Days and marks

- `[service] timezone` is an IANA zone name (`America/Los_Angeles`); unset means the host's own zone. It decides
  what a coverage day is, so it matches the local times the arrivals page shows. Daylight saving makes two days a
  year 23 or 25 hours long, which is accepted: a coverage day means "that calendar day here".
- `[[coverage.events]]` records each dated change to the receiver, with this TOML example:

  ```toml
  [[coverage.events]]
  at = 2026-10-04
  label = "new antenna"
  ```

- Each mark draws a line on every chart and a before/after table under them, comparing the period since the
  previous mark with the period until the next. Add one whenever the aerial, the feed line, the amplifier or the
  receiver changes — including the day the adsb.lol live merge was switched on (`label = "adsb.lol live merge"`),
  which moves the adsb.lol column sharply: from then on it counts endings only, not every approach that has an
  adsb.lol point.
- Neither setting changes a score or an outcome, so neither joins `runs.config_hash`.

## Run it as a service

### Linux with systemd (Debian family: Raspberry Pi OS, Debian, Ubuntu)

`deploy/install.sh` installs the checkout it lives in as two services, `adsb-approach` (the collector) and
`adsb-approach-web` (the page). It needs root,
[uv](https://docs.astral.sh/uv/), and a git clone: it copies the files git tracks, so a `git archive` extraction or
a plain copy of the directory stops it with "is it a git checkout?". FastAPI, Starlette, Pydantic and uvicorn come
from the distribution's apt packages rather than PyPI, so install them first:

```sh
sudo apt-get install --no-install-recommends python3-fastapi
```

On Ubuntu 26.04 that gives FastAPI 0.118.0, Starlette 0.50.0, uvicorn 0.38.0 and Pydantic 2.12.5. Development pins
those versions in `[tool.uv] constraint-dependencies`, and CI tests them on Python 3.12 to 3.14. Older distribution
versions, such as Debian 13's FastAPI 0.115 and uvicorn 0.32, are not tested any more. It is safe to run
`install.sh` again after pulling changes:

```sh
sudo UV="$(command -v uv)" deploy/install.sh
```

| Path | Holds | Owner |
|---|---|---|
| `/opt/adsb-approach/venv` | the program, in a virtual environment on `/usr/bin/python3` | root |
| `/etc/adsb-approach/adsb-approach.toml` | the configuration, written once from the example | root |
| `/etc/adsb-approach/airports/`, `type_classes.toml`, `nearby_airports.csv` | reference data, replaced once the configuration check passes | root |
| `/etc/default/adsb-approach` | the location settings above, as commented-out examples; written once | root |
| `/var/lib/adsb-approach/` | `approaches.sqlite` and `aircraft.csv.gz` | `adsb-approach` |
| `/etc/systemd/system/adsb-approach.service`, `adsb-approach-web.service` | the units, replaced on every run | root |
| `/etc/lighttpd/conf-available/89-adsb-approach.conf` | the proxy site, if lighttpd is installed; replaced on every run | root |

Each run:

1. [root] Creates the `adsb-approach` system user if it is missing, and the directories.
2. [root] Writes the configuration and the environment file if they are missing. It then swaps in the airport files,
   type classes and nearby-airports list and checks that the configuration loads, environment file and new
   reference data included, with `db_path` outside `/etc`. If the check fails, the reference data swap is rolled
   back and it stops before touching the running service.
3. [root] Builds the wheel from a copy of the checkout, so the checkout is never left holding build artifacts, and
   checks that `$PYTHON` can import FastAPI and uvicorn. If either fails, the reference data swap is rolled back too,
   and nothing else has been stopped or replaced; a failed import check names the fix
   (`sudo apt-get install --no-install-recommends python3-fastapi`).
4. [`adsb-approach`] On an update, fetches the aircraft type database now, before anything is stopped, using the
   install that is still running.
5. [root] Stops the page, then the collector, rebuilds the virtual environment with
   `uv venv --clear --system-site-packages --python /usr/bin/python3` and installs the wheel into it offline, with
   its bytecode compiled ahead of time.
6. [`adsb-approach`] Migrates the database, and on a first install, fetches the aircraft type database (an update
   already fetched it in step 4, before the service stopped).
7. [root] Installs the units, then enables and restarts the collector, then the page. The page's unit may read
   the database but write nothing at all.
8. [root] If lighttpd (tar1090's web server) is installed, writes the proxy site from its template, enables its
   `proxy` and `adsb-approach` modules (only those not already enabled, so a second run changes nothing), and tests
   the whole configuration with `lighttpd -tt`. A passing test reloads lighttpd, putting the page at
   `http://<host>/adsb-approaches/`. A failing test disables what this step enabled, restores the previous site file
   (or removes the new one if there was none), and only warns with the test's output — the collector and page above
   are unaffected either way, and the install still exits 0. If lighttpd is not installed, this step is skipped and
   the page stays reachable only on its own port, `[service] port` (8090 in the example configuration), which the
   example's `bind = "127.0.0.1"` limits to the host itself.

- **Behind lighttpd.** Needs lighttpd 1.4.46 or later, for `proxy.header`'s `map-urlpath` (an older lighttpd cannot
  rewrite `/adsb-approaches/` to `/` before proxying). `server.stream-response-body = 2` keeps the live event stream
  flowing to the browser as it happens; without it, lighttpd buffers the whole response, and for that stream the
  response never ends. The example configuration's `bind = "127.0.0.1"` keeps the page off the LAN directly,
  reachable only through lighttpd; set `bind = "0.0.0.0"` to reach it on `[service] port` as well.

`PYTHON`, `PREFIX`, `ETC_DIR`, `DATA_DIR`, `ENV_FILE` and `SERVICE_USER` change the defaults. Follow the logs with
`journalctl -u adsb-approach -u adsb-approach-web -f`.

**Enable the live merge.**

- `install.sh` writes `/etc/adsb-approach/adsb-approach.toml` once from the example and keeps it, so the merge stays
  off, as the example has it, until you turn it on there. Settings a later release adds don't appear in that file;
  copy them from the example.
- To turn it on, set `enabled = true` in the file's `[external]` section as root, adding the section from the
  example if the file has none. `api_key_file` is optional, and names a file the service user can read; the path
  here is only an example:

  ```toml
  [external]
  enabled = true
  api_key_file = "/etc/adsb-approach/adsb-lol.key"
  ```

  Then restart both units, `sudo systemctl restart adsb-approach adsb-approach-web`. The page reads the same file,
  so its health report says whether the merge is on.
- To check it: `journalctl -u adsb-approach` shows `merging adsb.lol positions` at start. While an approach is open,
  `curl -s http://127.0.0.1:8090/api/health` shows `external.enabled` true, `external.watching` above 0 and
  `external.last_success_at` within a few seconds, and `external.paused_until` null. An approach the receiver loses
  low should now close `landed` with `EXTERNAL_TAIL` once adsb.lol carries it down, instead of `lost_on_final`.
- The `health:` line in the log, once a minute, then ends with `adsb.lol kept N, dropped N, local dropped N`: the run's
  totals of adsb.lol positions taken, adsb.lol positions dropped as not newer than the aircraft's latest, and the
  receiver's positions dropped as older than an adsb.lol one already taken. `dropped` far above `kept` is normal,
  since adsb.lol repeats a position until it has a newer one.
- To turn it off, set `enabled = false` and restart the collector.

#### Stop, update, roll back and remove

All of these need root.

- **Stop for now:** `sudo systemctl stop adsb-approach`. It starts again at boot; approaches in progress close as
  `lost` or `lost_on_final`, and the run ends.
- **Stop and keep it off:** `sudo systemctl disable --now adsb-approach`. Turn it back on with
  `sudo systemctl enable --now adsb-approach`.
- **Update:** pull the new commit, then run `deploy/install.sh` again as above. The configuration and environment
  files are kept, and the aircraft type database is fetched before the service stops (see step 4), so the outage is
  just steps 5 to 7. If you may want to roll back, back up the database first, because a migration can't be undone;
  this is safe while the collector runs:

  ```sh
  sudo -u adsb-approach sqlite3 /var/lib/adsb-approach/approaches.sqlite \
    ".backup /var/lib/adsb-approach/before-update.sqlite"
  ```

- **Roll back:** check out an earlier release or commit and run `install.sh` again. A migration is not undone by a
  rollback, so when the upgrade changed the schema and that matters, stop the services and restore the database
  backup from before the upgrade. An older version also rejects configuration keys added since, so remove any you
  set. Check out the newer version and run `install.sh` again to return.
- **Moved `db_path`?** Run `install.sh` again rather than just editing the configuration and restarting. It creates
  the new directory owned by the service user, and regenerates the unit's `ReadWritePaths`, which
  `ProtectSystem=strict` otherwise limits to wherever `db_path` pointed at the last install.
- **Remove completely** (readsb and tar1090 are unaffected):

  ```sh
  sudo systemctl disable --now adsb-approach-web adsb-approach
  sudo lighty-disable-mod adsb-approach                                     # only if install.sh set up the lighttpd site
  sudo rm -f /etc/lighttpd/conf-available/89-adsb-approach.conf
  sudo systemctl reload-or-restart lighttpd                                 # skip these three if lighttpd is not installed
  sudo rm /etc/systemd/system/adsb-approach-web.service /etc/systemd/system/adsb-approach.service && sudo systemctl daemon-reload
  sudo rm -rf /opt/adsb-approach /etc/adsb-approach /etc/default/adsb-approach
  sudo cp /var/lib/adsb-approach/approaches.sqlite ~/approaches-backup.sqlite && sudo chown "$USER": ~/approaches-backup.sqlite   # to keep the data
  sudo rm -rf /var/lib/adsb-approach
  sudo userdel adsb-approach
  ```

#### Reading the database

`/var/lib/adsb-approach` is mode 0750, owned by the `adsb-approach` service user, so query it as that user:

```sh
sudo -u adsb-approach sqlite3 -readonly /var/lib/adsb-approach/approaches.sqlite \
  "SELECT id, hex, callsign, airport, runway, COALESCE(outcome, '(on final)') AS outcome, lowest_hat_ft
   FROM approaches ORDER BY id DESC LIMIT 10;"
```

Without the `sqlite3` package, the service's own Python can query it instead:

```sh
sudo -u adsb-approach /opt/adsb-approach/venv/bin/python -c \
  "import sqlite3; c = sqlite3.connect('file:/var/lib/adsb-approach/approaches.sqlite?mode=ro', uri=True); print(c.execute('SELECT COUNT(*), COUNT(outcome) FROM approaches').fetchone())"
```

### macOS with launchd

```sh
uv sync
cp config/adsb-approach.example.toml config/adsb-approach.toml       # set db_path and typedb_path
sed -e "s|@REPO@|$PWD|g" -e "s|@LOGS@|$HOME/Library/Logs|g" deploy/com.z2amiller.adsb-approach.plist \
    > ~/Library/LaunchAgents/com.z2amiller.adsb-approach.plist
```

Set the receiver in the installed plist's `EnvironmentVariables` (`ADSB_APPROACH_SOURCE_HOST`,
`ADSB_APPROACH_SOURCE_PORT` and `ADSB_APPROACH_TAR1090_URL`), then load it:

```sh
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.z2amiller.adsb-approach.plist
launchctl kickstart "gui/$(id -u)/com.z2amiller.adsb-approach"
```

The log goes to `~/Library/Logs/adsb-approach.log`, and
`launchctl bootout "gui/$(id -u)/com.z2amiller.adsb-approach"` stops the service.

macOS's Local Network privacy setting may stop a LaunchAgent from reaching a readsb on the LAN; if the service
can't connect, allow it under System Settings > Privacy & Security > Local Network.

## Reference data

The airport files in `config/airports/` are generated from the FAA's Coded Instrument Flight Procedures
(CIFP). OurAirports fills in runways that CIFP lacks. The committed files for KSFO, KOAK, KNUQ and KSJC come
from cycle `CIFP_260903`.

- **CIFP:** download the zip from the FAA's
  [CIFP page](https://www.faa.gov/air_traffic/flight_info/aeronav/digital_products/cifp/download/) after
  accepting its terms. A new cycle comes out every 28 days.
- **OurAirports:** download `airports.csv` and `runways.csv` from <https://ourairports.com/data/> into one
  directory. `tests/fixtures/ourairports` holds only the rows for the four airports above. That is enough to
  regenerate the committed files, but not to add other airports.

```sh
uv run adsb-approach build-config --cifp ~/Downloads/CIFP_260903.zip \
    --airports KSFO,KOAK,KNUQ,KSJC --out config/airports --ourairports tests/fixtures/ourairports \
    --overrides config/overrides
```

`--overrides DIR` applies hand corrections from `DIR/<icao>.toml` to the generated files.

**Another airport:** put its ICAO ident in `--airports`, with the full OurAirports download in `--ourairports`, and
restart the collector: it scores every airport whose file is in `[service].airports_dir`. List only the airports you
want scored, since each one adds its approaches to the log, the norms and the coverage page.

Procedure paths, such as the Quiet Bridge Visual into KSFO 28R, live in `config/overrides/<icao>.toml` as
`[[runways."28R".paths]]` tables. They reach the collector through the generated `config/airports/<icao>.toml`, so
regenerate the files with `--overrides config/overrides` after editing one, and commit both. A path's last point must
lie on the runway's extended centreline, within 5 nm of the threshold. `fit-path` proposes the points from recorded
globe_history traces, taking the flights that followed a seed line (here the SFO VOR's R-095, 112.0 true outbound) and
then reached a short final on the runway:

```sh
uv run adsb-approach fit-path --config config/adsb-approach.example.toml --airport KSFO --runway 28R \
    --navaid 37.619486,-122.373894 --radial-true 112.0 --from 2026-09-13 --to 2026-10-23 \
    --globe-history /var/globe_history --name quiet-bridge --label "Quiet Bridge"
```

It prints a `[[runways."28R".paths]]` TOML table, with the bins it was fitted from as comments, to paste into
`config/overrides/<icao>.toml`. It never writes configuration: after pasting, run `build-config` to regenerate
`config/airports/`, and commit both.

`config/nearby_airports.csv` lists the airports within 150 nm of the configured ones (`ident,name,type,lat,lon`;
large, medium and small airports only, from the same OurAirports `airports.csv`). The flight search names a
track's nearest airport from this list and the configured airports. The list is optional: without the file only the
configured airports can name a track. To rebuild it, download `airports.csv` into a directory and run:

```sh
uv run adsb-approach build-nearby --ourairports ~/Downloads/ourairports --airports-dir config/airports \
    --out config/nearby_airports.csv
```

It prints `wrote N airports to config/nearby_airports.csv`. `--radius-nm` changes the 150.

Aircraft types come from [tar1090-db](https://github.com/wiedehopf/tar1090-db), which is downloaded for local use
and not included here. Types only label flights with a traffic class and an approach category; outcomes do not
depend on them. `type_classes.toml`'s `ga` list is a third, unrelated use of type codes: it names the light singles
the arrivals and live pages' GA checkbox hides by default, same as the Helicopters checkbox, and never reaches
classification or scoring. METAR observations come from aviationweather.gov, where `--hours` counts back from now:

```sh
uv run adsb-approach fetch-typedb --out data/aircraft.csv.gz
uv run adsb-approach fetch-metar --ids KSFO,KOAK,KNUQ,KSJC --hours 12 --out data/metar.json
```

## Configuration

`config/adsb-approach.example.toml` sets the detection thresholds (`[detection]`), the stability-flag limits
(`[stability]`) and the fleet binning (`[fleet]`). `[detection] gates_ft` must still name 1000 and 500:
`coverage_daily` has a column for each of those two gates, not a row per gate, so loading fails if either is
missing. Further gates are still scored and shown on the arrivals page; they just get no coverage column.
Relative paths in it resolve against `config/`. Replay reads
only `typedb_path`, `type_classes_path`, `airports_dir` and `metar_max_age_min` from `[service]`. The rest of
`[service]` and all of `[source]` are for the live service. `[external]` sets the adsb.lol live merge, off by
default. Its polling settings are for the live service, and replay ignores them. Two keys differ. `watch_max_min` is
read by the live service and by replay whether or not `enabled` is set: it bounds a go-around's climb-out, which is
recorded either way, and with the merge on it also bounds the watch after a go-around. `globe_url` only sets the
drawer's second link on the web service, independent of `enabled`; an empty `globe_url` turns that link off. The
example file documents each key.
Three `[service]` settings are for the flight search: `globe_history_dir` (default `/var/globe_history`) holds
readsb's day-by-day trace files; `live_traces_dir` (default `/run/readsb/traces`) is read for a day those files lack,
and `""` turns that fallback off; `nearby_airports_path` (default `nearby_airports.csv`, resolved against `config/`
like the other paths) names the airport list above, and a missing file is not an error. Replay ignores all three, and
they stay out of `config_hash`.
`[coverage]` holds the dated marks the coverage page draws; like `[service] timezone` it changes no score, and
replay ignores both. `[airports.<ICAO>]` blocks override an airport's `enabled`, `name` and `metar`, and
`[airports.<ICAO>.runways."<ident>"]` blocks override runway values.

The keys of the trace backfill that live polling replaced (`trace_url`, `delay_s`, `retry_after_s`, `min_interval_s`,
`max_age_min`, `max_tail_min` and `max_nm`) are rejected: a configuration that sets one stops loading, with an error
saying the trace backfill was replaced by live polling. A database that backfill wrote keeps its flags, points and
`external_lookups` rows; the drawer says what such an approach's lookup did, and the coverage page counts any
adsb.lol position in its track towards the adsb.lol column.

`type_classes.toml`'s `ga` list is display-only, for the arrivals and live pages' GA filter: like `[coverage]` and
`[service] timezone`, it changes no score and no outcome, so it stays out of `config_hash` too.

For your own receiver, copy the example to `config/adsb-approach.toml`. Git ignores that file, so the receiver's
address stays out of the repository.

## How it works

Every position goes through three stages:

1. A tracker keeps flight sessions by hex and callsign.
2. A matcher runs a state machine per aircraft and runway end: candidate, established, then an outcome.
3. A scorer measures the approach against the runway's published path, with the height corrected to the
   local altimeter setting and temperature.

Closed approaches go to SQLite with their points, gates and distance bins, and every change of state is
published as an event. Coverage limits are expected. An approach that fades out low on final ends as
`lost_on_final` with the `PRESUMED_LANDED` flag, and a later ground report or departure by the same aircraft
confirms the landing. With the live merge on, adsb.lol's positions join that stream as they arrive, so an approach the
receiver loses low is followed to the ground and decided from the merged track. The approach's distance bins are
built from every point it holds, while the coverage statistics keep to the receiver's own points.

**Procedure paths.** A path is a polyline from the far end of a customary track, through its turn points, to a join on
the runway's extended centreline; the threshold is its last vertex. The Quiet Bridge Visual into KSFO 28R ships. An
approach flown along a path stays on its own runway, 28R, and its lateral numbers (cross-track, localizer deviation and
the lateral flags) are measured from the path, not the centreline. A flight that settles onto another reference of the
same runway, the centreline after an early join, say, switches to it within the same attempt, and its lateral numbers
measure from there. A flight that crosses to the parallel runway while on the path hands over: the path's attempt
closes as a runway change and the parallel's starts where the flight left the path.

`adsb_approach/` holds these modules:

- `model`: records, outcomes and flags.
- `geometry` and `altitude`.
- `reference`: a runway's centreline and procedure paths, and where a position lies against one.
- `weather`.
- `refdata/`: CIFP and OurAirports to airport TOML.
- `config` and `typedb`.
- `sources/`: the JSON port, captures, tar1090 chunks and trace files.
- `tracker`: flight sessions.
- `scorer`: points, gates, summary, flags and bins.
- `attempt` and `matcher`: the state machine.
- `events`: in-process publish and subscribe.
- `store/`: the SQLite schema and queries.
- `fleet`: the norms, the rebuild and reflag.
- `fitpath`: `fit-path`, which proposes a procedure path from recorded traces.
- `pipeline`: glue and replay.
- `poller`: the adsb.lol live merge: the watch list, the re-api polling and the replay merge.
- `cli`.

## Licence

MIT; see `LICENSE`. `tests/fixtures/README.md` records where each test fixture comes from. The page bundles
[uPlot](https://github.com/leeoniya/uPlot) 1.6.32 (MIT, Leon Sorokin; its licence is
`adsb_approach/web/static/vendor/uPlot.LICENSE`). readsb and tar1090, whose output this project reads, are separate
GPL-licensed programs. The aircraft type database is not included:
`fetch-typedb` downloads tar1090-db, which states no licence of its own and takes its data from the aircraft
database maintained by Mictronics.

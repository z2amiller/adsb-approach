# Test fixtures

| Path | Contents | Source |
|---|---|---|
| `raw/tar1090-chunks-20260912/` | 180 tar1090 history chunks, 11:31Z to 19:28Z on 2026-09-12 | Recorded by the author's own ADS-B receiver, with the per-aircraft message counts removed |
| `raw/jsonport-20260912-2011Z.jsonl.gz` | 45 minutes of readsb JSON-port lines from 20:11Z on 2026-09-12 | Recorded by the author's own receiver, with the receiver-relative `r_dst` and `r_dir` fields and the `rssi`, `messages` and `seen` fields removed |
| `cifp/FAACIFP18-subset.txt` | The CIFP records for KSFO, KOAK, KNUQ and KSJC from cycle 260903 | FAA Coded Instrument Flight Procedures, a work of the US Government |
| `ourairports/` | The rows of `airports.csv` and `runways.csv` for the same four airports | [OurAirports](https://ourairports.com/data/), released to the public domain |
| `metar-20260912.json` | METAR observations for the four airports on 2026-09-12 | aviationweather.gov, a US Government service |
| `typedb-sample.csv` | Two aircraft rows in tar1090-db's layout | Hand-written test data |
| `tracks/2026/` | Twelve readsb globe_history `trace_full_<hex>.json` files (gzip despite the name) covering nine aircraft and every kind of flight the label tells apart, kept byte for byte apart from the identities below, and `tracks/places.csv`, twenty Bay Area airports with their coordinates | The author's own receiver's `/var/globe_history`; the places are hand-written |

Aircraft flying under an ICAO operator designator (airlines, charter and fractional operators, flying-school
trainers, Civil Air Patrol) and military aircraft keep their real identities. Every other ICAO address in the
captures and traces is replaced by a made-up one in 001000-0011c1, a block ICAO has not allocated, together with any
non-ICAO `~` form of the same address. Its callsign is made up too, unless it is a generic word such as `VFR`: a
tail number becomes an `N0` one, which no US registration can be, and a callsign that embeds the address (`PING`
plus its last four digits) changes with it. Every replay gives the same approaches, gates, points and events as with
the real identities, row for row. The made-up identities are a courtesy to private owners, not anonymisation:
anyone with public ADS-B history for the same time and place can match the tracks.

The replays in `tests/test_replay_golden.py` don't depend on aircraft types, so the tests need no copy of an
aircraft database. Capture fixtures must never carry `r_dst` or `r_dir`, or signal strength and message rates
(`rssi`, `messages`, `seen`, and the per-aircraft message counts in tar1090's chunks): together with each position
they locate the receiver.

The live merge's tests use no reply from adsb.lol: `tests/reapi.py` builds each re-api reply they need, in re-api's
format, from synthetic positions, and `tests/traces.py` does the same for the readsb trace files the source tests
read. adsb.lol's docs license their data under ODbL 1.0, but what is fetched from it still never enters the repository
or a fixture.

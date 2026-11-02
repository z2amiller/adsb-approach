CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL);

CREATE TABLE runs (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL,
  source TEXT, code_version TEXT, config_hash TEXT, note TEXT
);

CREATE TABLE aircraft (
  hex TEXT PRIMARY KEY, registration TEXT, type_code TEXT, type_desc TEXT, db_flags TEXT, updated_at REAL
);

CREATE TABLE flights (
  id INTEGER PRIMARY KEY, hex TEXT NOT NULL, callsign TEXT, registration TEXT, type_code TEXT, traffic_class TEXT,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL,
  UNIQUE (hex, callsign, first_seen)
);
CREATE INDEX flights_hex ON flights (hex, last_seen);

CREATE TABLE weather (
  id INTEGER PRIMARY KEY, airport TEXT NOT NULL, observed_at REAL NOT NULL, fetched_at REAL,
  altim_hpa REAL, temp_c REAL, dewp_c REAL, wind_dir INTEGER, wind_kt INTEGER, gust_kt INTEGER, raw TEXT,
  UNIQUE (airport, observed_at)
);

CREATE TABLE approaches (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs (id),
  flight_id INTEGER REFERENCES flights (id),
  arrival_id INTEGER,
  attempt_no INTEGER NOT NULL DEFAULT 1,
  hex TEXT NOT NULL, callsign TEXT, type_code TEXT, traffic_class TEXT, approach_cat TEXT,
  airport TEXT NOT NULL, runway TEXT NOT NULL,
  outcome TEXT, flags INTEGER NOT NULL DEFAULT 0, landed_confirmed_by TEXT,
  established_at REAL NOT NULL, ended_at REAL, duration_s REAL,
  first_along_nm REAL, closest_along_nm REAL, lowest_hat_ft REAL, points INTEGER,
  descent_angle_deg REAL, gp_dev_mean_ft REAL, gp_dev_rms_ft REAL, gp_dev_max_abs_ft REAL,
  loc_dev_rms_ft REAL, loc_dev_max_abs_ft REAL, gs_at_4nm REAL, gs_at_2nm REAL, sink_max_fpm REAL,
  ga_initiated_at REAL, ga_initiated_along_nm REAL, ga_initiated_hat_ft REAL, ga_initiated_gs REAL,
  next_approach_id INTEGER, prev_approach_id INTEGER,
  weather_id INTEGER REFERENCES weather (id), reference_source TEXT, fleet_key TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX approaches_ended ON approaches (ended_at);
CREATE INDEX approaches_rwy ON approaches (airport, runway, ended_at);
CREATE INDEX approaches_outcome ON approaches (outcome);
CREATE INDEX approaches_hex ON approaches (hex, ended_at);
CREATE INDEX approaches_arrival ON approaches (arrival_id);
CREATE INDEX approaches_flags ON approaches (flags);

CREATE TABLE approach_gates (
  approach_id INTEGER NOT NULL REFERENCES approaches (id) ON DELETE CASCADE,
  gate_ft INTEGER NOT NULL, observed INTEGER NOT NULL, t REAL, along_nm REAL,
  gp_dev_ft REAL, gp_dev_deg REAL, gp_dots REAL, loc_dev_ft REAL, loc_dev_deg REAL, loc_dots REAL,
  gs REAL, gs_corr REAL, ias_est REAL, sink_fpm REAL, nav_modes TEXT,
  fleet_gp_delta_ft REAL, fleet_gs_delta_kt REAL,
  PRIMARY KEY (approach_id, gate_ft)
);

CREATE TABLE approach_points (
  approach_id INTEGER NOT NULL REFERENCES approaches (id) ON DELETE CASCADE,
  t REAL NOT NULL, lat REAL, lon REAL, alt_baro INTEGER, alt_geom INTEGER, alt_qnh REAL, alt_corr REAL,
  temp_corrected INTEGER, gs REAL, track REAL, baro_rate INTEGER, geom_rate INTEGER,
  along_nm REAL, cross_ft REAL, hat_ft REAL, gp_dev_ft REAL, gp_dev_deg REAL, loc_dev_ft REAL, loc_dev_deg REAL,
  gs_corr REAL, sink_fpm REAL, nav_altitude_mcp INTEGER, nav_qnh REAL, nav_modes TEXT, source TEXT,
  PRIMARY KEY (approach_id, t)
);

CREATE TABLE approach_bins (
  approach_id INTEGER NOT NULL REFERENCES approaches (id) ON DELETE CASCADE,
  bin_nm REAL NOT NULL, n INTEGER NOT NULL,
  gp_dev_mean_ft REAL, loc_dev_mean_ft REAL, gs_corr_mean REAL, sink_mean_fpm REAL,
  PRIMARY KEY (approach_id, bin_nm)
);

CREATE TABLE events (
  id INTEGER PRIMARY KEY, t REAL NOT NULL, kind TEXT NOT NULL, approach_id INTEGER, flight_id INTEGER, payload_json TEXT
);
CREATE INDEX events_t ON events (t);

CREATE TABLE annotations (
  id INTEGER PRIMARY KEY, flight_id INTEGER REFERENCES flights (id), hex TEXT, registration TEXT, callsign TEXT,
  source TEXT NOT NULL, kind TEXT NOT NULL, observed_at REAL NOT NULL, payload_json TEXT, matched_at REAL
);
CREATE INDEX annotations_reg ON annotations (registration, observed_at);
CREATE INDEX annotations_cs ON annotations (callsign, observed_at);

CREATE TABLE fleet_stats (
  airport TEXT NOT NULL, runway TEXT NOT NULL, key_kind TEXT NOT NULL, key TEXT NOT NULL, bin_nm REAL NOT NULL,
  n INTEGER NOT NULL, gp_p25 REAL, gp_p50 REAL, gp_p75 REAL, gs_p25 REAL, gs_p50 REAL, gs_p75 REAL, built_at REAL NOT NULL,
  PRIMARY KEY (airport, runway, key_kind, key, bin_nm)
);

CREATE TABLE coverage_daily (
  airport TEXT NOT NULL, runway TEXT NOT NULL, day TEXT NOT NULL, n INTEGER NOT NULL,
  lowest_hat_p25 REAL, lowest_hat_p50 REAL, lowest_hat_p75 REAL, closest_nm_p50 REAL,
  gate1000_observed_frac REAL, gate500_observed_frac REAL, built_at REAL NOT NULL,
  PRIMARY KEY (airport, runway, day)
);

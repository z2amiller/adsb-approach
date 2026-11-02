-- Live merge: whether a run merged adsb.lol's live positions, each poll of re-api, and each distinct position it
-- returned, kept so a replay can merge them as the live run did.
ALTER TABLE runs ADD COLUMN external_merge INTEGER NOT NULL DEFAULT 0;
-- A go-around's climb-out rows are drawn but never scored; the fleet rebuild reads only charts_only = 0.
ALTER TABLE approach_points ADD COLUMN charts_only INTEGER NOT NULL DEFAULT 0;
CREATE TABLE external_polls (
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES runs(id),
  requested_at REAL NOT NULL, arrived_at REAL NOT NULL, asked INTEGER NOT NULL, returned INTEGER,
  http_status INTEGER, error TEXT, refused INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX external_polls_arrived ON external_polls (arrived_at);
CREATE TABLE external_positions (
  hex TEXT NOT NULL, t REAL NOT NULL, poll_id INTEGER NOT NULL REFERENCES external_polls(id), arrived_at REAL NOT NULL,
  lat REAL NOT NULL, lon REAL NOT NULL, alt_baro INTEGER, on_ground INTEGER NOT NULL, alt_geom INTEGER, gs REAL,
  track REAL, baro_rate INTEGER, geom_rate INTEGER, callsign TEXT, squawk TEXT, category TEXT, nav_qnh REAL,
  nav_altitude_mcp INTEGER, nav_modes TEXT, source TEXT NOT NULL, reg TEXT, type_code TEXT, emergency TEXT,
  PRIMARY KEY (hex, t)
);
CREATE INDEX external_positions_arrived ON external_positions (arrived_at);

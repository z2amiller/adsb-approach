-- External backfill: where each stored point came from, what re-scoring from stored points needs, and the adsb.lol
-- trace lookups. `source` keeps readsb's position type (no rename), so an older commit still writes a correct row after
-- a rollback; the new `origin` column says whether a point is the local receiver's or adsb.lol's.
ALTER TABLE approach_points ADD COLUMN origin TEXT NOT NULL DEFAULT 'local';
ALTER TABLE approach_points ADD COLUMN on_ground INTEGER;
ALTER TABLE approach_points ADD COLUMN qnh_assumed INTEGER;

CREATE TABLE external_lookups (
  approach_id INTEGER PRIMARY KEY REFERENCES approaches (id) ON DELETE CASCADE,
  hex TEXT NOT NULL,
  closed_outcome TEXT NOT NULL,
  due_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'retry', 'done', 'failed', 'expired')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_fetch_at REAL,
  http_status INTEGER,
  last_error TEXT,
  points_added INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  -- the seam measurement: the trace vs. the receiver's points over the stretch both saw, ADS-B only.
  overlap_n INTEGER,
  offset_s REAL,
  offset_spread_s REAL,
  offset_ft REAL,
  offset_spread_ft REAL
);
CREATE INDEX external_lookups_due ON external_lookups (status, due_at);

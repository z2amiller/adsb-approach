-- Gap filling: how a lookup lined adsb.lol's clock up with the receiver's, and what it added inside gaps in the
-- receiver's own track. points_added keeps counting the tail's points only, and 0002's seam columns stay. match_n is
-- NULL until a lookup's first successful fetch; match_offset_s and match_spread_s stay NULL under 3 matches.
ALTER TABLE external_lookups ADD COLUMN match_n INTEGER;
ALTER TABLE external_lookups ADD COLUMN match_offset_s REAL;
ALTER TABLE external_lookups ADD COLUMN match_spread_s REAL;
ALTER TABLE external_lookups ADD COLUMN applied_offset_s REAL;
ALTER TABLE external_lookups ADD COLUMN offset_source TEXT CHECK (offset_source IN ('matches', 'along_track', 'default'));
ALTER TABLE external_lookups ADD COLUMN gaps_filled INTEGER NOT NULL DEFAULT 0;
ALTER TABLE external_lookups ADD COLUMN gap_points_added INTEGER NOT NULL DEFAULT 0;

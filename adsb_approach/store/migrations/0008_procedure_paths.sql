-- Procedure paths: an approach may be flown along a configured procedure path (the Quiet Bridge Visual) rather than the
-- runway's centreline. `approaches.path` is the name of the last path the attempt flew, NULL if it flew none;
-- `approach_points.reference` is the path a point was measured against, NULL for the centreline. Existing rows are
-- centreline approaches, so both columns stay NULL and nothing needs rewriting. (0006 and 0007 are the flight-search
-- and live-merge migrations, written in parallel; Store.migrate applies by version set, so landing out of order is
-- harmless.)
ALTER TABLE approaches ADD COLUMN path TEXT;
ALTER TABLE approach_points ADD COLUMN reference TEXT;

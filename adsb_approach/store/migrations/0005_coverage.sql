-- Coverage view: the backfill's share of a day. backfill_n counts the day's approaches whose stored track carries
-- adsb.lol positions -- the ones the receiver lost and the backfill finished -- and backfill_frac is that count over
-- the day's own n, the same way the gate columns hold a fraction of it. SQLite only allows a NOT NULL column added by
-- ALTER TABLE when it carries a DEFAULT; the DELETE below empties the table, so no row is ever read with that default
-- still in it.
ALTER TABLE coverage_daily ADD COLUMN backfill_n INTEGER NOT NULL DEFAULT 0;
ALTER TABLE coverage_daily ADD COLUMN backfill_frac REAL;

-- `day` was a UTC day and now means the configured local day, so every stored row means something else. The rows go
-- rather than being converted: a day cannot be re-cut without reading its approaches again. The next rebuild recomputes
-- the whole window's coverage in one pass instead of skipping the days that are over -- on a live database's five days
-- and 2,302 approaches, 13 s against 0.3 s.
DELETE FROM coverage_daily;

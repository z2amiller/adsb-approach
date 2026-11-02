-- Fleet norms: fleet_stats.key_n holds the key's own approach count in the window, the same on every bin row of that
-- key, so the drawer can name it instead of the densest bin's n. 0003_gaps.sql is already deployed, so this is a new
-- migration rather than a change to 0001's CREATE TABLE. SQLite only allows a NOT NULL column added by ALTER TABLE when
-- it carries a DEFAULT, but fleet_stats is empty everywhere this runs, so that default is never actually read: the next
-- rebuild deletes and rewrites the table whole (Store.replace_norms).
ALTER TABLE fleet_stats ADD COLUMN key_n INTEGER NOT NULL DEFAULT 0;

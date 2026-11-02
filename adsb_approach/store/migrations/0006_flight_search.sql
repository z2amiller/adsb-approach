-- Find-a-flight: a callsign or registration prefix, and a session's approaches, are looked up by range comparison, so
-- each needs an index. Callsigns and registrations are stored upper-case, as ADS-B sends them. flights_reg_key indexes
-- the registration with its hyphen removed (G-VPRD is typed GVPRD as often as G-VPRD); a query uses an expression index
-- only when it writes the expression exactly as it is written here.
CREATE INDEX flights_callsign ON flights (callsign);
CREATE INDEX flights_reg_key ON flights (replace(registration, '-', ''));
CREATE INDEX approaches_flight ON approaches (flight_id);

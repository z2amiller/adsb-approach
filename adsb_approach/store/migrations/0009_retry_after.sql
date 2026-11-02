-- How long an HTTP error answer asked to wait (its Retry-After, else x-ratelimit-reset, in seconds), so a refused
-- poll's pause follows it rather than the flat [external] backoff_min. NULL when the answer sent neither. Numbered
-- 0009, not 0008, because the procedure-paths migration already takes 0008: either can land first.
ALTER TABLE external_polls ADD COLUMN retry_after_s REAL;

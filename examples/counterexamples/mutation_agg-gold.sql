-- Independently authored addition by ordered row traversal, without SUM or MAX.
-- Numeric accumulation preserves NULL-on-empty and ignores NULL values like SUM.
WITH RECURSIVE totals(n, value, nonnulls) AS (
    SELECT 0, 0::numeric, 0
    UNION ALL
    SELECT n + 1,
           value + coalesce((SELECT qty FROM stock ORDER BY part_id, location_id LIMIT 1 OFFSET n), 0),
           nonnulls + CASE WHEN (SELECT qty FROM stock ORDER BY part_id, location_id LIMIT 1 OFFSET n) IS NULL THEN 0 ELSE 1 END
    FROM totals WHERE n < (SELECT count(*) FROM stock)
)
SELECT CASE WHEN nonnulls = 0 THEN NULL ELSE value END AS value
FROM totals ORDER BY n DESC LIMIT 1;

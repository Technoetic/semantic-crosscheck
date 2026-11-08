-- Synthetic Gold reference; semantic-preflight checks file existence only.
-- The offline diagnostic does not read, execute or certify this statement.
SELECT part_id, SUM(qty) AS on_hand
FROM sxc_demo.stock
WHERE part_id IS NOT NULL
  AND UPPER(BTRIM(location_id)) IN ('MAIN', 'SUB')
GROUP BY part_id;

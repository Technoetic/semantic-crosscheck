-- q02 where-used: every ancestor of :part with depth, path count, quantity of :part per ancestor unit,
-- the ancestor's class and its firm open order quantity (0 when it has none).
WITH RECURSIVE edge AS (
  SELECT parent_id, child_id, sum(qty_per)::numeric AS q
  FROM sxc_demo.bom_line GROUP BY parent_id, child_id
), up(node, depth, qty) AS (
  SELECT e.parent_id, 1, e.q FROM edge e WHERE e.child_id = :part
  UNION ALL
  SELECT e.parent_id, u.depth + 1, e.q * u.qty
  FROM up u JOIN edge e ON e.child_id = u.node
  WHERE u.depth < 64
) CYCLE node SET is_cycle USING path,
anc AS (
  SELECT node, min(depth) AS min_depth, max(depth) AS max_depth, count(*) AS path_count, sum(qty) AS qty
  FROM up WHERE NOT is_cycle GROUP BY node
), dem AS (
  SELECT part_id, sum(qty_open) AS firm_open FROM sxc_demo.sales_order WHERE status = 'FIRM' GROUP BY part_id
)
SELECT a.node AS part_id, p.part_class, a.min_depth, a.max_depth, a.path_count, a.qty,
       coalesce(d.firm_open, 0) AS firm_open
FROM anc a
LEFT JOIN sxc_demo.part p ON p.part_id = a.node
LEFT JOIN dem d ON d.part_id = a.node

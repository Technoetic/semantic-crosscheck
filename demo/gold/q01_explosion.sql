-- q01 explosion: every descendant of :root with depth, path count and summed path-product quantity.
-- Independent of the generated closure table (own recursive CTE; SQL-standard CYCLE guard).
WITH RECURSIVE edge AS (
  SELECT parent_id, child_id, sum(qty_per)::numeric AS q
  FROM sxc_demo.bom_line GROUP BY parent_id, child_id
), walk(node, depth, qty) AS (
  SELECT e.child_id, 1, e.q FROM edge e WHERE e.parent_id = :root
  UNION ALL
  SELECT e.child_id, w.depth + 1, w.qty * e.q
  FROM walk w JOIN edge e ON e.parent_id = w.node
  WHERE w.depth < 64
) CYCLE node SET is_cycle USING path
SELECT w.node AS part_id, p.part_class,
       min(w.depth) AS min_depth, max(w.depth) AS max_depth, count(*) AS path_count, sum(w.qty) AS qty
FROM walk w LEFT JOIN sxc_demo.part p ON p.part_id = w.node
WHERE NOT w.is_cycle
GROUP BY w.node, p.part_class

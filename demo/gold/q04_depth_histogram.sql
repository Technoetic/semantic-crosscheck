-- q04 depth histogram: number of ancestor/descendant pairs by their longest path length (whole graph).
WITH RECURSIVE edge AS (
  SELECT DISTINCT parent_id, child_id FROM sxc_demo.bom_line
), walk(root, node, depth) AS (
  SELECT parent_id, child_id, 1 FROM edge
  UNION ALL
  SELECT w.root, e.child_id, w.depth + 1
  FROM walk w JOIN edge e ON e.parent_id = w.node
  WHERE w.depth < 64
) CYCLE node SET is_cycle USING path,
pairs AS (
  SELECT root, node, max(depth) AS max_depth FROM walk WHERE NOT is_cycle GROUP BY root, node
)
SELECT max_depth, count(*) AS pairs FROM pairs GROUP BY max_depth

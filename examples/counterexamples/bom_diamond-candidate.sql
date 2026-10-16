WITH RECURSIVE paths(ancestor, descendant, qty, depth) AS (
  SELECT parent_id, child_id, qty_per::numeric, 1 FROM bom_line
  UNION ALL
  SELECT p.ancestor, b.child_id, p.qty * b.qty_per, p.depth + 1
  FROM paths p JOIN bom_line b ON b.parent_id = p.descendant
  WHERE p.depth < 16
)
SELECT ancestor, descendant, min(qty) AS qty
FROM paths GROUP BY ancestor, descendant;

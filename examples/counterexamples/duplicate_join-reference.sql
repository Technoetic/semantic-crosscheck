SELECT s.part_id, sum(s.qty) AS qty
FROM stock s
WHERE EXISTS (SELECT 1 FROM sales_order o WHERE o.part_id = s.part_id)
GROUP BY s.part_id;

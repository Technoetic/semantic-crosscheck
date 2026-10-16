SELECT s.part_id, sum(s.qty) AS qty
FROM stock s JOIN sales_order o ON o.part_id = s.part_id
GROUP BY s.part_id;

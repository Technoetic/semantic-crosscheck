SELECT part_id, sum(coalesce(qty_open, 0)) AS total_open
FROM sales_order
WHERE status = 'FIRM'
GROUP BY part_id;

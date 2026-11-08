SELECT part_id, sum(qty)
FROM sxc_demo.sales_order
WHERE ordered_at BETWEEN $1 AND $2
GROUP BY part_id;

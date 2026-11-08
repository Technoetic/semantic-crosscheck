SELECT part_id, sum(qty)
FROM sxc_demo.sales_order
WHERE ordered_at >= $1 AND ordered_at < $2
GROUP BY part_id;

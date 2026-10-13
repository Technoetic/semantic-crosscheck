-- q05 firm open quantity per part for orders due inside [:date_from, :date_to] (inclusive dates).
SELECT part_id, sum(qty_open) AS firm_open
FROM sxc_demo.sales_order
WHERE status = 'FIRM' AND due_date BETWEEN CAST(:date_from AS date) AND CAST(:date_to AS date)
GROUP BY part_id

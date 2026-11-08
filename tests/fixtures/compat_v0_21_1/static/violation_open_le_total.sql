SELECT t."order_id"
FROM "sxc_demo"."sales_order" t
WHERE (
t.qty_open > t.qty_total
)

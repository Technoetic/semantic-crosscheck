SELECT t."parent_id", t."child_id", t."line_no"
FROM "sxc_demo"."bom_line" t
WHERE (t."qty_per" IS NOT NULL AND NOT (t."qty_per" > 0))

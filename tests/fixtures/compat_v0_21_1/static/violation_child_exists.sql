SELECT t."parent_id", t."child_id", t."line_no"
FROM "sxc_demo"."bom_line" t
WHERE (t."child_id" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "sxc_demo"."part" c WHERE c."part_id" = t."child_id"))

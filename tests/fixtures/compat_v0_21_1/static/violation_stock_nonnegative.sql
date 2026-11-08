SELECT t."part_id", t."location_id"
FROM "sxc_demo"."stock" t
WHERE (t."qty" IS NOT NULL AND NOT (t."qty" >= 0))

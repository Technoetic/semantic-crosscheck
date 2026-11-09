-- rule parent_unknown (row): parameterized; none
SELECT DISTINCT t."parent_id" FROM "sxc_rules"."bom_line" t WHERE (t."parent_id" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "sxc_rules"."part" n WHERE n."part_id" = t."parent_id")) AND t."parent_id" IS NOT NULL;

-- rule parent_unknown: view
CREATE OR REPLACE VIEW "sxc_out"."v_rule_parent_unknown" AS
SELECT r.*, row_number() OVER (ORDER BY r."parent_id" COLLATE "C" NULLS FIRST) AS ord FROM (SELECT DISTINCT t."parent_id" FROM "sxc_rules"."bom_line" t WHERE (t."parent_id" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "sxc_rules"."part" n WHERE n."part_id" = t."parent_id")) AND t."parent_id" IS NOT NULL) r;

-- example parent_unknown[0]: parameterized; 5 parameters
WITH "sxc_ex_bom_line"("parent_id") AS (VALUES (CAST(:p1 AS text)), (CAST(:p2 AS text)), (CAST(:p3 AS text)), (CAST(:p4 AS text))), "sxc_ex_part"("part_id") AS (VALUES (CAST(:p5 AS text))) SELECT * FROM (SELECT DISTINCT t."parent_id" FROM "sxc_ex_bom_line" t WHERE (t."parent_id" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "sxc_ex_part" n WHERE n."part_id" = t."parent_id")) AND t."parent_id" IS NOT NULL) r;

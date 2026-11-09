-- rule kit_without_bom (row): parameterized; p1 text
SELECT t."part_id" FROM "sxc_rules"."part" t WHERE (t."issue" IS NOT NULL AND t."issue" IN (CAST(:p1 AS text))) AND (NOT EXISTS (SELECT 1 FROM "sxc_rules"."bom_line" e WHERE e."parent_id" = t."part_id" AND e."child_id" IS NOT NULL)) AND (EXISTS (SELECT 1 FROM "sxc_rules"."bom_line" e WHERE e."child_id" = t."part_id" AND e."parent_id" IS NOT NULL));

-- rule kit_without_bom: view
CREATE OR REPLACE VIEW "sxc_out"."v_rule_kit_without_bom" AS
SELECT r.*, row_number() OVER (ORDER BY r."part_id" COLLATE "C" NULLS FIRST) AS ord FROM (SELECT t."part_id" FROM "sxc_rules"."part" t WHERE (t."issue" IS NOT NULL AND t."issue" IN (CAST(E'KIT' AS text))) AND (NOT EXISTS (SELECT 1 FROM "sxc_rules"."bom_line" e WHERE e."parent_id" = t."part_id" AND e."child_id" IS NOT NULL)) AND (EXISTS (SELECT 1 FROM "sxc_rules"."bom_line" e WHERE e."child_id" = t."part_id" AND e."parent_id" IS NOT NULL))) r;

-- example kit_without_bom[0]: parameterized; 17 parameters
WITH "sxc_ex_part"("issue", "part_id") AS (VALUES (CAST(:p1 AS text), CAST(:p2 AS text)), (CAST(:p3 AS text), CAST(:p4 AS text)), (CAST(:p5 AS text), CAST(:p6 AS text)), (CAST(:p7 AS text), CAST(:p8 AS text))), "sxc_ex_bom_line"("parent_id", "child_id") AS (VALUES (CAST(:p9 AS text), CAST(:p10 AS text)), (CAST(:p11 AS text), CAST(:p12 AS text)), (CAST(:p13 AS text), CAST(:p14 AS text)), (CAST(:p15 AS text), CAST(:p16 AS text))) SELECT * FROM (SELECT t."part_id" FROM "sxc_ex_part" t WHERE (t."issue" IS NOT NULL AND t."issue" IN (CAST(:p17 AS text))) AND (NOT EXISTS (SELECT 1 FROM "sxc_ex_bom_line" e WHERE e."parent_id" = t."part_id" AND e."child_id" IS NOT NULL)) AND (EXISTS (SELECT 1 FROM "sxc_ex_bom_line" e WHERE e."child_id" = t."part_id" AND e."parent_id" IS NOT NULL))) r;

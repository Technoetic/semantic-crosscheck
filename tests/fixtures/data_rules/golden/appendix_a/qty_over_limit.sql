-- rule qty_over_limit (row): parameterized; p1 numeric
SELECT t."parent_id", t."child_id", t."qty_per", count(*) AS "n" FROM "sxc_rules"."bom_line" t WHERE (t."qty_per" IS NOT NULL AND t."qty_per" > CAST(:p1 AS numeric)) GROUP BY t."parent_id", t."child_id", t."qty_per";

-- rule qty_over_limit: view
CREATE OR REPLACE VIEW "sxc_out"."v_rule_qty_over_limit" AS
SELECT r.*, row_number() OVER (ORDER BY r."parent_id" COLLATE "C" NULLS FIRST, r."child_id" COLLATE "C" NULLS FIRST, r."qty_per" NULLS FIRST, r."n" NULLS FIRST) AS ord FROM (SELECT t."parent_id", t."child_id", t."qty_per", count(*) AS "n" FROM "sxc_rules"."bom_line" t WHERE (t."qty_per" IS NOT NULL AND t."qty_per" > CAST(E'500' AS numeric)) GROUP BY t."parent_id", t."child_id", t."qty_per") r;

-- example qty_over_limit[0]: parameterized; 13 parameters
WITH "sxc_ex_bom_line"("qty_per", "parent_id", "child_id") AS (VALUES (CAST(:p1 AS numeric(18,6)), CAST(:p2 AS text), CAST(:p3 AS text)), (CAST(:p4 AS numeric(18,6)), CAST(:p5 AS text), CAST(:p6 AS text)), (CAST(:p7 AS numeric(18,6)), CAST(:p8 AS text), CAST(:p9 AS text)), (CAST(:p10 AS numeric(18,6)), CAST(:p11 AS text), CAST(:p12 AS text))) SELECT * FROM (SELECT t."parent_id", t."child_id", t."qty_per", count(*) AS "n" FROM "sxc_ex_bom_line" t WHERE (t."qty_per" IS NOT NULL AND t."qty_per" > CAST(:p13 AS numeric)) GROUP BY t."parent_id", t."child_id", t."qty_per") r;

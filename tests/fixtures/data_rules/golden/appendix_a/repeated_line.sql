-- rule repeated_line (duplicate): parameterized; p1 bigint
SELECT t."parent_id", t."child_id", count(*) AS "n" FROM "sxc_rules"."bom_line" t WHERE t."parent_id" IS NOT NULL AND t."child_id" IS NOT NULL GROUP BY t."parent_id", t."child_id" HAVING count(*) >= CAST(:p1 AS bigint);

-- rule repeated_line: view
CREATE OR REPLACE VIEW "sxc_out"."v_rule_repeated_line" AS
SELECT r.*, row_number() OVER (ORDER BY r."parent_id" COLLATE "C" NULLS FIRST, r."child_id" COLLATE "C" NULLS FIRST, r."n" NULLS FIRST) AS ord FROM (SELECT t."parent_id", t."child_id", count(*) AS "n" FROM "sxc_rules"."bom_line" t WHERE t."parent_id" IS NOT NULL AND t."child_id" IS NOT NULL GROUP BY t."parent_id", t."child_id" HAVING count(*) >= CAST(E'2' AS bigint)) r;

-- example repeated_line[0]: parameterized; 11 parameters
WITH "sxc_ex_bom_line"("parent_id", "child_id") AS (VALUES (CAST(:p1 AS text), CAST(:p2 AS text)), (CAST(:p3 AS text), CAST(:p4 AS text)), (CAST(:p5 AS text), CAST(:p6 AS text)), (CAST(:p7 AS text), CAST(:p8 AS text)), (CAST(:p9 AS text), CAST(:p10 AS text))) SELECT * FROM (SELECT t."parent_id", t."child_id", count(*) AS "n" FROM "sxc_ex_bom_line" t WHERE t."parent_id" IS NOT NULL AND t."child_id" IS NOT NULL GROUP BY t."parent_id", t."child_id" HAVING count(*) >= CAST(:p11 AS bigint)) r;

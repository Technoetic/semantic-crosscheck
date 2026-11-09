-- rule temporary_name (row): parameterized; p1 text
SELECT t."part_id" FROM "sxc_rules"."part" t WHERE (t."name" IS NOT NULL AND (starts_with(t."name", CAST(:p1 AS text))));

-- rule temporary_name: view
CREATE OR REPLACE VIEW "sxc_out"."v_rule_temporary_name" AS
SELECT r.*, row_number() OVER (ORDER BY r."part_id" COLLATE "C" NULLS FIRST) AS ord FROM (SELECT t."part_id" FROM "sxc_rules"."part" t WHERE (t."name" IS NOT NULL AND (starts_with(t."name", CAST(E'TMP-' AS text))))) r;

-- example temporary_name[0]: parameterized; 9 parameters
WITH "sxc_ex_part"("name", "part_id") AS (VALUES (CAST(:p1 AS text), CAST(:p2 AS text)), (CAST(:p3 AS text), CAST(:p4 AS text)), (CAST(:p5 AS text), CAST(:p6 AS text)), (CAST(:p7 AS text), CAST(:p8 AS text))) SELECT * FROM (SELECT t."part_id" FROM "sxc_ex_part" t WHERE (t."name" IS NOT NULL AND (starts_with(t."name", CAST(:p9 AS text))))) r;

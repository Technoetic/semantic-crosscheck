-- gate encoding: columns []; none
SHOW server_encoding;

-- gate budget part: columns []; limit bigint
SELECT count(*) FROM (SELECT 1 FROM "sxc_rules"."part" LIMIT CAST(:limit AS bigint)) AS bounded;

-- gate budget bom_line: columns []; limit bigint
SELECT count(*) FROM (SELECT 1 FROM "sxc_rules"."bom_line" LIMIT CAST(:limit AS bigint)) AS bounded;

-- gate budget code_value: columns []; limit bigint
SELECT count(*) FROM (SELECT 1 FROM "sxc_rules"."code_value" LIMIT CAST(:limit AS bigint)) AS bounded;

-- gate key part: columns ['part_id']; none
SELECT count(*) FILTER (WHERE t."part_id" IS NULL) AS null_keys, (SELECT count(*) FROM (SELECT 1 FROM "sxc_rules"."part" k WHERE k."part_id" IS NOT NULL GROUP BY k."part_id" HAVING count(*) > 1) d) AS duplicate_keys FROM "sxc_rules"."part" t;

-- gate non_finite bom_line: columns ['qty_per']; none
SELECT count(*) FILTER (WHERE t."qty_per" IN (CAST('NaN' AS numeric), CAST('Infinity' AS numeric), CAST('-Infinity' AS numeric))) AS "qty_per" FROM "sxc_rules"."bom_line" t;

-- gate null_quantity bom_line: columns []; none
SELECT count(*) FROM "sxc_rules"."bom_line" t WHERE t."parent_id" IS NOT NULL AND t."child_id" IS NOT NULL AND t."qty_per" IS NULL;

-- gate height bom_line: columns []; p1 bigint
WITH RECURSIVE edge AS (SELECT DISTINCT t."parent_id" AS p, t."child_id" AS c FROM "sxc_rules"."bom_line" t WHERE t."parent_id" IS NOT NULL AND t."child_id" IS NOT NULL), h(node, d) AS (SELECT e.c, 0 FROM edge e WHERE NOT EXISTS (SELECT 1 FROM edge x WHERE x.p = e.c) UNION SELECT e.p, h.d + 1 FROM edge e JOIN h ON e.c = h.node WHERE h.d < CAST(:p1 AS bigint)), ht AS (SELECT h.node, max(h.d) AS height FROM h GROUP BY h.node) SELECT (SELECT count(*) FROM ht WHERE ht.height >= CAST(:p1 AS bigint)) + (SELECT count(*) FROM (SELECT DISTINCT edge.p FROM edge) x WHERE NOT EXISTS (SELECT 1 FROM ht WHERE ht.node = x.p)) AS unbounded;

-- gate text_length part: columns ['name']; none
SELECT max(length(t."name")) AS "name" FROM "sxc_rules"."part" t;

-- export part
SELECT t."part_id", t."issue", t."retired", t."procure", t."name", t."unit", t."part_class", t."stored_level" FROM "sxc_rules"."part" t;

-- export bom_line
SELECT t."line_id", t."parent_id", t."child_id", t."qty_per" FROM "sxc_rules"."bom_line" t;

-- export code_value
SELECT t."code_group", t."code" FROM "sxc_rules"."code_value" t;

-- catalog type plan: p1 text, p2 text, p3 text, p4 text, p5 text, p6 text, p7 text, p8 text, p9 text
SELECT w.entity, CAST(a.attname AS text) AS column_name, CAST(a.atttypid AS bigint) AS type_oid, a.atttypmod AS type_mod, CAST(ty.typtype AS text) AS type_kind, coll.collisdeterministic AS deterministic FROM (VALUES (CAST(:p1 AS text), CAST(:p2 AS text), CAST(:p3 AS text)), (CAST(:p4 AS text), CAST(:p5 AS text), CAST(:p6 AS text)), (CAST(:p7 AS text), CAST(:p8 AS text), CAST(:p9 AS text))) AS w(entity, schema_name, table_name) JOIN pg_catalog.pg_namespace ns ON ns.nspname = w.schema_name JOIN pg_catalog.pg_class c ON c.relnamespace = ns.oid AND c.relname = w.table_name JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped JOIN pg_catalog.pg_type ty ON ty.oid = a.atttypid LEFT JOIN pg_catalog.pg_collation coll ON coll.oid = a.attcollation ORDER BY w.entity, a.attnum;

-- lookup odd_quantity: limit bigint
SELECT v."parent_id", v."child_id", v."qty_per", v."mode", v."n", v.ord FROM "sxc_out"."v_rule_odd_quantity" v ORDER BY v.ord LIMIT CAST(:limit AS bigint);

-- lookup subjects: limit bigint
SELECT v.subject, v.severity, v.severity_rank, v.rule_count, v.rules, v.ord FROM "sxc_out"."v_rules_subjects" v ORDER BY v.ord LIMIT CAST(:limit AS bigint);

-- orphan views: p1 text, p2 text, p3 text, p4 text
SELECT CAST(c.relname AS text) AS relname FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace ns ON ns.oid = c.relnamespace JOIN pg_catalog.pg_description d ON d.objoid = c.oid AND d.classoid = CAST('pg_catalog.pg_class' AS regclass) AND d.objsubid = 0 WHERE ns.nspname = CAST(:p1 AS text) AND c.relkind = 'v' AND d.description = CAST(:p2 AS text) AND (starts_with(CAST(c.relname AS text), CAST(:p3 AS text)) OR CAST(c.relname AS text) = CAST(:p4 AS text)) ORDER BY 1;

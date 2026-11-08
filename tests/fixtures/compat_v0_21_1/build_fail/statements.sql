-- statement 1
SELECT c.oid IS NOT NULL AS found, has_schema_privilege(n.oid, 'USAGE') AS usage,
       c.oid IS NOT NULL AND has_table_privilege(c.oid, 'SELECT') AS readable,
       coalesce((SELECT array_agg(a.attname::text) FROM pg_attribute a
                 WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped), '{}') AS columns
FROM pg_namespace n
LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = :t AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
WHERE n.nspname = :s
-- params {"s": "sxc_demo", "t": "bom_line"}
-- statement 2
SELECT c.oid IS NOT NULL AS found, has_schema_privilege(n.oid, 'USAGE') AS usage,
       c.oid IS NOT NULL AND has_table_privilege(c.oid, 'SELECT') AS readable,
       coalesce((SELECT array_agg(a.attname::text) FROM pg_attribute a
                 WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped), '{}') AS columns
FROM pg_namespace n
LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = :t AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
WHERE n.nspname = :s
-- params {"s": "sxc_demo", "t": "part"}
-- statement 3
SELECT c.oid IS NOT NULL AS found, has_schema_privilege(n.oid, 'USAGE') AS usage,
       c.oid IS NOT NULL AND has_table_privilege(c.oid, 'SELECT') AS readable,
       coalesce((SELECT array_agg(a.attname::text) FROM pg_attribute a
                 WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped), '{}') AS columns
FROM pg_namespace n
LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = :t AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
WHERE n.nspname = :s
-- params {"s": "sxc_demo", "t": "stock"}
-- statement 4
SELECT c.oid IS NOT NULL AS found, has_schema_privilege(n.oid, 'USAGE') AS usage,
       c.oid IS NOT NULL AND has_table_privilege(c.oid, 'SELECT') AS readable,
       coalesce((SELECT array_agg(a.attname::text) FROM pg_attribute a
                 WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped), '{}') AS columns
FROM pg_namespace n
LEFT JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = :t AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
WHERE n.nspname = :s
-- params {"s": "sxc_demo", "t": "sales_order"}
-- statement 5
SELECT current_user
-- params {}
-- statement 6
SELECT 1 FROM pg_namespace WHERE nspname = :s
-- params {"s": "sxc_out"}
-- statement 7
SELECT has_schema_privilege(CAST(:s AS text), 'USAGE'), has_schema_privilege(CAST(:s AS text), 'CREATE')
-- params {"s": "sxc_out"}
-- statement 8
BEGIN
-- params {}
-- statement 9
SET LOCAL statement_timeout = 120000
-- params {}
-- statement 10
SELECT pg_advisory_xact_lock(hashtext(:key))
-- params {"key": "semantic-crosscheck build sxc_out"}
-- statement 11
SAVEPOINT sxc_shape
-- params {}
-- statement 12
CREATE OR REPLACE VIEW "sxc_out"."v_shape_child_exists" AS
SELECT t."parent_id", t."child_id", t."line_no"
FROM "sxc_demo"."bom_line" t
WHERE (t."child_id" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "sxc_demo"."part" c WHERE c."part_id" = t."child_id"))
-- params {}
-- statement 13
RELEASE SAVEPOINT sxc_shape
-- params {}
-- statement 14
SAVEPOINT sxc_shape
-- params {}
-- statement 15
SELECT count(*) FROM "sxc_out"."v_shape_child_exists"
-- params {}
-- statement 16
RELEASE SAVEPOINT sxc_shape
-- params {}
-- statement 17
SAVEPOINT sxc_shape
-- params {}
-- statement 18
CREATE OR REPLACE VIEW "sxc_out"."v_shape_qty_positive" AS
SELECT t."parent_id", t."child_id", t."line_no"
FROM "sxc_demo"."bom_line" t
WHERE (t."qty_per" IS NOT NULL AND NOT (t."qty_per" > 0))
-- params {}
-- statement 19
RELEASE SAVEPOINT sxc_shape
-- params {}
-- statement 20
SAVEPOINT sxc_shape
-- params {}
-- statement 21
SELECT count(*) FROM "sxc_out"."v_shape_qty_positive"
-- params {}
-- statement 22
RELEASE SAVEPOINT sxc_shape
-- params {}
-- statement 23
SAVEPOINT sxc_shape
-- params {}
-- statement 24
CREATE OR REPLACE VIEW "sxc_out"."v_shape_stock_nonnegative" AS
SELECT t."part_id", t."location_id"
FROM "sxc_demo"."stock" t
WHERE (t."qty" IS NOT NULL AND NOT (t."qty" >= 0))
-- params {}
-- statement 25
RELEASE SAVEPOINT sxc_shape
-- params {}
-- statement 26
SAVEPOINT sxc_shape
-- params {}
-- statement 27
SELECT count(*) FROM "sxc_out"."v_shape_stock_nonnegative"
-- params {}
-- statement 28
RELEASE SAVEPOINT sxc_shape
-- params {}
-- statement 29
SAVEPOINT sxc_shape
-- params {}
-- statement 30
CREATE OR REPLACE VIEW "sxc_out"."v_shape_open_le_total" AS
SELECT t."order_id"
FROM "sxc_demo"."sales_order" t
WHERE (
t.qty_open > t.qty_total
)
-- params {}
-- statement 31
RELEASE SAVEPOINT sxc_shape
-- params {}
-- statement 32
SAVEPOINT sxc_shape
-- params {}
-- statement 33
SELECT count(*) FROM "sxc_out"."v_shape_open_le_total"
-- params {}
-- statement 34
RELEASE SAVEPOINT sxc_shape
-- params {}
-- statement 35
COMMIT
-- params {}
-- statement 36
BEGIN
-- params {}
-- statement 37
SET LOCAL statement_timeout = 120000
-- params {}
-- statement 38
SELECT pg_advisory_xact_lock(hashtext(:key))
-- params {"key": "semantic-crosscheck build sxc_out"}
-- statement 39
DROP TABLE IF EXISTS sxc_edge
-- params {}
-- statement 40
CREATE TEMP TABLE sxc_edge AS
            SELECT "parent_id" AS parent, "child_id" AS child, sum("qty_per")::numeric AS q
            FROM "sxc_demo"."bom_line"
            WHERE "parent_id" IS NOT NULL AND "child_id" IS NOT NULL
            GROUP BY "parent_id", "child_id"
-- params {}
-- statement 41
CREATE INDEX ON sxc_edge (parent)
-- params {}
-- statement 42
ANALYZE sxc_edge
-- params {}
-- statement 43
SELECT count(*) FROM sxc_edge
-- params {}
-- statement 44
SELECT count(*) FROM "sxc_demo"."bom_line" WHERE "parent_id" IS NULL OR "child_id" IS NULL
-- params {}
-- statement 45
SELECT count(*) FROM sxc_edge WHERE q IS NULL
-- params {}
-- statement 46
DROP TABLE IF EXISTS sxc_reach
-- params {}
-- statement 47
CREATE TEMP TABLE sxc_reach AS
            WITH RECURSIVE reach(a, d) AS (
              SELECT parent, child FROM sxc_edge
              UNION
              SELECT r.a, e.child FROM reach r JOIN sxc_edge e ON e.parent = r.d
            ) SELECT a, d FROM reach
-- params {}
-- statement 48
CREATE INDEX ON sxc_reach (a, d)
-- params {}
-- statement 49
SELECT count(*) FROM sxc_reach WHERE a = d
-- params {}
-- statement 50
DROP TABLE IF EXISTS sxc_out.has_component_cycles
-- params {}
-- statement 51
DROP TABLE IF EXISTS sxc_level
-- params {}
-- statement 52
DROP TABLE IF EXISTS sxc_next
-- params {}
-- statement 53
DROP TABLE IF EXISTS sxc_acc
-- params {}
-- statement 54
DROP TABLE IF EXISTS sxc_closure_new
-- params {}
-- statement 55
CREATE TEMP TABLE sxc_level AS
            SELECT parent AS ancestor, child AS descendant, 1 AS depth, 1::numeric AS paths, q AS qty FROM sxc_edge
-- params {}
-- statement 56
CREATE TEMP TABLE sxc_acc AS TABLE sxc_level
-- params {}
-- statement 57
SELECT count(*) FROM sxc_level
-- params {}
-- statement 58
DROP TABLE IF EXISTS sxc_next
-- params {}
-- statement 59
CREATE TEMP TABLE sxc_next AS
                SELECT l.ancestor, e.child AS descendant, l.depth + 1 AS depth, sum(l.paths) AS paths, sum(l.qty * e.q) AS qty
                FROM sxc_level l JOIN sxc_edge e ON e.parent = l.descendant
                GROUP BY l.ancestor, e.child, l.depth
-- params {}
-- statement 60
INSERT INTO sxc_acc SELECT * FROM sxc_next
-- params {}
-- statement 61
DROP TABLE sxc_level
-- params {}
-- statement 62
ALTER TABLE sxc_next RENAME TO sxc_level
-- params {}
-- statement 63
SELECT count(*) FROM sxc_level
-- params {}
-- statement 64
DROP TABLE IF EXISTS sxc_next
-- params {}
-- statement 65
CREATE TEMP TABLE sxc_next AS
                SELECT l.ancestor, e.child AS descendant, l.depth + 1 AS depth, sum(l.paths) AS paths, sum(l.qty * e.q) AS qty
                FROM sxc_level l JOIN sxc_edge e ON e.parent = l.descendant
                GROUP BY l.ancestor, e.child, l.depth
-- params {}
-- statement 66
INSERT INTO sxc_acc SELECT * FROM sxc_next
-- params {}
-- statement 67
DROP TABLE sxc_level
-- params {}
-- statement 68
ALTER TABLE sxc_next RENAME TO sxc_level
-- params {}
-- statement 69
SELECT count(*) FROM sxc_level
-- params {}
-- statement 70
CREATE TEMP TABLE sxc_closure_new AS
            SELECT ancestor, descendant, min(depth) AS min_depth, max(depth) AS max_depth,
                   sum(paths) AS path_count, sum(qty) AS qty
            FROM sxc_acc GROUP BY ancestor, descendant
-- params {}
-- statement 71
ALTER TABLE sxc_closure_new ADD PRIMARY KEY (ancestor, descendant)
-- params {}
-- statement 72
SELECT max_depth, count(*) FROM sxc_closure_new GROUP BY max_depth ORDER BY max_depth
-- params {}
-- statement 73
SELECT count(*) FROM sxc_closure_new
-- params {}
-- statement 74
SELECT coalesce(sum(path_count), 0) FROM sxc_closure_new
-- params {}
-- statement 75
SELECT count(*) FROM sxc_closure_new WHERE path_count > 1
-- params {}
-- statement 76
SELECT coalesce(max(max_depth), 0) FROM sxc_closure_new
-- params {}
-- statement 77
SELECT to_regclass(:t) IS NOT NULL
-- params {"t": "sxc_out.has_component_closure"}
-- statement 78
WITH m AS (
              MERGE INTO sxc_out.has_component_closure t
              USING sxc_closure_new s ON t.ancestor = s.ancestor AND t.descendant = s.descendant
              WHEN MATCHED AND (t.min_depth, t.max_depth, t.path_count, t.qty)
                   IS DISTINCT FROM (s.min_depth, s.max_depth, s.path_count, s.qty) THEN
                UPDATE SET min_depth = s.min_depth, max_depth = s.max_depth, path_count = s.path_count, qty = s.qty
              WHEN NOT MATCHED BY TARGET THEN
                INSERT (ancestor, descendant, min_depth, max_depth, path_count, qty)
                VALUES (s.ancestor, s.descendant, s.min_depth, s.max_depth, s.path_count, s.qty)
              WHEN NOT MATCHED BY SOURCE THEN DELETE
              RETURNING merge_action() AS action
            ) SELECT action, count(*) FROM m GROUP BY action
-- params {}
-- statement 79
SELECT (SELECT count(*) FROM (SELECT ancestor, descendant, min_depth, max_depth, path_count, qty FROM sxc_out.has_component_closure EXCEPT ALL SELECT ancestor, descendant, min_depth, max_depth, path_count, qty FROM sxc_closure_new) a)
                                   + (SELECT count(*) FROM (SELECT ancestor, descendant, min_depth, max_depth, path_count, qty FROM sxc_closure_new EXCEPT ALL SELECT ancestor, descendant, min_depth, max_depth, path_count, qty FROM sxc_out.has_component_closure) b)
-- params {}
-- statement 80
ANALYZE sxc_out.has_component_closure
-- params {}
-- statement 81
COMMIT
-- params {}

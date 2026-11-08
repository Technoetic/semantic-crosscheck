-- semantic-crosscheck build: every statement this run executed, in order (generated from crosscheck.toml).
-- For review only: change crosscheck.toml and rerun `sxc.py build` instead of editing generated objects.
-- Statements with :name placeholders ran with bound parameters. A shape's sql_violation predicate is trusted
-- SQL copied from the configuration.

-- shapes (one transaction)

SELECT pg_advisory_xact_lock(hashtext(:key));  -- key = 'semantic-crosscheck build sxc_out'

CREATE OR REPLACE VIEW "sxc_out"."v_shape_child_exists" AS
SELECT t."parent_id", t."child_id", t."line_no"
FROM "sxc_demo"."bom_line" t
WHERE (t."child_id" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "sxc_demo"."part" c WHERE c."part_id" = t."child_id"));

CREATE OR REPLACE VIEW "sxc_out"."v_shape_qty_positive" AS
SELECT t."parent_id", t."child_id", t."line_no"
FROM "sxc_demo"."bom_line" t
WHERE (t."qty_per" IS NOT NULL AND NOT (t."qty_per" > 0));

CREATE OR REPLACE VIEW "sxc_out"."v_shape_stock_nonnegative" AS
SELECT t."part_id", t."location_id"
FROM "sxc_demo"."stock" t
WHERE (t."qty" IS NOT NULL AND NOT (t."qty" >= 0));

CREATE OR REPLACE VIEW "sxc_out"."v_shape_open_le_total" AS
SELECT t."order_id"
FROM "sxc_demo"."sales_order" t
WHERE (
t.qty_open > t.qty_total
);

-- hierarchy has_component (one transaction)

SELECT pg_advisory_xact_lock(hashtext(:key));  -- key = 'semantic-crosscheck build sxc_out'

DROP TABLE IF EXISTS sxc_edge;

CREATE TEMP TABLE sxc_edge AS
            SELECT "parent_id" AS parent, "child_id" AS child, sum("qty_per")::numeric AS q
            FROM "sxc_demo"."bom_line"
            WHERE "parent_id" IS NOT NULL AND "child_id" IS NOT NULL
            GROUP BY "parent_id", "child_id";

CREATE INDEX ON sxc_edge (parent);

ANALYZE sxc_edge;

SELECT count(*) FROM sxc_edge;

SELECT count(*) FROM "sxc_demo"."bom_line" WHERE "parent_id" IS NULL OR "child_id" IS NULL;

SELECT count(*) FROM sxc_edge WHERE q IS NULL;

DROP TABLE IF EXISTS sxc_reach;

CREATE TEMP TABLE sxc_reach AS
            WITH RECURSIVE reach(a, d) AS (
              SELECT parent, child FROM sxc_edge
              UNION
              SELECT r.a, e.child FROM reach r JOIN sxc_edge e ON e.parent = r.d
            ) SELECT a, d FROM reach;

CREATE INDEX ON sxc_reach (a, d);

SELECT count(*) FROM sxc_reach WHERE a = d;

DROP TABLE IF EXISTS sxc_out.has_component_cycles;

DROP TABLE IF EXISTS sxc_level;

DROP TABLE IF EXISTS sxc_next;

DROP TABLE IF EXISTS sxc_acc;

DROP TABLE IF EXISTS sxc_closure_new;

CREATE TEMP TABLE sxc_level AS
            SELECT parent AS ancestor, child AS descendant, 1 AS depth, 1::numeric AS paths, q AS qty FROM sxc_edge;

CREATE TEMP TABLE sxc_acc AS TABLE sxc_level;

SELECT count(*) FROM sxc_level;

DROP TABLE IF EXISTS sxc_next;

CREATE TEMP TABLE sxc_next AS
                SELECT l.ancestor, e.child AS descendant, l.depth + 1 AS depth, sum(l.paths) AS paths, sum(l.qty * e.q) AS qty
                FROM sxc_level l JOIN sxc_edge e ON e.parent = l.descendant
                GROUP BY l.ancestor, e.child, l.depth;

INSERT INTO sxc_acc SELECT * FROM sxc_next;

DROP TABLE sxc_level;

ALTER TABLE sxc_next RENAME TO sxc_level;

SELECT count(*) FROM sxc_level;

DROP TABLE IF EXISTS sxc_next;

CREATE TEMP TABLE sxc_next AS
                SELECT l.ancestor, e.child AS descendant, l.depth + 1 AS depth, sum(l.paths) AS paths, sum(l.qty * e.q) AS qty
                FROM sxc_level l JOIN sxc_edge e ON e.parent = l.descendant
                GROUP BY l.ancestor, e.child, l.depth;

INSERT INTO sxc_acc SELECT * FROM sxc_next;

DROP TABLE sxc_level;

ALTER TABLE sxc_next RENAME TO sxc_level;

SELECT count(*) FROM sxc_level;

CREATE TEMP TABLE sxc_closure_new AS
            SELECT ancestor, descendant, min(depth) AS min_depth, max(depth) AS max_depth,
                   sum(paths) AS path_count, sum(qty) AS qty
            FROM sxc_acc GROUP BY ancestor, descendant;

ALTER TABLE sxc_closure_new ADD PRIMARY KEY (ancestor, descendant);

SELECT max_depth, count(*) FROM sxc_closure_new GROUP BY max_depth ORDER BY max_depth;

SELECT count(*) FROM sxc_closure_new;

SELECT coalesce(sum(path_count), 0) FROM sxc_closure_new;

SELECT count(*) FROM sxc_closure_new WHERE path_count > 1;

SELECT coalesce(max(max_depth), 0) FROM sxc_closure_new;

SELECT to_regclass(:t) IS NOT NULL;

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
            ) SELECT action, count(*) FROM m GROUP BY action;

SELECT (SELECT count(*) FROM (SELECT ancestor, descendant, min_depth, max_depth, path_count, qty FROM sxc_out.has_component_closure EXCEPT ALL SELECT ancestor, descendant, min_depth, max_depth, path_count, qty FROM sxc_closure_new) a)
                                   + (SELECT count(*) FROM (SELECT ancestor, descendant, min_depth, max_depth, path_count, qty FROM sxc_closure_new EXCEPT ALL SELECT ancestor, descendant, min_depth, max_depth, path_count, qty FROM sxc_out.has_component_closure) b);

ANALYZE sxc_out.has_component_closure;

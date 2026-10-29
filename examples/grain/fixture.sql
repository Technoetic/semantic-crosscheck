-- Load only into a fresh, disposable synthetic database, using its loader role.
CREATE SCHEMA IF NOT EXISTS sxc_demo;
CREATE TABLE sxc_demo.grain_fact (fact_id integer, node_id text, qty numeric, status text);
CREATE TABLE sxc_demo.grain_node (node_id text);
INSERT INTO sxc_demo.grain_node VALUES ('a'), ('b');
INSERT INTO sxc_demo.grain_fact VALUES
  (1, 'a', 2, 'FIRM'), (2, 'a', 3, 'FIRM'), (3, 'b', 4, 'DRAFT');
-- Grant the read-only check role USAGE on sxc_demo and SELECT on these tables.
-- The intentional lack of PK/FK constraints permits testing invalid key claims.

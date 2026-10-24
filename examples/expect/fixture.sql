-- semantic-crosscheck expect example: a tiny synthetic BOM for the [[gold.expect]] cases in crosscheck.toml.
-- Load it yourself into a dedicated, empty fixture database (sxc never runs fixture SQL), for example with
--   psql -d <fixture database> -v ON_ERROR_STOP=1 -f examples/expect/fixture.sql
-- then point SXC_EXPECT_DSN at that database. The tables have the demo's columns (sxc_demo).
-- One P-100 is built from 2 A-200 and 1 C-300, one A-200 from 3 C-300, one C-300 from 4 R-400.
CREATE SCHEMA sxc_demo;
CREATE TABLE sxc_demo.part (part_id text, part_class text, unit text, procure text, name text, PRIMARY KEY (part_id));
CREATE TABLE sxc_demo.bom_line (parent_id text, child_id text, line_no integer, qty_per numeric(18,6),
                                PRIMARY KEY (parent_id, child_id, line_no));
INSERT INTO sxc_demo.part (part_id, part_class, unit, procure, name) VALUES
  ('P-100', 'product', 'EA', 'make', 'Product P-100'),
  ('A-200', 'assembly', 'EA', 'make', 'Assembly A-200'),
  ('C-300', 'component', 'EA', 'buy', 'Component C-300'),
  ('R-400', 'raw', 'KG', 'buy', 'Raw material R-400');
INSERT INTO sxc_demo.bom_line (parent_id, child_id, line_no, qty_per) VALUES
  ('P-100', 'A-200', 1, 2),
  ('P-100', 'C-300', 2, 1),
  ('A-200', 'C-300', 1, 3),
  ('C-300', 'R-400', 1, 4);

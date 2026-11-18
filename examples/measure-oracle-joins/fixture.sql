-- Owner setup in an isolated database with an existing sxc_demo schema.
CREATE TABLE sxc_demo.oracle_sales_order (
    id integer PRIMARY KEY, amount numeric, state text
);
CREATE TABLE sxc_demo.oracle_sales_line (
    id integer PRIMARY KEY, order_id integer
);
INSERT INTO sxc_demo.oracle_sales_order (id, amount, state) VALUES
    (1, 15.25, 'posted'), (2, 15.25, 'posted'), (3, -4.50, 'posted'),
    (4, NULL, 'posted'), (5, 100, 'posted'), (6, 11, 'void');
INSERT INTO sxc_demo.oracle_sales_line (id, order_id) VALUES
    (10, 1), (11, 1), (12, 2), (13, 3), (14, 4),
    (15, 6), (16, 6), (17, NULL), (18, 999);

CREATE VIEW sxc_demo.oracle_eligible_orders AS
    SELECT o.id, o.amount, o.state FROM sxc_demo.oracle_sales_order o
    WHERE EXISTS (SELECT 1 FROM sxc_demo.oracle_sales_line l WHERE l.order_id = o.id);
CREATE VIEW sxc_demo.oracle_orders_per_line AS
    SELECT l.id AS line_id, o.id, o.amount, o.state
    FROM sxc_demo.oracle_sales_order o JOIN sxc_demo.oracle_sales_line l ON l.order_id = o.id;

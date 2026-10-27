-- Synthetic boundary fixture, independently authored from fixture-spec.md.
-- Load into a dedicated empty database yourself. expect never executes fixture SQL.
CREATE SCHEMA sxc_boundary;
CREATE TABLE sxc_boundary.part (
    part_id text PRIMARY KEY,
    region text NOT NULL,
    label text NOT NULL
);
CREATE TABLE sxc_boundary.stock (
    stock_id integer PRIMARY KEY,
    part_id text REFERENCES sxc_boundary.part (part_id),
    status text NOT NULL,
    qty numeric(18,2) NOT NULL
);
INSERT INTO sxc_boundary.part (part_id, region, label) VALUES
    ('P-EMPTY', 'NORTH', 'No stock'),
    ('P-FILTER', 'NORTH', 'Filtered stock'),
    ('R-N1', 'NORTH', 'Relay'),
    ('R-N2', 'NORTH', 'Relay'),
    ('R-S1', 'SOUTH', 'Relay');
INSERT INTO sxc_boundary.stock (stock_id, part_id, status, qty) VALUES
    (1, 'P-FILTER', 'QUARANTINE', 9.00),
    (2, 'R-N1', 'USABLE', 1.25),
    (3, 'R-N1', 'USABLE', 2.75),
    (4, 'R-N2', 'USABLE', 6.00),
    (5, 'R-S1', 'USABLE', 7.00),
    (6, 'R-S1', 'USABLE', 13.00),
    (7, NULL, 'USABLE', 1000.00);

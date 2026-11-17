-- Explicit synthetic owner setup; measure-oracle never executes this file.
CREATE SCHEMA IF NOT EXISTS sxc_measure_oracle;
CREATE TABLE sxc_measure_oracle.orders (
    id integer PRIMARY KEY,
    part_id text,
    qty numeric,
    status text
);
INSERT INTO sxc_measure_oracle.orders (id, part_id, qty, status) VALUES
    (1, 'SYNTHETIC-A', 13, 'FIRM'),
    (2, 'SYNTHETIC-B', 2.25, 'FIRM'),
    (3, 'SYNTHETIC-C', NULL, 'FIRM'),
    (4, 'SYNTHETIC-D', 71, 'DRAFT'),
    (5, NULL, 1000, 'FIRM');

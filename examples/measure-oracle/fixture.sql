-- Explicit synthetic owner setup; measure-oracle never executes this file.
CREATE SCHEMA IF NOT EXISTS sxc_measure_oracle;
CREATE TABLE sxc_measure_oracle.orders (
    id integer PRIMARY KEY,
    part_id text,
    qty numeric,
    status text,
    ordered_on date
);
INSERT INTO sxc_measure_oracle.orders (id, part_id, qty, status, ordered_on) VALUES
    (1, 'SYNTHETIC-A', 13, 'FIRM', DATE '2026-01-01'),
    (2, 'SYNTHETIC-B', 2.25, 'FIRM', DATE '2026-02-01'),
    (3, 'SYNTHETIC-C', NULL, 'FIRM', DATE '2026-01-31'),
    (4, 'SYNTHETIC-D', 71, 'DRAFT', DATE '2026-01-08'),
    (5, NULL, 1000, 'FIRM', NULL);

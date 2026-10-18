-- SQL analogue of measure_agg_to_max, evaluated only on operator-directed fixtures.
SELECT max(qty) AS value FROM stock;

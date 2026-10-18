-- Trusted SQL adapted to the fixed synthetic stock CTE; this is not a Cube runtime test.
SELECT sum(qty) AS value FROM stock;

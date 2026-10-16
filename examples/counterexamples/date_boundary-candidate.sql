SELECT sum(qty_open) AS qty FROM sales_order
WHERE due_date >= DATE '2026-01-01' AND due_date <= DATE '2026-02-01';

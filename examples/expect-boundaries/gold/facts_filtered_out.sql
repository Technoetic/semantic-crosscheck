-- EB-1 section 3: facts exist, but none pass the usable-stock filter.
SELECT p.part_id, sum(s.qty) AS all_qty,
       coalesce(sum(s.qty) FILTER (WHERE s.status = 'USABLE'), 0) AS usable_qty
FROM sxc_boundary.part p
LEFT JOIN sxc_boundary.stock s ON s.part_id = p.part_id
WHERE p.part_id = :part
GROUP BY p.part_id

-- EB-1 section 2: preserve the part, including a raw NULL stock aggregate.
SELECT p.part_id, sum(s.qty) AS qty
FROM sxc_boundary.part p
LEFT JOIN sxc_boundary.stock s ON s.part_id = p.part_id
WHERE p.part_id = :part
GROUP BY p.part_id

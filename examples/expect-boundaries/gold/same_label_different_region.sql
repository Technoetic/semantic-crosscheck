-- EB-1 section 4: the same part label in two regions denotes two result groups.
SELECT p.region, p.label, sum(s.qty) AS qty
FROM sxc_boundary.part p
LEFT JOIN sxc_boundary.stock s ON s.part_id = p.part_id
WHERE p.label = :label
GROUP BY p.region, p.label

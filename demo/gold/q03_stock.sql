-- q03 stock by part for one part class: on-hand stock in MAIN and SUB and consigned stock, with location
-- codes normalised by upper(btrim(...)) (the demo's dirty variant has ' main' / 'Sub ' style codes).
SELECT p.part_id, p.part_class,
       coalesce(sum(s.qty) FILTER (WHERE upper(btrim(s.location_id)) IN ('MAIN', 'SUB')), 0) AS on_hand,
       coalesce(sum(s.qty) FILTER (WHERE upper(btrim(s.location_id)) = 'CONSIGN'), 0) AS consigned
FROM sxc_demo.part p
LEFT JOIN sxc_demo.stock s ON s.part_id = p.part_id
WHERE p.part_class = :part_class
GROUP BY p.part_id, p.part_class

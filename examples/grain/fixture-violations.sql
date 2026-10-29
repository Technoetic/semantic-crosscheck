-- Apply after fixture.sql, only in its disposable synthetic database.
-- One duplicate source key, a missing node, a NULL grain and a duplicate node:
INSERT INTO sxc_demo.grain_fact VALUES
  (1, 'b', 8, 'FIRM'), (4, 'missing', 1, 'FIRM'), (5, NULL, 9, 'FIRM');
INSERT INTO sxc_demo.grain_node VALUES ('a');

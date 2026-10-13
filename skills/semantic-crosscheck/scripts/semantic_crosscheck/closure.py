"""Transitive closure of a hierarchy in PostgreSQL 17 (generated, reviewable SQL).

For hierarchy h with edge entity E (parent P, child C, quantity Q):

1. Edges: one row per (parent, child) with the summed quantity (several lines between the same pair are one edge).
2. Cycles: reachability with WITH RECURSIVE ... UNION (set semantics, so it terminates on cycles); a node that
   reaches itself lies on a cycle. Example cycle paths come from the SQL-standard CYCLE clause, started only at
   those nodes and limited in rows. on_cycle = "error" fails the check and leaves the closure table untouched;
   "report" drops the edges inside each strongly connected component and builds the closure of what is left.
3. Closure: level-wise aggregation (one frontier per depth, grouped by (ancestor, descendant)), so path counts and
   summed path-product quantities are exact without enumerating paths. A frontier left over after max_depth
   levels fails the check (the closure would be truncated).
4. Apply: MERGE ... WHEN NOT MATCHED BY SOURCE THEN DELETE ... RETURNING merge_action() (PostgreSQL 17) applies the
   recomputed closure to the existing table and counts inserts/updates/deletes; an EXCEPT check in both directions
   then proves the table equals the recomputation.

Creating the closure table when it is missing and the MERGE are a check-then-write on a shared table, so the caller's
transaction must hold the output schema's build lock (stage_build.lock_output_schema): two builds of the same output
schema then run one after the other instead of failing with a unique violation. The temporary tables are private to
the session.

Every SQL statement is collected in `sql_log` so the build stage can write it out for review.
"""

from . import ident, naming

MAX_CYCLE_EXAMPLES = 50


class ClosureBuilder:
    def __init__(self, conn, cfg, h):
        self.conn, self.cfg, self.h = conn, cfg, h
        self.sql_log: list[str] = []
        edge = cfg.entity(h.edge, f"hierarchies.{h.name}")
        self.edge_table = ident.qualified(edge.table)
        self.p, self.c, self.q = ident.quote(h.parent), ident.quote(h.child), ident.quote(h.quantity)
        self.target = naming.closure_table(cfg, h.name)
        self.cycles = naming.cycles_table(cfg, h.name)

    def run(self, sql: str, **params):
        self.sql_log.append(sql.strip() + ";")
        return self.conn.run(sql, **params)

    def count(self, sql: str, **params) -> int:
        return int(self.run(sql, **params)[0][0])

    # ---- steps ----------------------------------------------------------------------------------
    def edges(self) -> dict:
        self.run("DROP TABLE IF EXISTS sxc_edge")
        self.run(f"""CREATE TEMP TABLE sxc_edge AS
            SELECT {self.p} AS parent, {self.c} AS child, sum({self.q})::numeric AS q
            FROM {self.edge_table}
            WHERE {self.p} IS NOT NULL AND {self.c} IS NOT NULL
            GROUP BY {self.p}, {self.c}""")
        self.run("CREATE INDEX ON sxc_edge (parent)")
        self.run("ANALYZE sxc_edge")
        return {
            "edges": self.count("SELECT count(*) FROM sxc_edge"),
            "null_endpoint_lines": self.count(f"SELECT count(*) FROM {self.edge_table} WHERE {self.p} IS NULL OR {self.c} IS NULL"),
            "null_quantity_edges": self.count("SELECT count(*) FROM sxc_edge WHERE q IS NULL"),
        }

    def cycle_nodes(self) -> int:
        self.run("DROP TABLE IF EXISTS sxc_reach")
        self.run("""CREATE TEMP TABLE sxc_reach AS
            WITH RECURSIVE reach(a, d) AS (
              SELECT parent, child FROM sxc_edge
              UNION
              SELECT r.a, e.child FROM reach r JOIN sxc_edge e ON e.parent = r.d
            ) SELECT a, d FROM reach""")
        self.run("CREATE INDEX ON sxc_reach (a, d)")
        return self.count("SELECT count(*) FROM sxc_reach WHERE a = d")

    def cycle_examples(self) -> int:
        """Store up to MAX_CYCLE_EXAMPLES cycle paths (rows for review only; results keep counts)."""
        self.run(f"DROP TABLE IF EXISTS {self.cycles}")
        self.run(f"""CREATE TABLE {self.cycles} AS
            WITH RECURSIVE scc_edge AS (
              SELECT e.parent, e.child FROM sxc_edge e
              WHERE EXISTS (SELECT 1 FROM sxc_reach r WHERE r.a = e.child AND r.d = e.parent)
            ), w(start, node, depth) AS (
              SELECT parent, child, 1 FROM scc_edge
              UNION ALL
              SELECT w.start, e.child, w.depth + 1
              FROM w JOIN scc_edge e ON e.parent = w.node
              WHERE w.node <> w.start AND w.depth < :max_depth
            ) CYCLE node SET is_cycle USING path
            SELECT start, depth AS length, path::text AS path FROM w
            WHERE node = start AND NOT is_cycle
            LIMIT {MAX_CYCLE_EXAMPLES}""", max_depth=self.h.max_depth)
        return self.count(f"SELECT count(*) FROM {self.cycles}")

    def drop_cycle_edges(self) -> int:
        """on_cycle = report: remove edges whose child reaches their parent (edges inside a cycle)."""
        rows = self.run("""WITH d AS (
              DELETE FROM sxc_edge e
              WHERE EXISTS (SELECT 1 FROM sxc_reach r WHERE r.a = e.child AND r.d = e.parent)
              RETURNING 1
            ) SELECT count(*) FROM d""")
        return int(rows[0][0])

    def levels(self) -> dict:
        """Level-wise closure into temp table sxc_closure_new. Returns stats or {'depth_limit_exceeded': True}."""
        for t in ("sxc_level", "sxc_next", "sxc_acc", "sxc_closure_new"):
            self.run(f"DROP TABLE IF EXISTS {t}")
        self.run("""CREATE TEMP TABLE sxc_level AS
            SELECT parent AS ancestor, child AS descendant, 1 AS depth, 1::numeric AS paths, q AS qty FROM sxc_edge""")
        self.run("CREATE TEMP TABLE sxc_acc AS TABLE sxc_level")
        depth = 1
        while True:
            if self.count("SELECT count(*) FROM sxc_level") == 0:
                break
            if depth >= self.h.max_depth:
                # one more level would exceed max_depth: is there anything left to expand?
                more = self.count("SELECT count(*) FROM sxc_level l JOIN sxc_edge e ON e.parent = l.descendant")
                if more:
                    return {"depth_limit_exceeded": True, "levels": depth}
                break
            self.run("DROP TABLE IF EXISTS sxc_next")
            self.run("""CREATE TEMP TABLE sxc_next AS
                SELECT l.ancestor, e.child AS descendant, l.depth + 1 AS depth, sum(l.paths) AS paths, sum(l.qty * e.q) AS qty
                FROM sxc_level l JOIN sxc_edge e ON e.parent = l.descendant
                GROUP BY l.ancestor, e.child, l.depth""")
            self.run("INSERT INTO sxc_acc SELECT * FROM sxc_next")
            self.run("DROP TABLE sxc_level")
            self.run("ALTER TABLE sxc_next RENAME TO sxc_level")
            depth += 1
        self.run("""CREATE TEMP TABLE sxc_closure_new AS
            SELECT ancestor, descendant, min(depth) AS min_depth, max(depth) AS max_depth,
                   sum(paths) AS path_count, sum(qty) AS qty
            FROM sxc_acc GROUP BY ancestor, descendant""")
        self.run("ALTER TABLE sxc_closure_new ADD PRIMARY KEY (ancestor, descendant)")
        hist = self.run("SELECT max_depth, count(*) FROM sxc_closure_new GROUP BY max_depth ORDER BY max_depth")
        return {
            "depth_limit_exceeded": False,
            "pairs": self.count("SELECT count(*) FROM sxc_closure_new"),
            "paths_total": int(self.run("SELECT coalesce(sum(path_count), 0) FROM sxc_closure_new")[0][0]),
            "multi_path_pairs": self.count("SELECT count(*) FROM sxc_closure_new WHERE path_count > 1"),
            "max_depth": int(self.run("SELECT coalesce(max(max_depth), 0) FROM sxc_closure_new")[0][0]),
            "depth_histogram": {str(d): int(n) for d, n in hist},
        }

    def apply(self) -> dict:
        """MERGE the recomputed closure into the target table; prove equality afterwards."""
        exists = self.run("SELECT to_regclass(:t) IS NOT NULL", t=self.target)[0][0]
        if not exists:
            self.run(f"""CREATE TABLE {self.target} (LIKE sxc_closure_new INCLUDING DEFAULTS)""")
            self.run(f"ALTER TABLE {self.target} ADD PRIMARY KEY (ancestor, descendant)")
            self.run(f"CREATE INDEX ON {self.target} (descendant)")
        actions = self.run(f"""WITH m AS (
              MERGE INTO {self.target} t
              USING sxc_closure_new s ON t.ancestor = s.ancestor AND t.descendant = s.descendant
              WHEN MATCHED AND (t.min_depth, t.max_depth, t.path_count, t.qty)
                   IS DISTINCT FROM (s.min_depth, s.max_depth, s.path_count, s.qty) THEN
                UPDATE SET min_depth = s.min_depth, max_depth = s.max_depth, path_count = s.path_count, qty = s.qty
              WHEN NOT MATCHED BY TARGET THEN
                INSERT (ancestor, descendant, min_depth, max_depth, path_count, qty)
                VALUES (s.ancestor, s.descendant, s.min_depth, s.max_depth, s.path_count, s.qty)
              WHEN NOT MATCHED BY SOURCE THEN DELETE
              RETURNING merge_action() AS action
            ) SELECT action, count(*) FROM m GROUP BY action""")
        counts = {"insert": 0, "update": 0, "delete": 0}
        for action, n in actions:
            counts[action.lower()] = int(n)
        cols = ", ".join(naming.CLOSURE_COLUMNS)
        diff = self.count(f"""SELECT (SELECT count(*) FROM (SELECT {cols} FROM {self.target} EXCEPT ALL SELECT {cols} FROM sxc_closure_new) a)
                                   + (SELECT count(*) FROM (SELECT {cols} FROM sxc_closure_new EXCEPT ALL SELECT {cols} FROM {self.target}) b)""")
        self.run(f"ANALYZE {self.target}")
        return {"merge": counts, "table_vs_recompute_diff": diff}


def build(conn, cfg, h) -> tuple[list[tuple], list[str]]:
    """Build the closure of hierarchy h inside the caller's transaction (which holds the output schema's build lock).

    Returns (checks, sql_log) where checks are (check_id, status, config_ref, detail) tuples:
      closure:<h>        pass when acyclic (or on_cycle=report) and within max_depth
      closure_apply:<h>  pass when the MERGE result equals the recomputation (only when the closure was applied)
    """
    b = ClosureBuilder(conn, cfg, h)
    ref = f"hierarchies.{h.name}"
    checks = []
    stats = b.edges()
    cyc = b.cycle_nodes()
    stats["cycle_nodes"] = cyc
    if cyc:
        stats["cycle_examples"] = b.cycle_examples()
        if h.on_cycle == "error":
            checks.append((f"closure:{h.name}", "fail", ref, {**stats, "reason": "cycle", "closure_applied": False}))
            return checks, b.sql_log
        stats["dropped_cycle_edges"] = b.drop_cycle_edges()
    else:
        b.run(f"DROP TABLE IF EXISTS {b.cycles}")   # examples left by an earlier run describe cycles that are gone
    lv = b.levels()
    if lv.get("depth_limit_exceeded"):
        checks.append((f"closure:{h.name}", "fail", ref, {**stats, "reason": "depth_limit", "max_depth": h.max_depth, "closure_applied": False}))
        return checks, b.sql_log
    stats.update(lv)
    if stats["null_quantity_edges"]:
        stats["note"] = "edges with NULL quantity give NULL qty for affected pairs"
    checks.append((f"closure:{h.name}", "pass", ref, {**stats, "closure_applied": True}))
    ap = b.apply()
    checks.append((f"closure_apply:{h.name}", "pass" if ap["table_vs_recompute_diff"] == 0 else "fail", ref, ap))
    return checks, b.sql_log

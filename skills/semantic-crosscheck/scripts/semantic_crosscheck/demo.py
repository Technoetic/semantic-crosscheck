"""Synthetic parts / BOM / orders / stock demo (no real data).

Deterministic: an own xorshift64* PRNG (not `random`) so output is identical on every Python version.
`build(dirty)` returns rows plus an `expected` dict computed here independently (closure via DFS with
exact Decimal path products), which the integration tests compare against what the SQL stages produce.
`reference()` turns that dict into demo/expected.json for people and agents: which numbers apply to which
variant, which check to compare them with, and which check shows each planted defect.

Dirty variant adds, each counted in `expected`:
  1 cycle (2-node), 2 orphan children, 1 zero-quantity line, 2 negative stock rows, 3 orders with
  qty_open > qty_total, 1 raw part with a child (ontology must become inconsistent), 3 stock rows with
  whitespace/case variants of location codes (normalisation), 2 FIRM orders with out-of-range due dates,
  and 2 malformed CSV rows that PostgreSQL 17 `COPY ... ON_ERROR ignore` must skip.
The closure numbers are a reference for acyclic data only: build refuses the closure of the dirty variant
(on_cycle = "error"), so there they are None and marked as not compared.

load() reloads the demo tables in place (create the missing ones, TRUNCATE, COPY) instead of dropping them:
build creates views on these tables in the output schema, owned by the check role, and a DROP fails on those
dependencies (with CASCADE it would silently remove another role's objects).
"""

import csv
import datetime as dt
import io
from decimal import Decimal
from typing import NamedTuple

M64 = (1 << 64) - 1
QTYS = ["1", "2", "0.5", "0.25", "3", "1.5", "4", "0.125", "6", "0.75"]
SCHEMA = "sxc_demo"
CLOSURE_KEYS = ("closure_pairs", "paths_total", "multi_path_pairs", "max_depth", "depth_histogram", "qty_checksum")
LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext(:key))"
LOCK_KEY = f"semantic-crosscheck demo {SCHEMA}"


class Table(NamedTuple):
    columns: tuple      # (name, type as format_type() prints it), in table order
    key: tuple


TABLES = {
    "part": Table((("part_id", "text"), ("part_class", "text"), ("unit", "text"), ("procure", "text"), ("name", "text")),
                  ("part_id",)),
    "bom_line": Table((("parent_id", "text"), ("child_id", "text"), ("line_no", "integer"), ("qty_per", "numeric(18,6)")),
                      ("parent_id", "child_id", "line_no")),
    "sales_order": Table((("order_id", "text"), ("part_id", "text"), ("status", "text"), ("qty_total", "numeric(18,3)"),
                          ("qty_open", "numeric(18,3)"), ("due_date", "date")), ("order_id",)),
    "stock": Table((("part_id", "text"), ("location_id", "text"), ("qty", "numeric(18,3)")), ("part_id", "location_id")),
}

# planted defect -> (stage, check, what the check shows on the dirty variant)
REPORTED = {
    "cycle": ("build", "closure:has_component", "fail with reason cycle; verify then reports the gold queries over the "
                                                "closure as not_run"),
    "orphan_children": ("build", "shape:child_exists", "fail with violations = orphan_child_refs; reason shacl:child_exists "
                                                       "agrees"),
    "zero_qty_lines": ("build", "shape:qty_positive", "fail with violations = zero_qty_lines; reason shacl:qty_positive agrees"),
    "negative_stock_rows": ("build", "shape:stock_nonnegative", "pass (severity warning) with violations = "
                                                                "negative_stock_rows; reason shacl:stock_nonnegative agrees"),
    "open_gt_total": ("build", "shape:open_le_total", "fail with violations = open_gt_total (sql_violation: SQL only)"),
    "raw_with_child": ("reason", "owl:consistency", "fail: a raw material (ontology.no_children) has a child"),
    "malformed_csv_rows": ("demo", "demo:copy_on_error", "pass with skipped = malformed_csv_rows"),
}
# planted values no check reports: they exercise checks that pass on them
EXERCISED = {
    "location_variants": ("verify", ("v1:q03_stock", "v2:q03_stock:values", "v4:measure_normalization_drop:on_hand"),
                          "Cube and the gold SQL both normalise location codes, so q03 passes; the variants make the "
                          "normalisation mutant observable (witness = location_variants) and q03 kills it"),
    "out_of_range_due": ("verify", ("v1:q05_firm_window", "v2:q05_firm_window:param_sets"),
                         "the q05 date windows include these orders (1990-01-01 to 2100-12-31) or leave them out "
                         "(the 2026 windows); Cube and the gold SQL agree on both"),
}


class TableMismatch(Exception):
    """Demo tables exist with other columns than TABLES defines; the message names each difference."""


class Rng:
    def __init__(self, seed: int):
        self.s = (seed or 0x9E3779B97F4A7C15) & M64

    def next(self) -> int:
        x = self.s
        x ^= x >> 12
        x ^= (x << 25) & M64
        x ^= x >> 27
        self.s = x
        return (x * 0x2545F4914F6CDD1D) & M64

    def below(self, n: int) -> int:
        return self.next() % n

    def pick(self, seq):
        return seq[self.below(len(seq))]

    def chance(self, pct: int) -> bool:
        return self.below(100) < pct


def _pid(n: int) -> str:
    return f"P-{n:06d}"


def build(dirty: bool = False, seed: int = 20261013, scale: int = 1) -> dict:
    r = Rng(seed)
    counts = {"product": 150 * scale, "assembly": 450 * scale, "component": 900 * scale, "raw": 500 * scale}
    parts, level = [], {}
    n = 0
    for cls, k in counts.items():
        for _ in range(k):
            n += 1
            pid = _pid(n)
            if cls == "product":
                lv = 0
            elif cls == "assembly":
                lv = 1 + r.below(6)          # 1..6
            elif cls == "component":
                lv = 2 + r.below(6)          # 2..7
            else:
                lv = 8                        # raw materials are leaves
            level[pid] = lv
            unit = ("kg" if r.chance(70) else "m") if cls == "raw" else "ea"
            procure = "buy" if cls == "raw" else ("make" if cls in ("product", "assembly") else ("make" if r.chance(50) else "buy"))
            parts.append({"part_id": pid, "part_class": cls, "unit": unit, "procure": procure, "name": f"{cls} {n}"})
    by_class = {c: [p["part_id"] for p in parts if p["part_class"] == c] for c in counts}

    edges = []   # parent, child, line_no, qty

    def add_children(parent, pool_classes, kmin, kmax):
        # prefer the next one or two levels down: long chains and shared subassemblies (multi-path pairs)
        lv = level[parent]
        pool = [c for cl in pool_classes for c in by_class[cl] if lv < level[c] <= lv + 2]
        if not pool:
            pool = [c for cl in pool_classes for c in by_class[cl] if level[c] > lv]
        if not pool:
            return
        chosen = set()
        common = pool[: max(1, len(pool) // 5)]      # a fifth of the pool are "common parts" picked half the time
        for _ in range(kmin + r.below(kmax - kmin + 1)):
            c = r.pick(common) if r.chance(50) else r.pick(pool)
            if c in chosen:
                continue
            chosen.add(c)
            edges.append([parent, c, 1, r.pick(QTYS)])
            if r.chance(3):                        # a second BOM line for the same pair
                edges.append([parent, c, 2, r.pick(QTYS)])

    for p in by_class["product"]:
        add_children(p, ["assembly", "component"], 3, 7)
    for a in by_class["assembly"]:
        add_children(a, ["assembly", "component", "raw"], 2, 6)
    procure = {p["part_id"]: p["procure"] for p in parts}
    for c in by_class["component"]:
        if procure[c] == "make":
            add_children(c, ["raw"], 1, 2)

    orders = []
    start = dt.date(2026, 1, 5)
    sellable = by_class["product"] + by_class["assembly"][: 120 * scale]
    for i in range(1, 700 * scale + 1):
        st = r.pick(["FIRM"] * 55 + ["FORECAST"] * 30 + ["CLOSED"] * 15)
        total = 1 + r.below(50)
        open_ = 0 if st == "CLOSED" else r.below(total + 1)
        orders.append({"order_id": f"SO-{i:06d}", "part_id": r.pick(sellable), "status": st, "qty_total": str(total), "qty_open": str(open_),
                       "due_date": (start + dt.timedelta(days=r.below(357))).isoformat()})

    stock = []
    for p in parts:
        if r.chance(70):
            stock.append({"part_id": p["part_id"], "location_id": "MAIN", "qty": str(r.below(501))})
            if r.chance(30):
                stock.append({"part_id": p["part_id"], "location_id": "SUB", "qty": str(r.below(201))})
            if r.chance(10):
                stock.append({"part_id": p["part_id"], "location_id": "CONSIGN", "qty": str(r.below(101))})

    extra = {"cycle": 0, "orphan_children": 0, "zero_qty_lines": 0, "negative_stock_rows": 0, "open_gt_total": 0,
             "raw_with_child": 0, "location_variants": 0, "out_of_range_due": 0, "malformed_csv_rows": 0}
    malformed = []
    if dirty:
        aa = next((e for e in edges if e[0] in by_class["assembly"] and e[1] in by_class["assembly"]), None)
        edges.append([aa[1], aa[0], 1, "1"]); extra["cycle"] = 1
        edges.append([by_class["assembly"][0], "P-ORPHAN-1", 1, "1"])
        edges.append([by_class["assembly"][1], "P-ORPHAN-2", 1, "2"]); extra["orphan_children"] = 2
        edges.append([by_class["assembly"][2], by_class["raw"][0], 9, "0"]); extra["zero_qty_lines"] = 1
        edges.append([by_class["raw"][1], by_class["component"][0], 1, "1"]); extra["raw_with_child"] = 1
        neg = [s for s in stock if s["location_id"] == "MAIN"][:2]
        for s in neg:
            s["qty"] = "-" + (s["qty"] if s["qty"] != "0" else "5")
        extra["negative_stock_rows"] = 2
        for o in [o for o in orders if o["status"] == "FIRM"][:3]:
            o["qty_open"] = str(int(o["qty_total"]) + 5)
        extra["open_gt_total"] = 3
        have = {(s["part_id"], s["location_id"]) for s in stock}
        variants = 0
        for p, loc in ((by_class["component"][5], " main"), (by_class["component"][6], "Sub "), (by_class["component"][7], "main ")):
            if (p, loc) not in have:
                stock.append({"part_id": p, "location_id": loc, "qty": "7"}); variants += 1
        extra["location_variants"] = variants
        firm = [o for o in orders if o["status"] == "FIRM"]
        firm[10]["due_date"] = "2099-01-01"; firm[11]["due_date"] = "1999-12-31"
        extra["out_of_range_due"] = 2
        malformed = ["SO-BAD-1,P-000001,FIRM,not-a-number,1,2026-03-01", "SO-BAD-2,P-000002,FIRM,5,x,2026-03-02"]
        extra["malformed_csv_rows"] = 2

    expected = _expected(parts, edges, orders, stock, extra)
    return {"parts": parts, "edges": edges, "orders": orders, "stock": stock, "malformed": malformed, "expected": expected}


def _expected(parts, edges, orders, stock, extra) -> dict:
    """Independent reference numbers (Python), used by integration tests. The closure numbers (CLOSURE_KEYS) are
    None when the edges contain a cycle: build refuses that closure, so there is nothing to compare them with."""
    pair_qty = {}
    for p, c, _ln, q in edges:
        pair_qty[(p, c)] = pair_qty.get((p, c), Decimal(0)) + Decimal(q)
    children = {}
    for (p, c), q in pair_qty.items():
        children.setdefault(p, []).append((c, q))
    known = {x["part_id"] for x in parts}
    counts = {"parts": len(parts), "bom_lines": len(edges), "edges": len(pair_qty),
              "orphan_child_refs": sum(1 for (_p, c) in pair_qty if c not in known)}
    tail = {"orders": len(orders), "stock_rows": len(stock), **extra}
    if _has_cycle(children):
        return {**counts, **dict.fromkeys(CLOSURE_KEYS), "has_cycle": True, **tail}
    closure = {}

    def walk(root, node, depth, qty):
        for c, q in children.get(node, []):
            k = (root, c)
            mn, mx, pc, tq = closure.get(k, (depth + 1, depth + 1, 0, Decimal(0)))
            closure[k] = (min(mn, depth + 1), max(mx, depth + 1), pc + 1, tq + qty * q)
            walk(root, c, depth + 1, qty * q)

    for root in children:
        walk(root, root, 0, Decimal(1))
    hist = {}
    for (_a, _d), (_mn, mx, _pc, _q) in closure.items():
        hist[mx] = hist.get(mx, 0) + 1
    return {
        **counts,
        "closure_pairs": len(closure), "paths_total": sum(v[2] for v in closure.values()),
        "multi_path_pairs": sum(1 for v in closure.values() if v[2] > 1),
        "max_depth": max((v[1] for v in closure.values()), default=0),
        "depth_histogram": {str(k): v for k, v in sorted(hist.items())},
        "qty_checksum": str(sum((v[3] for v in closure.values()), Decimal(0)).normalize()),
        "has_cycle": False, **tail,
    }


def _has_cycle(children: dict) -> bool:
    """Depth-first search with an explicit stack: a node met again while it is still on the path closes a cycle."""
    state = {}   # node -> 1 while on the current path, 2 when finished
    for root in children:
        if root in state:
            continue
        state[root] = 1
        stack = [(root, iter(children[root]))]
        while stack:
            node, it = stack[-1]
            child = next(it, None)
            if child is None:
                state[node] = 2
                stack.pop()
                continue
            c = child[0]
            if state.get(c) == 1:
                return True
            if c not in state:
                state[c] = 1
                stack.append((c, iter(children.get(c, []))))
    return False


def reference(expected: dict, variant: str, seed: int, scale: int) -> dict:
    """demo/expected.json: the reference numbers grouped by what they are compared with, the variants they apply
    to, and for every planted defect the check that shows it (or the checks it exercises without being reported)."""
    acyclic = not expected["has_cycle"]
    closure = {"applies_to": ["clean"], "compared": acyclic}
    if acyclic:
        closure["compare_with"] = ("build closure:has_component (detail pairs, paths_total, multi_path_pairs, max_depth, "
                                   "depth_histogram), the sum of qty in the closure table (qty_checksum), reason "
                                   "v5:closure:has_component (owl_pairs, sql_pairs) and verify q04_depth_histogram")
        closure.update({k: expected[k] for k in CLOSURE_KEYS})
    else:
        closure["not_compared"] = ("the data has a cycle and hierarchies.has_component.on_cycle = \"error\", so build "
                                   "refuses the closure (closure:has_component fails with reason cycle) and there is no "
                                   "closure table to compare with")
    planted = {}
    for key, (stage, check, outcome) in REPORTED.items():
        planted[key] = {"count": expected[key], "reported": True, "stage": stage, "check": check, "dirty_outcome": outcome}
    for key, (stage, checks, note) in EXERCISED.items():
        planted[key] = {"count": expected[key], "reported": False, "stage": stage, "exercised_by": list(checks),
                        "dirty_outcome": "not reported: " + note}
    return {
        "variant": variant, "seed": seed, "scale": scale,
        "about": "reference numbers computed in Python by demo.py, independently of the SQL stages, for "
                 "demo/crosscheck.toml; each section names the variants it applies to and the checks to compare it with",
        "rows": {"applies_to": ["clean", "dirty"],
                 "compare_with": "demo demo:rows:<table> (detail loaded; sales_order counts the valid orders) and "
                                 "demo:copy_on_error (detail skipped = malformed_csv_rows)",
                 **{k: expected[k] for k in ("parts", "bom_lines", "orders", "stock_rows", "malformed_csv_rows")}},
        "graph": {"applies_to": ["clean", "dirty"],
                  "compare_with": "build closure:has_component (detail edges; cycle_nodes > 0 exactly when has_cycle) "
                                  "and shape:child_exists (detail violations = orphan_child_refs)",
                  **{k: expected[k] for k in ("edges", "orphan_child_refs", "has_cycle")}},
        "closure": closure,
        "planted_defects": {"applies_to": ["dirty"], "note": "every count is 0 in the clean variant", **planted},
    }


def create_sql(name: str) -> str:
    t = TABLES[name]
    cols = ", ".join(f"{c} {typ} NOT NULL" for c, typ in t.columns)
    return f"CREATE TABLE {SCHEMA}.{name} ({cols}, PRIMARY KEY ({', '.join(t.key)}))"


def existing_tables(conn) -> dict:
    """{table: (relkind, {column: type})} for the demo tables that already exist in SCHEMA."""
    found = {}
    rows = conn.run("""SELECT c.relname, c.relkind, a.attname, format_type(a.atttypid, a.atttypmod)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE n.nspname = :s AND c.relname = ANY(:t)
        ORDER BY c.relname, a.attnum""", s=SCHEMA, t=list(TABLES))
    for name, kind, column, typ in rows or []:
        entry = found.setdefault(name, (kind, {}))
        if column is not None:
            entry[1][column] = typ
    return found


def table_problems(found: dict) -> list[str]:
    """How each existing demo table differs from TABLES (column names and types; order does not matter)."""
    problems = []
    for name in TABLES:
        if name not in found:
            continue
        kind, have = found[name]
        if kind not in ("r", "p"):
            problems.append(f"{SCHEMA}.{name} is not a table")
            continue
        want = dict(TABLES[name].columns)
        parts = []
        missing = [c for c in want if c not in have]
        unexpected = [c for c in have if c not in want]
        if missing:
            parts.append("missing column " + ", ".join(missing))
        if unexpected:
            parts.append("unexpected column " + ", ".join(unexpected))
        parts += [f"{c} is {have[c]} instead of {want[c]}" for c in want if c in have and have[c] != want[c]]
        if parts:
            problems.append(f"{SCHEMA}.{name}: " + "; ".join(parts))
    return problems


def _csv(rows, cols) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for row in rows:
        w.writerow([row[c] for c in cols] if isinstance(row, dict) else row)
    return buf.getvalue()


def load(conn, data: dict) -> dict:
    """Reload the demo tables inside the caller's transaction and return the loaded row counts.

    Concurrent demo runs wait for each other (advisory lock). The schema is created only when missing (an existing
    schema may belong to a loader role without CREATE on the database), and so is each table (a loader that owns
    the tables needs no CREATE on the schema). Existing tables must have the columns of TABLES (TableMismatch,
    raised before anything is written); they are emptied with TRUNCATE, so views built on them stay valid. Rows
    go in with COPY; orders use PG17 `ON_ERROR ignore` so malformed rows are skipped."""
    conn.run(LOCK_SQL, key=LOCK_KEY)
    if not conn.run("SELECT 1 FROM pg_namespace WHERE nspname = :s", s=SCHEMA):
        conn.run(f"CREATE SCHEMA {SCHEMA}")
    found = existing_tables(conn)
    problems = table_problems(found)
    if problems:
        raise TableMismatch("; ".join(problems))
    for name in TABLES:
        if name not in found:
            conn.run(create_sql(name))
    conn.run("TRUNCATE " + ", ".join(f"{SCHEMA}.{name}" for name in TABLES))
    conn.run(f"COPY {SCHEMA}.part (part_id, part_class, unit, procure, name) FROM STDIN WITH (FORMAT csv)",
             stream=io.StringIO(_csv(data["parts"], ["part_id", "part_class", "unit", "procure", "name"])))
    conn.run(f"COPY {SCHEMA}.bom_line (parent_id, child_id, line_no, qty_per) FROM STDIN WITH (FORMAT csv)",
             stream=io.StringIO(_csv(data["edges"], [])))
    orders_csv = _csv(data["orders"], ["order_id", "part_id", "status", "qty_total", "qty_open", "due_date"]) + "".join(m + "\n" for m in data["malformed"])
    conn.run(f"COPY {SCHEMA}.sales_order (order_id, part_id, status, qty_total, qty_open, due_date) FROM STDIN WITH (FORMAT csv, ON_ERROR ignore)",
             stream=io.StringIO(orders_csv))
    conn.run(f"COPY {SCHEMA}.stock (part_id, location_id, qty) FROM STDIN WITH (FORMAT csv)",
             stream=io.StringIO(_csv(data["stock"], ["part_id", "location_id", "qty"])))
    loaded = {t: conn.run(f"SELECT count(*) FROM {SCHEMA}.{t}")[0][0] for t in ("part", "bom_line", "sales_order", "stock")}
    loaded["sales_order_skipped"] = len(data["orders"]) + len(data["malformed"]) - loaded["sales_order"]
    return loaded

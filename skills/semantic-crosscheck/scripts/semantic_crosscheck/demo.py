"""Synthetic parts / BOM / orders / stock demo (no real data).

Deterministic: an own xorshift64* PRNG (not `random`) so output is identical on every Python version.
`build(dirty)` returns rows plus an `expected` dict computed here independently (closure via DFS with
exact Decimal path products), which the integration tests compare against what the SQL stages produce.

Dirty variant adds, each counted in `expected`:
  1 cycle (2-node), 2 orphan children, 1 zero-quantity line, 2 negative stock rows, 3 orders with
  qty_open > qty_total, 1 raw part with a child (ontology must become inconsistent), 3 stock rows with
  whitespace/case variants of location codes (normalisation), 2 FIRM orders with out-of-range due dates,
  and 2 malformed CSV rows that PostgreSQL 17 `COPY ... ON_ERROR ignore` must skip.
"""

import csv
import datetime as dt
import io
from decimal import Decimal

M64 = (1 << 64) - 1
QTYS = ["1", "2", "0.5", "0.25", "3", "1.5", "4", "0.125", "6", "0.75"]
SCHEMA = "sxc_demo"


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
    """Independent reference numbers (Python), used by integration tests."""
    pair_qty = {}
    for p, c, _ln, q in edges:
        pair_qty[(p, c)] = pair_qty.get((p, c), Decimal(0)) + Decimal(q)
    children = {}
    for (p, c), q in pair_qty.items():
        children.setdefault(p, []).append((c, q))
    known = {x["part_id"] for x in parts}
    closure = {}
    cyc = False

    def walk(root, node, depth, qty, path):
        nonlocal cyc
        for c, q in children.get(node, []):
            if c in path:
                cyc = True
                continue
            k = (root, c)
            mn, mx, pc, tq = closure.get(k, (depth + 1, depth + 1, 0, Decimal(0)))
            closure[k] = (min(mn, depth + 1), max(mx, depth + 1), pc + 1, tq + qty * q)
            walk(root, c, depth + 1, qty * q, path | {c})

    for root in children:
        walk(root, root, 0, Decimal(1), {root})
    hist = {}
    for (_a, _d), (_mn, mx, _pc, _q) in closure.items():
        hist[mx] = hist.get(mx, 0) + 1
    return {
        "parts": len(parts), "bom_lines": len(edges), "edges": len(pair_qty),
        "orphan_child_refs": sum(1 for (_p, c) in pair_qty if c not in known),
        "closure_pairs": len(closure), "paths_total": sum(v[2] for v in closure.values()),
        "multi_path_pairs": sum(1 for v in closure.values() if v[2] > 1),
        "max_depth": max((v[1] for v in closure.values()), default=0),
        "depth_histogram": {str(k): v for k, v in sorted(hist.items())},
        "qty_checksum": str(sum((v[3] for v in closure.values()), Decimal(0)).normalize()),
        "has_cycle": cyc, "orders": len(orders), "stock_rows": len(stock), **extra,
    }


DDL = f"""
DROP TABLE IF EXISTS {SCHEMA}.bom_line, {SCHEMA}.sales_order, {SCHEMA}.stock, {SCHEMA}.part;
CREATE TABLE {SCHEMA}.part (part_id text PRIMARY KEY, part_class text NOT NULL, unit text NOT NULL, procure text NOT NULL, name text NOT NULL);
CREATE TABLE {SCHEMA}.bom_line (parent_id text NOT NULL, child_id text NOT NULL, line_no int NOT NULL, qty_per numeric(18,6) NOT NULL,
  PRIMARY KEY (parent_id, child_id, line_no));
CREATE TABLE {SCHEMA}.sales_order (order_id text PRIMARY KEY, part_id text NOT NULL, status text NOT NULL,
  qty_total numeric(18,3) NOT NULL, qty_open numeric(18,3) NOT NULL, due_date date NOT NULL);
CREATE TABLE {SCHEMA}.stock (part_id text NOT NULL, location_id text NOT NULL, qty numeric(18,3) NOT NULL, PRIMARY KEY (part_id, location_id));
"""


def _csv(rows, cols) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for row in rows:
        w.writerow([row[c] for c in cols] if isinstance(row, dict) else row)
    return buf.getvalue()


def load(conn, data: dict) -> dict:
    """(Re)create the sxc_demo tables and load the rows with COPY. Orders use PG17 `ON_ERROR ignore` so malformed
    rows are skipped. The schema is created only when missing (an existing schema may belong to a loader role
    without CREATE on the database)."""
    if not conn.run("SELECT 1 FROM pg_namespace WHERE nspname = :s", s=SCHEMA):
        conn.run(f"CREATE SCHEMA {SCHEMA}")
    for stmt in [s for s in DDL.split(";") if s.strip()]:
        conn.run(stmt)
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

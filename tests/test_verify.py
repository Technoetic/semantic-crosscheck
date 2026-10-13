"""Integration tests of the verify stage against PostgreSQL 17 and, when installed, the Cube adapter.

  SweepStrataTest   (database)         bindings() against an independent Python classification of the demo
                                       graph: stratum membership, k per stratum and the md5(id || seed) order,
                                       with the closure table and with the bounded recursion that replaces it.
  DirtySweepTest    (database)         the dirty variant (a cycle, so no closure) still samples valid strata.
  VerifyStageTest   (database + Cube)  the clean demo passes every V1/V2/V3 check; a closure that did not pass
                                       in build turns the closure checks into not_run; a bom_line quantity
                                       changed after the closure was built fails exactly the bindings that reach
                                       the changed edge, and nothing else.

Database names start with sxc_t_verify. Skipped unless SXC_TEST_ADMIN_DSN names a superuser connection and, for
the Cube tests, the adapter is installed (SXC_CUBE_HOME or the per-user default) with node on PATH.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import pgfixture
from semantic_crosscheck import closure, config, cube_gen, cube_run, db, stage_verify, sweep
from semantic_crosscheck.result import Result

CUBE_READY = cube_run.installed(cube_run.cube_home()) and bool(shutil.which("node"))
ROW_ID = re.compile(r"P-\d{6}|P-ORPHAN")
V2_STRATA = {"q01_explosion": ["top", "mid", "deep", "leaf_parent", "empty"], "q02_where_used": ["shared", "mid", "leaf", "top"],
             "q03_stock": ["values"], "q04_depth_histogram": ["all"], "q05_firm_window": ["param_sets"]}
V3_TESTS = ["total:m_firm_open.value", "total:m_firm_open_dated.value", "total:m_on_hand.value", "total:m_consigned.value",
            "total:has_component_closure.qty", "total:has_component_closure.pairs",
            "additive:m_firm_open.value:part.part_class", "additive:m_on_hand.value:part.part_class",
            "additive:m_consigned.value:part.part_class", "pair:has_component_closure.qty+m_firm_open.value",
            "pair:m_firm_open.value+m_on_hand.value", "pair:m_firm_open.value+m_consigned.value",
            "pair:m_on_hand.value+m_consigned.value"]
CLOSURE_V3 = {"total:has_component_closure.qty", "total:has_component_closure.pairs", "pair:has_component_closure.qty+m_firm_open.value"}


def graph(data: dict) -> tuple[set, dict, dict]:
    """(node-entity keys, children, parents) of the generated rows."""
    children: dict[str, set] = {}
    parents: dict[str, set] = {}
    for p, c, _line, _q in data["edges"]:
        children.setdefault(p, set()).add(c)
        parents.setdefault(c, set()).add(p)
    return {p["part_id"] for p in data["parts"]}, children, parents


def classify(data: dict) -> dict[str, set]:
    """The strata of design section 7, computed in Python from the generated rows."""
    parts, children, parents = graph(data)
    nodes = parts | set(children) | set(parents)

    def has_children(n):
        return bool(children.get(n))

    def has_parents(n):
        return bool(parents.get(n))

    return {
        "top": {n for n in nodes if has_children(n) and not has_parents(n)},
        "mid": {n for n in nodes if has_children(n) and has_parents(n)},
        "leaf": {n for n in nodes if has_parents(n) and not has_children(n)},
        "leaf_parent": {n for n in nodes if has_children(n) and not any(has_children(c) for c in children[n])},
        "deep": {n for n in nodes if has_children(n)},
        "shared": {n for n in nodes if len(parents.get(n, ())) >= 2},
        "empty": {n for n in parts if not has_children(n)},
    }


def heights(data: dict) -> dict[str, int]:
    """Longest downward path of every node with children (acyclic data only)."""
    _parts, children, _parents = graph(data)
    memo: dict[str, int] = {}

    def h(n):
        if n not in memo:
            memo[n] = 1 + max(h(c) for c in children[n]) if children.get(n) else 0
        return memo[n]

    return {n: h(n) for n in children}


def md5_key(node: str, seed: int) -> str:
    return hashlib.md5(f"{node}{seed}".encode("utf-8")).hexdigest()


def expected_bindings(cfg, gold, data: dict) -> list:
    (name, p), = gold.params.items()
    strata, height, seed = classify(data), heights(data), cfg.verify.seed
    k = sweep.per_stratum(cfg, gold)
    out = []
    for s in p.strata:
        order = (lambda n: (-height[n], md5_key(n, seed), n)) if s == "deep" else (lambda n: (md5_key(n, seed), n))
        out.extend([(s, {name: n}) for n in sorted(strata[s], key=order)[:k]] or [(s, None)])
    return out


def reach(start: str, nexts: dict) -> set:
    """start and every node reachable from it through nexts (children: descendants, parents: ancestors)."""
    seen, todo = {start}, [start]
    while todo:
        for n in nexts.get(todo.pop(), ()):
            if n not in seen:
                seen.add(n)
                todo.append(n)
    return seen


def gold_of(cfg, gid: str):
    return next(g for g in cfg.gold if g.id == gid)


def activate(fx):
    os.environ["SXC_PG_DSN"] = pgfixture.dsn_for(fx.check, fx.name)
    os.environ["SXC_PG_OWNER_DSN"] = pgfixture.dsn_for(fx.owner, fx.name)


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run integration tests")
class SweepStrataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture("sxc_t_verify_sweep", "clean").setup()
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)
        try:
            cls.conn = cls.fx.check_conn()
        except BaseException:
            cls.fx.teardown()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.fx.teardown()

    def setUp(self):
        activate(self.fx)

    def test_strata_follow_the_reference_with_and_without_the_closure_table(self):
        table = db.scalar(self.conn, "SELECT to_regclass(:t) IS NOT NULL", t="sxc_out.has_component_closure")
        self.assertFalse(table, "the closure table must not exist yet: the first pass uses the recursion")
        golds = [gold_of(self.cfg, "q01_explosion"), gold_of(self.cfg, "q02_where_used")]
        recursive = {g.id: sweep.bindings(self.conn, self.cfg, g) for g in golds}
        with db.write_tx(self.conn):
            checks, _log = closure.build(self.conn, self.cfg, self.cfg.hierarchy())
        self.assertEqual(checks[0][1], "pass")
        for g in golds:
            want = expected_bindings(self.cfg, g, self.fx.data)
            self.assertEqual(recursive[g.id], want, f"{g.id} without the closure table")
            self.assertEqual(sweep.bindings(self.conn, self.cfg, g), want, f"{g.id} with the closure table")
            k = sweep.per_stratum(self.cfg, g)
            for s in g.params[next(iter(g.params))].strata:
                n = sum(1 for st, p in want if st == s and p is not None)
                self.assertEqual(n, min(k, len(classify(self.fx.data)[s])), s)

    def test_values_param_sets_and_no_parameters(self):
        classes = sorted({p["part_class"] for p in self.fx.data["parts"]})
        self.assertEqual(sweep.bindings(self.conn, self.cfg, gold_of(self.cfg, "q03_stock")),
                         [("values", {"part_class": c}) for c in classes])
        self.assertEqual(sweep.bindings(self.conn, self.cfg, gold_of(self.cfg, "q04_depth_histogram")), [("all", {})])
        q05 = gold_of(self.cfg, "q05_firm_window")
        self.assertEqual(sweep.bindings(self.conn, self.cfg, q05), [("param_sets", ps) for ps in q05.param_sets])

    def test_sampling_leaves_no_transaction_open(self):
        sweep.bindings(self.conn, self.cfg, gold_of(self.cfg, "q01_explosion"))
        self.assertEqual(db.scalar(self.conn, "SHOW transaction_read_only"), "off")
        self.assertTrue(db.scalar(self.conn, "SELECT now() = statement_timestamp()"), "a transaction is still open")


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run integration tests")
class DirtySweepTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture("sxc_t_verify_dirty", "dirty").setup()
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)
        try:
            cls.conn = cls.fx.check_conn()
        except BaseException:
            cls.fx.teardown()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.fx.teardown()

    def test_cycle_without_closure_still_samples_valid_strata(self):
        activate(self.fx)
        strata, seed = classify(self.fx.data), self.cfg.verify.seed
        for gid in ("q01_explosion", "q02_where_used"):
            g = gold_of(self.cfg, gid)
            k = sweep.per_stratum(self.cfg, g)
            got = sweep.bindings(self.conn, self.cfg, g)
            for s in g.params[next(iter(g.params))].strata:
                ids = [next(iter(p.values())) for st, p in got if st == s and p is not None]
                self.assertEqual(len(ids), min(k, len(strata[s])), f"{gid} {s}")
                self.assertTrue(set(ids) <= strata[s], f"{gid} {s}")
                if s != "deep":
                    self.assertEqual(ids, sorted(strata[s], key=lambda n: (md5_key(n, seed), n))[:k], f"{gid} {s}")
        self.assertIn("P-ORPHAN-1", strata["leaf"])
        self.assertNotIn("P-ORPHAN-1", strata["empty"])


@unittest.skipUnless(pgfixture.available() and CUBE_READY,
                     "needs SXC_TEST_ADMIN_DSN and the Cube adapter (SXC_CUBE_HOME) with node on PATH")
class VerifyStageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = pgfixture.PgFixture("sxc_t_verify_stage", "clean").setup()
        cls.tmp = Path(tempfile.mkdtemp(prefix="sxc_t_verify_"))
        cls.out = cls.tmp / "out"
        cls.cfg = config.load(pgfixture.DEMO_CONFIG)
        try:
            cls.prepare_stages()
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def prepare_stages(cls):
        """build (closure) and cube (generated model) results, as the real stages leave them."""
        conn = cls.fx.check_conn()
        try:
            build = Result("build")
            with db.write_tx(conn):
                checks, _log = closure.build(conn, cls.cfg, cls.cfg.hierarchy())
            for cid, status, ref, detail in checks:
                build.add(cid, status, ref, **detail)
            build.write(cls.out)
            with db.read_only(conn):
                model, _notes = cube_gen.generate(cls.cfg, conn)
        finally:
            conn.close()
        cube_gen.write_model(model, cls.out / "cube" / "model")
        cube = Result("cube")
        cube.add("cube:model", "pass", "", cubes=len(model["cubes"]))
        cube.write(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.fx.teardown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        activate(self.fx)

    def verify(self, out: Path, only: list[str], cfg=None) -> Result:
        res = stage_verify.run(cfg or self.cfg, argparse.Namespace(only=only, gold=[]), out)
        self.assertIsNone(ROW_ID.search(json.dumps(res.to_dict())), "results must not contain row values")
        return res

    def test_clean_demo_passes_every_check(self):
        res = self.verify(self.out, ["v1", "v2", "v3"])
        failing = [(c.id, c.status, c.detail) for c in res.checks if c.status != "pass"]
        self.assertEqual(failing, [("scope:v4", "not_run", {"reason": "excluded_by_only"})])
        want = ([f"v1:{g}" for g in V2_STRATA] + [f"v2:{g}:{s}" for g, ss in V2_STRATA.items() for s in ss]
                + [f"v3:{t}" for t in V3_TESTS] + ["scope:v4"])
        self.assertEqual([c.id for c in res.checks], want)
        self.assertEqual(res.status, "not_run", "--only left v4 out, so the run is not a pass")
        self.assertIn("without them", res.next_safe_action)
        self.assertEqual(res.not_proven, [])
        v2 = {c.id: c.detail for c in res.checks if c.id.startswith("v2:")}
        self.assertEqual(v2["v2:q01_explosion:top"]["bindings"], 8)
        self.assertEqual(v2["v2:q02_where_used:shared"]["bindings"], 10)
        self.assertEqual(v2["v2:q01_explosion:empty"]["empty_bindings"], 8, "roots without children: both sides empty")
        self.assertGreater(v2["v2:q02_where_used:shared"]["declared_null_as_zero"], 0)
        self.assertTrue((self.out / "verify" / "server.log").is_file())

    def test_closure_that_did_not_pass_turns_closure_checks_into_not_run(self):
        out = self.tmp / "gated"
        shutil.copytree(self.out / "cube", out / "cube", dirs_exist_ok=True)
        build = Result("build")
        build.add("closure:has_component", "fail", "hierarchies.has_component", reason="cycle")
        build.write(out)
        res = self.verify(out, ["v1", "v3"])
        status = {c.id: c.status for c in res.checks if not c.id.startswith("scope:")}
        self.assertEqual([c.id for c in res.checks if c.id.startswith("scope:")], ["scope:v2", "scope:v4"])
        gated = {"v1:q01_explosion", "v1:q02_where_used", "v1:q04_depth_histogram"} | {f"v3:{t}" for t in CLOSURE_V3}
        self.assertEqual(len(status), 5 + len(V3_TESTS))
        self.assertEqual({cid for cid, st in status.items() if st == "not_run"}, gated)
        self.assertEqual({cid: st for cid, st in status.items() if cid not in gated and st != "pass"}, {})
        for c in res.checks:
            if c.id in gated:
                self.assertEqual((c.config_ref, c.detail["reason"], c.detail["build_check"]),
                                 ("hierarchies.has_component", "closure_not_passed", "fail"))
        self.assertEqual(len(res.not_proven), len(gated))
        self.assertEqual(res.status, "not_run")

    def test_quantity_changed_after_build_fails_exactly_the_bindings_that_reach_it(self):
        conn = self.fx.check_conn()
        try:
            plans = {gid: sweep.bindings(conn, self.cfg, gold_of(self.cfg, gid)) for gid in ("q01_explosion", "q02_where_used")}
        finally:
            conn.close()
        root = next(p["root"] for _s, p in plans["q01_explosion"] if p)
        part = next(p["part"] for _s, p in plans["q02_where_used"] if p)
        owner = db.connect("SXC_PG_OWNER_DSN")
        try:
            lines = {tuple(owner.run("SELECT parent_id, child_id, line_no FROM sxc_demo.bom_line WHERE parent_id = :p "
                                     "ORDER BY child_id, line_no LIMIT 1", p=root)[0]),
                     tuple(owner.run("SELECT parent_id, child_id, line_no FROM sxc_demo.bom_line WHERE child_id = :c "
                                     "ORDER BY parent_id, line_no LIMIT 1", c=part)[0])}
            self.change(owner, lines, "+")
            try:
                res = self.verify(self.out, ["v1", "v2", "v3"])
            finally:
                self.change(owner, lines, "-")
        finally:
            owner.close()

        _parts, children, parents = graph(self.fx.data)
        reached_roots = set().union(*(reach(a, parents) for a, _b, _n in lines))     # the edge's parent and its ancestors
        reached_parts = set().union(*(reach(b, children) for _a, b, _n in lines))    # the edge's child and its descendants
        checks = {c.id: c for c in res.checks}
        for gid, reached in (("q01_explosion", reached_roots), ("q02_where_used", reached_parts)):
            v1 = checks[f"v1:{gid}"]
            self.assertEqual(v1.status, "fail", gid)
            self.assertEqual(set(v1.detail["mismatched_columns"]), {"qty"}, gid)
            for s in V2_STRATA[gid]:
                c = checks[f"v2:{gid}:{s}"]
                bound = [next(iter(p.values())) for st, p in plans[gid] if st == s and p]
                hit = sum(1 for n in bound if n in reached)
                self.assertEqual((c.detail["failed_bindings"], c.detail["error_bindings"]), (hit, 0), c.id)
                self.assertEqual(c.status, "fail" if hit else "pass", c.id)
                if hit:
                    self.assertEqual(set(c.detail["mismatched_columns"]), {"qty"}, c.id)
        self.assertEqual(checks["v2:q01_explosion:top"].status, "fail")
        self.assertEqual(checks["v2:q02_where_used:shared"].status, "fail")
        untouched = [c for c in res.checks if c.id.startswith("v3:") or re.match(r"v[12]:q0[345]_", c.id)]
        self.assertEqual([(c.id, c.status) for c in untouched if c.status != "pass"], [])
        self.assertEqual(res.status, "fail")
        self.assertIn("v1:q01_explosion", res.next_safe_action)

    def test_mutation_suite_kills_observable_mutants(self):
        cfg = config.load(pgfixture.DEMO_CONFIG)
        cfg.verify.mutation_operators = ["closure_qty_single_path", "closure_join_swap"]
        res = self.verify(self.tmp / "out", ["v4"], cfg)        # v1-v3 not requested: the baseline runs on its own
        checks = {c.id: c for c in res.checks if not c.id.startswith("scope:")}
        self.assertEqual(set(checks), {"v4:closure_qty_single_path:has_component", "v4:closure_join_swap:firm_open"})
        for c in checks.values():
            self.assertEqual(c.status, "pass", c.detail)
            self.assertRegex(c.detail["killed_by"], r"^v[123]:\S+$")
        self.assertEqual(res.not_proven, [])
        self.assertEqual({c.id for c in res.checks if c.status == "not_run"}, {"scope:v1", "scope:v2", "scope:v3"})

    @staticmethod
    def change(owner, lines: set, sign: str):
        with db.write_tx(owner):
            for parent, child, line_no in lines:
                owner.run(f"UPDATE sxc_demo.bom_line SET qty_per = qty_per {sign} 1 "
                          "WHERE parent_id = :p AND child_id = :c AND line_no = :n", p=parent, c=child, n=line_no)


if __name__ == "__main__":
    unittest.main()

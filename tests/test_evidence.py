"""The evidence-kind registry (evidence.py): every check id of the pipeline stages has one kind.

Why: a reader of a report must be able to tell whether a passing check compared a generated layer with something the
generators did not produce (authored gold SQL, an independent recomputation) or only generated layers with each
other. The registry is ordered, first match wins and it has no default row, so a new id family is `unclassified`
until a rule names it. These tests hold every rule row, the catalogue of docs/design.md section 6 and the rendered
table of section 8 together, and they pin that the only recomputed row reads no generator module at import time.
"""

import ast
import unittest

import pgfixture
import test_docs_catalogue as catalogue
from semantic_crosscheck import config, cube_gen, evidence, naming, report, shape_q

PACKAGE = catalogue.PACKAGE
REGISTRY_STAGES = {*report.PIPELINE, "report", "semantic-preflight"}
# Module-level import closure of the expected side of each recomputed row (the module itself left out).
RECOMPUTED_CLOSURES = {
    "closure_oracle": {"canon", "compare", "constraints", "cube_client", "db", "ident", "naming", "result",
                       "result_contract"},
}
# Tokens of the rendered table whose check is created by a later phase; the catalogue row comes with the check.
NOT_YET_CATALOGUED: set[str] = set()


def design_table() -> str:
    """The table under "### Evidence kinds" in section 8 of docs/design.md."""
    text = catalogue.DESIGN.read_text(encoding="utf-8")
    section = text.split("\n## 8. Report", 1)[1].split("\n## ", 1)[0]
    sub = section.split("\n### Evidence kinds\n", 1)[1].split("\n### ", 1)[0]
    lines = sub.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("|"))
    end = next((i for i in range(start, len(lines)) if not lines[i].startswith("|")), len(lines))
    return "\n".join(lines[start:end])


def import_closure(module: str) -> set[str]:
    """Engine modules reached from module by imports that run when it is imported (not those inside functions)."""
    def direct(name: str) -> set[str]:
        tree = ast.parse((PACKAGE / f"{name}.py").read_text(encoding="utf-8"))
        found, todo = set(), list(tree.body)
        while todo:
            node = todo.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(node, ast.ImportFrom):
                if node.level == 1 and node.module:
                    found.add(node.module.split(".")[0])
                elif node.level == 1:
                    found.update(alias.name for alias in node.names)
                elif node.level == 0 and (node.module or "").split(".")[0] == "semantic_crosscheck":
                    rest = node.module.split(".")[1:]
                    found.update(rest[:1] or [alias.name for alias in node.names])
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    if parts[0] == "semantic_crosscheck" and len(parts) > 1:
                        found.add(parts[1])
            todo.extend(ast.iter_child_nodes(node))
        return {d for d in found if (PACKAGE / f"{d}.py").is_file()}

    seen, todo = set(), [module]
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo.extend(direct(name) - seen)
    return seen - {module}


class RegistryRules(unittest.TestCase):
    # (row number of the rendered table, stage, check id, kind): one or more ids per row
    EXAMPLES = (
        (1, "build", "stage", "structural"),
        (1, "verify", "data_revision:stable", "structural"),
        (1, "report", "data_revision:current", "structural"),
        (1, "verify", "runtime_evidence:stable", "structural"),
        (1, "report", "runtime_evidence:current", "structural"),
        (2, "doctor", "reason:java", "structural"),
        (2, "doctor", "privileges:select:part", "structural"),
        (2, "demo", "demo:rows:part", "structural"),
        (2, "cube", "cube:meta", "structural"),
        (2, "cube", "cube:gold_members:q01_explosion", "structural"),
        (2, "semantic_preflight", "input_validation", "structural"),
        (2, "semantic-preflight", "contract_0", "structural"),
        (2, "report", "stage:verify", "structural"),
        (3, "build", "shape:no_negative_qty", "structural"),
        (3, "build", "closure:has_component", "structural"),
        (3, "build", "rule_view:r1", "structural"),
        (3, "build", "rule_subjects:view", "structural"),
        (4, "build", "closure_apply:has_component", "generated"),
        (5, "reason", "shacl:meta:generated", "structural"),
        (5, "reason", "owl:consistency", "structural"),
        (5, "reason", "owl:explanation", "structural"),
        (5, "reason", "scope:shacl", "structural"),
        (5, "reason", "scope:owl", "structural"),
        (5, "reason", "v5:witness:assembly", "structural"),
        (6, "reason", "shacl:pair:due_after_order", "generated"),
        (6, "reason", "shacl:no_negative_qty", "generated"),
        (6, "reason", "v5:defined:assembly", "generated"),
        (6, "reason", "v5:closure:has_component", "generated"),
        (7, "verify", "v1:q01_explosion", "authored"),
        (7, "verify", "v2:q01_explosion:top", "authored"),
        (8, "verify", "v3:closure_oracle:has_component", "recomputed"),
        (9, "verify", "v3:total:m_on_hand.value", "generated"),
        (9, "verify", "v3:total:has_component_closure.qty", "generated"),
        (9, "verify", "v3:additive:m_on_hand.value:part.part_class", "generated"),
        (9, "verify", "v3:pair:m_on_hand.value+m_consigned.value", "generated"),
        (9, "verify", "v3w:pair_witness:m_on_hand.value+m_consigned.value", "generated"),
        (10, "verify", "v4:suite", "structural"),
        (10, "verify", "scope:v1", "structural"),
        (10, "verify", "scope:v2", "structural"),
        (10, "verify", "scope:v3", "structural"),
        (10, "verify", "scope:v4", "structural"),
        (10, "verify", "scope:gold:q01_explosion", "structural"),
        (10, "verify", "gold_evidence:measures.on_hand", "structural"),
        (11, "verify", "v4:measure_agg_to_max:on_hand", "mutation"),
        (11, "verify", "v4:closure_join_swap:has_component", "mutation"),
    )

    def test_every_rule_row(self):
        self.assertEqual(len(evidence.RULES), 11)
        for row, stage, cid, kind in self.EXAMPLES:
            with self.subTest(stage=stage, check=cid):
                self.assertEqual(evidence.kind_of(stage, cid), kind)
                self.assertIs(evidence.rule_of(stage, cid), evidence.RULES[row - 1])
        self.assertEqual({row for row, *_rest in self.EXAMPLES}, set(range(1, len(evidence.RULES) + 1)))

    def test_stage_wide_rows_win_over_id_shape(self):
        for stage, cid in (("doctor", "reason:java"), ("doctor", "v3:total:m_on_hand.value"),
                           ("cube", "v4:suite"), ("demo", "v1:q01_explosion"), ("report", "shape:x")):
            with self.subTest(stage=stage, check=cid):
                self.assertEqual(evidence.kind_of(stage, cid), "structural")
        self.assertEqual(evidence.kind_of("verify", "data_revision:stable"), "structural")

    def test_no_default_row(self):
        for stage, cid in (("verify", "w9:x"), ("verify", "v1"), ("verify", "v1:g:extra"), ("verify", "v4:op"),
                           ("verify", "v4:op:target:extra"), ("verify", "scope:v5"), ("verify", ""),
                           ("verify", "shacl:no_negative_qty"), ("reason", "shape:s"), ("reason", "v3:total:x"),
                           ("build", "v1:g"), ("expect", "expect:q01_explosion[0]"), ("data-rules", "rule:r1"),
                           ("counterexample", "stage"), ("", "stage")):
            with self.subTest(stage=stage, check=cid):
                self.assertIsNone(evidence.rule_of(stage, cid))
                self.assertEqual(evidence.kind_of(stage, cid), "unclassified")
                self.assertEqual(evidence.check_kind(stage, cid, {"w9": "authored", "g": "authored"}),
                                 "unclassified")
        self.assertEqual(evidence.KINDS,
                         ("authored", "recomputed", "generated", "structural", "mutation", "unclassified"))
        for rule in evidence.RULES:
            with self.subTest(rule=rule.why):
                self.assertIn(rule.kind, evidence.KINDS)
                self.assertNotEqual(rule.kind, "unclassified")
                self.assertTrue(rule.stages)
                if "any" in rule.stages:
                    self.assertEqual(rule.stages, ("any",))
                    self.assertIsNotNone(rule.ids, "a row for every id of every stage is a default row")
                    self.assertTrue(all(ids[0] not in ("*",) and not ids[0].startswith("<") for ids in rule.ids))

    def test_catalogue_complete(self):
        self.assertEqual(evidence.STAGES, frozenset(REGISTRY_STAGES))
        rows = [(stage, pattern) for stage, pattern in catalogue.catalogue() if stage in REGISTRY_STAGES | {"any"}]
        self.assertGreater(len(rows), 40)
        for stage, pattern in rows:
            concrete = catalogue.skeleton(pattern)
            for s in sorted(REGISTRY_STAGES) if stage == "any" else [stage]:
                with self.subTest(stage=s, pattern=pattern):
                    self.assertNotEqual(evidence.kind_of(s, concrete), "unclassified")

    def test_docs_equal_registry(self):
        table = design_table()
        self.assertEqual(table, evidence.render_table())
        tokens = catalogue.BACKTICKED.findall(table)
        self.assertGreater(len(tokens), 25)
        for token in tokens:
            with self.subTest(token=token):
                if token in NOT_YET_CATALOGUED:
                    self.assertFalse(catalogue.names_a_check(token),
                                     "now catalogued: remove it from NOT_YET_CATALOGUED")
                else:
                    self.assertTrue(catalogue.names_a_check(token))

    def test_rendered_table_uses_rule_patterns(self):
        lines = evidence.render_table().split("\n")
        self.assertEqual(lines[0], "| # | Stage | Check ids | Kind | Why |")
        self.assertEqual(len(lines), 2 + len(evidence.RULES))
        self.assertIn("`v3:*`", lines[2 + 8])
        self.assertIn("`scope:v1`", lines[2 + 9])
        self.assertIn("| every id |", lines[2 + 1])
        for line in lines[2:]:
            self.assertNotIn("<test>", line)
            self.assertNotIn("<part>", line)

    def test_downgrade_only(self):
        def kinds(*entries):
            return evidence.gold_kinds(list(entries))

        golds, overridden = kinds({"id": "g", "kind": "authored", "generated_text": ["output_schema"]})
        self.assertEqual(overridden, 1)
        for cid in ("v1:g", "v2:g:top", "v2:g:all"):
            self.assertEqual(evidence.check_kind("verify", cid, golds), "generated")
        golds, overridden = kinds({"id": "g", "kind": "generated", "generated_text": []})
        self.assertEqual((evidence.check_kind("verify", "v1:g", golds), overridden), ("generated", 0))
        golds, overridden = kinds({"id": "g", "kind": "recomputed", "generated_text": []})
        self.assertEqual((evidence.check_kind("verify", "v2:g:top", golds), overridden), ("generated", 1))
        golds, overridden = kinds({"id": "g", "kind": "authored", "generated_text": []})
        self.assertEqual((evidence.check_kind("verify", "v1:g", golds), overridden), ("authored", 0))
        golds, overridden = kinds({"id": "g", "kind": "authored", "generated_text": "output_schema"})
        self.assertEqual((evidence.check_kind("verify", "v1:g", golds), overridden), ("generated", 1))
        golds, overridden = kinds({"id": "g", "kind": "authored", "generated_text": []},
                                  {"id": "g", "kind": "generated", "generated_text": []})
        self.assertEqual(evidence.check_kind("verify", "v1:g", golds), "generated")
        golds, overridden = kinds({"id": "other", "kind": "authored", "generated_text": []}, "bad",
                                  {"kind": "authored"})
        self.assertEqual(overridden, 0)
        for cid in ("v1:g", "v2:g:top"):
            self.assertEqual(evidence.check_kind("verify", cid, golds), "unclassified")
            self.assertEqual(evidence.check_kind("verify", cid, {}), "unclassified")
        self.assertEqual(evidence.check_kind("verify", "v1:g", {"g": "recomputed"}), "generated")
        raised = {"has_component": "authored", "m_on_hand.value": "authored", "x": "authored"}
        for stage, cid in (("verify", "v3:total:m_on_hand.value"), ("verify", "v3:closure_oracle:has_component"),
                           ("reason", "v5:closure:has_component"), ("reason", "v5:defined:x"),
                           ("verify", "v4:measure_agg_to_max:x"), ("build", "closure:has_component")):
            with self.subTest(stage=stage, check=cid):
                self.assertEqual(evidence.check_kind(stage, cid, raised), evidence.kind_of(stage, cid))

    def test_v3_total_is_generated(self):
        cfg = config.load(pgfixture.DEMO_CONFIG)
        self.assertTrue(cfg.measures)
        for m in cfg.measures.values():
            with self.subTest(measure=m.name):
                self.assertIn(cube_gen.where_sql(m), shape_q.measure_total_sql(cfg, m))
                member = naming.member(naming.cube_measure(m.name), naming.MEASURE_VALUE)
                self.assertEqual(evidence.kind_of("verify", f"v3:total:{member}"), "generated")
        self.assertNotIn("independent of the Cube model", shape_q.measure_total_sql.__doc__)

    def test_v5_closure_is_generated(self):
        self.assertEqual(evidence.kind_of("reason", "v5:closure:has_component"), "generated")
        self.assertEqual(evidence.kind_of("reason", "v5:closure:h"), "generated")

    def test_recomputed_rows_outside_generators(self):
        recomputed = [rule for rule in evidence.RULES if rule.kind == "recomputed"]
        self.assertEqual([rule.module for rule in recomputed], list(RECOMPUTED_CLOSURES))
        self.assertEqual(set(evidence.GENERATOR_MODULES), {"cube_gen", "closure", "shapes", "shape_q", "mutate", "owl"})
        for rule in recomputed:
            with self.subTest(module=rule.module):
                closure = import_closure(rule.module)
                self.assertEqual(closure, RECOMPUTED_CLOSURES[rule.module])
                self.assertFalse(closure & set(evidence.GENERATOR_MODULES))
        for rule in evidence.RULES:
            if rule.kind != "recomputed":
                self.assertEqual(rule.module, "")
        # the walk does see generators: a generated row's module reaches one
        self.assertIn("cube_gen", import_closure("shape_q"))


class SuiteItems(unittest.TestCase):
    GOLDS = {"a": "authored", "g": "generated", "odd": "recomputed"}

    def test_item_kind(self):
        for item, kind in (("v1:a", "authored"), ("v2:a:top[0]", "authored"), ("v2:a:all[3]", "authored"),
                           ("v1:g", "generated"), ("v2:g:empty[0]", "generated"),
                           ("v1:b", "unclassified"), ("v2:b:top[0]", "unclassified"), ("v1:odd", "unclassified"),
                           ("v3:total:m_on_hand.value", "generated"),
                           ("v3:pair:m_on_hand.value+m_consigned.value:chunk[0]", "generated"),
                           ("v3:additive:m_on_hand.value:part.part_class:null", "generated"),
                           ("v3:closure_oracle:has_component", "generated"),
                           ("v3w:pair_witness:x+y", "unclassified"), ("w9:x", "unclassified"),
                           ("v1:a:extra", "unclassified"), ("v2:a", "unclassified"), ("v3", "unclassified"),
                           ("", "unclassified")):
            with self.subTest(item=item):
                self.assertEqual(evidence.item_kind(item, self.GOLDS), kind)
                self.assertIn(evidence.item_kind(item, self.GOLDS), evidence.KINDS)

    def test_order_ranks(self):
        for item, rank in (("v1:a", 0), ("v2:a:top[0]", 0), ("v1:b", 0), ("v2:b:x[1]", 0), ("v1:odd", 0),
                           ("v1:g", 1), ("v2:g:top[0]", 1), ("v3:total:x", 2), ("w9:x", 2), ("v1:a:extra", 2)):
            with self.subTest(item=item):
                self.assertEqual(evidence.suite_rank(item, self.GOLDS), rank)

    def test_order_is_stable_and_unchanged_without_a_generated_gold(self):
        today = [("v1:a", "v1:a", 1), ("v2:a:top[0]", "v2:a:top", 2), ("v1:b", "v1:b", 3),
                 ("v2:b:all[0]", "v2:b:all", 4), ("v3:total:x", "v3:total:x", 5), ("v3:pair:x+y", "v3:pair:x+y", 6)]
        self.assertEqual(evidence.order_suite(today, {"a": "authored"}), today)
        self.assertEqual(evidence.order_suite(today, {}), today)
        golds = {"a": "generated", "b": "authored"}
        ordered = evidence.order_suite(today, golds)
        self.assertEqual([item[2] for item in ordered], [3, 4, 1, 2, 5, 6])
        pairs = [("v3:total:x", None), ("v1:g", None), ("v1:a", None), ("v2:g:s[0]", None), ("v2:a:s[0]", None)]
        self.assertEqual([iid for iid, _fn in evidence.order_suite(pairs, {"a": "authored", "g": "generated"})],
                         ["v1:a", "v2:a:s[0]", "v1:g", "v2:g:s[0]", "v3:total:x"])
        self.assertEqual(evidence.order_suite(["v3:x", "v1:a"], {}, key=lambda item: item), ["v1:a", "v3:x"])
        self.assertEqual(today[0], ("v1:a", "v1:a", 1), "the input list is not reordered in place")


if __name__ == "__main__":
    unittest.main()

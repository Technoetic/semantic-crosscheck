"""[[gold.expect]] literal expected-row cases and the `expect` stage, without a database.

Why: when the gold SQL and the configuration share a mistake they agree, and V1 passes. Rows written from the
business specification are a third oracle that is neither SQL nor the configuration. These tests hold the
configuration contract (exit 2 for every malformed case, values never echoed), the verdict of one case on
hand-written rows (pass, common_mode, regression, gold_mismatch, cube_mismatch, both_mismatch, partial columns, NULL,
empty expected rows), the guards that run before anything connects (consent, an unset fixture variable, a fixture
DSN that names an operational database) and the separation from the operational report.
tests/test_expect_pg.py runs the stage against PostgreSQL and Cube.
"""

import contextlib
import importlib
import io
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pgfixture

import sxc
from semantic_crosscheck import config, report
from semantic_crosscheck.result import EXIT_CONFIG, EXIT_ENV, Result, StageError

try:
    expect = importlib.import_module("semantic_crosscheck.expect")
except ModuleNotFoundError:
    expect = None

EXAMPLE = pgfixture.ROOT / "examples" / "expect" / "crosscheck.toml"
SENTINEL = "ROWVALUE_SENTINEL_7Q"
BASE = """version = 1
[source]
dsn_env = "SXC_T_EXPECT_OPERATIONAL"
owner_dsn_env = "SXC_T_EXPECT_OWNER"
schemas = ["sxc_demo"]
[entities.part]
table = "sxc_demo.part"
key = ["part_id"]
[entities.bom_line]
table = "sxc_demo.bom_line"
key = ["parent_id", "child_id", "line_no"]
[hierarchies.has_component]
edge = "bom_line"
node = "part"
parent = "parent_id"
child = "child_id"
quantity = "qty_per"
"""
GOLD = """[[gold]]
id = "q01_explosion"
sql = "q01.sql"
key = "part_id"
zero_if_null = ["qty"]
[gold.cube_query]
dimensions = ["has_component_closure.descendant", "has_component_descendant.part_class"]
measures = ["has_component_closure.qty"]
filters = [{ member = "has_component_closure.ancestor", operator = "equals", values = [":root"] }]
[gold.map]
part_id = "has_component_closure.descendant"
part_class = "has_component_descendant.part_class"
qty = "has_component_closure.qty"
[gold.params.root]
values = ["P"]
"""
CASE = """[[gold.expect]]
source = "authored"
spec = "EX-1 section 2"
params = { root = "P" }
rows = [{ part_id = "A", part_class = "assembly", qty = 2 }, { part_id = "C", part_class = { null = true }, qty = "7.0" }]
"""
FIXTURE_ENV = "SXC_T_EXPECT_FIXTURE"
OPERATIONAL = {"SXC_T_EXPECT_OPERATIONAL": "postgresql://xc_check@127.0.0.1:5432/sxc_operational_sentinel",
               "SXC_T_EXPECT_OWNER": "postgresql://xc_owner@127.0.0.1:5432/sxc_owner_sentinel"}


@contextlib.contextmanager
def config_dir(text: str, gold_sql: str = "SELECT 1 AS part_id"):
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "q01.sql").write_text(gold_sql, encoding="utf-8")
        path = Path(d) / "crosscheck.toml"
        path.write_text(text, encoding="utf-8")
        yield path


def load(text: str):
    with config_dir(text) as path:
        return config.load(path)


def case_text(**changes) -> str:
    """CASE with some keys replaced or added (key -> new right-hand side; None drops the line)."""
    lines = []
    for line in CASE.strip().split("\n"):
        key = line.split(" = ", 1)[0]
        if key in changes:
            if changes[key] is not None:
                lines.append(f"{key} = {changes[key]}")
        else:
            lines.append(line)
    present = {line.split(" = ", 1)[0] for line in CASE.strip().split("\n")}
    lines += [f"{key} = {value}" for key, value in changes.items() if key not in present and value is not None]
    return "\n".join(lines) + "\n"


def args(**changes):
    values = dict(dsn_env=FIXTURE_ENV, allow_fixture_database=True)
    values.update(changes)
    return SimpleNamespace(**values)


class ExpectConfigTest(unittest.TestCase):
    def refused(self, text: str, fragment: str):
        with self.assertRaises(StageError) as raised:
            load(text)
        message = str(raised.exception)
        self.assertEqual(raised.exception.exit_code, EXIT_CONFIG, message)
        self.assertIn("expect[0]", message)
        self.assertIn(fragment, message)
        self.assertNotIn(SENTINEL.lower(), message.lower())
        return message

    def test_a_valid_case_loads_with_null_cells_as_none(self):
        cfg = load(BASE + GOLD + CASE)
        case = cfg.gold[0].expect[0]
        self.assertIsInstance(case, config.Expect)
        self.assertEqual((case.source, case.spec, case.params, case.partial), ("authored", "EX-1 section 2", {"root": "P"}, False))
        self.assertEqual(case.rows, [{"part_id": "A", "part_class": "assembly", "qty": 2},
                                     {"part_id": "C", "part_class": None, "qty": "7.0"}])
        self.assertEqual(load(BASE + GOLD).gold[0].expect, [])

    def test_source_is_required_and_closed(self):
        self.refused(BASE + GOLD + case_text(source=None), "source")
        self.refused(BASE + GOLD + case_text(source=f'"{SENTINEL}"'), "source")

    def test_an_authored_case_names_its_specification(self):
        self.refused(BASE + GOLD + case_text(spec=None), "spec")
        self.refused(BASE + GOLD + case_text(spec='"   "'), "spec")
        self.refused(BASE + GOLD + case_text(spec="3"), "spec")
        captured = load(BASE + GOLD + case_text(source='"captured"', spec=None)).gold[0].expect[0]
        self.assertEqual((captured.source, captured.spec), ("captured", ""))

    def test_rows_are_required_even_when_empty(self):
        self.refused(BASE + GOLD + case_text(rows=None), "rows")
        self.assertEqual(load(BASE + GOLD + case_text(rows="[]")).gold[0].expect[0].rows, [])

    def test_parameter_names_must_be_the_gold_parameters(self):
        self.refused(BASE + GOLD + case_text(params=None), "root")
        self.refused(BASE + GOLD + case_text(params=f'{{ {SENTINEL.lower()} = "P" }}'), "root")
        self.refused(BASE + GOLD + case_text(params='{ root = "P", extra = 1 }'), "root")
        no_params = GOLD.replace('[gold.params.root]\nvalues = ["P"]\n', "").replace('values = [":root"]', 'values = ["P"]')
        self.refused(BASE + no_params + CASE, "no parameters")
        load(BASE + no_params + case_text(params=None))
        sets = GOLD.replace('[gold.params.root]\nvalues = ["P"]\n', "")
        sets = sets.replace('zero_if_null = ["qty"]\n', 'zero_if_null = ["qty"]\nparam_sets = [{ root = "P" }, { root = "A" }]\n')
        load(BASE + sets + CASE)
        self.refused(BASE + sets + case_text(params='{ part = "P" }'), "param_sets")

    def test_parameter_values_are_scalars(self):
        self.refused(BASE + GOLD + case_text(params=f'{{ root = ["{SENTINEL}"] }}'), "params")
        self.refused(BASE + GOLD + case_text(params=f'{{ root = {{ v = "{SENTINEL}" }} }}'), "params")
        self.refused(BASE + GOLD + case_text(params='"root"'), "params")

    def test_rows_name_gold_columns(self):
        self.refused(BASE + GOLD + case_text(rows=f'[{{ part_id = "A", part_class = "x", qty = 1, {SENTINEL.lower()} = 1 }}]'),
                     "gold column")
        self.refused(BASE + GOLD + case_text(rows='[{ part_id = "A", qty = 1 }]'), "part_class")
        self.refused(BASE + GOLD + case_text(rows='{ part_id = "A" }'), "rows")
        self.refused(BASE + GOLD + case_text(rows='["A"]'), "rows[0]")

    def test_partial_cases_name_every_key_column_and_the_same_columns(self):
        partial = load(BASE + GOLD + case_text(partial="true", rows='[{ part_id = "A", qty = 2 }]')).gold[0].expect[0]
        self.assertTrue(partial.partial)
        self.refused(BASE + GOLD + case_text(partial="true", rows='[{ qty = 2 }]'), "key column")
        self.refused(BASE + GOLD + case_text(partial="true", rows='[{ part_id = "A", qty = 2 }, { part_id = "C" }]'),
                     "same columns")
        self.refused(BASE + GOLD + case_text(partial='"yes"'), "partial")

    def test_cells_are_scalars_or_the_null_marker(self):
        self.refused(BASE + GOLD + case_text(rows=f'[{{ part_id = "A", part_class = ["{SENTINEL}"], qty = 1 }}]'),
                     "part_class")
        self.refused(BASE + GOLD + case_text(rows=f'[{{ part_id = "A", part_class = {{ null = false }}, qty = 1 }}]'),
                     "null = true")
        self.refused(BASE + GOLD + case_text(rows=f'[{{ part_id = "A", part_class = {{ v = "{SENTINEL}" }}, qty = 1 }}]'),
                     "null = true")

    def test_a_zero_if_null_column_cannot_expect_null(self):
        self.refused(BASE + GOLD + case_text(rows='[{ part_id = "A", part_class = "x", qty = { null = true } }]'),
                     "zero_if_null")

    def test_expected_keys_are_unique(self):
        self.refused(BASE + GOLD + case_text(rows=f'[{{ part_id = "{SENTINEL}", part_class = "x", qty = 1 }}, '
                                                  f'{{ part_id = "{SENTINEL}", part_class = "y", qty = 2 }}]'), "same key")

    def test_unknown_case_keys_and_a_non_array_are_refused(self):
        self.refused(BASE + GOLD + CASE + f'{SENTINEL.lower()} = 1\n', "unknown")
        with self.assertRaises(StageError) as raised:
            load(BASE + GOLD.replace('zero_if_null = ["qty"]\n', 'zero_if_null = ["qty"]\nexpect = "rows"\n'))
        self.assertEqual(raised.exception.exit_code, EXIT_CONFIG)
        self.assertIn("[[gold.expect]]", str(raised.exception))

    def test_the_example_configuration_loads_with_authored_cases(self):
        cfg = config.load(EXAMPLE)
        cases = [c for g in cfg.gold for c in g.expect]
        self.assertGreaterEqual(len(cases), 2)
        self.assertTrue(all(c.source == "authored" and c.spec.strip() for c in cases))
        self.assertTrue(any(c.partial for c in cases))
        self.assertTrue((EXAMPLE.parent / "fixture.sql").is_file())


def gold(**changes):
    values = dict(id="q", sql="q.sql", key=["part_id"], map={"part_id": "c.d", "part_class": "c.cls", "qty": "c.qty"},
                  zero_if_null=["qty"])
    values.update(changes)
    return config.Gold(**values)


def case(rows, source="authored", partial=False):
    return config.Expect(source=source, rows=rows, spec="EX-1" if source == "authored" else "", partial=partial)


TYPES = {"part_id": "string", "part_class": "string", "qty": "number"}
EXPECTED = [{"part_id": "A", "part_class": "assembly", "qty": 2}, {"part_id": "C", "part_class": "component", "qty": "7.0"}]
GOLD_ROWS = [{"part_id": "A", "part_class": "assembly", "qty": Decimal("2.000000")},
             {"part_id": "C", "part_class": "component", "qty": Decimal("7.000000")}]
CUBE_ROWS = [{"part_id": "A", "part_class": "assembly", "qty": "2"}, {"part_id": "C", "part_class": "component", "qty": "7"}]


class JudgeTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(expect, "the expect stage (semantic_crosscheck.expect) is missing")

    def judge(self, rows, g_rows, c_rows, **kw):
        status, detail = expect.judge(gold(), case(rows, **kw), g_rows, c_rows, TYPES)
        json.dumps(detail)
        return status, detail

    def test_both_sides_equal_the_expected_rows(self):
        status, detail = self.judge(EXPECTED, GOLD_ROWS, CUBE_ROWS)
        self.assertEqual(status, "pass")
        self.assertNotIn("reason", detail)
        self.assertEqual((detail["gold_equals_expected"], detail["cube_equals_expected"], detail["gold_equals_cube"]),
                         (True, True, True))
        self.assertEqual((detail["source"], detail["independent"], detail["expected_rows"], detail["columns"]),
                         ("authored", True, 2, 3))

    def test_gold_and_cube_dropping_the_same_row_is_common_mode(self):
        status, detail = self.judge(EXPECTED, GOLD_ROWS[:1], CUBE_ROWS[:1])
        self.assertEqual((status, detail["reason"]), ("fail", "common_mode"))
        self.assertTrue(detail["gold_equals_cube"])
        self.assertFalse(detail["gold_equals_expected"] or detail["cube_equals_expected"])
        self.assertEqual(detail["gold_vs_expected"]["only_in_expected"], 1)

    def test_a_shared_wrong_value_is_common_mode(self):
        wrong = [dict(GOLD_ROWS[0]), dict(GOLD_ROWS[1], qty=Decimal("6"))]
        status, detail = self.judge(EXPECTED, wrong, [dict(CUBE_ROWS[0]), dict(CUBE_ROWS[1], qty="6")])
        self.assertEqual((status, detail["reason"]), ("fail", "common_mode"))
        self.assertEqual(detail["cube_vs_expected"]["mismatched_columns"], {"qty": 1})

    def test_a_cube_fanout_alone_is_cube_mismatch(self):
        status, detail = self.judge(EXPECTED, GOLD_ROWS, CUBE_ROWS + [dict(CUBE_ROWS[0])])
        self.assertEqual((status, detail["reason"]), ("fail", "cube_mismatch"))
        self.assertTrue(detail["gold_equals_expected"])
        self.assertFalse(detail["cube_equals_expected"])
        self.assertEqual(detail["cube_vs_expected"]["duplicate_keys_cube"], 1)

    def test_a_wrong_gold_alone_is_gold_mismatch(self):
        status, detail = self.judge(EXPECTED, [GOLD_ROWS[0], dict(GOLD_ROWS[1], qty=Decimal("1"))], CUBE_ROWS)
        self.assertEqual((status, detail["reason"]), ("fail", "gold_mismatch"))
        self.assertTrue(detail["cube_equals_expected"])

    def test_gold_and_cube_wrong_in_different_ways_is_both_mismatch(self):
        status, detail = self.judge(EXPECTED, GOLD_ROWS[:1], CUBE_ROWS + [{"part_id": "R", "part_class": "raw", "qty": "1"}])
        self.assertEqual((status, detail["reason"]), ("fail", "both_mismatch"))
        self.assertFalse(detail["gold_equals_cube"])

    def test_a_captured_case_is_regression_evidence_only(self):
        status, detail = self.judge(EXPECTED, GOLD_ROWS, CUBE_ROWS, source="captured")
        self.assertEqual(status, "pass")
        self.assertFalse(detail["independent"])
        status, detail = self.judge(EXPECTED, GOLD_ROWS[:1], CUBE_ROWS[:1], source="captured")
        self.assertEqual((status, detail["reason"]), ("fail", "regression"))
        status, detail = self.judge(EXPECTED, GOLD_ROWS, CUBE_ROWS[:1], source="captured")
        self.assertEqual((status, detail["reason"]), ("fail", "cube_mismatch"))

    def test_a_partial_case_compares_only_its_columns(self):
        rows = [{"part_id": "A", "qty": 2}, {"part_id": "C", "qty": 7}]
        other = [dict(r, part_class="other") for r in CUBE_ROWS]
        status, detail = self.judge(rows, GOLD_ROWS, other, partial=True)
        self.assertEqual(status, "pass")
        self.assertEqual(detail["columns"], 2)
        status, detail = self.judge(rows, GOLD_ROWS, [CUBE_ROWS[0], dict(CUBE_ROWS[1], qty="8")], partial=True)
        self.assertEqual((status, detail["reason"]), ("fail", "cube_mismatch"))

    def test_null_cells(self):
        rows = [{"part_id": "A", "part_class": None, "qty": 2}]
        status, _ = self.judge(rows, [{"part_id": "A", "part_class": None, "qty": 2}], [{"part_id": "A", "part_class": None, "qty": "2"}])
        self.assertEqual(status, "pass")
        status, detail = self.judge(rows, [{"part_id": "A", "part_class": "", "qty": 2}], [{"part_id": "A", "part_class": None, "qty": "2"}])
        self.assertEqual((status, detail["reason"]), ("fail", "gold_mismatch"))
        # zero_if_null: a Cube NULL counts as 0 (the declared equivalence), a gold NULL does not (the gold SQL coalesces)
        zero = [{"part_id": "A", "part_class": "x", "qty": 0}]
        status, _ = self.judge(zero, [{"part_id": "A", "part_class": "x", "qty": 0}], [{"part_id": "A", "part_class": "x", "qty": None}])
        self.assertEqual(status, "pass")
        status, detail = self.judge(zero, [{"part_id": "A", "part_class": "x", "qty": None}], [{"part_id": "A", "part_class": "x", "qty": "0"}])
        self.assertEqual((status, detail["reason"]), ("fail", "gold_mismatch"))

    def test_a_column_the_gold_sql_does_not_return_never_matches_null(self):
        rows = [{"part_id": "A", "part_class": None, "qty": 2}]
        status, detail = self.judge(rows, [{"part_id": "A", "qty": 2}], [{"part_id": "A", "part_class": None, "qty": "2"}])
        self.assertEqual((status, detail["reason"]), ("fail", "gold_mismatch"))
        self.assertEqual(detail["gold_vs_expected"]["gold_columns_missing"], ["part_class"])

    def test_empty_expected_rows(self):
        status, detail = self.judge([], [], [])
        self.assertEqual(status, "pass")
        self.assertEqual(detail["expected_rows"], 0)
        status, detail = self.judge([], [], CUBE_ROWS[:1])
        self.assertEqual((status, detail["reason"]), ("fail", "cube_mismatch"))

    def test_details_hold_counts_and_ids_only(self):
        rows = [{"part_id": SENTINEL, "part_class": SENTINEL, "qty": 2}]
        _status, detail = self.judge(rows, [{"part_id": SENTINEL, "part_class": "x", "qty": 3}], [])
        self.assertNotIn(SENTINEL, json.dumps(detail))


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(expect, "the expect stage (semantic_crosscheck.expect) is missing")
        self.cfg = load(BASE + GOLD + CASE)

    def run_guarded(self, environ: dict, arguments):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, environ), \
                patch.object(expect.db, "connect", side_effect=AssertionError("forbidden connection")), \
                patch.object(expect.stage_build, "run", side_effect=AssertionError("forbidden build")):
            for name in (FIXTURE_ENV, *OPERATIONAL):
                if name not in environ:
                    os.environ.pop(name, None)
            with self.assertRaises(StageError) as raised:
                expect.run(self.cfg, arguments, tmp)
        message = str(raised.exception)
        self.assertNotIn("sentinel", message)
        self.assertNotIn("postgresql://", message)
        return raised.exception

    def test_consent_is_required(self):
        e = self.run_guarded({FIXTURE_ENV: "postgresql://u@127.0.0.1:5432/sxc_fixture_sentinel"},
                             args(allow_fixture_database=False))
        self.assertEqual(e.exit_code, EXIT_CONFIG)
        self.assertIn("--allow-fixture-database", str(e))

    def test_an_unset_fixture_variable_is_an_environment_error(self):
        e = self.run_guarded(dict(OPERATIONAL), args())
        self.assertEqual(e.exit_code, EXIT_ENV)
        self.assertIn(FIXTURE_ENV, str(e))
        self.assertEqual(self.run_guarded({}, args(dsn_env="not a name")).exit_code, EXIT_CONFIG)

    def test_the_fixture_must_not_be_an_operational_database(self):
        for name, value in OPERATIONAL.items():
            with self.subTest(variable=name):
                e = self.run_guarded({**OPERATIONAL, FIXTURE_ENV: value}, args())
                self.assertEqual(e.exit_code, EXIT_CONFIG)
                self.assertIn(name, str(e))
        same_database = "postgresql://someone_else@127.0.0.1/sxc_operational_sentinel?sslmode=disable"
        self.assertEqual(self.run_guarded({**OPERATIONAL, FIXTURE_ENV: same_database}, args()).exit_code, EXIT_CONFIG)
        self.assertEqual(self.run_guarded(dict(OPERATIONAL), args(dsn_env="SXC_T_EXPECT_OPERATIONAL")).exit_code,
                         EXIT_CONFIG)
        cube = load(BASE + '[cube]\ndsn_env = "SXC_T_EXPECT_CUBE"\n' + GOLD + CASE)
        reader = "postgresql://cube_reader@127.0.0.1:5432/sxc_cube_sentinel"
        with patch.dict(os.environ, {FIXTURE_ENV: reader, "SXC_T_EXPECT_CUBE": reader}):
            with self.assertRaises(StageError) as raised:
                expect.fixture_env(cube, args())
        self.assertEqual(raised.exception.exit_code, EXIT_CONFIG)
        self.assertIn("SXC_T_EXPECT_CUBE", str(raised.exception))
        self.assertNotIn("sentinel", str(raised.exception))

    def test_a_dedicated_database_passes_the_guard(self):
        fixture = "postgresql://xc_check@127.0.0.1:5432/sxc_fixture"
        with patch.dict(os.environ, {**OPERATIONAL, FIXTURE_ENV: fixture}):
            self.assertEqual(expect.fixture_env(self.cfg, args()), FIXTURE_ENV)

    def test_the_fixture_configuration_points_every_connection_at_the_fixture(self):
        cfg = self.cfg
        derived = expect.fixture_config(cfg, FIXTURE_ENV)
        self.assertEqual((derived.source.dsn_env, derived.source.owner_dsn_env, derived.cube_dsn_env()),
                         (FIXTURE_ENV, FIXTURE_ENV, FIXTURE_ENV))
        self.assertEqual((derived.source.data_revision_sql_file, derived.source.runtime_evidence), ("", False))
        self.assertEqual(cfg.source.dsn_env, "SXC_T_EXPECT_OPERATIONAL")
        self.assertIs(derived.gold, cfg.gold)
        self.assertEqual(derived.path, cfg.path)

    def test_no_cases_is_not_run_without_connecting(self):
        cfg = load(BASE + GOLD)
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(expect.db, "connect", side_effect=AssertionError("forbidden connection")):
            res = expect.run(cfg, args(allow_fixture_database=False), tmp)
        self.assertEqual(res.status, "not_run")
        self.assertEqual([(c.id, c.detail["reason"]) for c in res.checks], [("expect:cases", "no_cases")])


class CommandLineTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(expect, "the expect stage (semantic_crosscheck.expect) is missing")

    def test_expect_is_a_stage_that_needs_the_operational_configuration(self):
        self.assertEqual(sxc.STAGES.get("expect"), "expect")
        self.assertIn("expect", sxc.NEEDS_CONFIG)
        ns = sxc.parser().parse_args(["expect", "--config", "x.toml", "--allow-fixture-database"])
        self.assertEqual((ns.dsn_env, ns.allow_fixture_database), ("SXC_EXPECT_DSN", True))
        self.assertFalse(sxc.parser().parse_args(["expect"]).allow_fixture_database)

    def test_refusal_writes_a_count_only_result_and_leaves_operational_results_alone(self):
        with config_dir(BASE + GOLD + CASE) as path:
            out = path.parent / ".sxc"
            (out / "build").mkdir(parents=True)
            (out / "build" / "result.json").write_text('{"stage": "build"}', encoding="utf-8")
            err = io.StringIO()
            with patch.dict(os.environ, {**OPERATIONAL, FIXTURE_ENV: OPERATIONAL["SXC_T_EXPECT_OPERATIONAL"]}), \
                    contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                code = sxc.main(["expect", "--config", str(path), "--dsn-env", FIXTURE_ENV, "--allow-fixture-database"])
            self.assertEqual(code, EXIT_CONFIG)
            written = (out / "expect" / "result.json").read_text(encoding="utf-8")
            self.assertIn('"stage"', written)
            for text in (written, err.getvalue()):
                self.assertNotIn("sentinel", text)
                self.assertNotIn("postgresql://", text)
            self.assertEqual((out / "build" / "result.json").read_text(encoding="utf-8"), '{"stage": "build"}')

    def test_report_never_reads_the_expect_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = Result("expect")
            res.add("expect:q01_explosion[0]", "fail", "gold[0] q01_explosion expect[0]", reason="common_mode")
            res.write(tmp)
            self.assertNotIn("expect", report.read_results(Path(tmp)))
            self.assertNotIn("expect", report.PIPELINE)


if __name__ == "__main__":
    unittest.main()

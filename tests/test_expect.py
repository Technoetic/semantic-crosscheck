"""[[gold.expect]] literal expected-row cases and the `expect` stage, without a database.

Why: when the gold SQL and the configuration share a mistake they agree, and V1 passes. Rows written from the
business specification are a third oracle that is neither SQL nor the configuration. These tests hold the
configuration contract (exit 2 for every malformed case, values never echoed), the verdict of one case on
hand-written rows (pass, common_mode, regression, gold_mismatch, cube_mismatch, both_mismatch, partial columns, NULL,
empty expected rows), the guards that run before anything connects (consent, an unset fixture variable, a fixture
DSN that names an operational database) and the separation from the operational report. RunTest drives the whole
stage with a fake connection, build, Cube and digests to hold its fail-closed guards: a Cube row without a member is
an error, a changed or unreadable fixture turns passes into not_run, a failed closure, Cube or member check gates
the cases, a pinned fixture_sha256 must match, and the CLI runs no operational revision or runtime probe.
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

    def test_the_null_marker_is_exactly_null_equals_true(self):
        for marker in ("{ null = 1 }", "{ null = 1.0 }", '{ null = true, v = 1 }'):
            with self.subTest(marker=marker):
                self.refused(BASE + GOLD + case_text(rows=f'[{{ part_id = "A", part_class = {marker}, qty = 1 }}]'),
                             "null = true")

    def test_a_case_may_pin_its_fixture_digest(self):
        pin = "0123456789abcdef" * 4
        case = load(BASE + GOLD + case_text(fixture_sha256=f'"{pin}"')).gold[0].expect[0]
        self.assertEqual(case.fixture_sha256, pin)
        self.assertEqual(load(BASE + GOLD + CASE).gold[0].expect[0].fixture_sha256, "")
        for bad in (f'"{pin.upper()}"', f'"{SENTINEL}"', '"abc"', "7"):
            with self.subTest(pin=bad):
                self.refused(BASE + GOLD + case_text(fixture_sha256=bad), "fixture_sha256")

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

    def test_expected_keys_equal_only_in_typed_form_are_an_error_of_the_rows(self):
        rows = [{"part_id": 1, "part_class": "x", "qty": 1}, {"part_id": "1", "part_class": "x", "qty": 1}]
        status, detail = self.judge(rows, [{"part_id": "1", "part_class": "x", "qty": 1}],
                                    [{"part_id": "1", "part_class": "x", "qty": "1"}])
        self.assertEqual((status, detail["reason"]), ("error", "expected_duplicate_keys"))

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


KINDS = {"has_component_closure.descendant": "string", "has_component_descendant.part_class": "string",
         "has_component_closure.qty": "number"}
RUN_GOLD = [{"part_id": "A", "part_class": "assembly", "qty": Decimal("2")},
            {"part_id": "C", "part_class": None, "qty": Decimal("7")}]
RUN_CUBE = [{"has_component_closure.descendant": "A", "has_component_descendant.part_class": "assembly",
             "has_component_closure.qty": "2"},
            {"has_component_closure.descendant": "C", "has_component_descendant.part_class": None,
             "has_component_closure.qty": "7"}]
PIN = "a" * 64


def digest(fixture=PIN, tables="b" * 64):
    return {"fixture_sha256": fixture, "tables_sha256": tables, "rows": 6}


class FakeConn:
    def __init__(self, log, lock_error=None):
        self.log, self.lock_error = log, lock_error

    def run(self, sql, **_params):
        self.log.append(sql)
        if sql.startswith("LOCK TABLE") and self.lock_error:
            raise self.lock_error
        return []

    def close(self):
        self.log.append("close")


class FakeClient:
    def __init__(self, rows):
        self.rows = rows

    def load(self, _query):
        return [dict(r) for r in self.rows]


class RunTest(unittest.TestCase):
    """expect.run end to end with a fake fixture connection, inner stages, Cube and digests."""

    def setUp(self):
        self.assertIsNotNone(expect, "the expect stage (semantic_crosscheck.expect) is missing")

    def stage(self, text=BASE + GOLD + CASE, digests=None, closure="pass", closure_apply="pass", meta="pass",
              members="pass", cube_rows=RUN_CUBE, environ=None, lock_error=None, gold_rows=RUN_GOLD):
        """Run the stage; returns (result, checks by id, call order, SQL statements)."""
        order, sql = [], []
        sequence = iter(digests if digests is not None else [digest(), digest(), digest()])

        def fake_digest(_conn, _cfg, hierarchies=()):
            order.append(f"digest{len(hierarchies)}")
            value = next(sequence)
            if isinstance(value, BaseException):
                raise value
            return value

        def fake_build(_cfg, _args, work):
            order.append("build")
            Path(work).mkdir(parents=True, exist_ok=True)
            res = Result("build")
            for cid, st in (("closure:has_component", closure), ("closure_apply:has_component", closure_apply)):
                if st is not None:
                    res.add(cid, st, "hierarchies.has_component")
            return res

        def fake_cube(_cfg, _args, _work):
            order.append("cube")
            res = Result("cube")
            if meta is not None:
                res.add("cube:meta", meta, "", cube_version="1.8.3", sql_planner="tesseract")
            if members is not None:
                res.add("cube:gold_members:q01_explosion", members, "gold[0] q01_explosion")
            return res

        @contextlib.contextmanager
        def fake_serve(_cfg, _run_dir, _log):
            order.append("serve")
            yield FakeClient(cube_rows)

        def fake_connect(_name):
            order.append("connect")
            return FakeConn(sql, lock_error if order.count("connect") == 2 else None)

        def fake_gold_rows(_conn, _cfg, _gold, _params):
            order.append("gold")
            return [dict(r) for r in gold_rows]

        environ = {**OPERATIONAL, FIXTURE_ENV: "postgresql://u@127.0.0.1:5432/sxc_fixture"} if environ is None else environ
        cfg = load(text)
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, environ), \
                patch.object(expect, "fixture_digest", side_effect=fake_digest), \
                patch.object(expect.db, "connect", side_effect=fake_connect), \
                patch.object(expect.stage_build, "run", side_effect=fake_build), \
                patch.object(expect.stage_cube, "run", side_effect=fake_cube), \
                patch.object(expect.cube_gen, "load_model", return_value={}), \
                patch.object(expect.compare, "member_kinds", return_value=KINDS), \
                patch.object(expect.compare, "gold_rows", side_effect=fake_gold_rows), \
                patch.object(expect.cube_run, "serve", side_effect=fake_serve):
            for name in (FIXTURE_ENV, *OPERATIONAL):
                if name not in environ:
                    os.environ.pop(name, None)
            res = expect.run(cfg, args(), tmp)
        json.dumps(res.to_dict())
        return res, {c.id: c for c in res.checks}, order, sql

    def test_a_passing_run_fingerprints_before_build_and_locks_the_fixture_during_the_cases(self):
        res, checks, order, sql = self.stage()
        self.assertEqual(res.status, "pass", res.to_dict())
        self.assertEqual(order[:3], ["connect", "digest0", "build"], "the first digest must precede the build")
        self.assertLess(order.index("connect", 1), order.index("serve"))
        fixture = checks["expect:fixture"].detail
        self.assertEqual((fixture["fixture_sha256"], fixture["locked"], fixture["operational_variables_compared"]),
                         (PIN, True, 2))
        self.assertEqual(sum(s.endswith("IN SHARE MODE") for s in sql), 3)   # part, bom_line and the built closure
        self.assertTrue(any("separate sessions" in n for n in res.not_proven))
        self.assertIn("every case matched", res.next_safe_action)

    def test_a_cube_row_without_a_member_is_an_error_not_a_null(self):
        rows = [RUN_CUBE[0], {k: v for k, v in RUN_CUBE[1].items() if k != "has_component_descendant.part_class"}]
        res, checks, _order, _sql = self.stage(cube_rows=rows)
        case = checks["expect:q01_explosion[0]"]
        self.assertEqual((case.status, case.detail["side"], case.detail["error"]), ("error", "cube", "MissingMember"))
        self.assertEqual(res.status, "error")

    def test_a_fixture_that_changes_during_the_cases_turns_passes_into_not_run(self):
        res, checks, _order, _sql = self.stage(digests=[digest(), digest(), digest(tables="c" * 64)])
        self.assertEqual((checks["expect:fixture"].status, checks["expect:fixture"].detail["reason"]),
                         ("error", "fixture_changed"))
        case = checks["expect:q01_explosion[0]"]
        self.assertEqual((case.status, case.detail["reason"]), ("not_run", "fixture_changed"))
        self.assertIn("expect:q01_explosion[0]: not run (fixture_changed)", res.not_proven)
        self.assertNotEqual(res.status, "pass")

    def test_a_fixture_that_changes_between_build_and_the_cases_is_detected(self):
        _res, checks, _order, _sql = self.stage(digests=[digest(), digest(fixture="d" * 64), digest(fixture="d" * 64)])
        self.assertEqual(checks["expect:fixture"].detail["reason"], "fixture_changed")
        self.assertEqual(checks["expect:q01_explosion[0]"].status, "not_run")

    def test_a_failing_second_fingerprint_is_fingerprint_failed(self):
        _res, checks, _order, _sql = self.stage(digests=[digest(), digest(), ValueError("unreadable")])
        self.assertEqual((checks["expect:fixture"].status, checks["expect:fixture"].detail["reason"]),
                         ("error", "fingerprint_failed"))
        case = checks["expect:q01_explosion[0]"]
        self.assertEqual((case.status, case.detail["reason"]), ("not_run", "fingerprint_failed"))

    def test_an_unreadable_fixture_builds_and_serves_nothing(self):
        res, checks, order, _sql = self.stage(digests=[ValueError("no table")])
        self.assertEqual((checks["expect:fixture"].detail["reason"], checks["expect:fixture"].detail["error"]),
                         ("fingerprint_failed", "ValueError"))
        case = checks["expect:q01_explosion[0]"]
        self.assertEqual((case.status, case.detail["reason"]), ("not_run", "fixture_unreadable"))
        self.assertNotIn("build", order)
        self.assertNotIn("serve", order)
        self.assertIn("expect:q01_explosion[0]: not run (fixture_unreadable)", res.not_proven)

    def gated(self, reason, **stage):
        res, checks, order, _sql = self.stage(digests=[digest()], **stage)
        case = checks["expect:q01_explosion[0]"]
        self.assertEqual((case.status, case.detail["reason"]), ("not_run", reason))
        self.assertNotIn("serve", order)
        self.assertNotIn("gold", order)
        self.assertEqual((checks["expect:fixture"].status, checks["expect:fixture"].detail["reason"]),
                         ("not_run", "no_case_ran"))
        self.assertTrue(any(n.startswith(f"expect:q01_explosion[0]: not run ({reason}") for n in res.not_proven))
        self.assertNotEqual(res.status, "pass")
        return case

    def test_a_closure_that_did_not_pass_gates_its_cases(self):
        case = self.gated("closure_not_passed", closure="fail")
        self.assertEqual((case.detail["hierarchy"], case.detail["build_check"]), ("has_component", "fail"))
        self.gated("closure_not_passed", closure=None)
        self.gated("closure_not_passed", closure_apply="fail")

    def test_cube_not_serving_the_model_gates_the_cases(self):
        self.gated("cube_not_passed", meta="error")
        self.gated("cube_not_passed", meta=None)

    def test_members_that_did_not_pass_gate_the_cases(self):
        self.gated("gold_members_not_passed", members="fail")
        self.gated("gold_members_not_passed", members=None)

    def test_a_pinned_fixture_digest_must_match(self):
        other = BASE + GOLD + case_text(fixture_sha256='"' + "f" * 64 + '"')
        case = self.gated("fixture_mismatch", text=other)
        self.assertNotIn("hierarchy", case.detail)
        res, checks, _order, _sql = self.stage(text=BASE + GOLD + case_text(fixture_sha256=f'"{PIN}"'))
        self.assertEqual((res.status, checks["expect:q01_explosion[0]"].status), ("pass", "pass"))

    def test_unset_operational_variables_are_counted_and_named(self):
        res, checks, _order, _sql = self.stage(environ={FIXTURE_ENV: "postgresql://u@127.0.0.1:5432/sxc_fixture"})
        self.assertEqual(checks["expect:fixture"].detail["operational_variables_compared"], 0)
        self.assertTrue(any("0 operational DSN variable(s) set" in n for n in res.not_proven))
        unset = [n for n in res.not_proven if n.startswith("operational DSN variable(s) not set")]
        self.assertEqual(len(unset), 1)
        self.assertIn("SXC_T_EXPECT_OPERATIONAL", unset[0])
        self.assertIn("SXC_T_EXPECT_OWNER", unset[0])
        res, _checks, _order, _sql = self.stage()
        self.assertFalse(any(n.startswith("operational DSN variable(s) not set") for n in res.not_proven))

    def test_a_lock_that_cannot_be_taken_is_disclosed(self):
        res, checks, _order, _sql = self.stage(lock_error=ValueError("permission denied"))
        self.assertEqual(res.status, "pass")
        self.assertFalse(checks["expect:fixture"].detail["locked"])
        self.assertTrue(any("could not be locked" in n for n in res.not_proven))

    def test_common_mode_and_regression_first_check_the_fixture_and_the_rows(self):
        wrong = [dict(RUN_CUBE[0]), dict(RUN_CUBE[1], **{"has_component_closure.qty": "6"})]
        shared = [RUN_GOLD[0], dict(RUN_GOLD[1], qty=Decimal("6"))]
        res, checks, _order, _sql = self.stage(cube_rows=wrong, gold_rows=shared)
        self.assertEqual(checks["expect:q01_explosion[0]"].detail["reason"], "common_mode")
        self.assertIn("first check that the fixture rows and the expected rows follow the cited specification",
                      res.next_safe_action)
        captured = BASE + GOLD + case_text(source='"captured"', spec=None)
        res, checks, _order, _sql = self.stage(text=captured, cube_rows=wrong, gold_rows=shared)
        self.assertEqual(checks["expect:q01_explosion[0]"].detail["reason"], "regression")
        self.assertIn("first check that the fixture rows and the expected rows follow the cited specification",
                      res.next_safe_action)

    def test_a_pass_of_captured_cases_only_says_so(self):
        res, _checks, _order, _sql = self.stage(text=BASE + GOLD + case_text(source='"captured"', spec=None))
        self.assertEqual(res.status, "pass")
        self.assertIn("every case is captured", res.next_safe_action)


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

    def test_the_command_line_runs_no_operational_revision_or_runtime_probe(self):
        probed = BASE.replace('schemas = ["sxc_demo"]\n', 'schemas = ["sxc_demo"]\ndata_revision_sql_file = "revision.sql"\n'
                              'runtime_evidence = true\n')
        self.assertIn("runtime_evidence = true", probed)
        passing = Result("expect")
        passing.add("expect:q01_explosion[0]", "pass", "gold[0] q01_explosion expect[0]", source="authored")
        with config_dir(probed + GOLD + CASE) as path:
            (path.parent / "revision.sql").write_text("SELECT 'r1'", encoding="utf-8")
            with patch.dict(os.environ, {**OPERATIONAL, FIXTURE_ENV: "postgresql://u@127.0.0.1:5432/sxc_fixture"}), \
                    patch("semantic_crosscheck.db.connect", side_effect=AssertionError("probe connected")) as connect, \
                    patch.object(expect, "run", return_value=passing), \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                code = sxc.main(["expect", "--config", str(path), "--dsn-env", FIXTURE_ENV, "--allow-fixture-database"])
            written = json.loads((path.parent / ".sxc" / "expect" / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0, written)
        connect.assert_not_called()
        self.assertEqual(written["provenance"]["binding"], "not_applicable")
        self.assertEqual(written["runtime_evidence"]["binding"], "not_applicable")

    def test_report_never_reads_the_expect_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = Result("expect")
            res.add("expect:q01_explosion[0]", "fail", "gold[0] q01_explosion expect[0]", reason="common_mode")
            res.write(tmp)
            self.assertNotIn("expect", report.read_results(Path(tmp)))
            self.assertNotIn("expect", report.PIPELINE)


if __name__ == "__main__":
    unittest.main()

"""The README quick start, verbatim: its commands, its expected output and the CI run that mirrors it.

Why: the quick start is the first thing a new user runs, and they copy it line by line. Unit tests check that its
steps run the stages in the documented order (demo, doctor, build, reason, cube, verify, report), that the expected
summary lines follow the same order, that the table of planted defects names the checks demo.py reports for them,
and that the CI workflow runs the same command lines with the exit codes the README promises.

The integration test (SXC_TEST_ADMIN_DSN, Java, the reason packages, Node.js and a Cube adapter installed from this
checkout) runs those command lines in a copy of the demo directory, with roles created by docs/setup-roles.sql in
database sxc_f_docs_quick: every summary line and NOT PROVEN line of the clean run must be the one the README shows,
the dirty run must exit as the README says with the documented checks failing, and every check id in every result
must be in the catalogue of docs/design.md.
"""

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pgfixture
import test_docs_catalogue as catalogue
from test_docs_setup_roles import Psql

from semantic_crosscheck import cube_run, demo, owl, procutil

ROOT = pgfixture.ROOT
README = ROOT / "README.md"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
SCRIPT = "skills/semantic-crosscheck/scripts/sxc.py"
ORDER = ["demo", "doctor", "build", "reason", "cube", "verify", "report"]
DATABASE = "sxc_f_docs_quick"
LOADER, CHECK = "sxc_f_docs_q_loader", "sxc_f_docs_q_check"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def quick_start() -> str:
    return read(README).split("## 🚀 빠른 시작", 1)[1].split("\n## ", 1)[0]


def code_blocks(text: str) -> list[list[str]]:
    """Fenced code blocks, each as its lines with the fence's indentation removed."""
    blocks, current, indent = [], None, 0
    for line in text.split("\n"):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if current is None:
                current, indent = [], len(line) - len(stripped)
            else:
                blocks.append(current)
                current = None
        elif current is not None:
            current.append(line[indent:])
    return blocks


def commands(block: list[str]) -> list[list[str]]:
    """Arguments after sxc.py of the command lines in a block."""
    out = []
    for line in block:
        if line.startswith("python ") and SCRIPT in line:
            tokens = shlex.split(line, comments=True)
            out.append(tokens[tokens.index(SCRIPT) + 1:])
    return out


def stage_blocks() -> list[list[list[str]]]:
    return [cmds for cmds in (commands(b) for b in code_blocks(quick_start())) if cmds]


def clean_run() -> list[list[str]]:
    return next(cmds for cmds in stage_blocks() if [c[0] for c in cmds] == ORDER)


def dirty_run() -> list[list[str]]:
    return next(cmds for cmds in stage_blocks() if cmds[0][:1] == ["demo"] and "dirty" in cmds[0])


def expected_output() -> dict[str, dict]:
    """{stage: {"summary": text before '  ->', "not_proven": [lines]}} from the README's expected-output block."""
    block = next(b for b in code_blocks(quick_start()) if any(line.startswith("[sxc ") for line in b))
    out, stage = {}, None
    for line in block:
        m = re.match(r"\[sxc (\S+)\] ", line)
        if m:
            stage = m.group(1)
            out[stage] = {"summary": line.split("  ->", 1)[0], "not_proven": []}
        elif line.startswith("  NOT PROVEN  ") and stage:
            out[stage]["not_proven"].append(line.strip())
    return out


def promised_exit_codes() -> dict[str, int]:
    """{stage: exit code} from the README sentence about the dirty variant."""
    sentence = re.search(r"`demo`는 0[^.]*\.", quick_start()).group(0)
    return {stage: int(code) for stage, code in re.findall(r"`(\w+)`(?:은|는) (\d)", sentence)}


def defect_table() -> dict[str, str]:
    """{check id: outcome} of the README's planted-defect table (rows that name a check)."""
    rows = {}
    for line in quick_start().split("\n"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.strip().startswith("|") and len(cells) == 3:
            m = re.match(r"`([a-z_:]+)`", cells[1])
            if m:
                rows[m.group(1)] = cells[2]
    return rows


def workflow_runs() -> list[tuple[int, list[str]]]:
    """(expected exit code, arguments after sxc.py) of the CI end-to-end step, in order."""
    out = []
    for line in read(WORKFLOW).split("\n"):
        m = re.match(r"\s*expect (\d) python (\S+) (.*)$", line)
        if m and m.group(2) == SCRIPT:
            out.append((int(m.group(1)), shlex.split(m.group(3))))
    return out


class QuickStartDocumentTest(unittest.TestCase):
    def test_clean_run_order_and_expected_output(self):
        run = clean_run()
        self.assertTrue(all("--config" in argv and "demo/crosscheck.toml" in argv for argv in run))
        self.assertIn("--variant", run[0])
        self.assertEqual(run[1][run[1].index("--require") + 1], "core,reason,cube")
        expected = expected_output()
        self.assertEqual(list(expected), ORDER)
        for stage, item in expected.items():
            with self.subTest(stage=stage):
                self.assertRegex(item["summary"], rf"^\[sxc {stage}\] PASS  checks: \d+ pass, 0 fail, 0 error, 0 not_run$")

    def test_dirty_run_and_defect_table_follow_demo_py(self):
        run = dirty_run()
        self.assertEqual([argv[0] for argv in run], ["demo", "build", "reason", "cube", "verify", "report"])
        codes = promised_exit_codes()
        self.assertEqual(codes, {"demo": 0, "build": 1, "reason": 1, "cube": 0, "verify": 1, "report": 1})
        table = defect_table()
        for key, (_stage, check, _outcome) in demo.REPORTED.items():
            with self.subTest(defect=key):
                self.assertIn(check, table)
        for key, (_stage, checks, _note) in demo.EXERCISED.items():
            with self.subTest(defect=key):
                self.assertTrue(any(c in quick_start() for c in checks))
        xc_demo = read(ROOT / "commands" / "xc-demo.md")
        for key, (_stage, check, _outcome) in demo.REPORTED.items():
            with self.subTest(document="xc-demo.md", defect=key):
                self.assertIn(f"`{check}`", xc_demo)

    def test_ci_runs_the_quick_start_command_lines(self):
        runs = workflow_runs()
        clean = clean_run()
        self.assertEqual([argv for _code, argv in runs[:len(clean)]], clean)
        self.assertTrue(all(code == 0 for code, _argv in runs[:len(clean)]))
        dirty = runs[len(clean):]
        self.assertTrue(dirty and dirty[0][1][0] == "demo" and "dirty" in dirty[0][1])
        promised = promised_exit_codes()
        readme_dirty = dirty_run()
        position = -1
        for code, argv in dirty:
            with self.subTest(argv=" ".join(argv)):
                self.assertIn(argv, readme_dirty)
                self.assertGreater(readme_dirty.index(argv), position)
                position = readme_dirty.index(argv)
                self.assertEqual(code, promised[argv[0]])
        self.assertIn("report", [argv[0] for _code, argv in dirty])


def _prerequisites() -> str:
    if not pgfixture.available():
        return "SXC_TEST_ADMIN_DSN not set"
    if not owl.java_executable():
        return "Java not available"
    if not all(importlib.util.find_spec(m) for m in ("owlready2", "pyshacl", "rdflib")):
        return "reason packages not installed"
    if not procutil.which("node"):
        return "node not on PATH"
    if not cube_run.installed(cube_run.cube_home()):
        return "no Cube adapter installed from this checkout"
    return ""


@unittest.skipIf(_prerequisites(), _prerequisites() or "ready")
class QuickStartRunTest(unittest.TestCase):
    """Integration: the README commands, verbatim, in a copy of the demo directory."""

    @classmethod
    def setUpClass(cls):
        cls._drop()
        a = pgfixture._admin()
        try:
            a.run(f'CREATE DATABASE "{DATABASE}"')
        finally:
            a.close()
        conn = pgfixture._admin(DATABASE)
        try:
            Psql(conn, {"loader_role": LOADER, "check_role": CHECK, "source_schema": "sxc_demo",
                        "output_schema": "sxc_out"}).run_file(read(ROOT / "docs" / "setup-roles.sql"))
        finally:
            conn.close()
        pw = os.environ.get(pgfixture.PASSWORD_ENV)
        if pw:
            a = pgfixture._admin()
            try:
                for role in (LOADER, CHECK):
                    a.run(f"ALTER ROLE \"{role}\" PASSWORD '{pw.replace(chr(39), chr(39) * 2)}'")
            finally:
                a.close()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.work = Path(cls.tmp.name)
        shutil.copytree(ROOT / "demo", cls.work / "demo", ignore=shutil.ignore_patterns(".sxc"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        cls._drop()

    @classmethod
    def _drop(cls):
        a = pgfixture._admin()
        try:
            a.run(f'DROP DATABASE IF EXISTS "{DATABASE}" WITH (FORCE)')
            for role in (LOADER, CHECK):
                a.run(f'DROP ROLE IF EXISTS "{role}"')
        finally:
            a.close()

    def sxc(self, argv: list[str]) -> tuple[int, str]:
        env = dict(os.environ, SXC_PG_OWNER_DSN=pgfixture.dsn_for(LOADER, DATABASE),
                   SXC_PG_DSN=pgfixture.dsn_for(CHECK, DATABASE))
        env.pop(pgfixture.ADMIN_ENV, None)
        p = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / SCRIPT), *argv], cwd=str(self.work), env=env,
                           capture_output=True, text=True, encoding="utf-8", timeout=1800)
        return p.returncode, p.stdout + p.stderr

    def results(self) -> dict[str, dict]:
        out = {}
        for f in sorted((self.work / "demo" / ".sxc").glob("*/result.json")):
            out[f.parent.name] = json.loads(f.read_text(encoding="utf-8"))
        return out

    def assert_catalogued(self):
        for stage, result in self.results().items():
            for check in result["checks"]:
                with self.subTest(stage=stage, check=check["id"]):
                    self.assertTrue(catalogue.matches(check["id"]), "a result holds a check id design.md does not list")

    def test_readme_quick_start(self):
        expected = expected_output()
        for argv in clean_run():
            rc, output = self.sxc(argv)
            stage = argv[0]
            lines = output.split("\n")
            summary = next(line for line in lines if line.startswith(f"[sxc {stage}] ")).split("  ->", 1)[0]
            not_proven = [line.strip() for line in lines if line.startswith("  NOT PROVEN  ")]
            with self.subTest(stage=stage):
                self.assertEqual(rc, 0, output[-2000:])
                self.assertEqual(summary, expected[stage]["summary"])
                self.assertEqual(not_proven, expected[stage]["not_proven"])
        self.assert_catalogued()

        promised = promised_exit_codes()
        for argv in dirty_run():
            rc, output = self.sxc(argv)
            with self.subTest(stage=argv[0], variant="dirty"):
                self.assertEqual(rc, promised[argv[0]], output[-2000:])
        statuses = {c["id"]: c["status"] for r in self.results().values() for c in r["checks"]}
        table = defect_table()
        self.assertGreaterEqual(len(table), len(demo.REPORTED))
        for check, outcome in table.items():
            with self.subTest(check=check):
                self.assertEqual(statuses.get(check), re.match(r"(pass|fail)\b", outcome).group(1))
        self.assertEqual(statuses.get("v4:measure_normalization_drop:on_hand"), "pass")
        self.assert_catalogued()


if __name__ == "__main__":
    unittest.main()

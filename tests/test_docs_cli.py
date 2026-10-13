"""Documents against the command line, the runtime minimums and the configuration limits.

Why: the README, the command documents and the references are what a new user copies from. An option the parser
does not know, an option nobody documents, a minimum version that differs from the one the code enforces, or a
configuration limit that the loader does not apply all send the user into an error the documents did not predict.
These tests read the documents and compare them with sxc.parser(), the version constants (doctor, cube_run,
cube_install, db, config) and config.load. No database or network is needed.
"""

import contextlib
import io
import re
import shlex
import tempfile
import unittest
from pathlib import Path

import pgfixture

import sxc
from semantic_crosscheck import config, cube_client, cube_install, cube_run, db, doctor, shape_q
from semantic_crosscheck.result import EXIT_CONFIG, StageError

ROOT = pgfixture.ROOT
README = ROOT / "README.md"
COMMANDS = ROOT / "commands"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
SKILL_DIR = ROOT / "skills" / "semantic-crosscheck"
CONFIG_MD = SKILL_DIR / "references" / "config.md"
COMMAND_DOC = {"doctor": "xc-doctor.md", "demo": "xc-demo.md", "build": "xc-build.md", "reason": "xc-reason.md",
               "cube": "xc-cube.md", "verify": "xc-verify.md", "report": "xc-report.md", "cube-install": "xc-install.md"}
OPTION = re.compile(r"(?<![\w-])--[a-z][a-z-]*")
UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
TOML_SECTION = {"[source]": "source", "[hierarchies.<name>]": "hierarchies.h", "[cube]": "cube", "[verify]": "verify"}
BASE = 'version = 1\n[source]\nschemas = ["s"]\n'
HIERARCHY = ('[entities.e]\ntable = "s.e"\nkey = ["id"]\n[entities.n]\ntable = "s.n"\nkey = ["id"]\n'
             '[hierarchies.h]\nedge = "e"\nnode = "n"\nparent = "p"\nchild = "c"\nquantity = "q"\n')
ENV_VARS = ("SXC_PG_DSN", "SXC_PG_OWNER_DSN", "PGPASSWORD", "SXC_JAVA", "SXC_CUBE_HOME", "SXC_CUBE_DEBUG", "SXC_DEBUG")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def documents() -> list[Path]:
    return [README, ROOT / "SECURITY.md", ROOT / "THIRD_PARTY.md", ROOT / "CHANGELOG.md", ROOT / "docs" / "design.md",
            ROOT / "docs" / "releases" / "v0.1.0.md", SKILL_DIR / "SKILL.md", *sorted((SKILL_DIR / "references").glob("*.md")),
            *sorted(COMMANDS.glob("*.md"))]


def stage_parsers() -> dict:
    sub = next(a for a in sxc.parser()._actions if a.dest == "stage")
    return dict(sub.choices)


def stage_options(stage: str) -> set[str]:
    return {o for a in stage_parsers()[stage]._actions for o in a.option_strings if o.startswith("--") and o != "--help"}


def argument_hint(path: Path) -> str:
    m = re.search(r'^argument-hint:\s*"([^"]*)"', read(path), re.M)
    return m.group(1) if m else ""


def parses(argv: list[str]) -> bool:
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        try:
            sxc.parser().parse_args(argv)
        except SystemExit:
            return False
    return True


def workflow_invocations() -> list[list[str]]:
    """Arguments after sxc.py of every command line in the CI workflow."""
    out = []
    for line in read(WORKFLOW).split("\n"):
        if "scripts/sxc.py" not in line or line.strip().startswith(("#", "-")):
            continue
        tokens = shlex.split(line.strip())
        i = next(n for n, t in enumerate(tokens) if t.endswith("sxc.py"))
        out.append(tokens[i + 1:])
    return out


def load_text(text: str):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "crosscheck.toml"
        path.write_text(text, encoding="utf-8")
        return config.load(path)


def config_rows() -> list[tuple[str, str, str, str]]:
    """(section heading, key, type, meaning) of every key row in config.md."""
    rows, heading = [], ""
    for line in read(CONFIG_MD).split("\n"):
        if line.startswith("#"):
            heading = line.lstrip("#").strip().strip("`")
            continue
        cells = [c.strip() for c in UNESCAPED_PIPE.split(line.strip().strip("|"))]
        m = re.fullmatch(r"`([a-z_]+)`", cells[0]) if line.startswith("|") and len(cells) >= 4 else None
        if m:
            rows.append((heading, m.group(1), cells[1], cells[3]))
    return rows


def with_value(section: str, key: str, value: str) -> str:
    if section == "source":
        return BASE + f"{key} = {value}\n"
    if section == "hierarchies.h":
        return BASE + HIERARCHY + f"{key} = {value}\n"
    return BASE + f"[{section}]\n{key} = {value}\n"


class OptionsTest(unittest.TestCase):
    def test_every_stage_option_is_documented_in_its_command(self):
        for stage, name in COMMAND_DOC.items():
            text = read(COMMANDS / name)
            for option in stage_options(stage):
                with self.subTest(stage=stage, option=option):
                    self.assertIn(option, text)

    def test_argument_hints_name_real_options(self):
        for stage, name in COMMAND_DOC.items():
            hinted = set(OPTION.findall(argument_hint(COMMANDS / name)))
            with self.subTest(stage=stage):
                self.assertTrue(hinted)
                self.assertLessEqual(hinted, stage_options(stage))

    def test_readme_documents_the_common_options_and_every_stage(self):
        text = read(README)
        for option in ("--config", "--out", "--quiet", "--version"):
            with self.subTest(option=option):
                self.assertIn(option, text)
        for stage in stage_parsers():
            with self.subTest(stage=stage):
                self.assertIn(f"| `{stage}` |", text)
                for option in stage_options(stage) - {"--config", "--out", "--quiet"}:
                    self.assertIn(option, text)

    def test_workflow_command_lines_parse(self):
        argvs = workflow_invocations()
        self.assertGreaterEqual(len(argvs), 10)
        for argv in argvs:
            with self.subTest(argv=" ".join(argv)):
                self.assertTrue(argv == ["--version"] or parses(argv))


class VersionTest(unittest.TestCase):
    def mentions(self, pattern: str) -> list[tuple[str, str]]:
        return [(p.name, m) for p in documents() for m in re.findall(pattern, read(p))]

    def test_node_minimum(self):
        found = self.mentions(r"Node\.js (\d+)")
        self.assertTrue(found)
        for doc, major in found:
            with self.subTest(document=doc):
                self.assertEqual(int(major), cube_run.MIN_NODE)
        self.assertEqual(cube_install.MIN_NODE, cube_run.MIN_NODE)
        self.assertRegex(read(WORKFLOW), rf"node-version: '{cube_run.MIN_NODE}'")

    def test_java_python_and_postgresql_minimums(self):
        for doc, major in self.mentions(r"Java (\d+) or later"):
            with self.subTest(document=doc, runtime="java"):
                self.assertEqual(int(major), doctor.MIN_JAVA)
        for doc, minor in self.mentions(r"Python 3\.(\d+) or (?:later|newer)"):
            with self.subTest(document=doc, runtime="python"):
                self.assertEqual((3, int(minor)), doctor.MIN_PYTHON)
        for doc, major in self.mentions(r"PostgreSQL (\d+) or later"):
            with self.subTest(document=doc, runtime="postgresql"):
                self.assertEqual(int(major) * 10000, db.MIN_SERVER_VERSION)
        self.assertIn(f"'3.{doctor.MIN_PYTHON[1]}'", read(WORKFLOW))

    def test_glibc_minimums(self):
        allowed = {f"{a}.{b}" for a, b in cube_install.GLIBC_MIN.values()}
        found = self.mentions(r"glibc (\d+\.\d+)")
        self.assertTrue(found)
        for doc, version in found:
            with self.subTest(document=doc):
                self.assertIn(version, allowed)
        for path in (README, COMMANDS / "xc-install.md", ROOT / "THIRD_PARTY.md"):
            text = read(path)
            for version in allowed:
                with self.subTest(document=path.name, glibc=version):
                    self.assertIn(version, text)

    def test_cube_version_and_fixed_limits(self):
        for doc, version in self.mentions(r"[Cc]ube[ -](\d+\.\d+\.\d+)"):
            with self.subTest(document=doc):
                self.assertEqual(version, config.CUBE_VERSION)
        cap = f"{shape_q.PAIR_KEY_CAP:,}"
        limit = f"{cube_client.DEFAULT_LIMIT:,}"
        for path in (README, ROOT / "docs" / "design.md", COMMANDS / "xc-verify.md"):
            with self.subTest(document=path.name):
                self.assertIn(cap, read(path))
        for doc, n in self.mentions(r"(?<![\w/,])(\d[\d,]*) keys"):
            with self.subTest(document=doc, number=n):
                self.assertIn(n.replace(",", ""), (str(shape_q.PAIR_KEY_CAP),))
        self.assertIn(limit, read(README))
        self.assertIn(limit, read(SKILL_DIR / "references" / "pitfalls.md"))


class ConfigLimitTest(unittest.TestCase):
    def test_documented_integer_limits_are_the_loader_limits(self):
        checked = 0
        for heading, key, typ, meaning in config_rows():
            section = TOML_SECTION.get(heading)
            if section is None or typ != "integer":
                continue
            span = re.search(r"(\d+) to (\d+)", meaning)
            low = re.search(r"[Aa]t least (\d+)", meaning)
            lo, hi = (int(span.group(1)), int(span.group(2))) if span else ((int(low.group(1)), None) if low else (None, None))
            with self.subTest(key=f"{section}.{key}"):
                self.assertTrue(lo is not None or "Any integer" in meaning, "no limit documented")
                with self.assertRaises(StageError) as e:
                    load_text(with_value(section, key, "true"))
                self.assertEqual(e.exception.exit_code, EXIT_CONFIG)
                if lo is None:
                    continue
                load_text(with_value(section, key, str(lo)))
                with self.assertRaises(StageError):
                    load_text(with_value(section, key, str(lo - 1)))
                if hi is not None:
                    load_text(with_value(section, key, str(hi)))
                    with self.assertRaises(StageError):
                        load_text(with_value(section, key, str(hi + 1)))
                checked += 1
        self.assertGreaterEqual(checked, 9)

    def test_removed_and_refused_settings(self):
        for path in documents():
            with self.subTest(document=path.name):
                self.assertNotIn("show_rows", read(path))
                self.assertNotIn("[output]", read(path))
        for text in (BASE + "[output]\nshow_rows = 0\n", BASE + "[cube]\npassword = \"x\"\n",
                     'version = 1\n[source]\nschemas = ["s"]\ndsn_env = "postgresql://u@h/d"\n'):
            with self.subTest(text=text.splitlines()[-1]):
                with self.assertRaises(StageError) as e:
                    load_text(text)
                self.assertEqual(e.exception.exit_code, EXIT_CONFIG)
                self.assertNotIn("postgresql://u@h/d", str(e.exception))


class EnvironmentTest(unittest.TestCase):
    def test_environment_variables_are_documented_and_used(self):
        source = "\n".join(read(p) for p in (SKILL_DIR / "scripts").rglob("*.py"))
        for path in (README, CONFIG_MD):
            table = [line for line in read(path).split("\n") if line.startswith("| `")]
            for name in ENV_VARS:
                with self.subTest(document=path.name, variable=name):
                    self.assertTrue(any(line.startswith(f"| `{name}` |") for line in table))
        for name in ENV_VARS:
            with self.subTest(variable=name):
                self.assertIn(name, source)


if __name__ == "__main__":
    unittest.main()

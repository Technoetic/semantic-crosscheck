"""Packaging contracts: plugin manifests, command and skill documents, licence, requirement pins and the CI workflow.

These files are what users install and what agents read before they run sxc.py, so they must agree with the code
they describe: the version comes from semantic_crosscheck.VERSION, stages and options from sxc.parser(), and the
configuration keys and defaults from the config dataclasses. A drift between them sends an agent to a stage or an
option that does not exist, or to a default that is not the real one. The workflow must keep its actions pinned,
scan the whole history for leaks, and run the roles template, the tests and the demo in that order. No database or
network is needed; tests/test_docs_*.py cover the rest of the documents.
"""

import contextlib
import dataclasses
import inspect
import io
import json
import re
import shlex
import tempfile
import unittest
from pathlib import Path

import pgfixture

import sxc
from semantic_crosscheck import VERSION, config, cube_run
from semantic_crosscheck.result import EXIT_CONFIG, StageError

ROOT = pgfixture.ROOT
NAME = "semantic-crosscheck"
REPO_URL = "https://github.com/Technoetic/semantic-crosscheck"
SKILL_DIR = ROOT / "skills" / NAME
SKILL = SKILL_DIR / "SKILL.md"
REFERENCES = SKILL_DIR / "references"
COMMANDS = ROOT / "commands"
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
ADAPTER = SKILL_DIR / "adapters" / "cube"
THIRD_PARTY = ROOT / "THIRD_PARTY.md"
SCRIPT_REF = "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py"
MANIFESTS = (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", ".codex-plugin/plugin.json",
             ".agents/plugins/marketplace.json")

STAGE_MENTION = re.compile(r'sxc\.py"?\s+([a-z][a-z_-]*)')
SLASH_COMMAND = re.compile(r"(?<![\w/.-])/(xc-[a-z]+)\b")
LINK = re.compile(r"\]\(([^)\s]+)\)")
USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")
PINNED_ACTION = re.compile(r"[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}")
VERSION_COMMENT = re.compile(r"\s*#\s*v\d+(?:\.\d+){0,2}\s*")
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][0-9A-Za-z.+!-]*)")

# Keys the dataclasses give a default for but config.load requires anyway.
REQUIRED_BY_VALIDATION = {("Source", "schemas"), ("Ontology", "hierarchy"), ("Gold", "map")}
# Fields filled from the table name instead of a key.
NOT_KEYS = {("Entity", "name"), ("Hierarchy", "name"), ("Measure", "name")}
KEY_NAMES = {("Shape", "class_"): "class", ("Defined", "cls"): "class"}
CONFIG_SECTIONS = {
    "[source]": config.Source,
    "[entities.<name>]": config.Entity,
    "[hierarchies.<name>]": config.Hierarchy,
    "[[shapes]]": config.Shape,
    "[measures.<name>]": config.Measure,
    "[ontology]": config.Ontology,
    "[[ontology.defined]]": config.Defined,
    "[cube]": config.Cube,
    "[verify]": config.Verify,
    "[[gold]]": config.Gold,
    "[[gold.expect]]": config.Expect,
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_json(rel: str) -> dict:
    return json.loads(read(ROOT / rel))


def docs() -> list[Path]:
    """Every Markdown document shipped with the plugin."""
    return [ROOT / "README.md", ROOT / "SECURITY.md", THIRD_PARTY, ROOT / "CHANGELOG.md", SKILL,
            ROOT / "docs" / "design.md", *sorted((ROOT / "docs" / "releases").glob("*.md")),
            *sorted(REFERENCES.glob("*.md")), *sorted(COMMANDS.glob("*.md"))]


def command_files() -> list[Path]:
    return sorted(COMMANDS.glob("*.md"))


def frontmatter(path: Path) -> dict:
    """The flat `key: value` lines of the leading `---` block, values unparsed."""
    text = read(path)
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    out = {}
    for line in text[4:end].split("\n"):
        key, sep, value = line.partition(":")
        if sep and re.fullmatch(r"[a-z][a-z-]*", key):
            out[key] = value.strip()
    return out


def scalar(raw: str) -> str:
    """A frontmatter value as YAML reads it, in one of the two forms allowed here: a JSON-compatible double-quoted
    string, or a plain scalar that YAML cannot mistake for a sequence, mapping, alias or comment."""
    if raw.startswith('"'):
        return json.loads(raw)
    if not raw or raw[0] in "[]{}&*!|>'%@`#,?:-" or ": " in raw or " #" in raw:
        raise ValueError(f"not a safe plain YAML scalar: {raw!r}")
    return raw


def code_lines(text: str) -> list[str]:
    """Stripped lines inside fenced code blocks."""
    lines, inside = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            inside = not inside
        elif inside:
            lines.append(line.strip())
    return lines


def sxc_invocations(path: Path) -> list[tuple[str, list[str]]]:
    """(script token, arguments) of every `python ... sxc.py ...` line in a fenced code block. A synopsis
    (`sxc.py <stage> [options]`) and `sxc.py --version` are not stage command lines and are left out."""
    out = []
    for line in code_lines(read(path)):
        if not (line.startswith("python ") and "sxc.py" in line):
            continue
        tokens = shlex.split(line, comments=True)
        i = next(n for n, t in enumerate(tokens) if t.endswith("sxc.py"))
        args = tokens[i + 1:]
        if not args or args[0].startswith("<") or args == ["--version"]:
            continue
        out.append((tokens[i], args))
    return out


def parse_cli(argv: list[str]):
    """sxc.parser().parse_args(argv), turning argparse's exit into an assertion with its message."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        try:
            return sxc.parser().parse_args(argv)
        except SystemExit:
            pass
    raise AssertionError(f"sxc.py rejects {argv}: {err.getvalue().strip()[-300:]}")


def table_rows(text: str) -> list[list[str]]:
    return [[c.strip() for c in line.strip().strip("|").split("|")] for line in text.split("\n") if line.startswith("|")]


def pins(path: Path) -> tuple[dict, list, list]:
    """({name: version}, included files, lines that are not exact pins) of a requirements file."""
    reqs, includes, bad = {}, [], []
    for raw in read(path).split("\n"):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            includes.append(line[3:].strip())
            continue
        m = PIN.fullmatch(line)
        if m:
            reqs[m.group(1).lower()] = m.group(2)
        else:
            bad.append(line)
    return reqs, includes, bad


def config_doc_tables() -> dict:
    """{section heading: {key: default cell}} from the `| Key | Type | Default | Meaning |` tables of config.md."""
    sections, heading, in_table = {}, "", False
    for line in read(REFERENCES / "config.md").split("\n"):
        if line.startswith("#"):
            heading, in_table = line.lstrip("#").strip().strip("`"), False
        elif line.startswith("| Key | Type | Default |"):
            in_table = True
            sections.setdefault(heading, {})
        elif in_table and line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            m = re.fullmatch(r"`([a-z_]+)`", cells[0])
            if m:
                sections[heading][m.group(1)] = cells[2]
        else:
            in_table = False
    return sections


def documented_default(cls, f: dataclasses.Field) -> str:
    """The Default cell config.md must show for a dataclass field."""
    if (cls.__name__, f.name) in REQUIRED_BY_VALIDATION:
        return "required"
    if f.default is not dataclasses.MISSING:
        value = f.default
    elif f.default_factory is not dataclasses.MISSING:
        value = f.default_factory()
    else:
        return "required"
    if value is None:
        return "none"
    return f"`{json.dumps(value)}`" if isinstance(value, (str, dict, list)) else f"`{value}`"


class ManifestTest(unittest.TestCase):
    def test_manifests_are_json_with_one_name_and_the_package_version(self):
        for rel in MANIFESTS:
            with self.subTest(manifest=rel):
                self.assertIsInstance(load_json(rel), dict)
        claude = load_json(".claude-plugin/plugin.json")
        codex = load_json(".codex-plugin/plugin.json")
        for manifest in (claude, codex):
            self.assertEqual(manifest["name"], NAME)
            self.assertEqual(manifest["version"], VERSION)
            self.assertEqual(manifest["license"], "MIT")
            self.assertEqual(manifest["homepage"], REPO_URL)
            self.assertEqual(manifest["repository"], REPO_URL)
            self.assertTrue(manifest["author"]["name"])
        self.assertEqual(codex["interface"]["developerName"], "Technoetic")
        self.assertIn(f"v{VERSION}", codex["interface"]["longDescription"])

    def test_marketplaces_point_at_this_repository(self):
        market = load_json(".claude-plugin/marketplace.json")
        entries = [p for p in market["plugins"] if p["name"] == NAME]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["version"], VERSION)
        self.assertEqual((ROOT / entries[0]["source"]).resolve(), ROOT.resolve())
        local = load_json(".agents/plugins/marketplace.json")
        entries = [p for p in local["plugins"] if p["name"] == NAME]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source"]["source"], "local")
        self.assertEqual((ROOT / entries[0]["source"]["path"]).resolve(), ROOT.resolve())

    def test_codex_skills_path_holds_the_skill(self):
        codex = load_json(".codex-plugin/plugin.json")
        self.assertTrue((ROOT / codex["skills"] / NAME / "SKILL.md").is_file())


class CommandDocumentTest(unittest.TestCase):
    def test_every_command_has_a_yaml_safe_description(self):
        self.assertTrue(command_files())
        for path in command_files():
            with self.subTest(command=path.name):
                fm = frontmatter(path)
                self.assertTrue(scalar(fm.get("description", "")).strip())
                if "argument-hint" in fm:
                    scalar(fm["argument-hint"])

    def test_commands_name_only_real_stages_and_cover_all_of_them(self):
        seen = set()
        for path in command_files():
            with self.subTest(command=path.name):
                stages = set(STAGE_MENTION.findall(read(path)))
                self.assertTrue(stages, "no sxc.py stage named")
                self.assertLessEqual(stages, set(sxc.STAGES))
                seen |= stages
        self.assertEqual(seen, set(sxc.STAGES))

    def test_command_lines_use_the_plugin_root_and_parse(self):
        for path in command_files():
            with self.subTest(command=path.name):
                invocations = sxc_invocations(path)
                self.assertTrue(invocations, "no command line in a code block")
                for script, argv in invocations:
                    self.assertEqual(script, SCRIPT_REF)
                    parse_cli(argv)
        self.assertTrue((ROOT / SCRIPT_REF.replace("${CLAUDE_PLUGIN_ROOT}/", "")).is_file())


class DocumentTest(unittest.TestCase):
    def test_skill_frontmatter(self):
        fm = frontmatter(SKILL)
        self.assertEqual(scalar(fm["name"]), NAME)
        description = scalar(fm["description"])
        self.assertTrue(description.strip())
        self.assertLessEqual(len(description), 1024)

    def test_skill_links_every_command_and_reference(self):
        text = read(SKILL)
        for path in command_files():
            with self.subTest(command=path.name):
                self.assertIn(f"](../../commands/{path.name})", text)
        for path in sorted(REFERENCES.glob("*.md")):
            with self.subTest(reference=path.name):
                self.assertIn(f"](references/{path.name})", text)

    def test_stage_mentions_and_command_lines_match_the_cli(self):
        stage_docs = docs() + [ROOT / "docs" / "setup-roles.sql", WORKFLOW]
        for path in stage_docs:
            with self.subTest(doc=path.name):
                self.assertLessEqual(set(STAGE_MENTION.findall(read(path))), set(sxc.STAGES))
                for _script, argv in sxc_invocations(path):
                    parse_cli(argv)
        self.assertEqual(set(STAGE_MENTION.findall(read(REFERENCES / "codex.md"))), set(sxc.STAGES))

    def test_slash_commands_exist(self):
        for path in docs():
            for name in SLASH_COMMAND.findall(read(path)):
                with self.subTest(doc=path.name, command=name):
                    self.assertTrue((COMMANDS / f"{name}.md").is_file())

    def test_relative_links_resolve(self):
        for path in docs():
            for target in LINK.findall(read(path)):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                with self.subTest(doc=path.name, link=target):
                    self.assertTrue((path.parent / target.split("#", 1)[0]).exists())

    def test_release_version_appears_where_users_copy_it(self):
        readme = read(ROOT / "README.md")
        refs = {line.strip() for line in readme.split("\n")
                if line.startswith("codex plugin marketplace add Technoetic/")}
        self.assertEqual(refs, {f"codex plugin marketplace add Technoetic/{NAME} --ref v{VERSION}"})
        self.assertRegex(read(ROOT / "CHANGELOG.md"), rf"(?m)^## \[{re.escape(VERSION)}\] - \d{{4}}-\d{{2}}-\d{{2}}$")
        self.assertTrue((ROOT / "docs" / "releases" / f"v{VERSION}.md").is_file())
        major, minor, _patch = VERSION.split(".")
        self.assertIn(f"{major}.{minor}.x", read(ROOT / "SECURITY.md"))

    def test_no_machine_paths_or_inline_credentials(self):
        files = docs() + [ROOT / rel for rel in MANIFESTS] + [WORKFLOW, ROOT / "docs" / "setup-roles.sql",
                                                             ROOT / "requirements" / "core.txt", ROOT / "requirements" / "reason.txt"]
        patterns = {
            "drive path": re.compile(r"\b[A-Za-z]:[\\/]"),
            "home path": re.compile(r"(?:/Users|/home)/[A-Za-z]"),
            "credential in URL": re.compile(r"://[A-Za-z0-9._%-]+:[^\s/@\[\]<>{}`'\"]+@"),
        }
        for path in files:
            text = read(path)
            for label, rx in patterns.items():
                with self.subTest(file=path.name, pattern=label):
                    self.assertIsNone(rx.search(text))


class ConfigReferenceTest(unittest.TestCase):
    def test_every_key_is_documented_with_its_real_default(self):
        tables = config_doc_tables()
        for heading, cls in CONFIG_SECTIONS.items():
            with self.subTest(section=heading):
                self.assertIn(heading, tables)
                expected = {KEY_NAMES.get((cls.__name__, f.name), f.name): documented_default(cls, f)
                            for f in dataclasses.fields(cls) if (cls.__name__, f.name) not in NOT_KEYS}
                self.assertEqual(tables[heading], expected)
        self.assertNotIn("[output]", tables)          # removed: config.load refuses the table

    def test_malformed_configuration_is_a_configuration_error(self):
        base = 'version = 1\n[source]\nschemas = ["s"]\n'
        cases = {
            "identifier": base + '[entities.Part]\ntable = "s.part"\nkey = ["id"]\n',
            "missing key": base + '[entities.part]\nkey = ["id"]\n',
            "unqualified table": base + '[entities.part]\ntable = "part"\nkey = ["id"]\n',
            "wrong type": base + '[cube]\nport = "eighty"\n',
            "credentials": 'version = 1\n[source]\nschemas = ["s"]\ndsn = "x"\n',
        }
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "crosscheck.toml"
            for name, text in cases.items():
                with self.subTest(case=name):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(StageError) as e:
                        config.load(path)
                    self.assertEqual(e.exception.exit_code, EXIT_CONFIG)

    def test_mutation_operators_are_the_real_ones(self):
        try:
            from semantic_crosscheck import mutate
        except ImportError:
            self.skipTest("mutate module not present")
        row = next(line for line in read(REFERENCES / "config.md").split("\n") if line.startswith("| `mutation_operators` |"))
        self.assertEqual(re.findall(r"`([a-z_]+)`", row.split("|")[4]), list(mutate.OPERATORS))

    def test_demo_configuration_loads(self):
        cfg = config.load(ROOT / "demo" / "crosscheck.toml")
        self.assertEqual(cfg.cube.version, config.CUBE_VERSION)


class LicenseAndRequirementsTest(unittest.TestCase):
    def test_license_is_mit(self):
        text = read(ROOT / "LICENSE")
        self.assertEqual(text.split("\n", 1)[0], "MIT License")
        self.assertRegex(text, r"Copyright \(c\) 2026 \S")
        self.assertIn("Permission is hereby granted, free of charge, to any person obtaining a copy", text)
        self.assertIn('THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND', text)

    def test_requirement_pins_agree(self):
        core, core_includes, core_bad = pins(ROOT / "requirements" / "core.txt")
        reason, reason_includes, reason_bad = pins(ROOT / "requirements" / "reason.txt")
        self.assertEqual(core_bad + reason_bad, [])
        self.assertEqual(core_includes, [])
        self.assertEqual(reason_includes, ["core.txt"])
        self.assertIn("pg8000", core)
        self.assertEqual(set(core) & set(reason), set())
        self.assertLessEqual({"owlready2", "pyshacl", "rdflib"}, set(reason))
        rows = table_rows(read(THIRD_PARTY))
        for name, version in {**core, **reason}.items():
            with self.subTest(package=name):
                self.assertTrue(any(r[0].lower() == name and version in r for r in rows),
                                f"THIRD_PARTY.md has no row for {name} {version}")

    def test_requirement_files_named_anywhere_exist(self):
        texts = [read(p) for p in docs()] + [read(WORKFLOW)]
        texts += [read(p) for p in (SKILL_DIR / "scripts").rglob("*.py")]
        for name in sorted({m for t in texts for m in re.findall(r"requirements/([a-z_]+\.txt)", t)}):
            with self.subTest(file=name):
                self.assertTrue((ROOT / "requirements" / name).is_file())

    def test_cube_pins_agree(self):
        package = ADAPTER / "package.json"
        if not package.is_file():
            self.skipTest("adapter package.json not present")
        deps = json.loads(read(package))["dependencies"]
        self.assertEqual(deps["@cubejs-backend/server-core"], config.CUBE_VERSION)
        self.assertEqual(inspect.signature(cube_run.cube_home).parameters["version"].default, config.CUBE_VERSION)
        pins_file = ADAPTER / "native-sha256.json"
        if pins_file.is_file():
            self.assertEqual(json.loads(read(pins_file))["cube_version"], config.CUBE_VERSION)
        rows = table_rows(read(THIRD_PARTY))
        for name, version in deps.items():
            with self.subTest(package=name):
                self.assertTrue(any(r[0] == name and version in r for r in rows),
                                f"THIRD_PARTY.md has no row for {name} {version}")


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.text = read(WORKFLOW)

    def test_actions_are_pinned_by_commit_sha(self):
        uses = [USES.match(line) for line in self.text.split("\n")]
        uses = [m for m in uses if m]
        self.assertTrue(uses)
        for m in uses:
            ref, rest = m.groups()
            with self.subTest(action=ref):
                self.assertRegex(ref, PINNED_ACTION.pattern + "$")
                self.assertRegex(rest, "^" + VERSION_COMMENT.pattern + "$")
        checkouts = sum(1 for m in uses if m.group(1).startswith("actions/checkout@"))
        self.assertEqual(self.text.count("persist-credentials: false"), checkouts)

    def test_token_is_read_only(self):
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read\n")
        self.assertNotRegex(self.text, r":\s*write\b")
        self.assertNotIn("pull_request_target", self.text)

    def test_no_deny_list_in_ci(self):
        values = re.findall(r"SXC_DENYLIST:\s*(\S*)", self.text)
        self.assertTrue(values)
        for value in values:
            self.assertIn(value, ("none", "''", '""', ""))
        self.assertNotRegex(self.text, r"\.denylist\b")

    def test_referenced_scripts_exist(self):
        for rel in re.findall(r"python\s+(?:-\S+\s+)*([\w./-]+\.py)\b", self.text):
            with self.subTest(script=rel):
                self.assertTrue((ROOT / rel).is_file())

    def test_leak_scan_reads_the_whole_history(self):
        hygiene = jobs(self.text)["hygiene"]
        self.assertRegex(hygiene, r"uses: actions/checkout@\S+ # v[\d.]+\n\s+with:\n\s+fetch-depth: 0\n")
        scans = re.findall(r"python tools/leak_scan\.py([^\n]*)", hygiene)
        self.assertTrue(scans)
        for options in scans:
            self.assertIn("--history", options)
            self.assertNotIn("--allow-email", options)      # public addresses belong in tools/leak-allow.txt

    def test_integration_runs_the_roles_template_then_the_tests_then_the_demo(self):
        job = jobs(self.text)["integration"]
        roles = job.find("-f docs/setup-roles.sql")
        tests = job.find('suite = unittest.defaultTestLoader.discover("tests")')
        clean = job.find("expect 0 python skills/semantic-crosscheck/scripts/sxc.py demo")
        self.assertTrue(0 < roles < tests < clean, "setup-roles.sql with psql, then the tests, then the demo")
        self.assertRegex(job[:roles], r"\bpsql\b[^\n]*$|\bpsql -d \S+ -v loader_role=")
        for stage in ("demo", "doctor", "build", "reason", "cube", "verify", "report"):
            with self.subTest(stage=stage, variant="clean"):
                self.assertRegex(job, rf"expect 0 python skills/semantic-crosscheck/scripts/sxc\.py {stage} ")
        self.assertIn("--variant dirty", job)
        for stage in ("build", "report"):
            with self.subTest(stage=stage, variant="dirty"):
                self.assertRegex(job, rf"expect 1 python skills/semantic-crosscheck/scripts/sxc\.py {stage} ")
        self.assertIn("image: postgres:17", job)
        self.assertIn("fetch-depth: 0", job)
        self.assertIn("result.wasSuccessful() and result.testsRun > 0 and not result.skipped", job)
        self.assertIn("timeout-minutes:", job)


def jobs(text: str) -> dict[str, str]:
    """{job id: its block of the workflow} for the jobs under `jobs:` (two-space indented ids)."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"(?m)^  ([a-z][a-z0-9_-]*):\n", body)
    return dict(zip(parts[1::2], parts[2::2]))


if __name__ == "__main__":
    unittest.main()

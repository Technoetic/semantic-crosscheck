"""The check catalogue of docs/design.md against the code and the other documents.

Why: agents read check ids in results and look them up in design.md, the README, SKILL.md and the command
documents. An id the code emits but the catalogue does not list leaves the agent guessing; an id a document names
but the code never emits sends it after a check that does not exist. This module derives check-id templates from
the package source (f-strings become patterns: f"shape:{shape.id}" is shape:*) and requires:

  - every id a stage creates (the first argument of res.add(...), checks.append((...)) and Check(...), with simple
    local assignments resolved) and every id-shaped template with a catalogued prefix to match a catalogue row;
  - every catalogue row to be backed by the code: a template that produces it, or its literal text in the source;
  - every backticked check id in the user documents to name a catalogued check (or a family of them, such as
    `privileges:no_write` or `reason:*`).

tests/test_docs_quickstart.py holds the ids of real demo results against the same catalogue.
"""

import ast
import re
import unittest
from functools import lru_cache
from pathlib import Path

import pgfixture
import sxc

ROOT = pgfixture.ROOT
DESIGN = ROOT / "docs" / "design.md"
PACKAGE = ROOT / "skills" / "semantic-crosscheck" / "scripts" / "semantic_crosscheck"
STAGE_NAMES = {"any", *sxc.STAGES}
ID_SHAPE = re.compile(r"[a-z*][a-z0-9_*]*(?::[^\s:`'\"(){}]+)+")
BACKTICKED = re.compile(r"`([^`\s]+)`")
PLACEHOLDER = re.compile(r"<[^<>]*>")


def user_documents() -> list[Path]:
    skill = ROOT / "skills" / "semantic-crosscheck"
    return [ROOT / "README.md", ROOT / "SECURITY.md", ROOT / "CHANGELOG.md", DESIGN,
            *sorted((ROOT / "docs" / "releases").glob("v*.md")), skill / "SKILL.md", *sorted((skill / "references").glob("*.md")),
            *sorted((ROOT / "commands").glob("*.md"))]


@lru_cache(maxsize=None)
def catalogue() -> tuple[tuple[str, str], ...]:
    """(stage, pattern) for every check id in the table of design.md section 6, in table order."""
    text = DESIGN.read_text(encoding="utf-8")
    section = text.split("## 6. Check catalogue", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in section.split("\n"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not line.startswith("|") or len(cells) < 3 or cells[0] in ("Stage", "---"):
            continue
        rows += [(cells[0], token) for token in BACKTICKED.findall(cells[1])]
    return tuple(rows)


def patterns() -> list[str]:
    return [p for _stage, p in catalogue()]


def pattern_regex(pattern: str) -> re.Pattern:
    """A catalogue pattern (`<...>` = one id segment, no colon) as a regular expression."""
    parts = PLACEHOLDER.split(pattern)
    return re.compile("[^:]+".join(re.escape(p) for p in parts) + r"\Z")


def skeleton(pattern: str) -> str:
    return PLACEHOLDER.sub("x", pattern)


def matches(check_id: str) -> bool:
    """Whether a concrete check id (from a real result) is in the catalogue."""
    return any(pattern_regex(p).match(check_id) for p in patterns())


def prefixes() -> set[str]:
    """First segments of the catalogued ids that have one (shape, closure, v4, ...)."""
    return {p.split(":", 1)[0] for p in patterns() if ":" in p}


# ---- templates in the code ------------------------------------------------------------------------
def render(node) -> str | None:
    """A str constant or an f-string as a pattern (each replacement field becomes *); None for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            else:
                out.append("*")
        return "".join(out)
    return None


def _assignments(func) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            text = render(node.value)
            if text is not None:
                found.setdefault(node.targets[0].id, []).append(text)
    return found


def _first_arg(call: ast.Call):
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr == "add":
        receiver = f.value
        if (isinstance(receiver, ast.Name) and receiver.id == "res") or \
                (isinstance(receiver, ast.Attribute) and receiver.attr == "res"):
            return call.args[0] if call.args else None
    if isinstance(f, ast.Attribute) and f.attr == "append" and isinstance(f.value, ast.Name) \
            and f.value.id in ("checks", "rows") and call.args and isinstance(call.args[0], ast.Tuple):
        return call.args[0].elts[0] if call.args[0].elts else None
    if isinstance(f, ast.Name) and f.id == "Check":
        return call.args[0] if call.args else None
    return None


@lru_cache(maxsize=None)
def created_ids() -> tuple[tuple[str, str], ...]:
    """(module, template) of the ids passed to the calls that create checks."""
    out = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))] or [tree]
        for scope in scopes:
            local = _assignments(scope)
            for node in ast.walk(scope):
                if not isinstance(node, ast.Call):
                    continue
                arg = _first_arg(node)
                if arg is None:
                    continue
                text = render(arg)
                if text is not None:
                    out.append((path.name, text))
                elif isinstance(arg, ast.Name):
                    out += [(path.name, t) for t in local.get(arg.id, [])]
    return tuple(dict.fromkeys(out))


@lru_cache(maxsize=None)
def id_templates() -> tuple[tuple[str, str], ...]:
    """(module, template) of every id-shaped string or f-string in the package whose first segment is catalogued."""
    known = prefixes()
    out = []
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            text = render(node)
            if text and ID_SHAPE.fullmatch(text) and text.split(":", 1)[0] in known:
                out.append((path.name, text))
    return tuple(dict.fromkeys(out))


def template_regex(template: str) -> re.Pattern:
    """A code template (* = any text, colons included) as a regular expression."""
    return re.compile(".+".join(re.escape(p) for p in template.split("*")) + r"\Z")


def covered(template: str) -> bool:
    """Whether a code template can produce an id the catalogue lists (a concrete id must match a row)."""
    if "*" not in template:
        return matches(template)
    rx = template_regex(template)
    return any(rx.match(skeleton(p)) for p in patterns()) or matches(template.replace("*", "x"))


def source_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(PACKAGE.glob("*.py")))


def documented_tokens(path: Path) -> list[str]:
    """Backticked check ids in a document: id-shaped tokens whose first segment is a catalogued prefix."""
    known = prefixes()
    found = []
    for token in BACKTICKED.findall(path.read_text(encoding="utf-8")):
        if "//" in token or "=" in token:
            continue
        if ID_SHAPE.fullmatch(PLACEHOLDER.sub("x", token)) and token.split(":", 1)[0] in known:
            found.append(token)
    return found


def names_a_check(token: str) -> bool:
    """A documented token names a catalogued check, a family (`privileges:no_write`) or a wildcard (`reason:*`)."""
    if token.endswith("*"):
        return any(p.startswith(token[:-1]) for p in patterns())
    if any(PLACEHOLDER.split(p)[0].rstrip(":") == token for p in patterns() if "<" in p):
        return True
    concrete = skeleton(token)
    return any(pattern_regex(p).match(concrete) for p in patterns())


class CatalogueTest(unittest.TestCase):
    def test_catalogue_rows_are_well_formed(self):
        rows = catalogue()
        self.assertGreater(len(rows), 40)
        for stage, pattern in rows:
            with self.subTest(pattern=pattern):
                self.assertIn(stage, STAGE_NAMES)
                self.assertRegex(skeleton(pattern), r"^[a-z][a-z0-9_]*(?::[a-z0-9_.+\[\]]+)*$")
        self.assertEqual(len(patterns()), len(set(patterns())), "a check id is listed twice")

    def test_every_created_check_id_is_catalogued(self):
        created = created_ids()
        self.assertGreater(len(created), 30)
        for module, template in created:
            if template.startswith("*"):
                continue                       # composed from parts elsewhere (doctor's component ids)
            with self.subTest(module=module, template=template):
                self.assertTrue(covered(template), "the code creates a check id the catalogue does not list")

    def test_every_id_template_with_a_catalogued_prefix_is_catalogued(self):
        for module, template in id_templates():
            with self.subTest(module=module, template=template):
                self.assertTrue(covered(template))

    def test_every_catalogue_row_is_backed_by_the_code(self):
        templates = [template_regex(t) for _m, t in created_ids() + id_templates() if not t.startswith("*")]
        source = source_text()
        for pattern in patterns():
            with self.subTest(pattern=pattern):
                literal = PLACEHOLDER.split(pattern)[0]
                self.assertTrue(any(rx.match(skeleton(pattern)) for rx in templates) or literal in source,
                                "no code produces this check id")

    def test_documents_name_only_catalogued_checks(self):
        for path in user_documents():
            for token in documented_tokens(path):
                with self.subTest(document=path.name, token=token):
                    self.assertTrue(names_a_check(token))

    def test_stage_options_and_exit_codes_are_in_design(self):
        text = DESIGN.read_text(encoding="utf-8")
        for code in ("| 0 |", "| 1 |", "| 2 |", "| 3 |"):
            self.assertIn(code, text)
        for word in ("sql_violation", "class_and_bounds", "value_group", "suite_error", "suite_incomplete",
                     "excluded_by_only", "excluded_by_gold", "excluded_by_option", "PAIR_KEY_CAP"):
            with self.subTest(word=word):
                self.assertIn(word, text)


if __name__ == "__main__":
    unittest.main()

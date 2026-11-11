"""The documents that explain evidence kinds and V4 detection, against the registry and the line texts of the code.

Why: until 0.22.1 README, SKILL.md and the command documents read a passing V4 check as "the gold query detects this
change", although a V3 check between generated layers could have killed the mutant. The code now records `detection`,
and the documents say that only `detection = authored` means a gold item failed against the mutant. These tests keep
the old sentences out, require the new one, and hold the tables of docs/design.md section 8 ("Evidence kinds") and
docs/contract-report.md to the code: the codes to the tuples of evidence.py, the operator facets to
evidence.operator_facet, the remedies to evidence.remedy, and the not_proven lines and next actions to the strings
stage_verify.py writes. A line text is compared as a template: the parts the code fills in (`{...}` in the document,
a formatted value in the code) become `{}`, and a local variable that holds a literal string is read in. No database
is needed.
"""

import ast
import itertools
import re
import unittest
from pathlib import Path

import pgfixture
import test_docs_catalogue as catalogue

from semantic_crosscheck import config, evidence

ROOT = pgfixture.ROOT
README = ROOT / "README.md"
SKILL = ROOT / "skills" / "semantic-crosscheck" / "SKILL.md"
DESIGN = ROOT / "docs" / "design.md"
CONTRACT = ROOT / "docs" / "contract-report.md"
COMMANDS = ROOT / "commands"
PACKAGE = catalogue.PACKAGE
FIELD = re.compile(r"\{[^{}]*\}")
NEW_SENTENCE = "`detection = authored`"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def flat(text: str) -> str:
    """Whitespace runs collapsed, so a sentence wrapped over lines is found."""
    return re.sub(r"\s+", " ", text)


def evidence_section() -> str:
    """The subsection "Evidence kinds" of docs/design.md section 8, up to the next ### heading."""
    section = read(DESIGN).split("\n## 8. Report", 1)[1].split("\n## ", 1)[0]
    return section.split("\n### Evidence kinds\n", 1)[1].split("\n### ", 1)[0]


def table(heading: str) -> list[list[str]]:
    """The rows (cells, header and separator left out) of the first table under a #### heading of that subsection."""
    part = evidence_section().split(f"\n#### {heading}\n", 1)[1].split("\n#### ", 1)[0]
    rows, started = [], False
    for line in part.split("\n"):
        if line.startswith("|"):
            started = True
            rows.append([c.strip() for c in line.strip().strip("|").split(" | ")])
        elif started:
            break
    return rows[2:]


def ticked(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def template(text: str) -> str:
    return FIELD.sub("{}", text)


def _render(node, names: dict) -> list[str]:
    """The texts a string expression can produce, as templates: a formatted value is `{}` unless it is a local name
    bound to literal strings, which are read in (one text per binding)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [template(node.value)]
    if not isinstance(node, ast.JoinedStr):
        return []
    parts = []
    for value in node.values:
        if isinstance(value, ast.Constant):
            parts.append([template(str(value.value))])
        elif isinstance(value.value, ast.Name) and value.value.id in names:
            parts.append(names[value.value.id])
        else:
            parts.append(["{}"])
    return ["".join(p) for p in itertools.product(*parts)]


def source_templates(*modules: str) -> set[str]:
    """Every string literal and f-string of the modules, as templates, with local names of the enclosing function
    that are bound to literal strings read in."""
    found = set()
    for module in modules:
        tree = ast.parse(read(PACKAGE / f"{module}.py"))
        scopes = [tree] + [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for scope in scopes:
            names: dict[str, list[str]] = {}
            for node in ast.walk(scope):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    texts = _render(node.value, {})
                    if texts:
                        names.setdefault(node.targets[0].id, []).extend(texts)
            for node in ast.walk(scope):
                found.update(_render(node, names))
    return found


class OldSentencesTest(unittest.TestCase):
    def test_a_v4_pass_is_not_described_as_a_gold_detection(self):
        readme = read(README)
        v4_row = next(line for line in readme.split("\n") if line.startswith("| **V4"))
        self.assertNotIn("기준 질의가 잡아내는가", v4_row)
        self.assertNotIn("gold suite의 실패 항목이 변형 모델을 검출", readme)
        for path in (README, SKILL, DESIGN, *sorted(COMMANDS.glob("*.md"))):
            with self.subTest(document=path.name):
                self.assertNotIn("gold suite cannot see", flat(read(path)))
        self.assertNotIn("no gold query observes", read(PACKAGE / "stage_verify.py"))

    def test_the_documents_say_what_a_pass_means(self):
        for path in (README, SKILL, DESIGN, COMMANDS / "xc-verify.md", COMMANDS / "xc-report.md"):
            with self.subTest(document=path.name):
                self.assertIn(NEW_SENTENCE, read(path))
        self.assertIn("Only `detection = authored` means that a gold item did", flat(read(SKILL)))
        self.assertIn("gold 질의가 변화를 검출한 것은 `detection = authored`일 때뿐",
                      next(line for line in read(README).split("\n") if line.startswith("| **V4")))
        self.assertIn('<a id="evidence-kinds"></a>\n### 근거의 종류 읽기', read(README))
        self.assertIn("](#evidence-kinds)", read(README))


class RegistryTest(unittest.TestCase):
    def test_codes(self):
        codes = {row[0]: ticked(row[1]) for row in table("Codes")}
        self.assertEqual(codes["V4 `detection`"], list(evidence.DETECTIONS))
        self.assertEqual(codes["facet `status`"], list(evidence.FACET_STATUSES))
        self.assertEqual(codes["facet `reason`, `gold_evidence` `reason`"], list(evidence.FACET_REASONS))
        self.assertEqual(codes["mutant split buckets"], list(evidence.MUTANT_BUCKETS))
        self.assertEqual(codes["V4 `reason` (require mode)"],
                         [d for d in evidence.DETECTIONS if d not in ("authored", "none")] + ["suite_incomplete"])
        reasons = [*codes["report.json `mutation_reason`"], *codes["report.json `facets_reason`"]]
        source = read(PACKAGE / "evidence.py")
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertIn(f'"{reason}"', source)

    def test_operator_facets(self):
        cfg = config.load(pgfixture.DEMO_CONFIG)
        h = next(iter(cfg.hierarchies))
        documented = {}
        for row in operator_rows():
            for op in ticked(row[0]):
                documented[op] = row[1].strip("`")
        self.assertEqual(sorted(documented), sorted(evidence.MUTATION_OPERATORS))
        for op, target in evidence.operator_targets(cfg):
            with self.subTest(operator=op, target=target):
                facet = evidence.operator_facet(cfg, op, target)
                generic = facet.replace(f"{h}_", "<h>_").replace(f"m_{target}", "m_<m>")
                self.assertEqual(generic, documented[op])

    def test_report_kinds_in_readme(self):
        part = read(README).split("### 근거의 종류 읽기", 1)[1].split("\n| 종류 | 두 쪽 | 예 |\n", 1)[1]
        rows = [line for line in part.split("\n\n", 1)[0].split("\n") if line.startswith("| `")]
        self.assertEqual([ticked(r.split(" | ")[0])[0] for r in rows], list(evidence.REPORT_KINDS))

    def test_report_texts(self):
        text = flat(read(CONTRACT))
        for words in (*evidence._NOT_JUDGED.values(), "observation not recorded: the verify result has no readable "
                      "evidence section", "checks by kind (pass of all):", "killed with authored detection",
                      "compared with rows holding values by authored gold SQL",
                      "more detected by an authored mutant kill", "authored check(s) compared no row"):
            with self.subTest(text=words):
                self.assertIn(words, text)


def operator_rows() -> list[list[str]]:
    """The operator -> facet table of the V4 section of docs/design.md (section 7)."""
    text = read(DESIGN)
    part = text.split("| Operator | Facet it changes (`facet`) |", 1)[1]
    rows = []
    for line in part.split("\n")[2:]:
        if not line.startswith("|"):
            break
        rows.append([c.strip() for c in line.strip().strip("|").split(" | ")])
    return rows


class LineTextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.templates = source_templates("stage_verify", "evidence")

    def test_not_proven_lines(self):
        rows = table("Not proven lines")
        self.assertEqual([r[0] for r in rows], ["N1", "R", "N2", "N3", "N4", "N4a", "N5", "N5a", "N5b", "N6", "N7"])
        for line, _when, text in rows:
            with self.subTest(line=line):
                self.assertIn(template(ticked(text)[-1]), self.templates)

    def test_readers_and_killer(self):
        part = flat(evidence_section().split("\n#### Not proven lines\n", 1)[1].split("| Operator | Remedy |", 1)[0])
        texts = ticked(part.split("`{readers}` is", 1)[1])
        for text in ("gold queries that compare {facet}: {ids}", "no authored gold query compares {facet}",
                     "{item}, a check between generated layers", "{item}, which no evidence rule classifies"):
            with self.subTest(text=text):
                self.assertIn(text, texts)
                self.assertIn(template(text), self.templates)

    def test_remedies(self):
        part = evidence_section().split("| Operator | Remedy |", 1)[1]
        rows = []
        for line in part.split("\n")[2:]:
            if not line.startswith("|"):
                break
            rows.append([c.strip() for c in line.strip().strip("|").split(" | ")])
        documented = {}
        for ops, text in rows:
            for op in ticked(ops) or ["*"]:
                documented[op] = ticked(text)[0]
        self.assertEqual(documented.pop("*"), evidence.GENERIC_REMEDY)
        for op, text in documented.items():
            with self.subTest(operator=op):
                self.assertEqual(text.replace("{m}", "measures.x"), evidence.remedy(op, "x"))
        self.assertEqual(evidence.remedy("closure_end_swap", "h"), evidence.GENERIC_REMEDY)

    def test_next_actions(self):
        part = evidence_section().split("Next actions (`next_safe_action`)", 1)[1]
        rows = []
        for line in part.split("\n"):
            if line.startswith("| `") or line.startswith("| v"):
                rows.append([c.strip() for c in line.strip().strip("|").split(" | ")])
        self.assertEqual(len(rows), 7)
        for check, text in rows:
            with self.subTest(check=check):
                self.assertIn(template(ticked(text)[-1]), self.templates)


if __name__ == "__main__":
    unittest.main()

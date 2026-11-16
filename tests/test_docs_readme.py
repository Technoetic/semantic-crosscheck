"""The README against the facts it restates outside the quick-start commands.

Why: a reader takes these places as facts about this checkout. A navigation link that lands on another section, a
count in the prose that differs from the block above it, an example of a failing run that the code no longer
prints, or a release archive command for another version all read as documentation of this checkout while
describing something else. Two of these had happened at v0.22.0: two navigation links landed on other sections, and
the count of NOT PROVEN lines in the prose differed from the block above it. The example of a failing run and the
release archive command did not exist then and are pinned from the start. The dated record under the archive
command is about one published release, so it must not change with the version. The tests compare README.md with
its own anchors and expected-output block, with the compatibility goldens of tests/fixtures/compat_v0_21_1 (which
tests/test_data_rules_compat.py holds against the current code), with semantic_crosscheck.VERSION and with the
release dates in CHANGELOG.md. Since v0.27.0 the reference sections (stages and options, reading results, check ids,
optional diagnostics) are documents of their own in docs/reference/, and the README keeps a short stub under each
old anchor; the tests hold the stubs, the links between the README and those documents, and the column of the
requirements table that gives each minimum version's upstream end of support. No database or network is needed.
"""

import json
import re
import unittest
from datetime import date
from pathlib import Path

import pgfixture
import test_docs_quickstart as quickstart

from semantic_crosscheck import VERSION, provenance, runtime_provenance

ROOT = pgfixture.ROOT
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"
GOLDEN = ROOT / "tests" / "fixtures" / "compat_v0_21_1"
REFERENCE = ROOT / "docs" / "reference"
RESULTS = REFERENCE / "results.md"
ANCHOR = re.compile(r'<a id="([a-z0-9-]+)"></a>')
IN_PAGE_LINK = re.compile(r"\]\(#([^)\s]+)\)")
# The dated record of the archive comparison: the day of the check and the release whose asset was compared.
RECORD = re.compile(r"확인 기록\((\d{4}-\d{2}-\d{2}), v(\d+\.\d+\.\d+) 자산\)")
NOTE = "NOT PROVEN  "


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class AnchorTest(unittest.TestCase):
    def test_anchors_sit_above_their_headings_and_links_reach_them(self):
        lines = read(README).split("\n")
        anchors = {}
        for i, line in enumerate(lines):
            m = ANCHOR.fullmatch(line)
            if m:
                self.assertNotIn(m.group(1), anchors, "an anchor id is defined twice")
                anchors[m.group(1)] = i
        self.assertGreaterEqual(len(anchors), 20)
        for name, i in anchors.items():
            with self.subTest(anchor=name):
                # a heading, or the collapsed block the anchor opens (the environment variable table)
                self.assertRegex(lines[i + 1], r"^(?:#{2,3} \S|<details>$)")
        self.assertRegex(lines[anchors["reading-results"] + 1], r"^## .*결과 읽기$")
        self.assertRegex(lines[anchors["limitations-and-not-proven"] + 1], r"^## .*검증 한계와 not_proven$")
        for target in sorted(set(IN_PAGE_LINK.findall(read(README)))):
            with self.subTest(link=target):
                self.assertIn(target, anchors)


class QuickStartProseTest(unittest.TestCase):
    def test_prose_counts_the_not_proven_lines_of_the_expected_output(self):
        expected = quickstart.expected_output()
        notes = [line for item in expected.values() for line in item["not_proven"]]
        unbound = (NOTE + provenance.UNBOUND_NOTE, NOTE + runtime_provenance.UNBOUND_NOTE)
        revision, runtime = notes.count(unbound[0]), notes.count(unbound[1])
        other = {stage: sum(1 for line in item["not_proven"] if line not in unbound) for stage, item in expected.items()}
        prose = quickstart.quick_start()
        self.assertEqual(re.findall(r"`NOT PROVEN` (\d+)줄", prose), [str(len(notes))])
        self.assertEqual(re.findall(r"안내가 나옵니다\((\d+)줄\)", prose), [str(revision), str(runtime)])
        rest = re.search(r"나머지 (\d+)줄은 reason의 (\d+)줄과 verify의 (\d+)줄", prose)
        self.assertIsNotNone(rest)
        self.assertEqual([int(n) for n in rest.groups()], [sum(other.values()), other["reason"], other["verify"]])
        self.assertEqual(sum(other.values()), other["reason"] + other["verify"])


class FailureExampleTest(unittest.TestCase):
    def test_failing_run_is_the_compatibility_golden(self):
        """The two console blocks of the section are the goldens, without the `exit N` line capture.py puts first, and
        the counts the prose gives are the ones in the golden result."""
        section = read(RESULTS).split('<a id="reading-a-failure"></a>\n', 1)[1].split('\n<a id="', 1)[0]
        blocks = ["\n".join(block) + "\n" for block in quickstart.code_blocks(section)]
        goldens = []
        for name in ("build_fail", "report_fail"):
            first, rest = read(GOLDEN / name / "console.txt").split("\n", 1)
            self.assertEqual(first, "exit 1")
            goldens.append(rest)
            self.assertIn(f"](../../tests/fixtures/compat_v0_21_1/{name}/console.txt)", section)
        self.assertEqual(blocks, goldens)
        checks = json.loads(read(GOLDEN / "build_fail" / "result.json"))["checks"]
        statuses = [c["status"] for c in checks]
        self.assertEqual((statuses.count("fail"), statuses.count("pass"), len(checks)), (2, 4, 6))
        self.assertIn("검사 두 개가 실패", section)
        self.assertIn("통과한 검사 4개", section)
        warning = next(c for c in checks if c["id"] == "shape:stock_nonnegative")
        self.assertEqual((warning["status"], warning["detail"]["severity"], warning["detail"]["violations"]),
                         ("pass", "warning", 1))
        self.assertIn("위반 1건을 보고하고 통과한 `shape:stock_nonnegative`", section)


class ReferenceSplitTest(unittest.TestCase):
    """v0.27.0 moved the reference sections of the README into docs/reference/. Each explicit anchor of the moved
    sections, and the anchor of the preflight heading, keeps a short stub that links on, so a link to README#<anchor>
    from outside still lands, and every link between the README and the reference documents lands on an anchor that
    exists."""

    STUBS = {"stages": "docs/reference/cli.md", "reading-results": "docs/reference/results.md",
             "evidence-kinds": "docs/reference/results.md#evidence-kinds",
             "reading-a-failure": "docs/reference/results.md#reading-a-failure",
             "check-catalogue": "docs/reference/check-ids.md",
             "optional-diagnostic-contracts": "docs/reference/cli.md#optional-diagnostic-contracts",
             "offline-business-definition-preflight": "docs/reference/cli.md#offline-business-definition-preflight"}

    def test_each_moved_section_leaves_a_stub_that_links_on(self):
        text = read(README)
        for anchor, target in self.STUBS.items():
            with self.subTest(anchor=anchor):
                body = text.split(f'<a id="{anchor}"></a>\n', 1)[1].split("\n", 1)[1]
                stub = re.split(r'\n(?:#{1,3} |<a id=")', body, maxsplit=1)[0]
                self.assertIn(f"]({target})", stub)
                self.assertLess(len(stub.strip().split("\n")), 6, "the stub holds the section again")

    def test_links_between_the_readme_and_the_reference_land_on_anchors(self):
        readme = read(README)
        readme_anchors = set(ANCHOR.findall(readme))
        docs = {path.name: path for path in sorted(REFERENCE.glob("*.md"))}
        self.assertEqual(sorted(docs), ["check-ids.md", "cli.md", "results.md"])
        anchors = {name: set(ANCHOR.findall(read(path))) for name, path in docs.items()}
        links = re.findall(r"\]\(docs/reference/([a-z-]+\.md)(?:#([a-z0-9-]+))?\)", readme)
        self.assertGreaterEqual(len(links), 6)
        for name, anchor in links:
            with self.subTest(link=f"{name}#{anchor}"):
                self.assertIn(name, docs)
                if anchor:
                    self.assertIn(anchor, anchors[name])
        for name, path in docs.items():
            text = read(path)
            self.assertTrue(text.startswith("# "), f"{name} has no title")
            for anchor in re.findall(r"\]\(\.\./\.\./README\.md#([a-z0-9-]+)\)", text):
                with self.subTest(document=name, readme_anchor=anchor):
                    self.assertIn(anchor, readme_anchors)
            for anchor in IN_PAGE_LINK.findall(text):
                with self.subTest(document=name, anchor=anchor):
                    self.assertIn(anchor, anchors[name])
            lines = text.split("\n")
            for i, line in enumerate(lines):
                if ANCHOR.fullmatch(line):
                    with self.subTest(document=name, anchor_line=line):
                        self.assertRegex(lines[i + 1], r"^## \S")


class SupportWindowTest(unittest.TestCase):
    """v0.27.0 gives, for each minimum runtime, the day its upstream support ends and the page the day was read from
    (checked 2026-11-16). The days are the projects' published schedules, not something this checkout enforces, so
    the test holds the column, the days and their sources, and nothing in it fails when one of the days passes."""

    ROWS = {"Python + pg8000": ("2027-10", "https://peps.python.org/pep-0664/"),
            "PostgreSQL": ("2029-11-08", "https://www.postgresql.org/support/versioning/"),
            "Java": ("2027-10", "https://adoptium.net/support/"),
            "Node.js + npm": ("2027-04-30", "https://github.com/nodejs/Release")}

    def test_requirements_table_gives_each_minimum_its_upstream_end_of_support(self):
        section = read(README).split('<a id="requirements"></a>\n', 1)[1].split('\n<a id="', 1)[0]
        table = [line for line in section.split("\n") if line.startswith("|")]
        self.assertEqual(table[0], "| 구성 요소 | 필요한 단계 | 요구 버전 | 최소 버전의 상류 지원 종료 |")
        for line in table[1:]:
            with self.subTest(row=line[:40]):
                self.assertEqual(len(re.findall(r"(?<!\\)\|", line)), 5, "every row has four cells")
        for name, (day, source) in self.ROWS.items():
            with self.subTest(component=name):
                row = next(line for line in table[2:] if line.startswith(f"| {name}"))
                last = row.rsplit(" | ", 1)[1]
                self.assertIn(day, last)
                self.assertIn(f"]({source})", last)
        self.assertIn("2026-11-16에 확인", section)
        self.assertIn("Node.js 22(2027-04-30)", section)


class ReleaseArchiveTest(unittest.TestCase):
    def test_archive_command_names_this_version(self):
        commands = [line for block in quickstart.code_blocks(read(README)) for line in block if "git archive" in line]
        self.assertEqual(commands, [f"TZ=KST-9 git archive --format=zip -9 --prefix=semantic-crosscheck-{VERSION}/ "
                                    f"-o semantic-crosscheck-{VERSION}.zip v{VERSION}"])

    def test_archive_record_names_a_release_and_is_not_dated_before_it(self):
        """The record under the command says on which day the command's result was compared with a published asset,
        and of which release. It is history: a version bump changes the command, not the record. A record rewritten
        for a version without a dated changelog entry, or for a release dated after the day of the check, fails."""
        records = RECORD.findall(read(README))
        self.assertTrue(records, "no dated record of the archive comparison")
        changelog = read(CHANGELOG)
        for checked, release in records:
            with self.subTest(record=f"{checked} v{release}"):
                released = re.search(rf"^## \[{re.escape(release)}\] - (\d{{4}}-\d{{2}}-\d{{2}})$", changelog, re.M)
                self.assertIsNotNone(released, "the record names a version without a dated changelog entry")
                self.assertGreaterEqual(date.fromisoformat(checked), date.fromisoformat(released.group(1)),
                                        "the record is dated before the release it names")


if __name__ == "__main__":
    unittest.main()

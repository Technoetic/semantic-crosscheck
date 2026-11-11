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
release dates in CHANGELOG.md. No database or network is needed.
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
        section = read(README).split('<a id="reading-a-failure"></a>\n', 1)[1].split('\n<a id="', 1)[0]
        blocks = ["\n".join(block) + "\n" for block in quickstart.code_blocks(section)]
        goldens = []
        for name in ("build_fail", "report_fail"):
            first, rest = read(GOLDEN / name / "console.txt").split("\n", 1)
            self.assertEqual(first, "exit 1")
            goldens.append(rest)
            self.assertIn(f"](tests/fixtures/compat_v0_21_1/{name}/console.txt)", section)
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

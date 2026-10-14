"""Release documents: changelog, release notes, install and uninstall commands, security contact.

Why: these are the lines people copy on the day of a release. A changelog entry left "Unreleased", release notes
without the checksum and evidence sections, or an install command whose plugin or marketplace name differs from
the manifests make the first contact with the project fail. The names and the version come from the manifests and
semantic_crosscheck.VERSION, so a version bump that forgets a document fails here.
"""

import json
import re
import unittest
from datetime import date
from pathlib import Path

import pgfixture

from semantic_crosscheck import VERSION

ROOT = pgfixture.ROOT
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"
NOTES = ROOT / "docs" / "releases" / f"v{VERSION}.md"
NAME = "semantic-crosscheck"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def manifest(rel: str) -> dict:
    return json.loads(read(ROOT / rel))


def release_date() -> date:
    m = re.search(rf"^## \[{re.escape(VERSION)}\] - (\d{{4}}-\d{{2}}-\d{{2}})$", read(CHANGELOG), re.M)
    if not m:
        raise AssertionError(f"CHANGELOG.md has no dated entry for {VERSION}")
    return date.fromisoformat(m.group(1))


class ChangelogTest(unittest.TestCase):
    def test_entry_is_dated_and_linked(self):
        text = read(CHANGELOG)
        released = release_date()
        self.assertNotRegex(text, rf"## \[{re.escape(VERSION)}\] - Unreleased")
        self.assertIn(f"[{VERSION}]: https://github.com/Technoetic/{NAME}/releases/tag/v{VERSION}", text)
        self.assertIn(f"docs/releases/v{VERSION}.md", text)
        self.assertIn(released.isoformat(), read(NOTES))

    def test_entry_names_the_tools_and_documents_shipped(self):
        text = read(CHANGELOG)
        for item in ("tools/leak_scan.py", ".githooks", "docs/design.md", "docs/setup-roles.sql", "security policy", "CI"):
            with self.subTest(item=item):
                self.assertIn(item, text)


class ReleaseNotesTest(unittest.TestCase):
    def test_sections_checksums_and_evidence_placeholders(self):
        text = read(NOTES)
        for heading in ("## What it is", "## Highlights", "## Install", "## Verification evidence", "## Checksums",
                        "## Known limitations"):
            with self.subTest(heading=heading):
                self.assertIn(heading, text)
        self.assertIn("SHA256SUMS", text)
        evidence = text.split("## Verification evidence", 1)[1].split("\n## ", 1)[0]
        rows = [line for line in evidence.split("\n") if line.startswith("| ") and "---" not in line][1:]
        self.assertGreaterEqual(len(rows), 5)
        for label in ("Unit tests", "Integration tests", "CI run", "Leak scan", "Independent verification"):
            with self.subTest(evidence=label):
                self.assertTrue(any(label in row for row in rows))
        self.assertIn(f"--ref v{VERSION}", text)

    def test_readme_links_the_release_notes(self):
        self.assertIn(f"](docs/releases/v{VERSION}.md)", read(README))


class InstallCommandTest(unittest.TestCase):
    def test_install_and_uninstall_use_the_manifest_names(self):
        claude = manifest(".claude-plugin/marketplace.json")["name"]
        codex = manifest(".agents/plugins/marketplace.json")["name"]
        plugin = manifest(".claude-plugin/plugin.json")["name"]
        self.assertEqual(plugin, manifest(".codex-plugin/plugin.json")["name"])
        text = read(README)
        for line in (f"/plugin marketplace add Technoetic/{NAME}", f"/plugin install {plugin}@{claude}",
                     f"/plugin uninstall {plugin}@{claude}", f"/plugin marketplace remove {claude}",
                     f"codex plugin marketplace add Technoetic/{NAME} --ref v{VERSION}", f"codex plugin add {plugin}@{codex}",
                     f"codex plugin remove {plugin}@{codex}", f"codex plugin marketplace remove {codex}"):
            with self.subTest(command=line):
                self.assertIn(line, text)

    def test_python_requirements_are_installed_by_absolute_path_from_the_plugin(self):
        text = read(README)
        self.assertIn('python -m pip install -r "<plugin root>/requirements/reason.txt"', text)
        self.assertIn("## 🧹 제거", text)
        self.assertIn("## 🧰 문제 해결", text)

    def test_korean_summary_closes_the_readme(self):
        text = read(README)
        headings = re.findall(r"^## (.+)$", text, re.M)
        self.assertEqual(headings[-1], "한국어 요약")
        summary = text.split("## 한국어 요약", 1)[1]
        for fact in ("Python 3.11", "PostgreSQL 17", "Java 11", "Node.js 22", "glibc 2.34", f"v{VERSION}"):
            with self.subTest(fact=fact):
                self.assertIn(fact, summary)

    def test_security_contact_and_maintainer_checklist(self):
        text = read(ROOT / "SECURITY.md")
        self.assertIn(f"https://github.com/Technoetic/{NAME}/security/advisories/new", text)
        self.assertIn("private vulnerability reporting", text)
        self.assertIn("git config core.hooksPath .githooks", text)
        self.assertIn("## TLS", text)


if __name__ == "__main__":
    unittest.main()

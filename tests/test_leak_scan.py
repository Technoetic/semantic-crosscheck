"""tools/leak_scan.py and the .githooks scripts, against throw-away git repositories.

Every repository lives in a temporary directory with its own local user config; git runs with an empty global
config so the developer's settings (signing, hooks, autocrlf) cannot leak in. Fake secrets, the disallowed e-mail
address and the deny-listed term are assembled at run time, so this file never matches the scanner itself.
"""

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import pgfixture

ROOT = pgfixture.ROOT
SCANNER = ROOT / "tools" / "leak_scan.py"
HOOKS = ROOT / ".githooks"

_spec = importlib.util.spec_from_file_location("leak_scan", SCANNER)
leak_scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(leak_scan)

TOKEN = "gh" + "p_" + "A1b2C3d4" * 4 + "E5f6"               # GitHub token shape, 36 characters after the prefix
AWS_KEY = "AK" + "IA" + "ABCDEFGHIJ234567"
PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
TERM = "Zebra" + "Lantern"                                   # the synthetic deny-listed term (mixed case in files)


def at(local: str, domain: str) -> str:
    return local + "@" + domain


DISALLOWED = at("jane.roe", "mail.invalid")
MAINTAINER = at("maintainer", "example.net")                 # the throw-away repositories' configured user.email
PW = "pass" + "word"
DENYLIST_TEXT = "# private terms, one per line\n\n" + TERM.lower() + "\n"
TERM_LINE = 3


def _rmtree(path: Path):
    def retry(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


@unittest.skipUnless(shutil.which("git"), "git is not on PATH")
class RepoTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="sxc_leak_"))
        self.addCleanup(_rmtree, self.base)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.base / "gitconfig").write_text("", encoding="utf-8")
        self.deny = self.base / "terms.txt"
        self.deny.write_text(DENYLIST_TEXT, encoding="utf-8")
        self.env = {**os.environ, "GIT_CONFIG_GLOBAL": str(self.base / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
                    "PYTHONIOENCODING": "utf-8"}
        self.env.pop("SXC_DENYLIST", None)
        self.git("init", "-q")
        for key, value in (("user.name", "Test Maintainer"), ("user.email", MAINTAINER),
                           ("commit.gpgsign", "false"), ("tag.gpgsign", "false"), ("core.autocrlf", "false")):
            self.git("config", key, value)

    def git(self, *args, check=True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env, check=check, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    def write(self, rel: str, text: str):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")

    def commit(self, message: str, *extra):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, *extra)

    def scan(self, *args, deny: bool = True, cwd: Path | None = None) -> tuple[int, str, str]:
        env = dict(self.env)
        if deny:
            env["SXC_DENYLIST"] = str(self.deny)
        p = subprocess.run([sys.executable, str(SCANNER), *args], cwd=cwd or self.repo, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout, p.stderr

    def assertNoEcho(self, *texts):
        for t in texts:
            self.assertNotIn(TERM.lower(), t.lower())
            self.assertNotIn(TOKEN, t)


class HistoryScan(RepoTest):
    def setUp(self):
        super().setUp()
        self.write("README.md", "Questions: someone@example.org or noreply@anthropic.com\n")
        self.write("notes.txt", f"first line\nsee {TOKEN} here\ncustomer {TERM} order\n")
        self.commit("add notes")
        self.git("rm", "-q", "notes.txt")
        self.commit("remove notes")

    def test_history_finds_deleted_token_and_term_without_echoing_them(self):
        code, out, err = self.scan("--history")
        self.assertEqual(code, 1, out + err)
        self.assertRegex(out, r"history blob [0-9a-f]{12} \(notes\.txt\):2: github_token")
        self.assertRegex(out, rf"history blob [0-9a-f]{{12}} \(notes\.txt\):3: denylist#{TERM_LINE}")
        self.assertNoEcho(out, err)
        self.assertIn("deny-list: 1 terms", err)

    def test_working_tree_does_not_see_the_deleted_file(self):
        for args in ((), ("--worktree",)):
            code, out, err = self.scan(*args)
            self.assertEqual((code, out), (0, ""), err)
            self.assertIn("0 finding(s) in worktree", err)

    def test_commit_and_tag_metadata(self):
        self.git("commit", "-q", "--allow-empty", "-m", f"mention {TERM} in a message", "--author", f"Other Person <{DISALLOWED}>")
        self.git("tag", "-a", "v0.0.1", "-m", f"release notes naming {TERM}")
        code, out, err = self.scan("--history")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"history commit [0-9a-f]{12}:3: email")              # tree, parent, author
        self.assertRegex(out, rf"history commit [0-9a-f]{{12}}:6: denylist#{TERM_LINE}")
        self.assertRegex(out, rf"history tag [0-9a-f]{{12}}:6: denylist#{TERM_LINE}")
        self.assertNotIn(DISALLOWED, out + err)
        self.assertNoEcho(out, err)

    def test_term_in_a_path_is_hidden_in_the_location(self):
        self.write(f"docs/{TERM.lower()}-plan.txt", "nothing secret\n")
        self.commit("add plan")
        code, out, err = self.scan("--history")
        self.assertEqual(code, 1)
        self.assertIn(f"history path docs/<denylist#{TERM_LINE}>-plan.txt:name: denylist#{TERM_LINE}", out)
        code, out, err = self.scan()
        self.assertIn(f"docs/<denylist#{TERM_LINE}>-plan.txt:name: denylist#{TERM_LINE}", out)
        self.assertNoEcho(out, err)


class EmailRules(RepoTest):
    def test_allowed_addresses_pass_and_others_are_found(self):
        self.write("README.md", f"Mail someone@example.org, a@sub.example.com, noreply@anthropic.com or {MAINTAINER}\n")
        self.commit("readme")
        self.assertEqual(self.scan("--worktree", "--history")[0], 0)
        self.write("contact.txt", f"write to {DISALLOWED}\n")
        code, out, err = self.scan()
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), "contact.txt:1: email")
        self.assertNotIn(DISALLOWED, out + err)
        self.assertEqual(self.scan("--allow-email", DISALLOWED)[0], 0)
        self.assertEqual(self.scan("--allow-email", "@mail.invalid")[0], 0)
        self.assertEqual(self.scan("--allow-email", "@other.invalid")[0], 1)


class StagedScan(RepoTest):
    def test_only_the_staged_blobs_are_scanned(self):
        self.write("README.md", "clean\n")
        self.commit("initial")
        self.write("staged.txt", f"key {AWS_KEY}\n")
        self.git("add", "staged.txt")
        self.write("staged.txt", "cleaned in the working tree but not restaged\n")
        self.write("untracked.txt", f"see {TOKEN}\n")
        code, out, err = self.scan("--staged")
        self.assertEqual(code, 1, err)
        self.assertEqual(out.strip(), "staged staged.txt:1: aws_access_key")
        self.assertIn("in staged", err)

    def test_staged_on_an_unborn_branch(self):
        self.write("id_key.txt", PRIVATE_KEY + "\n")
        self.git("add", "id_key.txt")
        code, out, _ = self.scan("--staged")
        self.assertEqual((code, out.strip()), (1, "staged id_key.txt:1: private_key"))

    def test_nothing_staged_is_clean(self):
        self.assertEqual(self.scan("--staged")[0], 0)


class ExtraAndUsage(RepoTest):
    def test_extra_file_outside_the_repository(self):
        log = self.base / "ci.log"
        secret = "s3cr3t" + "Passw0rd"
        log.write_text(f"ok\nconnecting to postgresql://loader:{secret}@db.internal/demo\n", encoding="utf-8")
        code, out, err = self.scan("--extra", str(log))
        self.assertEqual(code, 1)
        self.assertEqual(out.strip(), f"extra {log}:2: url_credentials")
        self.assertNotIn(secret, out + err)

    def test_usage_errors_exit_2(self):
        self.write("README.md", "clean\n")
        self.commit("initial")
        missing = self.base / "missing.txt"
        self.assertEqual(self.scan("--bogus")[0], 2)
        self.assertEqual(self.scan("--extra", str(missing))[0], 2)
        self.assertEqual(self.scan("--allow-email", "not-an-address")[0], 2)
        self.deny.unlink()
        self.assertEqual(self.scan()[0], 2)
        outside = self.base / "not-a-repo"
        outside.mkdir()
        self.assertEqual(self.scan(deny=False, cwd=outside)[0], 2)

    def test_denylist_must_not_live_in_the_repository(self):
        self.write("README.md", "clean\n")
        self.commit("initial")
        self.deny = self.repo / "terms.txt"
        self.deny.write_text(DENYLIST_TEXT, encoding="utf-8")
        code, _, err = self.scan()
        self.assertEqual(code, 2)
        self.assertIn("outside the repository", err)
        self.write(".gitignore", "*.denylist\n")
        self.deny = self.repo / "private.denylist"
        self.deny.write_text(DENYLIST_TEXT, encoding="utf-8")
        (self.repo / "terms.txt").unlink()
        code, out, err = self.scan()
        self.assertEqual((code, out), (0, ""), err)


class Hooks(RepoTest):
    """The real hook scripts, enabled through core.hooksPath in the throw-away repository only."""

    def setUp(self):
        super().setUp()
        for name in ("pre-commit", "pre-push"):
            target = self.repo / ".githooks" / name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(HOOKS / name, target)
            os.chmod(target, 0o755)
        (self.repo / "tools").mkdir()
        shutil.copyfile(SCANNER, self.repo / "tools" / "leak_scan.py")
        self.git("config", "core.hooksPath", ".githooks")
        self.write("README.md", "clean\n")
        self.git("add", "-A")
        first = self.git("commit", "-q", "-m", "initial", check=False)
        if first.returncode != 0:
            if "no Python" in first.stderr:
                self.skipTest("the hooks found no python3 or python on PATH")
            self.fail(first.stdout + first.stderr)

    def test_hook_files_are_posix_sh_with_lf(self):
        for name, flag in (("pre-commit", b"tools/leak_scan.py --staged"), ("pre-push", b"tools/leak_scan.py --history")):
            data = (HOOKS / name).read_bytes()
            self.assertTrue(data.startswith(b"#!/bin/sh\n"))
            self.assertNotIn(b"\r", data)
            self.assertIn(flag, data)

    def test_pre_commit_refuses_a_staged_secret(self):
        self.write("leak.txt", f"see {TOKEN}\n")
        self.git("add", "leak.txt")
        r = self.git("commit", "-q", "-m", "leak", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("staged leak.txt:1: github_token", r.stdout + r.stderr)
        self.assertNoEcho(r.stdout, r.stderr)

    def test_pre_push_refuses_a_secret_in_history(self):
        remote = self.base / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], env=self.env, check=True, capture_output=True)
        self.git("remote", "add", "origin", str(remote))
        branch = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.assertEqual(self.git("push", "-q", "origin", branch, check=False).returncode, 0)
        self.write("notes.txt", f"see {TOKEN}\n")
        self.git("add", "notes.txt")
        self.git("commit", "-q", "--no-verify", "-m", "slipped past pre-commit")
        self.git("rm", "-q", "notes.txt")
        self.git("commit", "-q", "-m", "removed again")
        r = self.git("push", "-q", "origin", branch, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("(notes.txt):1: github_token", r.stdout + r.stderr)


class Patterns(unittest.TestCase):
    """The pattern set, in process."""

    def setUp(self):
        self.rules = leak_scan.Rules(deny=[(TERM_LINE, TERM.lower())], allow=[MAINTAINER, "@corp.example"])

    def hits(self, line: str, kind: str = "text") -> list:
        return self.rules.line_hits(line, kind)

    def test_tokens(self):
        cases = {
            "github_token": f"x {TOKEN} y",
            "github_pat": "github_" + "pat_" + "11ABCDEFG0" * 3,
            "aws_access_key": f"id={AWS_KEY}",
            "private_key": PRIVATE_KEY,
            "slack_token": "xo" + "xb-" + "123456789012-abcdefghijkl",
            "npm_token": "np" + "m_" + "a1B2c3D4e5" * 3 + "F6g7H8",
        }
        for pid, line in cases.items():
            self.assertIn(pid, self.hits(line), pid)
        self.assertEqual(self.hits("ghp_short and AKIA-not-a-key"), [])

    def test_url_credentials(self):
        self.assertIn("url_credentials", self.hits("postgresql://loader:" + "Xy7-real-Secret" + "@db.local/x"))
        for line in ("postgresql://user[:password]@host[:port]/database", "postgresql://{auth}@{host}{port}/{database}",
                     "postgresql://postgres@127.0.0.1:5432/postgres", f"https://user:{PW}@example.org/",
                     "postgres://u:${PGPASSWORD}@h/db", "mysql://%s:%s@%s/db", "postgresql://u:****@h/db"):
            self.assertNotIn("url_credentials", self.hits(line), line)

    def test_password_assignments(self):
        up = PW.upper()
        for line, kind in ((f'{PW} = "Tr0ub4dor&3"', "code"), (f"{up}=hunter2hunter", "text"),
                           (f"export DB_{up}=hunter2hunter", "text"), (f"https://h/x?{PW}=hunter2hunter&y=1", "text"),
                           (f'  "{PW}": "s3cr3t-value",', "text"), (f"{PW}: plain-yaml-value", "text")):
            self.assertIn("password_assignment", self.hits(line, kind), line)
        for line, kind in ((f"connect({PW}=pw)", "code"), (f"{PW} = some_variable", "code"), (f'{PW} = ""', "code"),
                           (f"{PW}: str", "text"), (f"{PW}: ${{DB_{up}}}", "text"), (f"{PW}=<your {PW}>", "text"),
                           (f"{PW} = self.{PW}", "text"), (f'"{PW}": urllib.parse.unquote(u.{PW}),', "code"),
                           (f'{up}_ENV = "SXC_TEST_ROLE_{up}"', "code"), (f"{PW}: the role's {PW}", "text"),
                           (f"ALTER ROLE x {up} 'abc'", "code")):
            self.assertNotIn("password_assignment", self.hits(line, kind), line)

    def test_high_entropy_assignments(self):
        value = "Zx9" + "Qw7Er5Ty3Ui1Op8As6Df4Gh2"
        self.assertIn("secret_assignment", self.hits(f"api_key = '{value}'"))
        self.assertIn("secret_assignment", self.hits(f"GITHUB_TOKEN={value}"))
        for line in ("secret = secrets.token_hex(32)", "token = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaa'",
                     "api_key = 'your_api_key_goes_here'", '"CUBEJS_API_SECRET": secret,'):
            self.assertNotIn("secret_assignment", self.hits(line), line)
        self.assertGreater(leak_scan.entropy(value), 3.5)

    def test_email_allowlist(self):
        for addr in ("noreply@anthropic.com", "x@example.org", "x@sub.example.com", MAINTAINER, at("a", "corp.example")):
            self.assertTrue(self.rules.email_allowed(addr), addr)
        for addr in (DISALLOWED, at("x", "example.com.evil.invalid"), at("x", "notexample.org")):
            self.assertFalse(self.rules.email_allowed(addr), addr)
        self.assertIn("email", self.hits(f"contact {DISALLOWED}"))
        self.assertIn("email", self.hits(f"https://host.invalid/form?reply={DISALLOWED}"))
        self.assertEqual(self.hits("@decorator and pkg@1.2.3 and a@b and x@y.T"), [])
        self.assertEqual(self.hits(f"postgresql://{at('xc_check', 'db.internal')}:5432/demo"), [])
        self.assertEqual(self.hits(f"ssh://{at('git', 'host.internal')}/repo.git"), [])

    def test_lockfile_registry_messages(self):
        line = f'      "deprecated": "Support may be purchased by contacting {DISALLOWED}",'
        self.assertEqual(self.rules.scan_text(line, "adapters/cube/package-lock.json"), [])
        self.assertEqual(self.rules.scan_text(line, "notes.json"), [(1, "email")])
        other = f'      "resolved": "https://registry.invalid/{DISALLOWED}/x.tgz",'
        self.assertEqual(self.rules.scan_text(other, "package-lock.json"), [(1, "email")])

    def test_denylist_and_locations(self):
        self.assertEqual(self.hits(f"a {TERM.upper()} b"), [f"denylist#{TERM_LINE}"])
        self.assertEqual(self.rules.safe(f"dir/{TERM}-x.txt"), f"dir/<denylist#{TERM_LINE}>-x.txt")
        self.assertEqual(self.rules.safe("plain/path.txt"), "plain/path.txt")

    def test_long_binary_like_lines_stay_fast(self):
        line = ("a" * 100000 + "@") * 20 + "://" + "b" * 100000 + "token=" + "c" * 600
        start = time.perf_counter()
        self.rules.line_hits(line)
        self.assertLess(time.perf_counter() - start, 5.0)

    def test_files_of_this_change_are_clean(self):
        files = [SCANNER, HOOKS / "pre-commit", HOOKS / "pre-push", Path(__file__),
                 ROOT / "tests" / "test_doctor_demo_report.py"]
        files += [pgfixture.SCRIPTS / "semantic_crosscheck" / f for f in ("doctor.py", "report.py", "stage_demo.py")]
        args = [a for f in files for a in ("--extra", str(f))]
        env = {k: v for k, v in os.environ.items() if k != "SXC_DENYLIST"}
        p = subprocess.run([sys.executable, str(SCANNER), *args], cwd=ROOT, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()

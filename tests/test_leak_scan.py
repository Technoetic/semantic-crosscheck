"""tools/leak_scan.py and the .githooks scripts, against throw-away git repositories.

Every repository lives in a temporary directory with its own local user config; git runs with an empty global
config so the developer's settings (signing, hooks, autocrlf) cannot leak in. Fake secrets, the disallowed e-mail
address and the deny-listed term are assembled at run time, so this file never matches the scanner itself.
test_leak_encodings.py and test_leak_history.py build on RepoTest and HookRepo from this module.
"""

import hashlib
import importlib.util
import os
import re
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
ALLOWLIST = ROOT / "tools" / "leak-allow.txt"
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


def masked(name: str, kind: str = "path") -> str:
    """The stand-in the scanner prints for a name holding a finding, computed independently of the scanner."""
    return f"<{kind} sha256:{hashlib.sha256(name.encode('utf-8')).hexdigest()[:12]}>"


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
        self.init_repo(self.repo)

    def init_repo(self, repo: Path, email: str = MAINTAINER):
        self.git("init", "-q", cwd=repo)
        for key, value in (("user.name", "Test Maintainer"), ("user.email", email),
                           ("commit.gpgsign", "false"), ("tag.gpgsign", "false"), ("core.autocrlf", "false")):
            self.git("config", key, value, cwd=repo)

    def git(self, *args, check=True, cwd: Path | None = None, env: dict | None = None,
            stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=cwd or self.repo, env=env or self.env, check=check,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", input=stdin)

    def write(self, rel: str, text: str):
        self.write_bytes(rel, text.encode("utf-8"))

    def write_bytes(self, rel: str, data: bytes, repo: Path | None = None):
        p = (repo or self.repo) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def commit(self, message: str, *extra, cwd: Path | None = None):
        self.git("add", "-A", cwd=cwd)
        self.git("commit", "-q", "-m", message, *extra, cwd=cwd)

    def scan(self, *args, deny: bool = True, cwd: Path | None = None, scanner: Path = SCANNER,
             stdin: str | None = None) -> tuple[int, str, str]:
        env = dict(self.env)
        if deny:
            env["SXC_DENYLIST"] = str(self.deny)
        p = subprocess.run([sys.executable, str(scanner), *args], cwd=cwd or self.repo, env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", input=stdin if stdin is not None else "")
        return p.returncode, p.stdout, p.stderr

    def assertNoEcho(self, *texts):
        for t in texts:
            self.assertNotIn(TERM.lower(), t.lower())
            self.assertNotIn(TOKEN, t)
            self.assertNotIn(DISALLOWED, t)


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
        self.assertIn("belongs in leak-allow.txt", err)               # the scanner lives outside this repository
        self.assertNoEcho(out, err)

    def test_term_in_a_path_is_masked_in_every_location(self):
        name = f"docs/{TERM.lower()}-plan.txt"
        self.write(name, f"see {TOKEN}\n")
        self.commit("add plan")
        hidden = re.escape(masked(name))
        code, out, err = self.scan("--history")
        self.assertEqual(code, 1)
        self.assertRegex(out, rf"(?m)^history path {hidden} \(commit [0-9a-f]{{12}}\):name: denylist#{TERM_LINE}$")
        self.assertRegex(out, rf"(?m)^history blob [0-9a-f]{{12}} \({hidden}\):1: github_token$")
        code, out, err = self.scan()
        self.assertRegex(out, rf"(?m)^{hidden}:name: denylist#{TERM_LINE}$")
        self.assertRegex(out, rf"(?m)^{hidden}:1: github_token$")
        self.assertNotIn("-plan.txt", out + err)
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

    def test_percent_encoded_and_escaped_addresses_are_found(self):
        for text in (f"mailto:{DISALLOWED.replace('@', '%40')}", f'{{"reply": "{DISALLOWED.replace("@", "&#64;")}"}}'):
            self.write("contact.txt", text + "\n")
            code, out, err = self.scan()
            self.assertEqual((code, out.strip()), (1, "contact.txt:1: email"), text)
            self.assertNotIn(DISALLOWED, out + err)


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

    def test_extra_path_holding_a_term_is_masked_but_not_a_finding(self):
        log = self.base / f"{TERM}-ci.log"
        log.write_text(f"see {TOKEN}\n", encoding="utf-8")
        code, out, err = self.scan("--extra", str(log))
        self.assertEqual((code, out.strip()), (1, f"extra {masked(str(log))}:1: github_token"))
        self.assertNoEcho(out, err)

    def test_usage_errors_exit_2(self):
        self.write("README.md", "clean\n")
        self.commit("initial")
        missing = self.base / "missing.txt"
        self.assertEqual(self.scan("--bogus")[0], 2)
        self.assertEqual(self.scan("--extra", str(missing))[0], 2)
        for bad in ("not-an-address", "@com", at("someone", "localhost")):
            code, _, err = self.scan("--allow-email", bad)
            self.assertEqual(code, 2, bad)
            self.assertNotIn(bad, err)
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

    def test_denylist_none_and_utf16(self):
        self.write("notes.txt", f"customer {TERM}\n")
        self.deny.write_bytes(DENYLIST_TEXT.encode("utf-16"))       # what PowerShell 5.1 `>` writes
        self.assertEqual(self.scan()[:2], (1, f"notes.txt:1: denylist#{TERM_LINE}\n"))
        env_none = {**self.env, "SXC_DENYLIST": "none"}
        p = subprocess.run([sys.executable, str(SCANNER)], cwd=self.repo, env=env_none, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        self.assertEqual((p.returncode, p.stdout), (0, ""), p.stderr)
        self.assertIn("deny-list: none (SXC_DENYLIST=none)", p.stderr)


class HookRepo(RepoTest):
    """The real hook scripts, enabled through core.hooksPath in the throw-away repository only."""

    def setUp(self):
        super().setUp()
        self.install_hooks(self.repo)
        self.write("README.md", "clean\n")
        self.git("add", "-A")
        first = self.git("commit", "-q", "-m", "initial", check=False)
        if first.returncode != 0:
            if "no Python" in first.stderr:
                self.skipTest("the hooks found no python3 or python on PATH")
            self.fail(first.stdout + first.stderr)

    def install_hooks(self, repo: Path, allowlist: str | None = None):
        """Copy the hooks and the scanner into repo (and an allowlist next to the scanner, when given)."""
        for name in ("pre-commit", "pre-push"):
            target = repo / ".githooks" / name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(HOOKS / name, target)
            os.chmod(target, 0o755)
        (repo / "tools").mkdir(exist_ok=True)
        shutil.copyfile(SCANNER, repo / "tools" / "leak_scan.py")
        if allowlist is not None:
            (repo / "tools" / "leak-allow.txt").write_text(allowlist, encoding="utf-8")
        self.git("config", "core.hooksPath", ".githooks", cwd=repo)

    def bare_remote(self, name: str = "remote.git") -> Path:
        remote = self.base / name
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], env=self.env, check=True, capture_output=True)
        return remote

    def branch(self, cwd: Path | None = None) -> str:
        return self.git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd).stdout.strip()


class Hooks(HookRepo):
    def test_hook_files_are_posix_sh_with_lf(self):
        for name, flag in (("pre-commit", b"tools/leak_scan.py --staged\n"),
                           ("pre-push", b"tools/leak_scan.py --history --push-refs -\n")):
            data = (HOOKS / name).read_bytes()
            self.assertTrue(data.startswith(b"#!/bin/sh\n"))
            self.assertNotIn(b"\r", data)
            self.assertIn(flag, data)
            self.assertIn(b"</dev/null", data)                       # the probes leave the pushed refs on stdin

    def test_pre_commit_refuses_a_staged_secret(self):
        self.write("leak.txt", f"see {TOKEN}\n")
        self.git("add", "leak.txt")
        r = self.git("commit", "-q", "-m", "leak", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("staged leak.txt:1: github_token", r.stdout + r.stderr)
        self.assertNoEcho(r.stdout, r.stderr)

    def test_pre_push_refuses_a_secret_in_history(self):
        self.git("remote", "add", "origin", str(self.bare_remote()))
        branch = self.branch()
        self.assertEqual(self.git("push", "-q", "origin", branch, check=False).returncode, 0)
        self.write("notes.txt", f"see {TOKEN}\n")
        self.git("add", "notes.txt")
        self.git("commit", "-q", "--no-verify", "-m", "slipped past pre-commit")
        self.git("rm", "-q", "notes.txt")
        self.git("commit", "-q", "-m", "removed again")
        r = self.git("push", "-q", "origin", branch, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("(notes.txt):1: github_token", r.stdout + r.stderr)

    def test_hooks_warn_while_no_denylist_is_set(self):
        warning = "warning: SXC_DENYLIST is unset"
        self.write("a.txt", "clean\n")
        self.git("add", "a.txt")
        r = self.git("commit", "-q", "-m", "a")
        self.assertIn(f"pre-commit: {warning}", r.stderr)
        self.write("b.txt", "clean\n")
        self.git("add", "b.txt")
        r = self.git("commit", "-q", "-m", "b", env={**self.env, "SXC_DENYLIST": "none"})
        self.assertNotIn(warning, r.stderr)
        self.assertIn("deny-list: none", r.stderr)
        self.git("remote", "add", "origin", str(self.bare_remote()))
        r = self.git("push", "-q", "origin", self.branch())
        self.assertIn(f"pre-push: {warning}", r.stderr)


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

    def test_denylist_and_masked_names(self):
        self.assertEqual(self.hits(f"a {TERM.upper()} b"), [f"denylist#{TERM_LINE}"])
        found = leak_scan.Findings(self.rules)
        for name, kind in ((f"dir/{TERM}-x.txt", "path"), (f"backup-{TOKEN}.txt", "path"),
                           (f"export_{DISALLOWED}.eml", "path"), (f"refs/heads/pilot-{TERM}", "ref")):
            self.assertEqual(found.show(name, kind), masked(name, kind), name)
        self.assertEqual(found.show("plain/path.txt"), "plain/path.txt")
        self.assertEqual(found.scan_name(f"{TERM}.txt", "staged "), "staged " + masked(f"{TERM}.txt"))
        self.assertEqual(found.items, [("staged " + masked(f"{TERM}.txt"), "name", f"denylist#{TERM_LINE}")])

    def test_long_binary_like_lines_stay_fast(self):
        line = ("a" * 100000 + "@") * 20 + "://" + "b" * 100000 + "token=" + "c" * 600
        start = time.perf_counter()
        self.rules.line_hits(line)
        self.assertLess(time.perf_counter() - start, 5.0)

    def test_files_of_this_change_are_clean(self):
        tests = ROOT / "tests"
        files = [SCANNER, ALLOWLIST, HOOKS / "pre-commit", HOOKS / "pre-push", Path(__file__),
                 tests / "test_leak_encodings.py", tests / "test_leak_history.py", tests / "test_doctor_demo_report.py"]
        files += [pgfixture.SCRIPTS / "semantic_crosscheck" / f for f in ("doctor.py", "report.py", "stage_demo.py")]
        args = [a for f in files for a in ("--extra", str(f))]
        env = {k: v for k, v in os.environ.items() if k != "SXC_DENYLIST"}
        p = subprocess.run([sys.executable, str(SCANNER), *args], cwd=ROOT, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()

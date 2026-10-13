"""--history and the pre-push hook against throw-away git repositories: every path name that ever existed, ref
names, the refs and commits git hands the hook, and the committed allowlist that lets a fresh clone pass.

The last test clones this repository itself (when it is a full git clone) and requires its history to pass with
no user.email configured, which is what CI and every contributor's pre-push hook see.
"""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_leak_scan import (ALLOWLIST, AWS_KEY, DISALLOWED, ROOT, TERM, TERM_LINE, TOKEN, HookRepo, RepoTest, at,
                            leak_scan, masked)

DENY = f"denylist#{TERM_LINE}"
ID = r"[0-9a-f]{12}"
PUBLISHER = at("publisher", "example.net")


def path_line(name: str, pattern: str = DENY) -> str:
    return rf"(?m)^history path {re.escape(masked(name))} \(commit {ID}\):name: {re.escape(pattern)}$"


class PathAndRefNames(RepoTest):
    def test_second_path_to_a_shared_blob(self):
        self.write("aaa_empty.txt", "")
        self.write(f"{TERM}_minutes.txt", "")
        self.commit("placeholders")
        code, out, err = self.scan("--history")
        self.assertEqual(code, 1, err)
        self.assertRegex(out, path_line(f"{TERM}_minutes.txt"))
        self.assertNoEcho(out, err)

    def test_renamed_away_and_merge_only_paths(self):
        self.write(f"{TERM}-draft.txt", "draft\n")
        self.write("keep.txt", "")
        self.commit("draft")
        self.git("mv", f"{TERM}-draft.txt", "final.txt")
        self.commit("rename")
        main = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        self.git("checkout", "-q", "-b", "side")
        self.write("side.txt", "side\n")
        self.commit("side")
        self.git("checkout", "-q", main)
        self.git("merge", "-q", "--no-ff", "--no-commit", "side")
        self.write(f"merge-{TERM}.txt", "")                      # same empty blob as keep.txt, added by the merge
        self.commit("merge side")
        code, out, err = self.scan("--history")
        self.assertEqual(code, 1, err)
        for name in (f"{TERM}-draft.txt", f"merge-{TERM}.txt"):
            self.assertRegex(out, path_line(name))
        self.assertNoEcho(out, err)

    def test_branch_and_tag_names(self):
        self.write("README.md", "clean\n")
        self.commit("initial")
        for args in (("branch", f"pilot-{TERM}"), ("tag", f"v1-{TERM}"), ("tag", "-a", f"rel-{TERM}", "-m", "release")):
            self.git(*args)
        code, out, err = self.scan("--history")
        self.assertEqual(code, 1, err)
        for ref in (f"refs/heads/pilot-{TERM}", f"refs/tags/v1-{TERM}", f"refs/tags/rel-{TERM}"):
            self.assertRegex(out, rf"(?m)^history ref {re.escape(masked(ref, 'ref'))} \({ID}\):name: {DENY}$")
        self.assertNoEcho(out, err)
        self.assertEqual(self.scan("--worktree")[:2], (0, ""))

    def test_secrets_in_path_names_are_never_printed(self):
        token_name, address_name = f"backup-{TOKEN}.txt", f"export_{DISALLOWED}.eml"
        self.write(token_name, "clean\n")
        self.write(address_name, f"key {AWS_KEY}\n")
        self.git("add", "-A")
        token_shown, address_shown = masked(token_name), masked(address_name)
        expected = {
            "--staged": [f"staged {token_shown}:name: github_token", f"staged {address_shown}:name: email",
                         f"staged {address_shown}:1: aws_access_key"],
            "--worktree": [f"{token_shown}:name: github_token", f"{address_shown}:name: email",
                           f"{address_shown}:1: aws_access_key"],
            "--history": [path_line(token_name, "github_token"), path_line(address_name, "email"),
                          rf"(?m)^history blob {ID} \({re.escape(address_shown)}\):1: aws_access_key$"],
        }
        for mode, lines in expected.items():
            if mode == "--worktree":
                self.git("commit", "-q", "-m", "exports")
            code, out, err = self.scan(mode, deny=False)
            self.assertEqual(code, 1, err)
            for line in lines:
                if line.startswith("(?m)"):
                    self.assertRegex(out, line)
                else:
                    self.assertIn(line + "\n", out)
            self.assertNoEcho(out, err)
            self.assertNotIn(AWS_KEY, out + err)

    def test_clean_history_passes(self):
        self.write("README.md", "Contact: someone@example.org\n")
        self.write("docs/guide.txt", "nothing private\n")
        self.commit("initial")
        self.git("tag", "-a", "v0.1.0", "-m", "first release")
        self.git("branch", "feature/clean-name")
        self.write("docs/guide.txt", "still nothing private\n")
        self.commit("update")
        code, out, err = self.scan("--worktree", "--history")
        self.assertEqual((code, out), (0, ""), err)
        self.assertIn("0 finding(s) in worktree, history", err)


class PushHook(HookRepo):
    """The pre-push hook hands git's ref lines to the scanner."""

    def setUp(self):
        super().setUp()
        self.git("remote", "add", "origin", str(self.bare_remote()))
        self.main = self.branch()
        self.push_env = {**self.env, "SXC_DENYLIST": str(self.deny)}

    def push(self, *refspecs: str) -> subprocess.CompletedProcess:
        return self.git("push", "-q", "origin", *refspecs, check=False, env=self.push_env)

    def git_input(self, *args: str, data: str) -> str:
        p = subprocess.run(["git", *args], cwd=self.repo, env=self.env, input=data.encode("utf-8"),
                           capture_output=True, check=True)
        return p.stdout.decode("utf-8").strip()

    def test_remote_ref_name_is_scanned(self):
        self.assertEqual(self.push(self.main).returncode, 0)
        ref = f"refs/heads/pilot-{TERM}"
        r = self.push(f"{self.main}:{ref}")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"push ref {masked(ref, 'ref')}:name: {DENY}", r.stdout + r.stderr)
        self.assertNoEcho(r.stdout, r.stderr)
        self.assertEqual(self.git("ls-remote", "origin").stdout.count("pilot-"), 0)

    def test_pushed_commit_that_no_local_ref_reaches(self):
        blob = self.git_input("hash-object", "-w", "--stdin", data=f"see {TOKEN}\n")
        tree = self.git_input("mktree", data=f"100644 blob {blob}\tnotes.txt\n")
        commit = self.git_input("commit-tree", tree, "-p", "HEAD", "-m", "dangling", data="")
        self.assertEqual(self.scan("--history", deny=False)[0], 0)        # no ref reaches it
        r = self.push(f"{commit}:refs/heads/side")
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stdout + r.stderr, rf"history blob {ID} \(notes\.txt\):1: github_token")
        self.assertNoEcho(r.stdout, r.stderr)

    def test_a_push_that_only_deletes_refs_is_not_scanned(self):
        self.assertEqual(self.push(self.main, f"{self.main}:refs/heads/feature").returncode, 0)
        self.write("notes.txt", f"see {TOKEN}\n")
        self.git("add", "notes.txt")
        self.git("commit", "-q", "--no-verify", "-m", "slipped past pre-commit")
        self.assertNotEqual(self.push(self.main).returncode, 0)
        r = self.push(":refs/heads/feature")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("the push only deletes refs", r.stderr)

    def test_push_input_format(self):
        head = self.git("rev-parse", "HEAD").stdout.strip()
        zero = "0" * len(head)
        for text in ("refs/heads/main abc refs/heads/main def\n", "only three fields\n"):
            self.assertEqual(self.scan("--push-refs", "-", stdin=text)[0], 2, text)
        typed = f"HEAD@{{1 day ago}} {head} refs/heads/elsewhere {zero}\n"
        code, out, err = self.scan("--push-refs", "-", stdin=typed)
        self.assertEqual((code, out), (0, ""), err)
        self.assertIn("in history", err)
        self.assertEqual(self.scan("--push-refs", "-", stdin="")[:2], (0, ""))


class FreshClone(HookRepo):
    """A clone has no user.email of its own: the committed allowlist is what lets the published history pass."""

    def publish(self, allowlist: str) -> Path:
        origin = self.base / "origin"
        origin.mkdir()
        self.init_repo(origin, email=PUBLISHER)
        self.install_hooks(origin, allowlist=allowlist)
        self.write_bytes("README.md", b"clean\n", repo=origin)
        self.commit("publish", cwd=origin)
        clone = self.base / "clone"
        self.git("clone", "-q", str(origin), str(clone), cwd=self.base)
        self.assertEqual(self.git("config", "--get", "user.email", cwd=clone, check=False).returncode, 1)
        return clone

    def test_allowlisted_identity_passes_and_others_are_reported(self):
        lines = ["# public identities", f"{PUBLISHER.upper()}  # the publisher", "", "@users.noreply.github.com", ""]
        clone = self.publish("\n".join(lines))
        scanner = clone / "tools" / "leak_scan.py"
        code, out, err = self.scan("--history", deny=False, cwd=clone, scanner=scanner)
        self.assertEqual((code, out), (0, ""), err)
        self.assertIn("allowlist: 2 entries", err)
        (clone / "tools" / "leak-allow.txt").write_text("# nothing allowed\n", encoding="utf-8")
        code, out, err = self.scan("--history", deny=False, cwd=clone, scanner=scanner)
        self.assertEqual(code, 1)
        self.assertRegex(out, rf"(?m)^history commit {ID}:3: email$")
        self.assertIn("belongs in tools/leak-allow.txt", err)
        self.assertNotIn(PUBLISHER, out + err)

    def test_pre_push_hook_of_a_fresh_clone(self):
        clone = self.publish(f"{PUBLISHER}\n")
        self.git("config", "core.hooksPath", ".githooks", cwd=clone)
        public = self.bare_remote("public.git")
        env = {**self.env, "SXC_DENYLIST": "none"}
        r = self.git("push", "-q", str(public), "HEAD:refs/heads/main", cwd=clone, check=False, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("allowlist: 1 entries", r.stderr)
        origin = self.base / "origin"
        self.git("commit", "-q", "--allow-empty", "-m", "outside", "--author", f"Other <{DISALLOWED}>", cwd=origin)
        self.git("fetch", "-q", cwd=clone)
        r = self.git("push", "-q", str(public), "refs/remotes/origin/HEAD:refs/heads/main", cwd=clone, check=False, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stdout + r.stderr, rf"history commit {ID}:3: email")
        self.assertNotIn(DISALLOWED, r.stdout + r.stderr)


class AllowlistFile(unittest.TestCase):
    def test_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "leak-allow.txt"
            lines = ["# comment", "", f"{PUBLISHER.upper()}   # trailing comment", "@Corp.Example", ""]
            path.write_text("\n".join(lines), encoding="utf-8-sig")
            self.assertEqual(leak_scan.load_allowlist(path), [PUBLISHER, "@corp.example"])
            self.assertEqual(leak_scan.load_allowlist(Path(tmp) / "missing.txt"), [])
            for line_no, bad in ((2, "@com"), (3, "not-an-address"), (2, at("a", "b"))):
                path.write_text("# fine\n" * (line_no - 1) + bad + "\n", encoding="utf-8")
                with self.assertRaisesRegex(leak_scan.UsageError, rf"line {line_no}:") as caught:
                    leak_scan.load_allowlist(path)
                self.assertNotIn(bad, str(caught.exception))
            path.write_bytes(PUBLISHER.encode("utf-16"))
            with self.assertRaises(leak_scan.UsageError):
                leak_scan.load_allowlist(path)

    def test_committed_allowlist_is_valid(self):
        entries = leak_scan.load_allowlist(ALLOWLIST)
        self.assertIn("@users.noreply.github.com", entries)
        self.assertIn(at("noreply", "github.com"), entries)
        self.assertEqual(len(entries), len(set(entries)))
        self.assertEqual(leak_scan.ALLOWLIST, ALLOWLIST.resolve())


class ThisRepository(RepoTest):
    def test_history_passes_in_a_fresh_clone(self):
        probe = self.git("-C", str(ROOT), "rev-parse", "--is-shallow-repository", "--show-toplevel", check=False,
                         cwd=self.base)
        lines = probe.stdout.splitlines()
        if probe.returncode != 0 or len(lines) != 2 or Path(lines[1]).resolve() != ROOT.resolve():
            self.skipTest("the repository root is not a git work tree")
        if lines[0] == "true":
            self.skipTest("a shallow clone does not hold the history (CI scans it with a full clone)")
        clone = self.base / "clone"
        self.git("clone", "-q", "--no-local", str(ROOT), str(clone), cwd=self.base)
        self.assertEqual(self.git("config", "--get", "user.email", cwd=clone, check=False).returncode, 1)
        code, out, err = self.scan("--history", deny=False, cwd=clone)
        self.assertEqual((code, out), (0, ""), err)


if __name__ == "__main__":
    unittest.main()

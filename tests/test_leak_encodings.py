"""Deny-listed terms and secrets in the forms a person would still recognise, against throw-away git repositories.

The deny-listed Korean term is synthetic ("zebra lantern"). Each fixture stores it, the synthetic English term or a
fake secret in one form: decomposed Hangul (NFD, as macOS writes it) in content and in a file name, full-width
letters, UTF-16 with a byte order mark (what PowerShell 5.1 writes) and without one, CP949 (Korean Windows), JSON
\\uXXXX escapes, HTML character references, percent-encoding in content and in a file name, gzip, bzip2, xz, zip and
raw binary. Every fixture is checked in the staged blobs, the working tree and history, and the output must hold
exactly the expected findings: no miss, no false positive, no echo. The limits of decompression are checked in
process.
"""

import bz2
import gzip
import io
import json
import lzma
import random
import re
import unicodedata
import unittest
import zipfile
from unittest import mock
from urllib.parse import quote

from test_leak_scan import DISALLOWED, TERM, TOKEN, RepoTest, leak_scan, masked

KOREAN = "얼룩말등불"                                        # the synthetic deny-listed Korean term
DENY_TEXT = "# synthetic private terms\n" + KOREAN + "\n" + TERM.lower() + "\n"
KO, EN = "denylist#2", "denylist#3"
DENY = [(2, KOREAN), (3, TERM.lower())]


def table(rows: int, at_row: int, cell: str) -> bytes:
    """CSV rows that compress well, with cell in one row only, so the decompressed line number is checked."""
    return "".join(f"{i},{cell if i == at_row else 'placeholder'}\n" for i in range(1, rows + 1)).encode()


def zip_bytes(members: dict, method: int = zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", method) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def fixtures() -> list:
    """[(file name, content, [(location suffix or 'name', line or None for any, pattern)])]."""
    rows = table(200, 150, KOREAN)
    fullwidth = "".join(chr(ord(c) + 0xFEE0) for c in TERM)
    references = "".join(f"&#{ord(c)};" for c in KOREAN[:2]) + "".join(f"&#x{ord(c):x};" for c in KOREAN[2:])
    member = f"{TERM}.txt"
    return [
        ("nfd.txt", unicodedata.normalize("NFD", f"customer {KOREAN}\n").encode(), [("", 1, KO)]),
        (unicodedata.normalize("NFD", f"memo-{KOREAN}.txt"), b"nothing\n", [("name", "name", KO)]),
        ("fullwidth.txt", f"customer {fullwidth}\n".encode(), [("", 1, EN)]),
        ("utf16-bom.txt", f"customer {KOREAN} and {TERM}\r\n".encode("utf-16"), [("", 1, KO), ("", 1, EN)]),
        ("utf16-be.txt", f"first line\ncustomer {KOREAN}\n".encode("utf-16-be"), [("", 2, KO)]),
        ("cp949.csv", f"id,name\n1,{KOREAN}\n".encode("cp949"), [("", 2, KO)]),
        ("customers.json", (json.dumps({"customer": KOREAN}) + "\n").encode(), [("", 1, KO)]),
        ("page.html", f"<p>{references}</p>\n".encode(), [("", 1, KO)]),
        ("links.txt", "\n".join(["start", f"https://example.org/{quote(KOREAN)}",
                                 f"https://example.org/{quote(KOREAN, encoding='cp949')}", ""]).encode(),
         [("", 2, KO), ("", 3, KO)]),
        (f"notes-{quote(KOREAN)}.txt", b"nothing\n", [("name", "name", KO)]),
        ("rows.csv.gz", gzip.compress(rows), [(" [gzip]", 150, KO)]),
        ("rows.csv.bz2", bz2.compress(rows), [(" [bz2]", 150, KO)]),
        ("rows.csv.xz", lzma.compress(rows), [(" [xz]", 150, KO)]),
        ("book.zip", zip_bytes({"xl/sharedStrings.xml": f"<t>{KOREAN}</t>".encode(), member: b"x"}),
         [(" [zip xl/sharedStrings.xml]", 1, KO), (f" [zip {masked(member)}]", "name", EN), ("", None, EN)]),
        ("blob.bin", b"\x00\x01\xff\xfe" + KOREAN.encode() + b"\x00\x80", [("", 1, KO)]),
        ("token-utf16.txt", f"key {TOKEN}\n".encode("utf-16"), [("", 1, "github_token")]),
        ("contact.txt.gz", gzip.compress(f"mailto:{DISALLOWED.replace('@', '%40')}\n".encode()), [(" [gzip]", 1, "email")]),
        ("clean-utf16.txt", "nothing to see\r\n".encode("utf-16"), []),
        ("clean.csv.gz", gzip.compress(table(50, 0, "")), []),
    ]


class EncodedForms(RepoTest):
    def setUp(self):
        super().setUp()
        self.deny.write_text(DENY_TEXT, encoding="utf-8")
        self.fixtures = fixtures()
        for name, data, _ in self.fixtures:
            self.write_bytes(name, data)
        self.git("add", "-A")

    def expected(self, mode: str) -> list:
        """One regex per expected output line of the given scan mode."""
        patterns = []
        for name, _, findings in self.fixtures:
            shown = masked(name) if any(where == "name" for where, _, _ in findings) else name
            for where, line, pattern in findings:
                number = r"\d+" if line is None else re.escape(str(line))
                if mode == "history" and where == "name":
                    location = rf"history path {re.escape(shown)} \(commit [0-9a-f]{{12}}\)"
                elif mode == "history":
                    location = rf"history blob [0-9a-f]{{12}} \({re.escape(shown)}\){re.escape(where)}"
                else:
                    prefix = "staged " if mode == "staged" else ""
                    location = re.escape(prefix + shown + ("" if where == "name" else where))
                patterns.append(rf"{location}:{number}: {re.escape(pattern)}")
        return patterns

    def assertExactly(self, mode: str, out: str, err: str):
        lines = out.splitlines()
        for pattern in self.expected(mode):
            with self.subTest(mode=mode, expected=pattern):
                self.assertTrue(any(re.fullmatch(pattern, line) for line in lines), out)
        unexpected = [line for line in lines if not any(re.fullmatch(p, line) for p in self.expected(mode))]
        self.assertEqual(unexpected, [], mode)
        text = out + err
        for secret in (KOREAN, unicodedata.normalize("NFD", KOREAN), quote(KOREAN), TOKEN, DISALLOWED):
            self.assertNotIn(secret, text)
        self.assertNotIn(TERM.lower(), text.lower())

    def test_staged_blobs(self):
        code, out, err = self.scan("--staged")
        self.assertEqual(code, 1, err)
        self.assertExactly("staged", out, err)

    def test_working_tree_and_history(self):
        self.git("commit", "-q", "-m", "fixtures")
        for mode in ("worktree", "history"):
            code, out, err = self.scan(f"--{mode}")
            self.assertEqual(code, 1, err)
            self.assertExactly(mode, out, err)


class Decoding(unittest.TestCase):
    """Normalisation, decodings and decompression limits, in process."""

    def scan(self, data: bytes, path: str = "file.bin", deny=DENY) -> list:
        found = leak_scan.Findings(leak_scan.Rules(deny=deny))
        found.scan(path, data, path)
        return found.items

    def test_normalisation_works_both_ways(self):
        nfd = unicodedata.normalize("NFD", KOREAN)
        self.assertEqual(leak_scan.Rules(deny=[(1, nfd)]).line_hits(f"a {KOREAN} b"), ["denylist#1"])
        self.assertEqual(leak_scan.Rules(deny=[(1, KOREAN)]).line_hits(f"a {nfd} b"), ["denylist#1"])
        fullwidth = "".join(chr(ord(c) + 0xFEE0) for c in TERM)
        self.assertEqual(leak_scan.Rules(deny=[(1, fullwidth)]).line_hits(f"x {TERM.upper()} y"), ["denylist#1"])
        self.assertEqual(leak_scan.Rules(deny=[(1, KOREAN)]).line_hits("unrelated text"), [])

    def test_name_bytes_in_cp949(self):
        name = ("dir/" + KOREAN + ".txt").encode("cp949").decode("utf-8", "surrogateescape")
        self.assertEqual(leak_scan.Rules(deny=DENY).name_hits(name), [KO])

    def test_utf16_without_a_byte_order_mark_by_its_nul_pattern(self):
        hangul_lines = (KOREAN * 4 + "\n") * 20                    # one NUL (a newline's high byte) per 21 units
        self.assertEqual(leak_scan.utf16_order(hangul_lines.encode("utf-16-le")), "utf-16-le")
        self.assertEqual(leak_scan.utf16_order("plain text\n".encode("utf-16-be")), "utf-16-be")
        self.assertIsNone(leak_scan.utf16_order(random.Random(5).randbytes(65536)))
        self.assertIsNone(leak_scan.utf16_order(b"\x00\x00\x01\x02" * 1000))
        self.assertEqual(self.scan(f"intro\n{KOREAN}\n".encode("utf-16-le")), [("file.bin", "2", KO)])

    def test_utf32_with_a_byte_order_mark(self):
        self.assertEqual(self.scan(f"x\n{KOREAN}\n".encode("utf-32")), [("file.bin", "2", KO)])

    def test_clean_binary_has_no_findings(self):
        noise = random.Random(7).randbytes(200000)
        self.assertEqual(self.scan(noise), [])

    def test_zip_member_name_in_cp949(self):
        placeholder = "N" * len(KOREAN.encode("cp949"))
        data = zip_bytes({f"{placeholder}.txt": b"x"}, zipfile.ZIP_STORED)
        data = data.replace(placeholder.encode(), KOREAN.encode("cp949"))
        member = (KOREAN + ".txt").encode("cp949").decode("utf-8", "surrogateescape")
        self.assertIn((f"file.bin [zip {leak_scan.mask(member)}]", "name", KO), self.scan(data))

    def test_three_levels_are_scanned_and_a_fourth_is_reported(self):
        data = f"x {KOREAN}\n".encode()
        for _ in range(3):
            data = gzip.compress(data)
        self.assertEqual(self.scan(data), [("file.bin [gzip] [gzip] [gzip]", "1", KO)])
        self.assertEqual(self.scan(gzip.compress(data)), [("file.bin [gzip] [gzip] [gzip]", "0", "compressed_unscanned")])

    def test_decompression_stops_at_the_cap(self):
        data = gzip.compress(f"{KOREAN}\n".encode() + b"y" * 5000 + f"\n{TERM}\n".encode())
        with mock.patch.object(leak_scan, "EXPAND_BYTES", 1000):
            items = self.scan(data)
        self.assertEqual(items, [("file.bin [gzip]", "1", KO), ("file.bin", "0", "compressed_unscanned")])
        self.assertEqual(self.scan(data), [("file.bin [gzip]", "1", KO), ("file.bin [gzip]", "3", EN)])

    def test_corrupt_and_truncated_data_is_reported(self):
        rows = table(20000, 3, KOREAN)
        truncated = gzip.compress(rows)[:-2000]
        self.assertEqual(self.scan(truncated), [("file.bin [gzip]", "3", KO), ("file.bin", "0", "compressed_unscanned")])
        for data in (b"\x1f\x8b" + b"not a gzip stream", b"PK\x03\x04" + b"\0" * 40, lzma.compress(rows)[:-500]):
            self.assertIn(("file.bin", "0", "compressed_unscanned"), self.scan(data))

    def test_zip_limits_cover_every_member(self):
        data = zip_bytes({"a.txt": f"{KOREAN}\n".encode() + b"y" * 600, "b.txt": b"z" * 600 + f"\n{TERM}\n".encode()})
        with mock.patch.object(leak_scan, "EXPAND_BYTES", 1000):
            self.assertEqual(self.scan(data), [("file.bin [zip a.txt]", "1", KO), ("file.bin", "0", "compressed_unscanned")])
        with mock.patch.object(leak_scan, "ZIP_MEMBERS", 1):
            self.assertEqual(self.scan(data), [("file.bin [zip a.txt]", "1", KO), ("file.bin", "0", "compressed_unscanned")])
        self.assertEqual(self.scan(data), [("file.bin [zip a.txt]", "1", KO), ("file.bin [zip b.txt]", "2", EN)])

    def test_encrypted_zip_member_is_reported(self):
        data = bytearray(zip_bytes({"secret.txt": b"hidden"}, zipfile.ZIP_STORED))
        local, central = data.find(b"PK\x03\x04"), data.find(b"PK\x01\x02")
        data[local + 6] |= 1
        data[central + 8] |= 1
        self.assertIn(("file.bin", "0", "compressed_unscanned"), self.scan(bytes(data)))

    def test_magic_bytes_without_compression_stay_plain_text(self):
        self.assertEqual(self.scan(b"BZh9 is how a bzip2 header starts\n", "notes.txt"), [])


if __name__ == "__main__":
    unittest.main()

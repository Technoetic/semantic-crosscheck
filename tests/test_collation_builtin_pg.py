"""PostgreSQL 17 built-in C.UTF-8 collation guidance and code-point order check.

Why: docs/data-rules.md and references/pitfalls.md recommend PostgreSQL 17's
built-in collation provider (`provider = builtin, locale = 'C.UTF-8'`) when
creating dedicated databases or deterministic views for `data-rules`, because
it is deterministic, immune to OS glibc/ICU collation drift, matches Python's
code-point `sorted()` order, and keeps Unicode case folding (`upper('é') = 'É'`).
The documentation check runs everywhere without a database; the database check
runs when SXC_TEST_ADMIN_DSN is set and creates its collation inside a rolled-back
transaction without altering persistent database objects.
"""

from __future__ import annotations

import re
import unittest

import pgfixture

DATA_RULES_DOC = pgfixture.ROOT / "docs" / "data-rules.md"
PITFALLS_DOC = pgfixture.ROOT / "skills" / "semantic-crosscheck" / "references" / "pitfalls.md"

SAMPLES = [
    "",
    "0",
    "10",
    "2",
    "A",
    "B",
    "a",
    "b",
    "_",
    "-",
    "É",
    "é",
    "e\u0301",
    "가",
    "나",
    "다",
]


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


class BuiltinCollationDocTest(unittest.TestCase):
    def test_docs_recommend_postgresql_17_builtin_c_utf8(self) -> None:
        data_rules = _flat(DATA_RULES_DOC.read_text(encoding="utf-8"))
        pitfalls = _flat(PITFALLS_DOC.read_text(encoding="utf-8"))
        for doc_name, text in (("docs/data-rules.md", data_rules), ("references/pitfalls.md", pitfalls)):
            with self.subTest(document=doc_name):
                self.assertIn("provider = builtin", text)
                self.assertIn("C.UTF-8", text)


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run PostgreSQL integration tests")
class BuiltinCollationDatabaseTest(unittest.TestCase):
    def test_builtin_c_utf8_matches_python_code_point_order_and_unicode_upper(self) -> None:
        conn = pgfixture._admin()
        try:
            conn.run("BEGIN")
            try:
                conn.run(
                    "CREATE COLLATION pg_temp.sxc_builtin_c_utf8 "
                    "(provider = builtin, locale = 'C.UTF-8')"
                )
                meta = conn.run(
                    "SELECT collprovider, collisdeterministic, colllocale "
                    "FROM pg_collation "
                    "WHERE collname = 'sxc_builtin_c_utf8' "
                    "AND collnamespace = pg_my_temp_schema()"
                )
                self.assertEqual(meta, [["b", True, "C.UTF-8"]])

                rows = conn.run(
                    "SELECT v FROM unnest(:vals::text[]) AS t(v) "
                    "ORDER BY v COLLATE pg_temp.sxc_builtin_c_utf8",
                    vals=SAMPLES,
                )
                pg_sorted = [row[0] for row in rows]
                self.assertEqual(pg_sorted, sorted(SAMPLES))

                upper_builtin = conn.run(
                    "SELECT upper(:ch COLLATE pg_temp.sxc_builtin_c_utf8)",
                    ch="é",
                )[0][0]
                upper_c = conn.run(
                    'SELECT upper(:ch COLLATE "C")',
                    ch="é",
                )[0][0]
                self.assertEqual(upper_builtin, "É")
                self.assertEqual(upper_c, "é")
            finally:
                conn.run("ROLLBACK")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

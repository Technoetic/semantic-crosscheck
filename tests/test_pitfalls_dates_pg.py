"""The pitfall "Do not compare a `timestamp` column with a `timestamptz` value" against a PostgreSQL server.

Why: references/pitfalls.md tells gold SQL authors that comparing a `timestamp` column with a `timestamptz` value
gives an answer that depends on the plan across a daylight-saving change, and quotes the counts of one example. The
test builds that example in a temporary table and counts it under a sequential scan and under an index scan, with
`TimeZone` set to America/New_York and to UTC: the mixed-type condition must give the quoted counts (36 and 30, and
73 in UTC) and miss the rows the pitfall names, and the same-type forms the pitfall recommends must give one answer
under both plan settings in both zones. The two forms that can use the index on `ts` are checked to use it under the
index settings; the form that casts the column to `date` cannot, so it runs as a sequential scan under both. If a
server release stops showing the difference, this test fails and the pitfall text has to change with it. The text
check needs no database; the counts run when SXC_TEST_ADMIN_DSN is set and use only a temporary table of the admin
session.
"""

import re
import unittest

import pgfixture

PITFALLS = pgfixture.ROOT / "skills" / "semantic-crosscheck" / "references" / "pitfalls.md"
NEW_YORK = "America/New_York"
# 73 rows five minutes apart from 00:00 to 06:00 local time on 2026-03-08, the day New York skips 02:00 to 03:00.
ROWS = 73
MIXED = "ts < timestamptz '2026-03-08 07:30:00+00'"
# form: (condition, expected ids, whether the index on ts can serve it)
SAME_TYPE = {
    "timestamp value": ("ts < CAST('2026-03-08 02:30:00' AS timestamp)", list(range(30)), True),
    "date window": ("ts::date BETWEEN CAST('2026-03-08' AS date) AND CAST('2026-03-08' AS date)", list(range(ROWS)),
                    False),
    "half-open date window": ("ts >= CAST('2026-03-08' AS date) AND ts < CAST('2026-03-08' AS date) + 1",
                              list(range(ROWS)), True),
}
SEQUENTIAL = ("SET LOCAL enable_seqscan = on", "SET LOCAL enable_indexscan = off",
              "SET LOCAL enable_indexonlyscan = off", "SET LOCAL enable_bitmapscan = off")
INDEX = ("SET LOCAL enable_seqscan = off", "SET LOCAL enable_indexscan = on",
         "SET LOCAL enable_indexonlyscan = on", "SET LOCAL enable_bitmapscan = on")
PLANS = {"sequential": SEQUENTIAL, "index": INDEX}


def flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


class PitfallTextTest(unittest.TestCase):
    def test_the_pitfall_quotes_the_example_this_file_runs(self):
        text = flat(PITFALLS.read_text(encoding="utf-8"))
        self.assertIn("**Do not compare a `timestamp` column with a `timestamptz` value.**", text)
        for words in (f"`TimeZone` set to `{NEW_YORK}`", f"{ROWS} rows five minutes apart from 00:00 to 06:00",
                      f"`{MIXED}`", "36 rows by a sequential scan and 30 by an index scan",
                      "the six rows from 03:00 to 03:25 were missed", f"{ROWS} by both with `TimeZone` set to UTC",
                      "(`tests/test_pitfalls_dates_pg.py`)", "The harness does not set `TimeZone` for the gold SQL"):
            with self.subTest(words=words):
                self.assertIn(words, text)


@unittest.skipUnless(pgfixture.available(), "set SXC_TEST_ADMIN_DSN to run PostgreSQL integration tests")
class DaylightSavingComparisonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = pgfixture._admin()
        cls.conn.run("CREATE TEMP TABLE sxc_dst (id int PRIMARY KEY, ts timestamp NOT NULL)")
        cls.conn.run("INSERT INTO sxc_dst SELECT i, timestamp '2026-03-08 00:00' + make_interval(mins => i * 5) "
                     f"FROM generate_series(0, {ROWS - 1}) AS i")
        cls.conn.run("CREATE INDEX sxc_dst_ts ON sxc_dst (ts)")
        cls.conn.run("ANALYZE sxc_dst")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.conn.run("DROP TABLE IF EXISTS sxc_dst")
        finally:
            cls.conn.close()

    def select(self, zone: str, plan: str, condition: str) -> tuple[list[int], str]:
        """(sorted ids of the rows the condition selects, the plan text), in one transaction that sets the zone and
        the plan switches with SET LOCAL and is rolled back."""
        query = f"SELECT id FROM sxc_dst WHERE {condition}"
        self.conn.run("BEGIN")
        try:
            self.conn.run(f"SET LOCAL TimeZone = '{zone}'")
            for setting in PLANS[plan]:
                self.conn.run(setting)
            plan_text = "\n".join(row[0] for row in self.conn.run("EXPLAIN (COSTS OFF) " + query))
            ids = sorted(row[0] for row in self.conn.run(query))
        finally:
            self.conn.run("ROLLBACK")
        return ids, plan_text

    def test_mixed_types_count_differently_under_the_two_plans_in_new_york(self):
        sequential, sequential_plan = self.select(NEW_YORK, "sequential", MIXED)
        index, index_plan = self.select(NEW_YORK, "index", MIXED)
        self.assertIn("Seq Scan", sequential_plan)
        self.assertNotIn("Index", sequential_plan)
        self.assertIn("Index", index_plan)
        self.assertEqual((len(sequential), len(index)), (36, 30))
        # 00:00 to 02:25 convert to instants before 07:30 UTC; 02:30 does not, and the index scan stops there. 03:00 to
        # 03:25 (ids 36 to 41) convert to 07:00 to 07:25 UTC, so only the sequential scan counts them.
        self.assertEqual(index, list(range(30)))
        self.assertEqual(sequential, list(range(30)) + list(range(36, 42)))

    def test_mixed_types_agree_in_utc(self):
        for plan in PLANS:
            with self.subTest(plan=plan):
                ids, _ = self.select("UTC", plan, MIXED)
                self.assertEqual(ids, list(range(ROWS)))

    def test_same_type_forms_give_one_answer_under_both_plans_and_zones(self):
        for form, (condition, expected, indexable) in SAME_TYPE.items():
            for zone in (NEW_YORK, "UTC"):
                for plan in PLANS:
                    with self.subTest(form=form, zone=zone, plan=plan):
                        ids, plan_text = self.select(zone, plan, condition)
                        self.assertEqual(ids, expected)
                        if plan == "sequential":
                            self.assertNotIn("Index", plan_text)
                        elif indexable:
                            self.assertIn("Index", plan_text)


if __name__ == "__main__":
    unittest.main()

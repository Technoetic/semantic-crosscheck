# Bounded comparison of observed PostgreSQL plans

`plan-compare` is an optional differential query plan (DQP) experiment for a
small, declarative synthetic SELECT. It compares the complete typed result
bags of the same query and bindings under observed sequential and index plans.
A pass means those two observed plans returned the same answer for that
fixture. It does not establish which answer is correct, general query
correctness, SQL equivalence, or operational V1-V5 coverage.

```console
python skills/semantic-crosscheck/scripts/sxc.py plan-compare --spec examples/plan-compare/integer-index.json --out /tmp/sxc-plan-compare
```

Set `SXC_TEST_ADMIN_DSN` to an explicitly chosen, dedicated PostgreSQL test
environment. `--dsn-env NAME` selects another environment variable; it never
falls back to a company or operational configuration. The stage accepts no
`crosscheck.toml`. The optional `--statement-timeout-ms` defaults to 1000 ms,
and `--quiet` prints only the summary line. DSNs come from environment
variables only. Use a test account that can create temporary tables.

## Closed version 1 spec

```json
{
  "schema_version": 1,
  "columns": {"id": "integer", "x": "integer"},
  "rows": [[0, 0], [1, 1], [2, 2]],
  "projection": ["id", "x"],
  "predicate": [
    {"column": "x", "op": "lt", "value": {"type": "integer", "value": 2}}
  ],
  "index": "x",
  "timezone": "UTC",
  "max_rows": 2000,
  "operation_budget": 100000
}
```

All fields above except `max_rows` and `operation_budget` are required.
Unknown fields, duplicate JSON keys, invalid UTF-8, nonfinite numbers, and
files larger than 256 KiB are rejected. Identifiers are plain PostgreSQL
identifiers; the implementation quotes them and does not interpret them as
SQL. No free SQL, expressions, relation names, functions, joins, aggregates,
subqueries, ordering, DML, or DDL are accepted.

- `columns` contains 1..16 unique identifiers with types `integer`, `numeric`,
  `text`, `boolean`, `timestamp`, or `timestamptz`. Identifiers match ASCII
  `[A-Za-z_][A-Za-z0-9_]{0,62}` and are always quoted.
- `rows` contains at most 2000 rows. Each row is an array in `columns` order or
  an object with exactly the declared column keys. Cells must match their
  declared types; a cell may be JSON `null` for any type.
- Integer values are signed 64-bit JSON integers (stored as PostgreSQL
  `bigint`), and booleans are JSON booleans. Numeric
  values are finite, exact JSON numbers or decimal strings. They are not
  converted through binary floating point. Numeric values have at most 128
  coefficient digits and an absolute decimal exponent of at most 512;
  numeric text tokens, whether JSON numbers or strings, contain at most 256
  characters before conversion.
- `timestamp` values are ISO timestamps without an offset. `timestamptz`
  values have an explicit ISO offset. Timestamps require seconds and allow
  at most six fractional digits. Text contains at most 4096 characters and
  cannot contain NUL.
- `projection` is a nonempty list of distinct declared columns.
- `predicate` is a flat AND of 1..16 comparisons. Each comparison names
  one declared column and an `op` of `eq`, `ne`, `lt`, `le`, `gt`, or `ge`, plus
  a nonnull typed `value` object as above. `is_null` and `is_not_null` omit
  `value`. Nested logic and arbitrary expressions are unsupported.
- Comparison value types match the column type, with two deliberate
  exceptions: integer/numeric comparisons and timestamp/timestamptz
  comparisons. The latter preserves the mixed-type DST experiment below.
- `index` names one declared column. `timezone` is a bounded string supplied
  as a bound value; PostgreSQL validates the zone. Its syntax is
  `[A-Za-z0-9_+./:-]{1,128}`.
- `max_rows` defaults to 2000 and must be in 1..2000. Each query fetches at
  most the cap plus one; seeing that extra row produces `not_run`, never a
  comparison of truncated prefixes.
- `operation_budget` defaults to 100000 and cannot exceed 100000. A
  conservative host budget includes fixture cells, predicate evaluation and
  projected comparison work: `rows * (columns + 2 * predicates + 2 * projection) + 64`.
  A fixture exceeding the budget is `not_run`. This bounds the admitted
  experiment; it does not estimate PostgreSQL execution cost. The statement
  timeout must be in 1..120000 ms.

## Execution and evidence

The stage creates a fixed, session-owned TEMP table and index, binds every
cell value, and analyzes that fixture. Setup writes only temporary objects.
It then starts one READ ONLY, REPEATABLE READ transaction and runs an
ordinary SELECT against that table. This is not a materialized CTE
comparison: PostgreSQL can use the temporary table's real index.

Two fixed profiles set planner flags with `SET LOCAL`: one encourages a
sequential scan and disables index access, and the other encourages index
access and discourages a sequential scan. Both execute identical typed SQL
and bindings over stable data. Changing flags alone is insufficient:
`EXPLAIN (FORMAT JSON)` must show actual physical plan diversity. Plan
structure hashes omit costs, estimates and literal expressions. An unchanged
plan is `not_run` even if the results agree.

Comparison uses full typed row bags, including NULLs and duplicate
multiplicities. Counts alone cannot establish equality. A mismatch is
`fail`; neither arm is identified as ground truth. Two empty answers are
`not_run`; a one-sided empty answer is a discrepancy. Empty fixtures,
an unavailable engine, insufficient budget, row-cap overflow,
unobserved plan diversity, query errors, and timeouts never produce a
vacuous pass. Errors are sanitized. Transactions are rolled back, planner
settings remain local to their transaction, and the connection is closed
on every exit path.

The result stage is `plan_compare`. Persisted evidence contains hashes,
counts and fixed profile flags only. It excludes SQL text, full plans,
fixture values, result rows and DSNs. The original spec is not rewritten,
and its byte hash is rechecked before accepting execution evidence. This
diagnostic stage is excluded from the default operational report.

## Included experiments

`integer-index.json` is a positive integer case: the predicate selects five
of twenty rows and both observed plans should return the same bag.

`dst-mixed.json` has 73 timestamp rows, five minutes apart from 00:00 to
06:00 on 2026-03-08, with `TimeZone` set to `America/New_York`. It compares
the timestamp column with the explicitly typed timestamptz value
`2026-03-08T07:30:00+00:00`. PostgreSQL releases still affected by the known
mixed-type DST behavior return 36 rows under a sequential scan and 30
under an index scan. That observed mismatch is a useful diagnostic failure.
A fixed PostgreSQL release may return equal bags; the experiment must not
require a release to preserve the old bug. Integration tests check actual
plan diversity and condition the historic discrepancy assertion on the
server's observed behavior.

`dst-utc.json` uses the same mixed comparison in UTC, where all 73 rows
match under both plans. `dst-same-type.json` uses a timestamp value in New
York, where both plans select the same 30 rows. These controls distinguish
the mixed-type DST case from general fixture or comparison failure.

This command admits only the query class described above. It is not a
generic SQL fuzzer or unrestricted DQP implementation, and it does not
change the existing TLP or counterexample commands.

## Reduction boundary

Version 0.28 does not reduce a plan mismatch automatically, and makes no
minimality claim. The submitted fixture remains the witness. Any future
reducer must recheck both the result discrepancy and actual physical plan
diversity after each accepted change; preserving only a row-count gap or
planner flags is insufficient.

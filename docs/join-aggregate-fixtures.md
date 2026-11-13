# Authored join and aggregate fixtures

`examples/join-aggregate-fixtures/cases.json` contains thirteen small synthetic
cases with independently authored inputs, literal expected bags and PostgreSQL
queries. The literals were written before the reference arithmetic and database
execution. They are specifications, rather than captured database output or
expectations calculated by the implementation. The existing one-column
provenance corpus remains separate.

The Python standard-library module `semantic_crosscheck.join_aggregate_fixtures`
implements only two fixed operations:

| Operation | Input | Output |
| --- | --- | --- |
| `inner_join(left, right)` | Two `(key,payload)` tables | `(key,left_payload,right_payload)` for every matching pair |
| `aggregate(rows, grouped=True)` | `(group,integer-or-NULL)` rows | `(group,count_rows,count_nonnull,sum)` per group |
| `aggregate(rows, grouped=False)` | Same rows | One `(count_rows,count_nonnull,sum)` row |

Inputs must be finite lists or tuples of two-column lists or tuples. A join
accepts at most 64 rows on each side, so its product has at most 4096 rows. An
aggregate accepts at most 4096 rows, including such a product after explicit
projection to `(group,value)`. Integers are bounded to -1,000,000 through
1,000,000 and text to 256 Python characters per input cell. Each column has one
non-NULL type, and the two join-key columns must have compatible types. Boolean,
float, Decimal, objects, mixed column types and unbounded iterators are rejected.
Aggregate values must be integers or NULL. Counts and sums are exact integers;
the sum bound follows from the row and value bounds. Inputs are never changed.

A NULL join key never matches, including NULL on the other side. Duplicates
multiply: two identical left A rows and two identical right A/x rows produce
four identical output rows. The third A/y right row adds two A/y output rows.
Grouping instead places all NULL group keys in one group. `COUNT(*)` counts
every row; `COUNT(value)` counts non-NULL values; `SUM(value)` ignores NULLs and
returns NULL when there are no non-NULL values. Zero and negative integers
participate normally. Output row ordering is irrelevant and tests compare bags.

The authored cases cover duplicates on both sides, NULL and integer join keys,
empty text, NULL payloads, an empty input on either side, mixed/NULL groups,
all-NULL groups versus a zero-valued group, negative sums, global mixed and
all-NULL inputs, empty grouped input, empty global input, join-then-grouped
aggregation and an unmatched join followed by a global aggregate. Empty grouped
input yields no rows; empty global input yields exactly `(0,0,NULL)`.

Six additional literal witnesses distinguish wrong operations. Deduplicating
the left, right or both join inputs loses different multiplicities. Substituting
`COUNT(value)` for `COUNT(*)`, or the reverse, changes the NULL-bearing groups.
Replacing SUM with MAX changes both the positive mixed group and the group
containing zero and a negative value. Each witness includes an explicit wrong
PostgreSQL query and its independently authored expected result. Wrong Python
variants live only in tests.

Run the offline cases and input-boundary tests with:

```text
python -m unittest discover -s tests -p "test_join_aggregate_fixtures.py" -v
```

The companion integration test executes every correct query and all six wrong
query pairs through `tests/pgfixture.py`. It creates and drops a process-specific
disposable database, checks that the actual server is PostgreSQL 17, and uses
read-only transactions for the synthetic VALUES queries. The actual PostgreSQL
answers and the offline arithmetic each compare against the literal bags.
Use `SXC_TEST_ADMIN_DSN` only for an existing dedicated throw-away PG17 server
and `SXC_TEST_ROLE_PASSWORD` when required by that server:

```text
python -m unittest discover -s tests -p "test_join_aggregate_fixtures*.py" -v
```

Without the admin DSN the two PG tests explicitly skip. A skip supplies no
PostgreSQL evidence; provisioned integration CI requires them to execute. These
tests do not connect to an MES or company database.

This is a narrow characterization corpus, with no new runtime dependency,
CLI stage or operational Gold registration. General SQL evaluation, outer joins,
expressions, float/decimal arithmetic, coercions, locale-sensitive or
nondeterministic collations, ordering and production data are outside scope.
Passing the cases does not prove SQL equivalence, establish PostgreSQL/Cube
equivalence, or transfer a formal theorem to an execution engine. Operational
report authority and coverage do not change.

## Conceptual references

[SPES: A Symbolic Approach to Proving Query Equivalence Under Bag Semantics, arXiv v2](https://arxiv.org/abs/2004.00481v2)
provides background on preserving duplicate multiplicities when reasoning about query results. The
[public SPES repository pinned at 8049f98d8e64a5c2c43d1ed581e7d5b6e0a006e2](https://github.com/georgia-tech-db/spes/tree/8049f98d8e64a5c2c43d1ed581e7d5b6e0a006e2)
is an Apache-2.0 design reference only. The literals, SQL and Python arithmetic here are original; no upstream
code, proof engine or paper examples are incorporated. See [THIRD_PARTY.md](../THIRD_PARTY.md).

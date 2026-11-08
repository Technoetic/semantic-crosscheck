# Authored bags and provenance proof scope

`examples/provenance-fixtures/cases.json` contains eleven small synthetic cases.
Inputs, PostgreSQL queries and expected multiplicities were independently authored
for this project. Expected bags are literal specifications, not captured database
answers or values calculated by the reference implementation. The corpus adds no
CLI stage, default runtime dependency or production-database workflow.

The inspiration is the proof-scope boundary discussed in
[the provenance formalization paper, pinned to arXiv v3](https://arxiv.org/abs/2504.12058v3)
and its
[QueryAdequacy.lean at commit 92af5cf2a057dfb46aab0a364d4ade5fde9c5699, lines 17–46](https://github.com/PierreSenellart/provenance-lean/blob/92af5cf2a057dfb46aab0a364d4ade5fde9c5699/Provenance/QueryAdequacy.lean#L17-L46).
ProvSQL is prior background, not a newly integrated engine. Neither Lean nor
ProvSQL is installed or invoked. The upstream semantics use noncomputable
definitions; the small Python arithmetic here is our own synthetic implementation,
not extracted upstream code or an execution of its formal proofs.

## Literal specification

Each row has one text column. `NULL` below means SQL NULL; `A`, `B` and `C` stand
for the synthetic identifiers `P-A`, `P-B` and `P-C`. `A:2` means two copies of A;
ordering has no meaning. An empty bag has no rows, rather than one NULL row.

| Case | Input and operation | Expected bag |
| --- | --- | --- |
| duplicate_projection | Project `[A,A,B]` | A:2, B:1 |
| distinct_projection | DISTINCT `[A,A,B]` | A:1, B:1 |
| except_presence | `[A,A,B,B,C,C]` EXCEPT `[A,B]` | C:1 |
| except_all_multiplicity | Same inputs, EXCEPT ALL | A:1, B:1, C:2 |
| except_all_saturates | `[A,A]` EXCEPT ALL `[A,A,A,B]` | empty |
| null_distinct | DISTINCT `[NULL,NULL,A]` | NULL:1, A:1 |
| null_except_presence | `[NULL,NULL,A,A,B]` EXCEPT `[NULL,A]` | B:1 |
| null_except_all | Same inputs, EXCEPT ALL | NULL:1, A:1, B:1 |
| equality_null_is_unknown | `[NULL,NULL,A]` WHERE value = NULL | empty |
| is_null_keeps_duplicates | Same input WHERE value IS NULL | NULL:2 |
| text_equality_keeps_duplicates | `[NULL,A,A,B]` WHERE value = A | A:2 |

EXCEPT removes every matching value and emits each survivor once. EXCEPT ALL
instead subtracts multiplicities: `max(left_count - right_count, 0)`. The C:2
survivor distinguishes these operations even for a value absent on the right.
DISTINCT and set-operation duplicate matching group NULLs together. In an equality
predicate, `NULL = NULL` is UNKNOWN, so WHERE discards it; IS NULL is TRUE for
NULL rows and preserves duplicate rows. These are different uses of equality.
The reference also keeps SQL NULL, the text `"NULL"` and empty text distinct.

## The upstream counterexample is a scope caution

The pinned
[Nat.counterexample_diff_adequacy, lines 411–452](https://github.com/PierreSenellart/provenance-lean/blob/92af5cf2a057dfb46aab0a364d4ade5fde9c5699/Provenance/QueryAdequacy.lean#L411-L452)
uses `(Dedup R1) Diff R2`, `R1 = {t,t}`, `R2 = {t}`, with every base annotation 1.
Dedup accumulates annotation 2 in one slot. Annotated difference applies natural
number monus: `2 - 1 = 1`, still nonzero. Plain difference removes t because it
is present on the right. Thus nonzero annotated support need not equal the plain
result once this combination is allowed. The cited positive, Diff-free adequacy
claim must not be extended across that boundary.

The offline helper `dedup_diff_annotations` models only this expression shape,
non-NULL text values, base annotations 1, deduplication by annotation addition and
truncated subtraction. Its dictionary values are annotation weights, **not output
row counts**. It retains zero-weight left slots explicitly. It does not interpret
nested expressions or implement a general provenance algebra.

The PostgreSQL bridge checks the corresponding ordinary queries directly:

```sql
WITH r1(part_id) AS (VALUES ('P-A'::text), ('P-A')),
     r2(part_id) AS (VALUES ('P-A'::text))
SELECT DISTINCT part_id FROM r1
EXCEPT ALL
SELECT part_id FROM r2;
```

This returns no rows: DISTINCT first leaves one P-A and EXCEPT ALL removes one.
Replacing EXCEPT ALL with EXCEPT also returns no rows. Removing DISTINCT makes
EXCEPT ALL return one P-A, matching ordinary bag subtraction. The annotation
calculation's nonzero weight is not a PostgreSQL result or a PostgreSQL bug.
Upstream's no-NULL counterexample does not establish SQL NULL semantics; those are
separate authored characterization cases checked against PostgreSQL.

## Run and limits

Offline reference tests require only Python and the existing test import helper:

```text
python -m unittest discover -s tests -p "test_provenance_fixtures.py" -v
```

The real PostgreSQL companion follows the project's `tests/pgfixture.py`
convention. Set `SXC_TEST_ADMIN_DSN` only for a dedicated throw-away PostgreSQL 17
test server; set `SXC_TEST_ROLE_PASSWORD` if its authentication requires it. The
fixture creates and drops a process-specific database and uses the standard test
roles. Each query contains its synthetic inputs in VALUES CTEs and runs read-only:

```text
python -m unittest discover -s tests -p "test_provenance_fixtures*.py" -v
```

Without the admin DSN, the two database tests are explicitly skipped. That is no
evidence that the PostgreSQL bridge passed. No production DSN is accepted by an
additional command, and no external service is contacted by these tests.

The offline reference and real PostgreSQL each compare to the same independent
literal bags. These cases detect duplicate loss, DISTINCT/EXCEPT ALL confusion,
negative-count mistakes and conflation of NULL duplicate matching with NULL
equality. Coverage is limited to one-column synthetic text, the listed operations
and the one annotated expression shape. Collations, other types, joins, aggregates,
ordering and arbitrary SQL are outside scope. Passing this corpus neither proves
SQL equivalence nor transfers a Lean theorem to PostgreSQL, Cube or production
data. It does not add proof status or report coverage to operational checks.

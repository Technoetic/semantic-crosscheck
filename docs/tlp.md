# Bounded SQL ternary-logic partition checks

`tlp` checks PostgreSQL's internal consistency for a trusted relational SELECT,
a boolean predicate, and an exact COUNT or SUM on bounded synthetic fixtures.
It partitions every row into `p`, `NOT p`, and `p IS NULL`, combines the
partitions with `UNION ALL`, and compares the full row multiset and aggregate.
A pass applies to the tested partition identity. It does not prove that the
query implements a business rule, establish SQL equivalence, or promote V1-V5
operational coverage.

```console
python skills/semantic-crosscheck/scripts/sxc.py tlp --spec examples/tlp/sum-null.json --out /tmp/sxc-tlp
```

Set `SXC_TLP_DSN` to a dedicated synthetic PostgreSQL 17+ connection. The DSN is
read from the environment only. `--dsn-env NAME` selects another environment
variable. Defaults are `--seed 7`, `--max-examples 64`, and
`--statement-timeout-ms 10000`. Bounds are 1..1000 examples and 1..120000 ms.
The stage runs without an operational configuration.

## Optional NoREC oracle (since 0.10.0)

`--oracle tlp` is the default and preserves the partition checks below.
`--oracle norec` requires the aggregate to be exactly `{"function":"count","column":"*"}`.
Other aggregates are rejected before connecting. For example:

```console
python skills/semantic-crosscheck/scripts/sxc.py tlp --oracle norec --spec examples/tlp/norec-count.json --out /tmp/sxc-norec
```

NoREC compares `COUNT(*) WHERE predicate` with
`COALESCE(SUM(CASE WHEN predicate THEN 1 ELSE 0 END), 0)`. Only the bounded
relation is materialized; each oracle arm evaluates the predicate independently.
TRUE contributes one, FALSE/NULL zero, and an empty relation counts zero.
A third diagnostic evaluation retains the bounded row/predicate bag in memory;
the complete statement runs twice within READ ONLY, REPEATABLE READ, and changed
diagnostic rows or counts are errors. Known volatile SQL, row/response limits,
timeouts and changed input files use the existing fail-closed controls.

Results still live under `tlp`, but `inputs.oracle`, check details and
`norec:consistency`, `norec:engine`, `norec:execution` identify the selected oracle.
Normal output includes only counts, not diagnostic rows. A replay uses format
`sxc-synthetic-norec` and `oracle: norec`; selecting the wrong oracle is rejected.
Existing `sxc-synthetic-tlp` replay artifacts remain compatible with default TLP.

This follows the count-oracle idea of Rigger and Su's
[NoREC (ESEC/FSE 2020)](https://arxiv.org/abs/2007.08292). Materializing the
relation limits optimizer and index coverage compared with unrestricted NoREC.
Agreement is bounded predicate-count consistency, not business correctness,
equal full result sets, or operational V1-V5 coverage.

## Version 1 spec

```json
{
  "schema_version": 1,
  "scenario": "nulls",
  "relation_sql": "SELECT qty FROM stock",
  "predicate_sql": "qty > 0",
  "aggregate": {"function": "sum", "column": "qty"}
}
```

All five top-level fields and both aggregate fields are required; extra fields,
duplicate JSON keys, nonfinite JSON numbers, invalid UTF-8 and files larger than
64 KiB are rejected. The scenario is one of the existing deterministic fixture
families: `nulls`, `duplicate_join`, `bom_diamond`, or `date_boundary`. Fixtures
define the existing `part`, `bom_line`, `sales_order`, and `stock` CTEs.

`relation_sql` starts with SELECT and may contain joins, nested SELECTs, WHERE,
UNION ALL, and projections. WITH/recursive queries, DML/DDL, SELECT INTO, locking,
comments, dollar quoting, backslash escapes and statement separators outside
quoted strings are outside this deliberately conservative syntax. There is no
trailing semicolon. `predicate_sql` is a boolean expression over the relation's
output columns; predicate subqueries are unsupported. Output aliases must be
unique, and `__sxc_tlp_p` is reserved for the frozen predicate result.

The aggregate function is exactly `count` or `sum`. A column is a plain lowercase
PostgreSQL identifier of at most 63 characters. `count` supports `*` and nullable
columns: COUNT(*) counts every row while COUNT(column) skips NULL values. `sum`
requires an exact integer/numeric column. Floating point SUM and nonfinite
aggregate values are rejected. Decimal parsing and comparison preserve all
digits. Partial SUMs are recombined with SQL SUM; no empty or all-NULL SUM is
silently changed to zero.

## Execution and trust boundary

For the default TLP oracle, the two bags and SQL aggregates are evaluated within one statement. MATERIALIZED
CTEs freeze the relation and the predicate once per row before partitioning.
The statement runs twice in a single READ ONLY, REPEATABLE READ transaction;
different baseline bags, predicate outcomes or aggregates produce an error.
Known volatile primitives such as random(), clock_timestamp() and sequence
updates are unsupported. The relation is bounded at 10001 rows; crossing the
10000-row comparison limit produces an error rather than checking a prefix.
Statement timeouts, malformed engine responses and unsupported types also fail
closed. An unavailable PostgreSQL engine is `not_run`.

This is trusted SQL with the same boundary as trusted counterexample SQL. It is
not a SQL sandbox: qualified relations and custom functions may read beyond the
synthetic CTEs. Read-only transactions block database writes but do not establish
that functions are immutable or free of external effects. Rechecking observable
stability does not prove determinism. Collations, integrity constraints and
production PostgreSQL types are not exhaustively represented by these fixture
families. Use only reviewed SQL and a dedicated synthetic connection.

## Synthetic replay artifact

A completed check saves the final checked case in `<out>/tlp/fixture.json`,
explicitly labelled `synthetic: true`, including generated input rows. No query
answer rows or SQL text are saved. Normal Result/console output carries only
counts, identifiers, booleans, hashes and fixed disclosures. The fixture binds
the exact raw spec SHA-256, schema version, seed, scenario, case index, generator
version, generator implementation SHA-256, generated tables and tables SHA-256.
Replay regenerates the case and requires an exact typed match; it cannot import
arbitrary row data. Input files changed during execution cannot retain a pass.

```console
python skills/semantic-crosscheck/scripts/sxc.py tlp --spec examples/tlp/sum-null.json --seed 7 --replay /tmp/sxc-tlp/tlp/fixture.json --out /tmp/sxc-tlp-replay
```

Replay runs one recorded case, not all examples from the earlier run. Generator
implementation changes intentionally invalidate old artifacts. Artifacts are
bounded at 1 MiB and are local evidence; they are not part of an operational
report or a business-query correctness certificate.

# Independent raw-row measure oracle

`measure-oracle` compares one explicitly authored global Cube measure query with
an exact Python SUM or COUNT over raw source rows. Its predicate is supplied in
a separate JSON file; it is never copied from the configured measure, generated
SQL, or Cube model. A shared wrong configured filter can therefore produce a
disagreement even when the generated SQL and Cube agree with each other.

```console
python skills/semantic-crosscheck/scripts/sxc.py cube --config examples/measure-oracle/crosscheck.toml --out .sxc-measure
python skills/semantic-crosscheck/scripts/sxc.py measure-oracle --config examples/measure-oracle/crosscheck.toml --spec examples/measure-oracle/sum.json --out .sxc-measure
```

The synthetic source must first be loaded separately from
`examples/measure-oracle/fixture.sql`, and the normal Cube adapter must be
installed. The command requires a configuration and a current successful `cube`
receipt/model in the same output directory. It writes only
`<out>/measure_oracle/result.json`. It is optional, adds no report authority,
and does not alter existing stage or default report behavior.

## Closed version-1 spec

```json
{
  "schema_version": 1,
  "entity": "orders",
  "column": "qty",
  "aggregation": "sum",
  "where": [
    {"column": "part_id", "operator": "is_not_null"},
    {"column": "status", "operator": "eq", "type": "text", "value": "FIRM"}
  ],
  "cube_query": {"measures": ["m_total.value"]},
  "max_rows": 10000
}
```

All keys shown except `max_rows` are required; unknown keys are rejected at
every level. Files are nonempty UTF-8 JSON, at most 65,536 bytes, with duplicate
keys, nonfinite constants and fractional JSON numbers rejected. The version and
row limit must be actual integers, not booleans. `max_rows` defaults to 10,000
and accepts 1–50,000. `entity` names a configured entity; `column` names a raw
field. Identifiers follow the existing lowercase ASCII identifier policy.

`where` is an AND array of zero to 32 predicates. Empty `where` selects every
visible row. Supported predicates are:

| Operator | Exact required fields | Meaning |
| --- | --- | --- |
| `eq`, `ne` | `column`, `operator`, `type`, `value` | Typed equality or inequality |
| `in` | `column`, `operator`, `type`, `values` | Typed membership in 1–256 literals |
| `is_null`, `is_not_null` | `column`, `operator` | Explicit NULL test |

Types are `integer` (JSON integer), `numeric` (finite decimal text such as
`"0.0001"`), `text` (JSON string) and `boolean` (JSON boolean). NULL literals are
not accepted; use a NULL operator. Numeric predicates can select integer or
numeric columns; other declared types must match the source catalog type.
Text and numeric literals are bounded to 4,096 UTF-8 bytes/characters,
respectively. Text remains exact: there is no trimming, normalization, case
folding, locale collation or numeric conversion. NULL never qualifies an `eq`,
`ne` or `in` predicate. A numeric-looking text such as `"01"` remains text.

`aggregation` is `sum` or `count`. SUM accepts integer/numeric source fields,
ignores NULLs, and returns NULL for empty/all-NULL selections. COUNT counts
non-NULL values of the declared column, including for text/boolean fields; an
empty selection returns zero. COUNT(*) is not supported.

The Cube query is authored separately and is passed to the existing Cube
client. It contains exactly one `measures` member and optional flat `filters`.
Filters accept `member`, `operator` and, for `equals`/`notEquals`, a nonempty
`values` array of at most 256 strings. `set`/`notSet` filters omit `values`.
There are at most 32 filters. All members must belong to the queried measure's
cube. Dimensions, cross-cube members, joins, nested Boolean filters, segments,
time dimensions, order, offset and caller limits are rejected. Referenced
members must be public in the saved model. This version intentionally has no
grouped, joined or dated measure oracle.

## Execution and bounds

The source entity table and selected raw fields are safely quoted. Only fixed
catalog/projection SQL is issued: no SQL supplied in the JSON, no generated
measure SQL and no configured measure predicate enters the calculation.
Catalog types and at most `max_rows + 1` raw rows are read in one READ ONLY,
REPEATABLE READ transaction. The statement timeout is the configured source
timeout capped at 30 seconds. The transaction is rolled back and the connection
closed before waiting for Cube.

The catalog allowlist is PostgreSQL's built-in `smallint`, `integer`, `bigint`,
`numeric`, `text`, `varchar` and `boolean`. Domains, floats, dates, arrays,
custom types and other unsupported catalog types produce `not_run`, including
for empty sources. Floats, nonfinite numbers or unexpected driver value types
also produce `not_run`. Raw values are evaluated in Python with the explicit
typed predicate. SUM uses the shared pure exact-numeric helper, bounded to
4,096 digits and 100,000 terms; it never rounds under the ambient Decimal
context. Exceeded row/numeric bounds produce `not_run` with no partial answer.
The row limit bounds cardinality; it is not a bound on source row byte width.

Cube must return exactly one global row containing exactly the requested
measure. Missing/extra members, missing/multiple rows, floats, booleans,
nonfinite or oversized numeric representations are errors. Exact integer,
Decimal and bounded decimal-text values compare numerically, with NULL
distinct from zero. An empty Cube row array is an error, even for empty raw
input; it cannot become a vacuous match.

The saved Cube receipt must be successful and match the current configuration,
gold/revision input fingerprints and engine. The spec, configuration, declared
gold/revision files, receipt, model manifest and model YAML files are captured
before execution and checked again after Cube returns. Captured non-spec files
are limited to 4 MiB each, and there may be at most 256 model YAML files.
Changing any captured file or model file membership prevents PASS.
Input/output aliases and observed links are rejected before
cleanup/publication; this is not a lock against hostile filesystem races.
Local hashes detect ordinary changes, not the authenticity of rewritten
receipts or models.

## Meaning and retained evidence

PASS means that one supplied Cube scalar matched the independent computation
over the entire visible source within the row bound. FAIL means a value
disagreement. Unsupported inputs or exhausted bounds yield `not_run`; invalid
specs, missing/stale prerequisites and execution/response errors yield `error`.
Every new attempt invalidates its prior receipt before execution, including
attempts that subsequently fail. Unsafe output aliases are rejected while
preserving the selected input.

The result retains source/qualifying/non-NULL/Cube row counts, the row bound,
fixed check/reason identifiers and input hashes. Raw values, aggregate answers,
filter constants, SQL, source identifiers, local paths, credentials and
exception messages are never serialized. Empty sources, no qualifying rows
and all-NULL selections are disclosed explicitly in `not_proven`.

Raw source and Cube use separate sessions and do not share an MVCC snapshot.
Operate within a stable-data window or an owner-maintained revision contract
that advances a never-reused revision atomically with every relevant change.
This command does not itself establish that contract. A match does not prove
arbitrary measures, business truth, hidden rows, joins, time semantics, or
unbounded equivalence.

Text/varchar inputs are admitted only when their catalog collation is explicitly deterministic (`collisdeterministic is True`). Missing, malformed or nondeterministic collation metadata is `not_run/unsafe_collation`, including COUNT over such a selected text field.

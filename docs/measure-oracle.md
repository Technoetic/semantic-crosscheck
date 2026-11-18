# Independent raw-row measure oracle

`measure-oracle` compares one explicitly authored Cube measure query with
an exact Python SUM or COUNT over raw source rows, globally or per item and
optionally within a UTC calendar window. Its predicate is supplied in
a separate JSON file; it is never copied from the configured measure, generated
SQL, or Cube model. A shared wrong configured filter can therefore produce a
disagreement even when the generated SQL and Cube agree with each other.
For a fact measure that should count each fact once when related rows exist,
see the [version-2 related-row contract](measure-oracle-joins.md).

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
cube and must be public in the saved model. Grouping and time filtering use the
closed forms below. The query excludes nested Boolean filters, cross-cube
members, segments, ordering, offsets and caller-supplied limits.

### Per-item totals and calendar windows

The optional `group_by` and `time_window` fields can be used together or
individually. This fragment of the [runnable January example](../examples/measure-oracle/item-window.json)
checks a separate SUM for each qualifying part.

```json
{
  "group_by": "part_id",
  "time_window": {"column": "ordered_on", "start": "2026-01-01", "end": "2026-02-01"},
  "cube_query": {
    "measures": ["m_dated_total.value"],
    "dimensions": ["m_dated_total.key"],
    "timeDimensions": [{"dimension": "m_dated_total.ts", "dateRange": ["2026-01-01", "2026-01-31"]}]
  }
}
```

- `group_by` names one raw integer, numeric, text/varchar or boolean
  column. Supply exactly one corresponding public Cube dimension in
  `cube_query.dimensions`. Its model type must match the source kind even for
  empty data. A qualifying NULL key forms one group. Specify `is_not_null`
  explicitly when the business measure excludes such rows.
- Each key's value and the complete set of keys must agree, independently of
  result order. Text keys retain case and leading zeros. Numeric keys compare
  exactly, and boolean keys accept JSON booleans or the lowercase wire
  encodings `true`, `false`, `t`, `f`. A duplicate Cube key, including one
  repeated with a different numeric representation, yields `error/duplicate_cube_group`.
- `time_window` uses two strict ISO calendar dates (`YYYY-MM-DD`) with
  `start < end`. The raw calculation includes the UTC start day and excludes
  the end day. It reads PostgreSQL `date` as a calendar day, interprets a
  timestamp without time zone as UTC, converts a timestamp with time zone to
  UTC, and excludes NULL times. Independently specify one Cube time dimension
  and two inclusive calendar dates, using Cube's default UTC timezone. The
  validator accepts different ranges so an incorrect endpoint can be detected
  when rows populate the difference between the two intervals. Sub-day
  windows, local-time calendars and time buckets fall
  outside this contract.
- A grouped comparison admits at most 10,000 keys per side. The Cube request
  asks for one additional row to detect truncation. Either side exceeding the
  bound yields `not_run/group_limit`. An empty grouped result can match only a
  valid empty Cube row list and discloses the lack of qualifying values.

## Execution and bounds

The source entity table and selected raw fields are safely quoted. Only fixed
catalog/projection SQL is issued: no SQL supplied in the JSON, no generated
measure SQL and no configured measure predicate enters the calculation.
Catalog types and at most `max_rows + 1` raw rows are read in one READ ONLY,
REPEATABLE READ transaction. The statement timeout is the configured source
timeout capped at 30 seconds. The transaction is rolled back and the connection
closed before waiting for Cube.

The measure, grouping and predicate allowlist is PostgreSQL's built-in
`smallint`, `integer`, `bigint`, `numeric`, `text`, `varchar` and `boolean`.
Only the window column admits the three temporal types above. Domains, floats, arrays,
custom types and other unsupported catalog types produce `not_run`, including
for empty sources. Floats, nonfinite numbers or unexpected driver value types
also produce `not_run`. Raw values are evaluated in Python with the explicit
typed predicate. SUM uses the shared pure exact-numeric helper, bounded to
4,096 digits and 100,000 terms; it never rounds under the ambient Decimal
context. Exceeded row/numeric bounds produce `not_run` with no partial answer.
The row limit bounds cardinality; it is not a bound on source row byte width.

For a global comparison Cube must return exactly one row containing exactly the requested
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

PASS means that the scalar or every key and value in the supplied grouped query
matched the independent computation over the entire bounded visible source.
FAIL means a missing group, an extra group or a different aggregate value.
Unsupported inputs or exhausted bounds yield `not_run`; invalid
specs, missing/stale prerequisites and execution/response errors yield `error`.
Every new attempt invalidates its prior receipt before execution, including
attempts that subsequently fail. Unsafe output aliases are rejected while
preserving the selected input.

The result retains source/qualifying/non-NULL/Cube row counts, the row bound,
fixed check/reason identifiers and input hashes. A grouped result also records
counts of source, missing, extra and value-mismatched groups. Raw values, aggregate answers,
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

# PostgreSQL SQL structure diagnostic

`sql-structure` is an opt-in, offline comparison of one SQL file with a separately
authored JSON structural expectation. It parses with the actual PostgreSQL 17
parser through libpg_query and the optional pglast binding. It does not infer an
expectation from business prose, a semantic-preflight contract, a plan, Gold SQL,
or a database. An author must explicitly write and review the expectation.

Install the optional dependency, then run from the repository root:

```sh
python -m pip install -r requirements/sql-structure.txt
python skills/semantic-crosscheck/scripts/sxc.py sql-structure \
  --sql examples/sql-structure/half-open.sql \
  --expect examples/sql-structure/half-open.expect.json --out .sxc
```

The matching fixture exits 0. Replacing `half-open.sql` with
`inclusive-between.sql` exits 1 and fails the fixed `predicates` check. SQL
`BETWEEN` is represented separately from `>=` and `<`; it cannot pass an
expectation for a half-open period. This establishes the written operators,
not which dates or timestamps belong in the business period.

The exact optional pin is `pglast==7.22`, using PostgreSQL 17.7 locally. Runtime
requires both that pglast version and PostgreSQL parser major 17. Missing,
unloadable or incompatible bindings return `not_run`, with CLI exit 3; there is
no fallback parser or automatic installation. The core dependency set remains
unchanged. [Upstream v7 documentation](https://pglast.readthedocs.io/en/v7/development.html#version-7)
identifies its PostgreSQL 17 target, and [PyPI 7.22 files](https://pypi.org/project/pglast/7.22/#files)
include Windows wheels for Python 3.11, 3.12 and 3.13, including win_amd64.
Wheel availability does not establish that every platform has been tested here.
The pglast binding is GPL-3.0-or-later, distinct from libpg_query's BSD license
and its PostgreSQL-licensed parser sources; it is externally installed and is
not bundled in this project. See [the binding source](https://github.com/lelit/pglast/tree/v7.22)
and [libpg_query's licenses](https://github.com/pganalyze/libpg_query#license).

## Expectation version 1

All five top-level fields are required; unknown fields are rejected:

```json
{
  "version": 1,
  "relation": {"schema": "sxc_demo", "name": "sales_order"},
  "select": [{"column": "part_id"}, {"aggregate": "sum", "column": "qty"}],
  "group_by": ["part_id"],
  "where": [
    {"column": "ordered_at", "op": ">=", "value": {"parameter": 1}},
    {"column": "ordered_at", "op": "<", "value": {"parameter": 2}}
  ]
}
```

`relation.schema` is optional. An omitted schema matches only an unqualified
relation in the SQL; this diagnostic does not resolve a search path. Identifier
strings match the names produced by PostgreSQL's parser, including quoted case,
and are limited to 63 UTF-8 bytes. Column references must be unqualified.

`select` is nonempty and ordered. Each item is a direct `column`, optionally
with `aggregate` equal to `sum`, `count`, `avg`, `min` or `max`. Only `count`
admits `column: "*"`. These are syntactic function-call names; database aggregate
resolution, function shadowing and numeric behavior remain unproven.

`group_by` is an explicit list of direct column names; `[]` expects no grouping.
Grouping order and AND-conjunction order are ignored, while multiplicity is
preserved. Projection order remains significant. Duplicate predicates are not
silently removed.

`where` is an explicit conjunction; `[]` expects no filter. A scalar comparison
uses `column`, `op` and `value`. Operators are `=`, `<>`, `<`, `<=`, `>` and `>=`.
The value is exactly one of `{"parameter": 1}` (positive parameter number,
at most 65535) or `{"literal": "synthetic-value"}`. Literals may be strings
(at most 4096 UTF-8 bytes), booleans or signed 32-bit integers. Types are compared
exactly: Boolean true does not match integer 1. SQL strings are compared locally
but are never copied to results. Decimal, floating-point and NULL literals are
outside this version's subset.

An inclusive range is explicit:

```json
{"column": "ordered_at", "op": "between", "lower": {"parameter": 1}, "upper": {"parameter": 2}}
```

## Fail-closed subset and result boundaries

Only one SELECT over one ordinary relation is admitted, with direct selected
columns or the supported simple calls, direct grouping, and supported predicates.
CTEs, joins, subqueries, set operations, aliases, qualified column references,
casts, date/time-zone expressions, NULL tests, OR/NOT, DISTINCT, aggregate filters,
window calls, ordering, limits, grouping positions/sets, ONLY, and other syntax
return `not_run` with reason `unsupported_or_invalid_sql`. The entire AST is
admitted before any of the four structural checks can pass. There is no regex
recognition or SQLGlot substitute. Syntax errors use the same sanitized reason.

A missing top-level expectation field returns `not_run` and
`needs_clarification`. Unknown fields, invalid types, duplicate JSON keys,
nonfinite numbers and over-budget inputs are input errors (exit 2). A supported
structural mismatch returns `fail` (exit 1). Unsupported or incomplete input
returns exit 1. A successful structural comparison exits 0 and still includes
the unproven business, runtime, catalog, type and parameter-value limitations.
Passing this command does not establish semantic equivalence or business
correctness, nor contribute operational report authority.

The command performs no SQL execution or generation, database connection, network
request or model call. Explicit files alone are captured, using the preflight
guarded file reader: regular single-link files, checked ancestors, bounded reads
and unchanged identity/bytes before publishing. SQL is limited to 64 KiB;
expectation capture is limited to 256 KiB. Strict JSON and parser AST decoding
have depth 16 and node budget 10000. These are ordinary local-change and resource
guards, not an OS sandbox or a defense against hostile ancestor replacement races
or a compromised native parser.

Results go to `<out>/sql_structure/result.json`, defaulting to `.sxc` next to
the SQL input. Results contain fixed check/reason IDs, hashes and fixed explanatory
text, with provenance and runtime bindings `not_applicable`. SQL text, identifiers,
literal values, input/output paths and parser exception messages are suppressed,
including when `SXC_DEBUG=1`. Hashes bind captured bytes; they are neither encryption
nor authentication. Input aliases of output files and link/reparse paths are
rejected before cleanup, and inputs are checked again before writing. As with
preflight, an unsafe capture cannot authorize deleting a pre-existing output;
do not treat an old file as the result of a command that exited on unsafe input.

## Tests

```sh
python -m unittest discover -s tests -p "test_sql_structure*.py" -v
```

The actual AST tests run with the optional pinned dependency and otherwise skip.
Environment and input-safety tests still run without it. The fixtures use synthetic
table/column names and do not require a PostgreSQL server. Core test discovery
does not install or require pglast.

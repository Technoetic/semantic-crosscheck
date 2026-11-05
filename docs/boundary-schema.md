# Offline scalar boundary schemas

`boundary-schema` derives a small finite witness domain from one authored scalar
comparison. It runs offline, without PostgreSQL, Z3, a Cube model or a new runtime
dependency. A successful result establishes that the supported declaration was
converted into a valid witness schema. It does not establish query equivalence,
business correctness, or operational V1–V5 coverage.

```console
python skills/semantic-crosscheck/scripts/sxc.py boundary-schema --spec examples/boundary-schema/integer.json --out .sxc-boundaries
```

The command rejects `--config` and ignores an implicit `crosscheck.toml`. Default
output is `.sxc` in the current directory. The generated file is
`<out>/boundary_schema/witness-schema.json`; the normal count/hash-only result is
`<out>/boundary_schema/result.json`. Generated scalar values appear only in the
explicit witness-schema artifact.

## Version 1 declaration

Exactly these six fields are required; unknown or duplicate fields are rejected:

```json
{
  "version": 1,
  "table": "items",
  "column": "qty",
  "type": "integer",
  "rows": [0, 2],
  "predicate_sql": "qty >= 10"
}
```

`table` and `column` are lowercase ASCII identifiers, at most 63 characters,
beginning with a letter or underscore. `sxc_candidate` is reserved as a table
name. `rows` is an inclusive `[minimum, maximum]` pair of integers in `0..4`,
matching the existing finite witness contract.

The fully consumed grammar is `column operator literal`, with the declared
column on the left. Operators are `=`, `<>`, `!=`, `<`, `<=`, `>`, and `>=`.
ASCII whitespace may surround the parts. Supported type/literal forms are:

| Type | Literal | Immediate neighbor |
| --- | --- | --- |
| `integer` | signed decimal integer in `-2147483648..2147483647` | one integer |
| `numeric(p,s)` | finite signed decimal, optionally scientific notation | `10^-s` |
| `date` | `DATE 'YYYY-MM-DD'`, with case-insensitive `DATE` | one day |

Numeric declarations use canonical spelling, `1 <= p <= 38`, and `0 <= s <= p`.
The literal must already be exactly representable; values needing scale rounding
or exceeding the declared precision are rejected. Output uses exact fixed-point
strings, independent of the ambient Decimal precision or rounding mode. Date
support is `0001-01-01..9999-12-31`, matching the existing Python witness consumer.

The output domain is NULL followed by the literal and its representable immediate
neighbors in ascending order. At a type boundary only the unavailable neighbor is
omitted. The operator does not filter this domain: both sides of the boundary are
useful candidates for later comparison. Examples also cover
[`numeric(6,2)`](../examples/boundary-schema/numeric.json) and a
[leap-day date](../examples/boundary-schema/date.json).

Input is bounded at 64 KiB, predicate text at 4096 characters, literal text at 256
characters, and absolute numeric exponents at 1000. Comments, semicolons, casts,
quoted/qualified or unrelated identifiers, functions, joins, subqueries,
parameters, compound predicates, reversed comparisons and unsupported types are
rejected. This is a supported-subset grammar, not a PostgreSQL parser or SQL
sandbox.

## Optional PostgreSQL witness search

The output is an ordinary version-1 `--witness-schema` input accepted by
`witness.load`. Actual SQL comparison remains a separate opt-in command. Use only
a dedicated synthetic test connection named by `SXC_BOUNDARY_TEST_DSN`:

```console
python skills/semantic-crosscheck/scripts/sxc.py counterexample --witness-schema .sxc-boundaries/boundary_schema/witness-schema.json --reference-sql examples/boundary-schema/inclusive.sql --candidate-sql examples/boundary-schema/exclusive.sql --dsn-env SXC_BOUNDARY_TEST_DSN --max-examples 5 --out .sxc-boundary-search
```

The included pair distinguishes `>= 10` from `> 10` at the row containing 10.
The search, solver budgets, constraint scope, and synthetic fixture/replay
contracts are unchanged. Deriving a domain does not verify that the authored
predicate corresponds to either supplied SQL file. Failure to find a difference
does not prove equivalence or completeness of the domain.

## Binding and input preservation

A passing result retains SHA-256 hashes of the exact source bytes, this generator
module, and the generated witness-schema bytes. The command checks source and
generator stability and reads back the published artifact before reporting a
pass. These observations do not authenticate rewritten results or prevent file
changes after the command returns. Counterexample replay separately binds the
generated witness declaration under its existing contract; this command has no
independent `--replay` mode.

Declared input aliases, hardlinked aliases and observed symbolic-link/reparse
containers are protected before output cleanup or result writes, including
forbidden configuration inputs and repeated `--spec` arguments. Malformed
declarations produce sanitized errors; unsupported CLI arguments produce a
sanitized usage error without mutating output. File observations are bounded
checks, not a continuous filesystem lock or protection against hostile races.

On macOS, the standard OS root aliases `/var` → `/private/var` and `/tmp` →
`/private/tmp` are admitted only after verifying their link text, plain directory
target and canonical target. This exception preserves normal temporary paths;
user-created input, output and nested directory symlinks remain rejected.

This experiment takes the query-directed test-data motivation of
[XData](https://arxiv.org/abs/1411.6704) and implements only the bounded scalar
boundary heuristic described above; it does not implement XData's complete
mutation-killing method.

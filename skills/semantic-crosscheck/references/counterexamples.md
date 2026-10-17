# Synthetic SQL counterexamples

The optional `counterexample` command runs two trusted SELECT files on the same generated fixture.
It uses PostgreSQL read-only transactions and bound values in CTE tables, without creating or loading
source tables. Use a dedicated test database; arbitrary SQL can explicitly reference other objects.

The built-in scenarios target NULL aggregation, duplicate join keys, two paths through a BOM diamond,
and date-window boundaries. The fixture schema and runnable reference/candidate pairs are in
`examples/counterexamples/`. Both query outputs must have matching column names and equal exact
row multisets to agree; row ordering is irrelevant and duplicate multiplicity matters.

```text
python skills/semantic-crosscheck/scripts/sxc.py counterexample --reference-sql examples/counterexamples/nulls-reference.sql --candidate-sql examples/counterexamples/nulls-candidate.sql --scenario nulls --seed 7 --out .sxc
```

Supply the same SQL files, repeat the saved `--scenario` and `--seed`, and add
`--replay .sxc/counterexample/fixture.json` to rerun a saved case.
Replay checks format, the supported synthetic schema and both SQL hashes. Generated values are local
fixture data, not evidence extracted from a production database. Keep a fixture with its SQL files to
reproduce the comparison; hashes do not preserve the SQL content themselves.

Search and shrinking are bounded. The reducer tries deleting individual rows in a deterministic order
and accepts a deletion only after a successful comparison still shows a difference. It restarts after
an accepted deletion. A complete last pass can establish **1-minimality under row deletion**, not the
globally smallest fixture or minimality under value changes. A budget limit leaves minimality unproven.
Query errors never establish a counterexample and are not accepted as shrink witnesses.
If a deletion cannot execute, the valid witness is retained and minimality stays unproven.

## Optional integrity profile

Add `--constraints examples/counterexamples/constraints.json` to enforce a declared domain. The
profile is not inferred from a production schema. It accepts only the four fixed synthetic tables
and their existing columns, format `version: 1`, and the following rules:

- `tables.<table>.primary_key`: one nonempty column list; implies NOT NULL and uniqueness.
- `tables.<table>.unique`: a list of column lists; PostgreSQL default NULLS DISTINCT semantics.
- `tables.<table>.not_null`: a column list.
- `foreign_keys`: source `table`/`columns` and `references.table`/`references.columns`. Column types
  must match exactly and the referenced list must be a declared primary/unique key. MATCH SIMPLE
  ignores a child key containing any NULL.
- `checks`: `table`, `column`, `op`, `value`. Operators are `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `in`;
  `in` takes a nonempty list. A NULL column satisfies CHECK unless separately declared NOT NULL.
  Text supports only `eq`/`ne`/`in`; ordered text predicates are rejected.

Numeric fixture values are rounded to the declared PostgreSQL precision/scale before validation,
including rounding overflow. Numeric CHECK constants retain their exact precision; decimal strings
are convenient in JSON. Integers are signed 32-bit and dates are ISO `YYYY-MM-DD`, years 0001–9999.
Constrained fixture text expressions use explicit PostgreSQL `C` collation, so this domain does not
model application-specific or nondeterministic collation behavior. Explicit collation overrides in
trusted SQL are outside the profile contract.

Unknown fields, duplicate JSON keys, nonfinite numbers, invalid types and unsupported rules fail
before DB access. Profiles are limited to 256 KiB and 128 compiled rules. Arbitrary SQL CHECKs,
column-to-column expressions, functions, MATCH FULL, deferred constraints and NULLS NOT DISTINCT
are unsupported. The profile is not a PostgreSQL DDL interpreter.

Every generated case and shrink trial must satisfy the profile. Invalid cases are counted separately
and never run as SQL; an all-invalid search is `not_run`, not PASS. A parent-row deletion that would
orphan a child is rejected without a cascade. A completed shrink pass proves at most **1-minimality
among admissible single-row deletions**. Simultaneous deletions might still yield a smaller witness.

The fixture records `{mode, version, sha256}` for the exact profile bytes, including whitespace.
Replay needs the same SQL, scenario, seed and exact profile file. A changed mode or profile is rejected
before connecting; a profile changed during a witnessed run prevents saving that witness. Legacy
fixtures without the binding remain replayable only without `--constraints`.

```text
python skills/semantic-crosscheck/scripts/sxc.py counterexample --reference-sql examples/counterexamples/nulls-reference.sql --candidate-sql examples/counterexamples/nulls-candidate.sql --constraints examples/counterexamples/constraints.json --scenario nulls --seed 7 --out .sxc
```

The opt-in approach is informed by integrity-constrained bounded SQL checking in
[VeriEQL](https://doi.org/10.1145/3649849). No VeriEQL source is copied or bundled; its
CC BY-NC-SA code is not incorporated into this MIT project.

A discovered mismatch is `fail`/exit 1; no mismatch within the bound is `not_run`/exit 1, not global
equivalence. Invalid arguments or replay files are configuration errors. Runtime query failures are
errors. `result.json` contains statistics and identifiers only; `fixture.json` alone contains the
generated rows, together with generator/replay metadata and SQL fingerprints.

This capability adopts generated-test-input and shrinking ideas described by
[Hypothesis](https://github.com/HypothesisWorks/hypothesis), and the counterexample/reduction approach
in [SQLancer](https://github.com/sqlancer/sqlancer). Neither project is bundled or copied, and no new
runtime dependency is added. A comparison detects disagreement between the supplied trusted queries;
it does not prove either query expresses the intended business rule or detect every DBMS defect.

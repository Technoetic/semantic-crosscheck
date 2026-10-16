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

A discovered mismatch is `fail`/exit 1; no mismatch within the bound is `not_run`/exit 1, not global
equivalence. Invalid arguments or replay files are configuration errors. Runtime query failures are
errors. `result.json` contains statistics and identifiers only; `fixture.json` alone contains the
generated rows, together with generator/replay metadata and SQL fingerprints.

This capability adopts generated-test-input and shrinking ideas described by
[Hypothesis](https://github.com/HypothesisWorks/hypothesis), and the counterexample/reduction approach
in [SQLancer](https://github.com/sqlancer/sqlancer). Neither project is bundled or copied, and no new
runtime dependency is added. A comparison detects disagreement between the supplied trusted queries;
it does not prove either query expresses the intended business rule or detect every DBMS defect.

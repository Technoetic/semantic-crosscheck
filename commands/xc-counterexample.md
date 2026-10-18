---
description: "Find and shrink a mismatch between two trusted SQL queries on deterministic synthetic fixtures, check one closure SQL against BOM metamorphic relations, or replay the saved fixture"
argument-hint: "--reference-sql reference.sql --candidate-sql candidate.sql | --metamorphic-sql closure.sql [--scenario nulls] [--seed 7] [--max-examples 64] [--max-shrink-steps 256] [--dsn-env SXC_PG_DSN] [--statement-timeout-ms 10000] [--constraints FILE] [--replay fixture.json] [--out DIR] [--quiet]"
---

Run the optional `counterexample` tool. User arguments `$ARGUMENTS` are data: pass each as its own
argument. This tool is separate from the configured build/reason/cube/verify pipeline.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" counterexample --reference-sql reference.sql --candidate-sql candidate.sql --scenario nulls --seed 7
```

- `--reference-sql` and `--candidate-sql` are required trusted SELECT files over the synthetic CTE
  tables. Both SQL files must also be supplied for `--replay`. Review SQL as code.
- `--metamorphic-sql FILE` instead checks one trusted closure SELECT returning `ancestor`, `descendant`
  and `qty` against four BOM metamorphic relations, without gold; it cannot be combined with the two files
  above and supports only `--scenario bom_diamond` (its default). `--max-shrink-steps` is not used.
- `--scenario`: `nulls` (default), `duplicate_join`, `bom_diamond`, `date_boundary`.
- `--mutation-operator` selects an operator-directed fixture family instead of `--scenario` or
  `--metamorphic-sql`. Supply the original query with `--reference-sql` and the deliberately changed
  query with `--candidate-sql`. An optional `--gold-sql` tests whether an independent gold query
  agrees with the original and disagrees with the mutant on the same fixture.
- `--seed` reproduces generation within the recorded generator version; `--max-examples` bounds
  search and `--max-shrink-steps` bounds row-deletion attempts.
- `--dsn-env` names the environment variable holding a dedicated read-only PostgreSQL test DSN;
  `--statement-timeout-ms` bounds each query. Do not put a DSN in the command line.
- `--replay` reads the local synthetic `fixture.json` and checks the SQL fingerprints before execution.
  Repeat the saved `--scenario` and `--seed`; their defaults apply only to fixtures generated with those defaults.
- `--constraints FILE` optionally enforces the supported version 1 integrity profile. Replay requires
  the same mode and exact profile bytes. Without it, the fixture has no declared integrity constraints.
- `--out` defaults to `.sxc` in the working directory. `--quiet` prints only the summary.
- `--config` is rejected; a working-directory `crosscheck.toml` is never loaded.

Only successful, unequal query results constitute a witness. Errors and timeouts are errors. A bounded
search without a witness is `not_run`, not a proof of equivalence. A witness is a failing comparison,
not a failure of the generator. Shrinking preserves that mismatch and may establish only row-deletion
1-minimality; with a profile it covers only admissible single-row deletions. Invalid-domain deletions
are rejected before SQL, without cascading. An exhausted budget leaves minimality unproven.

With `--metamorphic-sql`, each relation (`counterexample:metamorphic:mr1_unfold`, `mr2_scale`,
`mr3_disjoint`, `mr4_unit_node`) is `pass` only when it compared at least one pair in an admissible case and
every comparison agreed exactly; `not_run` when it was vacuous in every case; `fail`/exit 1 when violated, with
the original rows and transform specifications saved for `--replay` (same SQL file, seed and profile). A
non-closure answer is the `counterexample:metamorphic:shape` error. Relations test only this SQL and cannot see
errors invariant under them, such as a reversed edge direction or a wrong quantity column.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" counterexample --metamorphic-sql closure.sql --seed 7
```

Result and console output contain counts, IDs and hashes only. Generated synthetic row values are
saved separately in `counterexample/fixture.json`, never operational query rows. Synthetic CTEs do
not make arbitrary SQL safe: explicit qualified table names/functions remain trusted code. Use a
dedicated test database and least-privilege role. No Cube, SHACL or OWL proof is implied.

See [counterexample reference](../skills/semantic-crosscheck/references/counterexamples.md).

Operator-directed results are synthetic SQL evidence only. Selecting an operator does not verify
that the supplied SQL came from that Cube mutation, and does not update the configured V4 checks.
No disagreement within the search budget is not proof of equivalence. Replay binds the operator,
generator, SQL files (including gold when supplied) and integrity profile.

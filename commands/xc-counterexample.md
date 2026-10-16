---
description: "Find and shrink a mismatch between two trusted SQL queries on deterministic synthetic fixtures, or replay the saved fixture"
argument-hint: "--reference-sql reference.sql --candidate-sql candidate.sql [--scenario nulls] [--seed 7] [--max-examples 64] [--max-shrink-steps 256] [--dsn-env SXC_PG_DSN] [--statement-timeout-ms 10000] [--replay fixture.json] [--out DIR] [--quiet]"
---

Run the optional `counterexample` tool. User arguments `$ARGUMENTS` are data: pass each as its own
argument. This tool is separate from the configured build/reason/cube/verify pipeline.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" counterexample --reference-sql reference.sql --candidate-sql candidate.sql --scenario nulls --seed 7
```

- `--reference-sql` and `--candidate-sql` are required trusted SELECT files over the synthetic CTE
  tables. Both SQL files must also be supplied for `--replay`. Review SQL as code.
- `--scenario`: `nulls` (default), `duplicate_join`, `bom_diamond`, `date_boundary`.
- `--seed` reproduces generation within the recorded generator version; `--max-examples` bounds
  search and `--max-shrink-steps` bounds row-deletion attempts.
- `--dsn-env` names the environment variable holding a dedicated read-only PostgreSQL test DSN;
  `--statement-timeout-ms` bounds each query. Do not put a DSN in the command line.
- `--replay` reads the local synthetic `fixture.json` and checks the SQL fingerprints before execution.
  Repeat the saved `--scenario` and `--seed`; their defaults apply only to fixtures generated with those defaults.
- `--out` defaults to `.sxc` in the working directory. `--quiet` prints only the summary.
- `--config` is rejected; a working-directory `crosscheck.toml` is never loaded.

Only successful, unequal query results constitute a witness. Errors and timeouts are errors. A bounded
search without a witness is `not_run`, not a proof of equivalence. A witness is a failing comparison,
not a failure of the generator. Shrinking preserves that mismatch and may establish only row-deletion
1-minimality; an exhausted budget leaves minimality unproven.

Result and console output contain counts, IDs and hashes only. Generated synthetic row values are
saved separately in `counterexample/fixture.json`, never operational query rows. Synthetic CTEs do
not make arbitrary SQL safe: explicit qualified table names/functions remain trusted code. Use a
dedicated test database and least-privilege role. No Cube, SHACL or OWL proof is implied.

See [counterexample reference](../skills/semantic-crosscheck/references/counterexamples.md).

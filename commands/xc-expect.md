---
description: "Run the gold queries and Cube on a dedicated fixture database and compare both with the literal expected rows of [[gold.expect]] (not released yet)"
argument-hint: "--config crosscheck.toml --allow-fixture-database [--dsn-env SXC_EXPECT_DSN] [--out DIR] [--quiet]"
---

Run the `expect` stage (after 0.8.0, not released yet). Pass user arguments as separate argv entries.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" expect --config crosscheck.toml --allow-fixture-database
```

- `--config` names the operational configuration: `expect` reads its `[[gold]]` entries and their `[[gold.expect]]`
  cases ([keys](../skills/semantic-crosscheck/references/config.md)), but never connects to its DSNs.
- `--dsn-env` defaults to `SXC_EXPECT_DSN`: the NAME of the environment variable holding the DSN of a dedicated
  fixture database that the user has already loaded with every configured source table (empty ones included) and
  the small fixture rows (for example `examples/expect/fixture.sql` with psql). sxc never executes fixture SQL. An
  unset variable is exit 3; a value that equals, or names the same host, port and database as, `source.dsn_env`,
  `source.owner_dsn_env` or `cube.dsn_env` is refused with exit 2, without printing either value.
- `--allow-fixture-database` is required (exit 2 without it): it permits the stage to build the configured views and
  closure tables in that database's output schema.
- `--out DIR` defaults to `.sxc` next to the configuration; the fixture's build and Cube files go to
  `<out>/expect/`, so the operational `build/` and `cube/` results are never touched. `--quiet` prints only the summary.

The stage runs the normal build and cube stages against the fixture, serves Cube once and, for every case, runs the
gold SQL and the gold's Cube query with the case's parameters. Each side is compared with the expected rows
(`expect:<gold>[<i>]`): both equal them is a pass; gold and Cube agreeing with each other but not with the rows is
`common_mode` (authored rows) or `regression` (captured rows); a disagreement is `gold_mismatch`, `cube_mismatch` or
`both_mismatch`, and the detail says which side equals the rows. A side that cannot run is an error, a case whose
closure or Cube members did not pass is not_run, and a fixture that changes during the run turns every pass into
not_run. A configuration without cases gives `expect:cases` not_run.

Rows written from the business specification (`source = "authored"`, with `spec` naming where) are the only
independent evidence; captured rows are regression evidence only and are listed in `not_proven`. The result is
reported separately: `report` does not read it and a pass widens neither operational V1-V5 nor report coverage.
Results hold counts, ids and a fixture hash only, never rows, parameter values or DSNs.

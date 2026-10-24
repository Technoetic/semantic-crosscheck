---
description: "Run the gold queries and Cube on a dedicated fixture database and compare both with the literal expected rows of [[gold.expect]]"
argument-hint: "--config crosscheck.toml --allow-fixture-database [--dsn-env SXC_EXPECT_DSN] [--out DIR] [--quiet]"
---

Run the `expect` stage. Pass user arguments as separate argv entries.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" expect --config crosscheck.toml --allow-fixture-database
```

- `--config` names the operational configuration: `expect` reads its `[[gold]]` entries and their `[[gold.expect]]`
  cases ([keys](../skills/semantic-crosscheck/references/config.md)), but never connects to its DSNs.
- `--dsn-env` defaults to `SXC_EXPECT_DSN`: the NAME of the environment variable holding the DSN of a dedicated
  fixture database that the user has already loaded with every configured source table (empty ones included) and
  the small fixture rows (for example `examples/expect/fixture.sql` with psql). sxc never executes fixture SQL. An
  unset variable is exit 3, a value that is not a variable name exit 2. The fixture DSN is refused with exit 2,
  without printing either value, when it equals, or names the same host, port and database as, an operational DSN
  variable (`source.dsn_env`, `source.owner_dsn_env`, `cube.dsn_env`) set in this environment; another name for the
  same server is not detected, so the fixture must be a dedicated database. `expect:fixture` records how many
  operational variables were compared (`operational_variables_compared`) and `not_proven` names the unset ones.
- `--allow-fixture-database` is required when the configuration has cases (exit 2 without it): it permits the stage
  to build the configured views and closure tables in that database's output schema, and to hold SHARE locks on the
  fixture's source and closure tables while the cases run, so writers to them wait. There is no scratch schema and
  no cleanup: those objects stay in the fixture database and the next run rebuilds them.
- Take the fixture DSN variable and `--allow-fixture-database` only from the user, for a database the user names as a
  dedicated fixture. Never select, guess or suggest an operational DSN for it.
- `--out DIR` defaults to `.sxc` next to the configuration; the fixture's build and Cube files go to
  `<out>/expect/`, so the operational `build/` and `cube/` results are never touched. `--quiet` prints only the summary.

The stage first fingerprints the fixture's source tables (`fixture_sha256`), runs the normal build and cube stages
against the fixture (the cube stage starts Cube for its `cube:meta` check), serves the cases from one more Cube process
and, for every case, runs the gold SQL and the gold's Cube query with the case's parameters. All cases share the one
fixture database. Each side is compared with the expected rows (`expect:<gold>[<i>]`): both equal them is a pass; gold
and Cube agreeing with each other but not with the rows is `common_mode` (authored rows) or `regression` (captured
rows); a disagreement is `gold_mismatch`, `cube_mismatch` or `both_mismatch`, and the detail says which side equals
the rows. For `common_mode`, first check that the fixture rows and the expected rows follow the cited specification:
a wrongly loaded fixture gives the same verdict, and changing the gold SQL and the configuration to match it would
create the shared mistake this stage looks for. For `regression`, first check that the loaded fixture is the one the
rows were captured on (compare `expect:fixture`'s `fixture_sha256` with the case's pin, or pin it). A case can pin the
`fixture_sha256` of the fixture it was written for; on another fixture it is not_run (`fixture_mismatch`). The pin
depends on the source tables' names, their rows and the order and types of their columns (and on the database
encoding for text outside ASCII), not on the database's default collation or the session's settings: the digest
orders the row text by `COLLATE "C"` and fixes `TimeZone`, `DateStyle`, `IntervalStyle`, `extra_float_digits`,
`bytea_output` and `lc_monetary` while it reads. A side that cannot run, or expected rows whose keys are equal in
their typed form (`expected_duplicate_keys`), is an error; a case whose closure or Cube members did not pass is
not_run; every not_run case gets a `not_proven` line. While the cases run, a second session holds SHARE locks on the
source and built closure tables when the role can take them, so writers wait; otherwise `expect:fixture` records
`locked: false` and a `not_proven` line says so. `expect:fixture` compares the source-table digest taken before the
build with digests of the source and closure tables taken before and after the cases, and a difference turns every
pass into not_run. This shows equality at those points, not one shared snapshot, covers no other table, and gold and
Cube read in separate sessions; `not_proven` says so. A configuration without cases gives `expect:cases` not_run.

Rows written from the business specification (`source = "authored"`, with `spec` naming where) are the only
independent evidence; captured rows are regression evidence only and are listed in `not_proven`. The result is
reported separately: `report` does not read it and a pass widens neither operational V1-V5 nor report coverage.
Results hold counts, ids and a fixture hash only, never rows, parameter values or DSNs. The hash is not salted, so
someone holding the result and the schema can confirm guessed fixture rows: keep the fixture synthetic. The cases
live in the operational `crosscheck.toml`, so editing one makes earlier `build`, `reason`, `cube` and `verify`
results stale for `report` until they are rerun. `SXC_CUBE_DEBUG=1` can write fixture values to `expect/server.log`
and `expect/cube/server.log`.

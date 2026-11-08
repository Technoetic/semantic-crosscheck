# semantic-crosscheck report

Overall: **FAIL**, config `crosscheck.toml`, generated <time>

Required stages: build, reason, cube, verify. The overall status passes only when each of them ran after the stages it depends on and passed.

| Stage | Status | Pass | Fail | Error | Not run | Not proven | Finished |
|---|---|---:|---:|---:|---:|---:|---|
| doctor | not run (optional) | | | | | | |
| demo | not run (optional) | | | | | | |
| build | fail | 4 | 2 | 0 | 0 | 2 | <time> |
| reason | not run (required) | | | | | | |
| cube | not run (required) | | | | | | |
| verify | not run (required) | | | | | | |

## Missing or stale stages

- reason: required, but it has not run (no reason/result.json)
- cube: required, but it has not run (no cube/result.json)
- verify: required, but it has not run (no verify/result.json)

## Checks that did not pass

- build: `fail` `shape:child_exists`, fix `shapes[0] child_exists`: violations=2 severity=violation kind=class view=sxc_out.v_shape_child_exists
- build: `fail` `shape:open_le_total`, fix `shapes[3] open_le_total`: violations=3 severity=violation kind=sql view=sxc_out.v_shape_open_le_total trusted_sql=True

## Not proven

- build: runtime evidence is unbound: actual PostgreSQL, Node, Java, HermiT and selected Cube installation were not bound; local engine/package metadata alone does not establish the executing runtime
- build: data revision is unbound: no owner-maintained revision was checked; separate database sessions and cached gold answers are not proven to observe one data version
- report: data revision is unbound: no owner-maintained revision was checked; separate database sessions and cached gold answers are not proven to observe one data version
- report: stage doctor was not run, so the check role's privileges (read-only on the source tables) and the optional components were not verified

Data revision binding: **unbound**. Only revision hashes are retained.

Actual runtime binding: **unbound**. Only normalized versions and artifact/setting hashes are retained.

runtime evidence is unbound: actual PostgreSQL, Node, Java, HermiT and selected Cube installation were not bound; local engine/package metadata alone does not establish the executing runtime.

## Next safe actions

- build: shapes failed: inspect the violating keys in sxc_out.v_shape_child_exists, sxc_out.v_shape_open_le_total, then fix the data or the [[shapes]] entries; then rerun build
- report: fix build first (fail): shapes failed: inspect the violating keys in sxc_out.v_shape_child_exists, sxc_out.v_shape_open_le_total, then fix the data or the [[shapes]] entries; then rerun build

## Trusted code

`sql_violation` predicates and gold SQL files are trusted code from the configuration: the harness runs them as written (values only as bound parameters) and does not generate or validate them, so review them like code.

- shape `open_le_total` (sql_violation)
- gold `q01_explosion`: `gold/q01_explosion.sql`
- gold `q02_where_used`: `gold/q02_where_used.sql`
- gold `q03_stock`: `gold/q03_stock.sql`
- gold `q04_depth_histogram`: `gold/q04_depth_histogram.sql`
- gold `q05_firm_window`: `gold/q05_firm_window.sql`

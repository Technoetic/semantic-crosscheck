# semantic-crosscheck report

Overall: **NOT_RUN**, config `crosscheck.toml`, generated <time>

Required stages: build, reason, cube, verify. The overall status passes only when each of them ran after the stages it depends on and passed.

| Stage | Status | Pass | Fail | Error | Not run | Not proven | Finished |
|---|---|---:|---:|---:|---:|---:|---|
| doctor | not run (optional) | | | | | | |
| demo | not run (optional) | | | | | | |
| build | pass | 6 | 0 | 0 | 0 | 2 | <time> |
| reason | not run (required) | | | | | | |
| cube | not run (required) | | | | | | |
| verify | not run (required) | | | | | | |

## Missing or stale stages

- reason: required, but it has not run (no reason/result.json)
- cube: required, but it has not run (no cube/result.json)
- verify: required, but it has not run (no verify/result.json)

## Checks that did not pass

None.

## Not proven

- build: runtime evidence is unbound: actual PostgreSQL, Node, Java, HermiT and selected Cube installation were not bound; local engine/package metadata alone does not establish the executing runtime
- build: data revision is unbound: no owner-maintained revision was checked; separate database sessions and cached gold answers are not proven to observe one data version
- report: data revision is unbound: no owner-maintained revision was checked; separate database sessions and cached gold answers are not proven to observe one data version
- report: stage doctor was not run, so the check role's privileges (read-only on the source tables) and the optional components were not verified

Data revision binding: **unbound**. Only revision hashes are retained.

Actual runtime binding: **unbound**. Only normalized versions and artifact/setting hashes are retained.

runtime evidence is unbound: actual PostgreSQL, Node, Java, HermiT and selected Cube installation were not bound; local engine/package metadata alone does not establish the executing runtime.

## Next safe actions

- build: run `sxc.py reason` to cross-check the shapes and the hierarchy with SHACL and OWL, then `sxc.py cube`
- report: run the stages that have not run yet (reason, cube, verify), in this order, then rerun report

## Trusted code

`sql_violation` predicates and gold SQL files are trusted code from the configuration: the harness runs them as written (values only as bound parameters) and does not generate or validate them, so review them like code.

- shape `open_le_total` (sql_violation)
- gold `q01_explosion`: `gold/q01_explosion.sql`
- gold `q02_where_used`: `gold/q02_where_used.sql`
- gold `q03_stock`: `gold/q03_stock.sql`
- gold `q04_depth_histogram`: `gold/q04_depth_histogram.sql`
- gold `q05_firm_window`: `gold/q05_firm_window.sql`

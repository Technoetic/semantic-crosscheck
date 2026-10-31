# semantic-crosscheck design

semantic-crosscheck is a verification harness for an ontology plus semantic layer built on PostgreSQL 17.
It does not try to be another modelling tool. It generates four layers from one configuration file and
checks that they agree with each other and with hand-written "gold" SQL:

| Layer | Technology | Produced by |
|---|---|---|
| Hierarchy closure | PostgreSQL 17 (`WITH RECURSIVE`, `CYCLE`, `MERGE ... RETURNING merge_action()`) | `build` |
| Data rules | SQL violation views + generated SHACL shapes checked with pySHACL | `build`, `reason` |
| Ontology | OWL 2 classes and axioms checked with HermiT (through owlready2) | `reason` |
| Semantic layer | Cube Core data model (YAML) served on 127.0.0.1 | `cube` |

Disagreement between two layers is a defect in one of them; the harness reports which check failed and
which `crosscheck.toml` entry to fix. Everything is generated: the agent edits the configuration, never
the generated SQL or YAML. This document is the contract the stages, the command documents and the tests
follow; the configuration keys are in `skills/semantic-crosscheck/references/config.md`.

## 1. Stages and command line

```
python skills/semantic-crosscheck/scripts/sxc.py <stage> [--config PATH] [--out DIR] [--quiet] [stage options]
python skills/semantic-crosscheck/scripts/sxc.py --version
```

| Stage | Role | Stage options | Writes (under the output directory) |
|---|---|---|---|
| `doctor` | Python, pg8000, the check-role DSN, PostgreSQL >= 17, the check role's privileges, optional components | `--require core,reason,cube` (default `core`) | `doctor/result.json` |
| `demo` | loads the synthetic demo with the loader role | `--variant clean\|dirty` (default `clean`), `--seed N` (default 20261013), `--scale N` (default 1) | `demo/result.json`, `demo/expected.json` |
| `build` | shape violation views and hierarchy closures in the output schema | `--hierarchy NAME` | `build/result.json`, `build/build.sql`, `build/shapes.ttl`, `build/fk_review.sql` (only while a shape has `fk_ddl = "review_only"`) |
| `reason` | SHACL against SQL, OWL consistency, OWL against SQL (V5) | `--only shacl,owl` (default both) | `reason/result.json` |
| `cube` | generates the Cube model, serves it, checks gold members and metadata | none | `cube/result.json`, `cube/model/*.yml`, `cube/model.json`, `cube/server.log` |
| `verify` | V1 strict compare, V2 strata sweep, V3 query shapes, V4 mutation tests | `--only v1,v2,v3,v4` (default all), `--gold ID` (repeatable) | `verify/result.json`, `verify/server.log`, `verify/mutants/<operator>__<target>/` (`model/*.yml`, `model.json`, `server.log`) |
| `report` | summary of every stage | none | `report/result.json`, `report.md`, `report.json` |
| `cube-install` | installs the pinned Cube adapter once per user | `--home DIR`, `--native-file PATH`, `--force` | `cube_install/result.json`, `cube_install/npm-ci.log`, `cube_install/smoke.log` |
| `counterexample` | optional synthetic SQL mismatch search and reduction, or BOM metamorphic relations (and, with `--scenario bom_exhaustive`, the exact closure oracle over a bounded-exhaustive enumeration) for one closure SQL | `--reference-sql`, `--candidate-sql`, `--metamorphic-sql`, `--scenario`, `--seed`, `--max-examples`, `--max-shrink-steps`, `--dsn-env`, `--statement-timeout-ms`, `--constraints`, `--replay` | `counterexample/result.json`, `counterexample/fixture.json` |
| `cube-replay` | optional actual original/mutant Cube replay against independent gold SQL on owned synthetic tables | required `--mutation-operator`, `--allow-scratch-schema`, `--seed` (7), `--dsn-env` (`SXC_CUBE_REPLAY_DSN`), `--statement-timeout-ms` (10000), `--replay` | `cube_replay/result.json`, successful bound `cube_replay/fixture.json` |
| `tlp` | optional ternary-logic partition consistency of a synthetic relation, including exact COUNT or SUM | required `--spec FILE`, `--seed` (7), `--max-examples` (64), `--dsn-env` (`SXC_TLP_DSN`), `--statement-timeout-ms` (10000), `--replay` | `tlp/result.json`, `tlp/fixture.json` |
| `shacl-corpus` | optional actual pySHACL and Apache Jena comparison against independent expected nonrecursive reports, or (since 0.9.0) against the pinned W3C SHACL core suite's expected reports inside the sxc profile | `--jena-home` (`SXC_JENA_HOME`), `--timeout-ms` (30000 per case and engine), `--w3c-suite [DIR]` (bare: `SXC_W3C_SHACL_SUITE`), `--allow-unpinned-suite` | `shacl_corpus/result.json` |
| `expect` | optional literal expected rows of `[[gold.expect]]` against the gold SQL and Cube on a dedicated fixture database | required `--allow-fixture-database`, `--dsn-env` (`SXC_EXPECT_DSN`) | `expect/result.json`, `expect/build/`, `expect/cube/`, `expect/server.log` |

Common options:

- `--config PATH` names the operational configuration. When supplied to an operational stage, the file must
  exist, `doctor` and `cube-install` included: `--config <name>: no such file (check the path; nothing was run)`,
  exit 2. Without it, operational stages use `./crosscheck.toml`; `doctor` and `cube-install` may run without
  one, and other operational stages stop with exit 2 (`no crosscheck.toml in the current directory: pass
  --config PATH`). Standalone `counterexample`, `cube-replay`, `tlp` and `shacl-corpus` reject explicit
  `--config` and never load a configuration from the working directory. `expect` needs
  the operational configuration like `build`, but connects only to its fixture database.
- `--out DIR` is the output directory. Default: `.sxc` next to the configuration, or `./.sxc` in the current
  directory when there is no configuration (`doctor` and `cube-install` without `--config`, or a standalone
  synthetic stage).
- `--quiet` prints only the summary line.

A run goes through these steps:

1. `sxc.py` refuses Python older than 3.11 before importing anything: exit 3 with `semantic-crosscheck needs
   Python 3.11 or newer (this is X.Y)` on stderr. doctor's own `python` check can therefore only be seen on 3.11
   or newer.
2. The configuration path is resolved as above and the output directory is created. A directory created here
   gets a `.gitignore` containing `*` (results, generated files and logs belong to the local run); an existing
   directory is left alone.
3. The stage's previous `<stage>/result.json` is deleted, so an interrupted or killed run cannot leave an earlier
   verdict behind for `report`.
4. An operational configuration, when present, is loaded and validated (exit 2 on any malformed entry,
   see config.md).
5. The stage module runs: `run(cfg, args, out: Path) -> Result` (`cfg` is `None` for standalone synthetic
   stages, and for `doctor` and `cube-install` without a configuration). `sxc.py` writes the result atomically
   (`result.json.part`, then a rename) and prints the summary.

When the stage cannot produce its own checks, the result holds a single `stage` check with status `error`:
`message` for a `StageError` (exit 2 or 3), `reason=interrupted` for Ctrl-C (exit 1), or `exception` (the type)
and `sqlstate` for an unexpected exception (exit 1). An unexpected exception prints only its type and location on
stderr; `SXC_DEBUG=1` prints the full traceback for local debugging (its messages can quote row values).

Console output: one summary line `[sxc <stage>] <STATUS>  checks: N pass, N fail, N error, N not_run  -> <path of
result.json>`, then, unless `--quiet`, every check that did not pass with its `config_ref` and a short detail,
every `NOT PROVEN` line and a `next:` line. `cube-install` also prints progress lines on stderr.

Exit codes:

| Code | Meaning | Typical causes |
|---|---|---|
| 0 | the stage status is `pass` | every check passed |
| 1 | a check failed, errored or did not run | a `fail`, `error` or `not_run` check; a run narrowed by `--only`, `--gold` or `--hierarchy`; `report` with a missing or stale required stage; Ctrl-C; an unexpected exception |
| 2 | configuration or usage error | a malformed `crosscheck.toml` entry, an explicit `--config` that does not exist, no `./crosscheck.toml` for a stage that needs one, an unknown `--require`, `--only`, `--gold` or `verify.mutation_operators` value, `--scale` below 1, a missing prerequisite result (`run sxc.py <stage> first`), `build/shapes.ttl` older than the shapes in the configuration, a configured table or column that does not exist, demo tables with other columns than the demo defines, a `--native-file` that does not exist, a `--home` directory that is neither empty nor an adapter install, `expect` with cases but without `--allow-fixture-database`, with a `--dsn-env` that is not a variable name, or with a fixture DSN that equals, or names the same host, port and database as, an operational DSN variable set in this environment (another name for the same server is not detected) |
| 3 | environment missing | a DSN variable that is not set or holds a malformed DSN, an unsupported `sslmode` or DSN parameter, an unreadable `sslrootcert`, a failed connection, PostgreSQL older than 17, pg8000 or the reason packages missing, Java missing (`reason`), Node.js missing or older than 22, the Cube adapter missing, incomplete or stale, Cube not starting, the check role unable to read a source table or to create in the output schema (`build`), an unsupported platform or a glibc too old (`cube-install`), Python older than 3.11 |

`doctor` reports a missing DSN, a failed connection and an old server as failing checks (exit 1) instead of
exit 3, so one run lists everything to fix.

## 2. Result contract (`result.py`)

- `Result(stage)`; `res.add(check_id, status, config_ref, **detail)` with status `pass|fail|error|not_run`.
- Stage status is fail-closed: any `error` > any `fail` > any `not_run` > all `pass`; no checks = `not_run`.
- `not_run` means the check could not run or was deliberately left out (a gated closure, a narrowed run, a
  missing prerequisite). It is never a pass: a stage with a `not_run` check and no failures has status `not_run`
  and exits 1.
- `detail` holds counts, ids of configuration entries and small enums only. **Never row values** (no keys,
  names, quantities from the database), never DSNs or secrets, never local paths. Messages of database errors
  are not stored (they can quote values); the SQLSTATE is. Install hints are stored with the placeholders
  `<python>` and `<plugin>`, which `sxc.py` expands only when it prints, so a printed hint works from any
  directory while result files stay path-free (a `--home` directory the user typed is repeated as typed).
- `res.not_proven` lists what the run could not prove (an empty stratum, an equivalent mutant, a shape compared
  only in SQL, a vacuous defined class, a sampled or chunked pair test, a join path additivity does not apply to, a
  component that is not installed). It does not
  change the status; `report` shows every line.
- `res.next_safe_action` is one sentence the agent can follow.
- `started_at` and `finished_at` are local times with UTC offset at one-second resolution; `report` uses them
  to find stale results.
- Prior stage results: `load_result(out, stage)`, `check_status(out, stage, check_id)` and
  `require_stage(out, stage)`, which raises exit 2 with `run sxc.py <stage> first (no <stage>/result.json in
  the output directory)`. `discard(out, stage)` removes a result before its stage runs again; `Result.write`
  replaces `result.json` atomically.

## 3. Security model

- **Two database roles.** The **loader** (`SXC_PG_OWNER_DSN`, named by `source.owner_dsn_env`) is used only by
  `demo`. The **check role** (`SXC_PG_DSN`, named by `source.dsn_env`) reads the source tables and owns the
  output schema (`source.output_schema`), where all generated tables and views live. `docs/setup-roles.sql`
  creates both with psql as a superuser.
- **doctor proves the check role's privileges** with a configuration: `privileges:select:<entity>` (USAGE on the
  schema, SELECT on the table), `privileges:no_write:<entity>` (no INSERT or UPDATE on any column, no DELETE,
  TRUNCATE or TRIGGER; not the owner of the table or of its schema, nor a member of either owner's role; no
  CREATE on the schema), `privileges:temporary` (temporary tables for the closure) and `output_schema`.
- **Transactions.** `doctor`, `reason`, the catalog reads of `cube` and every read of `verify` (gold SQL,
  sampling, witnesses, V3 SQL) run in `READ ONLY` transactions with `source.statement_timeout_ms`
  (`db.read_only`). The SQL reads inside one check share one snapshot: where a check combines the results of
  several statements, they run in one `REPEATABLE READ` transaction (the key count and the key sample of a V3
  pair test, and the `information_schema` reads the `cube` stage builds the model from, through
  `db.read_only(..., isolation="repeatable read")`; the row exports of `reason` and the counterexample
  comparisons set the same level inside their transaction). A read of one statement (gold SQL, a witness, a
  strata sample, a V3 total) keeps the default `READ COMMITTED`, under which that statement sees one snapshot.
  Catalog lookups such as `to_regclass` are not frozen by either level. No transaction stays open while Cube
  answers, and no snapshot is exported to another session: Cube's sessions, the stages, and the SQL reads
  before and after a Cube query share no snapshot, so a run relies on stable data or on the
  `source.data_revision_sql_file` contract (section 8). `build` reads the source tables inside its write
  transactions (it counts the shape views and copies the edges into temporary tables there) and relies on the
  check role's privileges; it writes only to the output schema and to temporary tables. Each build transaction
  first takes `pg_advisory_xact_lock(hashtext('semantic-crosscheck build <output_schema>'))`, so two builds of
  one output schema wait for each other. `demo` writes with the loader role under its own advisory lock, in one
  transaction with a 10-minute statement timeout.
- **Cube sessions are read-only and bounded.** Cube connects with the check role (or `[cube].dsn_env`), with
  `PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=<cube.query_timeout_s * 1000>'` and
  `CUBEJS_DB_QUERY_TIMEOUT=<cube.query_timeout_s>`. Cube's Postgres driver sets `statement_timeout` from the
  latter on every connection (its default is 10 minutes, and it is also the queue's execution timeout), so a
  query the client gave up on stops in PostgreSQL as well.
- **The Cube child's environment** is the inherited one minus every `CUBEJS_*`, `PG*` and `SXC_*` variable,
  `NODE_OPTIONS`, every variable whose value contains `postgres://` or `postgresql://` and the configured DSN
  variable names; plus the connection parsed from the Cube DSN, a random per-run API secret, development mode
  and telemetry off, `CUBEJS_LOG_LEVEL=error`, `CUBEJS_TESSERACT_SQL_PLANNER=true`, TLS settings from the DSN's
  `sslmode`, `SXC_PORT`, `SXC_MODEL_DIR` and, only when it is `1`, `SXC_CUBE_DEBUG`. `node` is looked up on
  `PATH` only.
- **The Cube SQL planner is pinned and recorded.** Cube 1.8.3 picks its SQL planner from
  `CUBEJS_TESSERACT_SQL_PLANNER` (default `true`, the Tesseract planner; `false` selects the legacy planner, which
  Cube warns it will remove). The runner sets `true` itself (`cube_run.PLANNER_ENV`), so neither Cube's implicit
  default nor an inherited value (dropped with every other `CUBEJS_*` variable) decides which planner serves a
  result. The legacy-planner SQL injection GHSA-cfx6-94gc-h774 affected Cube 1.7.x and earlier, not the pinned
  1.8.3; pinning the planner keeps a later Cube move from silently changing it. `serve` records the Cube version
  and the planner the child was started with (`cube_run.runtime_facts`: `cube_version`, `sql_planner`), the cube
  stage writes both into the `cube:meta` detail and `report` treats a cube result that recorded another version or
  planner, or neither, as stale (section 8). The value is the one the runner passes, not one read back from Cube;
  it lives in `cube_run.py`, which the engine fingerprint hashes.
- **The adapter listens on 127.0.0.1 only** and requires an HS256 JWT signed with the per-run secret. The client
  talks to it through an opener without proxies, so `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and the system
  proxy settings never see the JWT or filter values, and no `NO_PROXY` entry is needed. `cube-install` downloads
  still honour proxy settings (their content is hash-pinned).
- **Adapter logs are redacted.** `server.js` drops request logs (they contain filter values) and writes each
  error as one line, `SXC_CUBE_ERROR <event>: <class> [code] (category)`: no message text at all, because
  PostgreSQL quotes values in a style that depends on its message language ('x', »x«, « x »). The code is the
  SQLSTATE when Cube passes one (wrapped query errors usually carry none); the category is `timeout` or `model`, and only model compile errors keep their text
  (they quote the generated model, never rows). Startup, uncaught and
  unhandled errors use the prefix `SXC_ERROR`. Console output of the Cube packages is replaced by one
  `SXC_CONSOLE` notice. `SXC_CUBE_DEBUG=1` adds `SXC_DEBUG` lines with the full text and lets console output
  through; such a log can hold row values and must stay local. Results and startup error messages never include
  `SXC_DEBUG` lines.
- **Credentials only in the environment.** `source.dsn_env`, `source.owner_dsn_env` and `cube.dsn_env` hold the
  NAME of a variable (`[A-Za-z_][A-Za-z0-9_]*`); a value that looks like a DSN is refused without being echoed.
  A key named `dsn` or containing `password` is refused in `[source]` and `[cube]`. A malformed DSN is refused
  before anything prints it (exit 3, naming the variable only); messages show a DSN as
  `postgresql://user@host:port/database` at most.
- **TLS** follows the DSN's `sslmode` (section 3.1).
- **Supply chain.** The Cube adapter is installed with `npm ci --ignore-scripts` from a committed lockfile; the
  native module is downloaded over https (redirects must stay on https) from the Cube GitHub release and
  accepted only if its size and SHA-256 match `native-sha256.json`; only the regular file `native/index.node`
  is extracted. On Linux the glibc version is checked first (2.34 for x64, 2.30 for arm64). `installed.json`
  records the hashes of `server.js`, `package.json` and `package-lock.json`, so an install made from other
  adapter files counts as stale.
- **Programs** (`node`, `npm`, `java`) are looked up on `PATH` only (`procutil.which`), never in the current
  directory.
- **Generated SQL** uses validated identifiers (`ident.py`) and bound parameters; `sql_violation` predicates and
  gold SQL files are trusted code from the configuration and are labelled as such in reports.

### 3.1 TLS

| `sslmode` in the DSN | Python stages (pg8000) | Cube child |
|---|---|---|
| `disable` | no TLS | `CUBEJS_DB_SSL=false` |
| `allow`, `prefer` or none | TLS when the server offers it, unverified | `CUBEJS_DB_SSL=false` (no TLS) |
| `require` | TLS required, unverified | `CUBEJS_DB_SSL=true`, `CUBEJS_DB_SSL_REJECT_UNAUTHORIZED=false` |
| `verify-ca` | certificate checked against `sslrootcert` (or the system store when it is absent or `system`), no host name check | `CUBEJS_DB_SSL=true`, `CUBEJS_DB_SSL_REJECT_UNAUTHORIZED=true`, `CUBEJS_DB_SSL_CA=<absolute sslrootcert>`; Node.js then also checks the host name |
| `verify-full` | certificate and host name checked | as `verify-ca` |

Another `sslmode` value is exit 3 (`<VAR>: sslmode must be one of disable, allow, prefer, require, verify-ca,
verify-full`); an `sslrootcert` that cannot be loaded as a PEM file is exit 3; a relative `sslrootcert` is
resolved against the current directory. `sslcert`, `sslkey`, `sslpassword`, `sslcrl`, `sslcrldir` and
`sslcertmode` are refused with exit 3 (client certificates and revocation lists are not implemented). Other DSN
parameters are ignored.

## 4. Shared modules (stable contracts)

| Module | Contract |
|---|---|
| `ident` | `check`, `quote`, `qualified('schema.table')`, `split` |
| `canon` | `canon(v, kind=None)`: strict canonical form; `kind` is one of `KINDS = ('string', 'number', 'time', 'boolean')`, `None` compares untyped; `same(a, b, kind=None)`; `key(v)`: text exactly, other values untyped (RDF IRIs and key sets) |
| `config` | `load(path) -> Config` (dataclasses; see the module); `cfg.hierarchy(name='')`, `cfg.entity(name, ref)`, `cfg.cube_dsn_env()` |
| `db` | `connect(env_name, require_pg17=True)` (doctor passes `False`), `parse_dsn`, `redact`, `rows(conn, sql, **params) -> list[dict]`, `scalar`, `read_only(conn, ms, isolation=None)` (`None` or `"repeatable read"`), `write_tx(conn, ms)`, `server_version_num` |
| `procutil` | `which(name)`: absolute path of a program on `PATH`, never the current directory |
| `naming` | every generated name: closure/cycles tables, shape views, cube and member names, RDF IRIs, OWL property names |
| `closure` | `build(conn, cfg, h) -> (checks, sql_log)` inside the caller's transaction, which holds the output schema's build lock |
| `cube_gen` | `generate(cfg, conn) -> (model, notes)`, `members(model)`, `to_yaml`, `write_model(model, dir)`, `load_model(out)`, `where_sql(measure)`, `measure_cube(cfg, m, cols)`, `columns(conn, table)` |
| `cube_run` | `cube_home()` (absolute), `install_state(home) -> 'ok'\|'missing'\|'incomplete'\|'stale'`, `installed(home)`, `adapter_hashes()`, `serve(cfg, run_dir, log_path) -> CubeClient` (context manager; `client.runtime` is `runtime_facts` of the started child), `runtime_facts(version, env=None) -> {'cube_version', 'sql_planner'}`, `sql_planner(env) -> 'tesseract'\|'legacy'\|'unknown'`, `PLANNER_ENV`, `ADAPTER_DIR`, `MIN_NODE` |
| `cube_client` | `CubeClient.load(query) -> list[dict]` (polls "Continue wait", explicit limit 50,000, never `renewQuery`, no proxies), `meta()` |
| `compare` | `run_binding(conn, client, cfg, gold, params, cache=None, types=None)`, `compare_rows(gold, g_rows, c_rows, types=None)`, `member_kinds(model)`, `gold_kinds(gold, kinds)` |
| `sweep` | `bindings(conn, cfg, gold)`, `strata_names(gold)`, `md5_order(column)` |
| `shape_q` | `tests(cfg, model)`, `run_test(..., budget=None, witness=None)` (the baseline V3 run passes `verify.pair_key_budget` and, with `verify.existence_witness`, a `PairWitness` of each pair test), `pair_keys(conn, cfg, test, budget=None, witness=None)`, `PairWitness(conn, cfg, test)` (`result(pair_status)`), `witness_id(test)`, `witness_note()`, `sampled(test)`, `d95_keys(sampled, total)`, `sample_note(detail)`, `chunk_note(detail)`, `PAIR_KEY_CAP = 2000` (defined in `config`), `join_path(cubes, measure_cube, dim_cube)`, `additive_path(steps)`, `null_group_sql(cfg, test)`, `not_applicable(cfg, model)` |
| `mutate` | `OPERATORS`, `CLOSURE_OPERATORS`, `OUTCOMES`, `selected(cfg)`, `run_mutants(cfg, out, conn, model, suite, closure=None, sampled_items=())`, `verdict(status, item)`, `closure_status(cfg, out)` |
| `demo` | `build(dirty, seed, scale) -> {"parts","edges","orders","stock","malformed","expected"}`, `load(conn, data)`, `reference(expected, variant, seed, scale)`, `TABLES`, `TableMismatch` |
| `cube_replay` | `run(None, args, out) -> Result('cube_replay')`: actual model generation, named mutation, original/gold agreement and mutant/gold disagreement on owned synthetic schemas; hash-bound replay and scoped cleanup |
| `tlp` | `run(None, args, out) -> Result('tlp')`: strict versioned trusted spec, bounded synthetic fixtures, materialized predicate partitions, exact multiset/aggregate comparisons and hash-bound replay |
| `expect` | `run(cfg, args, out) -> Result('expect')`; `fixture_env(cfg, args)` (the guards before any connection) and `operational_variables(cfg) -> (compared, unset)`, `fixture_config(cfg, env)`, `compared_columns(gold, case)`, `judge(gold, case, gold_rows, cube_rows, types)`, `run_case(conn, client, cfg, gold, case, types)`, `fixture_relations(cfg, hierarchies)`, `fixture_digest(conn, cfg, hierarchies=()) -> {fixture_sha256, tables_sha256, rows}`, `share_locks(env, cfg, relations)` (context manager yielding whether the SHARE locks were taken) |
| `shacl_corpus` | `run(None, args, out) -> Result('shacl_corpus')`: fixed independent expected reports compared with actual pySHACL and Jena; bounded output/deadlines and owned process-tree cleanup. Since 0.9.0, `load_w3c_suite(root)` reads W3C core manifests (bounded, inside the suite, digested) and classifies each entry against the profile; `run_w3c` runs both engines on in-profile cases |

## 5. Generated objects (names from `naming.py`)

PostgreSQL, in the output schema:
- `<h>_closure(ancestor, descendant, min_depth, max_depth, path_count, qty)`: one row per pair; `qty` is the
  sum over all paths of the product of the edge quantities; edges are (parent, child) pairs with the summed
  quantity of their lines. Built level by level (no path enumeration); applied with MERGE.
- `<h>_cycles(start, length, path)`: example cycles (only when cycles exist).
- `v_shape_<id>`: key columns of the target rows violating shape `<id>`.

Cube (see `cube_gen.py` for the exact model):
- `<entity>` cubes; `<h>_closure` with `qty` (sum) and `pairs` (count); `<h>_ancestor` / `<h>_descendant`
  copies of the node cube; `m_<measure>` with `key` and `value` (pre-aggregated per key) or, with a
  `date_column`, a row-level cube with `key`, `ts` and `value`.

RDF/SHACL: `naming.rdf_*` helpers define the IRIs of entity classes, rows (reversible key encoding of
`canon.key` values, so two text keys never share an IRI), predicates (`<iri><entity>.<column>`), node shapes and
property shapes (`<iri>shape/<id>/p`; `<iri>shape/<id>/b` for the bounds of a shape with class and bounds;
since 0.11.0, `<iri>shape/<id>/pair` for the reason-time property-pair shape of `verify.shacl_pairs`).

## 6. Check catalogue

Every check id a result can hold. `<entity>`, `<h>`, `<id>`, `<gold>`, `<name>` and `<target>` are
configuration ids; `<member>` is a Cube member (`cube.member`); `<stratum>` is a stratum name, `values`,
`param_sets` or `all`.

| Stage | Check id | Passes when | Other outcomes |
|---|---|---|---|
| any | `stage` | never passes | `error`: the stage stopped before its own checks (`message` for exit 2 or 3, `reason=interrupted`, or `exception` and `sqlstate`) |
| any | `data_revision:stable` | build/reason/cube/verify configured start/end source and Cube revision hashes agree | `fail` on change; `error` when a probe cannot run; omitted and explicitly unbound without a probe |
| report | `data_revision:current` | error check only | `error` when the configured current revision cannot be read; missing or changed stage evidence is also stale |
| any | `runtime_evidence:stable` | opted-in build/reason/cube/verify actual full-pipeline evidence agrees at start/end | `fail` on change; `error` on a failed end probe; a failed start probe stops the stage |
| report | `runtime_evidence:current` | error check only | `error` when opted-in current runtime cannot be read; missing/changed stage binding is also stale |
| counterexample | `counterexample:search` | never proves general equivalence | `fail` with a reduced successful mismatch; `not_run` when bounded examples agree |
| counterexample | `counterexample:query`, `counterexample:recheck`, `counterexample:inputs`, `counterexample:artifact` | error checks only | `error` on query failure, unstable witness, changed inputs or a replay artifact exceeding 1 MiB; no new replay fixture |
| counterexample | `counterexample:audit_<arm>` | never passes; opt-in audit with fixed arm names `declared` and `relaxed`, unchanged finite values/types/row bounds, and PostgreSQL-confirmed disagreement | Declared arm: `fail` only for `declared_domain_disagreement`. Relaxed arm: `not_run`, `found=true`, `outside_declared_domain` only while satisfying the projected profile and violating the full declared profile. Either arm: `not_run` for bounded no-witness, incomplete generation or the other arm's replay (`not_replayed`); `error` on execution, reduction, confirmation or artifact-size failure. Outside evidence is not an automatic production defect, false-constraint finding or equivalence proof |
| counterexample | `counterexample:audit_inputs` | error check only | `error` (`inputs_changed_during_run`) when original SQL, spec, profile, manifest or projected-profile bytes change during execution; no new audit witness is published |
| counterexample | `counterexample:audit_publication` | error check only | `error` (`artifact_publication_error`) when final audit admissibility or atomic artifact publication fails; audit witness files are removed |
| counterexample | `counterexample:audit_` | composition prefix only, never emitted as a standalone result check | Appending the fixed arm name produces the arm-family IDs above; this prefix has no independent status. Audit errors dominate declared failures, diagnostics and no-witness outcomes; no audit outcome is `pass` |
| counterexample | `counterexample:mutation:search` | never proves equivalence or a real-data V4 verdict | `fail` when supplied original/mutant SQL differ on an admissible operator-directed synthetic fixture; `not_run` when no difference is found within the bound |
| counterexample | `counterexample:mutation:gold_agreement` | supplied original and independent gold SQL agree on the saved synthetic fixture | `fail` on disagreement; this does not establish correspondence to an actual Cube model |
| counterexample | `counterexample:mutation:query`, `counterexample:mutation:recheck`, `counterexample:mutation:inputs` | no synthetic detection is established by an error | `error` on execution failure, unstable witness or changed inputs; unsupported SQL relation/column schema is `not_run` |
| counterexample | `counterexample:metamorphic:<relation>` | `--metamorphic-sql` only (`<relation>`: `mr1_unfold`, `mr2_scale`, `mr3_disjoint`, `mr4_unit_node`): every admissible case with a non-vacuous transform agrees exactly with the relation's expected path-product answer | `fail` on a violated relation (replay fixture saved); `not_run` when vacuous in every case (`not_run_reasons`); with `bom_exhaustive` also `not_run` after a resource bound (`resource_bound`), an early stop (`stopped_at_first_failure`) or in a replay (`replay_covers_one_case`) |
| counterexample | `counterexample:metamorphic:oracle` | `--metamorphic-sql --scenario bom_exhaustive` only: every admissible case of the whole bounded enumeration (1,743 structures under 2 label placements) agrees exactly with the independent closure-oracle fold (qty only) | `fail` at the first disagreeing case (replay fixture saved); `not_run` when a case was not compared (`enumeration_incomplete`, e.g. `size_bound`), the run stopped at another check's failure, in a replay (`replay_covers_one_case`) or with no admissible case |
| counterexample | `counterexample:metamorphic:shape` | error check only | `error` when the SQL answer is not closure-shaped (`reason`: missing or duplicate columns, duplicate pair, NULL, non-text id, non-exact qty) |
| cube-replay | `cube_replay:comparison` | actual original Cube answers equal independent gold SQL, and actual named-mutant answers differ on the same frozen synthetic fixture | `fail` when either comparison condition is false; `not_run` for an unsupported operator; errors do not count as mutation detections |
| cube-replay | `cube_replay:runtime` | error or unavailable-runtime check only | `not_run` for missing dependencies or connection failure; `error` for runtime execution failure |
| cube-replay | `cube_replay:cleanup` | only this invocation's recorded schemas were removed | `error` for schema ownership changes, schema cleanup or connection cleanup failure |
| cube-replay | `cube_replay:artifact` | error check only | `error` when the bound successful replay fixture could not be written |
| cube-replay | `cube_replay:inputs` | error check only | `error` when replay file bytes change during execution; no successful fixture is retained |
| tlp | `tlp:consistency` | every bounded comparison has the same complete row multiset and exact COUNT or SUM after partitioning by predicate TRUE, FALSE and NULL | `fail` when a complete row or aggregate comparison disagrees; a pass proves internal partition consistency only |
| tlp | `tlp:engine` | unavailable-engine check only | `not_run` when PostgreSQL cannot be reached |
| tlp | `tlp:execution` | error check only | `error` for invalid/unsupported answers, timeout, unstable execution, changed spec/generator/replay inputs or artifact failure |
| tlp | `norec:consistency`, `norec:engine`, `norec:execution` | since 0.10.0, `--oracle norec` selects independent WHERE and CASE counts over a bounded relation, `count(*)` only | strict integer counts; missing/error/unstable execution cannot pass; oracle-bound replay; outside operational V1-V5 |
| expect | `expect:<gold>[<i>]` | the gold SQL and the gold's Cube query, run on the fixture with case `<i>`'s parameters, both equal the case's expected rows | `fail` (`reason` `common_mode`: gold and Cube agree but differ from authored rows; `regression`: they agree but differ from captured rows; `gold_mismatch`, `cube_mismatch`, `both_mismatch`: they disagree); `error` (`side`, error type, SQLSTATE, HTTP status; `expected_duplicate_keys`: expected keys equal in their typed form); `not_run` with a `not_proven` line (`fixture_mismatch`, `closure_not_passed`, `cube_not_passed`, `gold_members_not_passed`, `fixture_changed`, `fingerprint_failed`, `fixture_unreadable`) |
| expect | `expect:build`, `expect:cube` | the fixture's closure checks passed in the inner build; `cube:meta` passed in the inner cube stage | the worst closure status (`fail`, `error`); the `cube:meta` status (`error` when it is missing) |
| expect | `expect:fixture` | the fixture's source tables hashed the same before the build and after the cases, and its source and built closure tables the same before and after the cases (`fixture_sha256` of the source tables, `locked`, `operational_variables_compared`) | `error` (`fixture_changed`, `fingerprint_failed`); `not_run` (`no_case_ran`): every case was gated |
| expect | `expect:cases` | never passes | `not_run` (`no_cases`): no `[[gold.expect]]` case in the configuration |
| shacl-corpus | `pyshacl:<case>`, `jena:<case>` | each actual engine independently matches the case's hand-written canonical constraint-report multiset | `fail` on a constraint, path, severity or semantic value mismatch; `error` on timeout, nonzero engine exit, malformed/missing report or cleanup failure; valid conforming empty reports are compared explicitly |
| shacl-corpus | `w3c:<engine>:<case>` | since 0.9.0, with `--w3c-suite`: the engine (`pyshacl` or `jena`) matches the W3C suite's expected report for an approved core `sht:Validate` entry inside the sxc profile (canonical multiset; absent path/value compared, `sh:sourceShape` ignored) | `not_run` with `reason` for every entry outside the profile, and for every entry of an unpinned suite without `--allow-unpinned-suite` (`suite_not_pinned`, `duplicate_entry`, `unsupported_component`, `complex_path`, `non_iri_focus`, ...); `fail` on a mismatch; `error` as for the fixed corpus |
| shacl-corpus | `w3c:suite` | never passes | `not_run` (`suite_not_pinned`): an unpinned suite ran under `--allow-unpinned-suite`, so its expected reports are not verified as the W3C suite's and the stage cannot pass |
| doctor | `python` | Python >= 3.11 | `fail` |
| doctor | `pg8000` | pg8000 can be imported | `fail`, with the absolute pip command as the next action |
| doctor | `dsn` | the check-role DSN variable (`source.dsn_env`, or `SXC_PG_DSN` without a configuration) is set | `fail`; the database checks after it are `not_run` (`no_dsn`) |
| doctor | `connect` | a connection opens | `fail` (`reason` `bad_dsn`, `rejected` with SQLSTATE, or `unreachable`; later checks `not_run`, `no_connection`); `not_run` (`no_pg8000`) |
| doctor | `pg_version` | server_version_num >= 170000 | `fail` |
| doctor | `privileges:temporary` | the check role may create temporary tables in the database | `fail` |
| doctor | `privileges:select:<entity>` | USAGE on the schema and SELECT on the table | `fail` (a missing table gets a load hint, not a grant hint) |
| doctor | `privileges:no_write:<entity>` | no write privilege, no ownership or owner membership of the table or its schema, no CREATE on the schema | `fail` (detail `held`, `member_of_owner`, `schema_owner`, `schema_create`); `not_run` (`table_missing`) |
| doctor | `output_schema` | the output schema exists with CREATE and USAGE, or the role may create it | `fail` |
| doctor | `database` | never passes | `error`: a catalog query failed (exception type, SQLSTATE) |
| doctor | `reason:owlready2`, `reason:pyshacl`, `reason:java` | with `--require reason`: the packages import; Java 11 or later (`SXC_JAVA` or `java` on `PATH`) | `fail`; without `--require reason` a missing component is a `not_proven` line instead of a check |
| doctor | `cube:node`, `cube:adapter`, `cube:native` | with `--require cube`: Node.js 22 or later on `PATH`; the adapter installed from this checkout's adapter files; `index.node` matches the install record and the pin | `fail` (adapter `reason`: `missing`, `no_install_record`, `incomplete`, `adapter_files_changed`, `adapter_files_missing`; native `reason`: `no_install_record`, `missing`, `hash_mismatch`, `no_pin`, `pin_mismatch`); without `--require cube` a `not_proven` line |
| demo | `demo:rows:<table>` | the table holds exactly the generated rows (for `sales_order`: the valid orders) | `fail` |
| demo | `demo:copy_on_error` | `COPY ... ON_ERROR ignore` skipped exactly the malformed rows (0 clean, 2 dirty) | `fail` |
| demo | `demo:load` | never passes | `error`: the load was rolled back (SQLSTATE) |
| build | `shape:<id>` | no violations (a `warning` shape passes and reports its count) | `fail`; `error` (SQLSTATE; the view is dropped, the other views still build) |
| build | `closure:<h>` | acyclic (or `on_cycle = "report"`) within `max_depth` | `fail` (`reason` `cycle` or `depth_limit`); `error` (SQLSTATE); `not_run` (`excluded_by_option`: left out by `--hierarchy`) |
| build | `closure_apply:<h>` | after the MERGE the table equals the recomputation in both directions | `fail` |
| reason | `shacl:<id>` | pySHACL's focus nodes equal the keys in `v_shape_<id>` | `fail` (`shacl_only`, `sql_only`); `error` (SQLSTATE or `validation_failure`); `not_run` (`shape_not_built`, `view_missing`, `resource_limit`) |
| reason | `shacl:meta:generated` | since 0.10.0, generated shapes conform to the pinned shacl-shacl syntax subset before source export | `fail` for nonconformance, sanitized `error` for parse/engine/report failure; affected comparisons `not_run` (`generated_shapes_invalid`), independent OWL may proceed |
| reason | `shacl:pair:<id>` | since 0.11.0, opt-in `verify.shacl_pairs`: for a shape whose `sql_violation` is exactly `t.<a> <op> t.<b>`, pySHACL's focus nodes for the generated `sh:lessThan` or `sh:lessThanOrEquals` property shape equal the keys in `v_shape_<id>` | `fail` (`shacl_only`, `sql_only`); `error` (SQLSTATE or `validation_failure`); `not_run` (`generated_shapes_invalid`, `shape_not_built`, `view_stale`, `view_missing`, `column_type`, `non_finite_values`, `duplicate_keys`, `resource_limit`) |
| reason | `owl:consistency` | HermiT finds the ontology consistent | `fail`; `error` (SQLSTATE or `reasoner_error`); `not_run` (`resource_limit`) |
| reason | `owl:explanation` | requested same-HermiT explanation completed all retained-deletion checks over generated axiom groups; original consistency failure remains; detail `uniqueness` is `unique` only when removing each retained group alone from the full set is consistent, `not_unique` when one removal stays inconsistent | `not_run` on caps, deadline, initial-consistent disagreement or reasoner/worker errors; no independent or global-minimum proof; `uniqueness` stays `not_proven` when its checks hit caps, deadline or errors, or groups repeat an axiom |
| grain | `grain:c` | composition prefix only; never emitted as a standalone check | Adding an ordinal and obligation produces the contract/evidence families below |
| grain | `grain:c<case>:contract` | independently authored source/group/output/key/filter/null-policy operation agrees with the supported derived fragment | `fail` on a declared mismatch; malformed or unsupported requests are configuration errors |
| grain | `grain:c<case>:evidence` | actual bounded visible source/node key and join obligations all completed and passed | `fail` on null/duplicate keys, orphan/fanout or disallowed exclusion; `not_run` on empty/unsupported/oversized scope |
| grain | `grain:execution` | never passes | `error` on incomplete SQL, input binding or connection cleanup |
| shacl-repair | `fixture:<case>:profile` | local graph and shapes are inside the supported nonrecursive profile | `not_run` for unsupported profile; `error` for malformed input |
| shacl-repair | `fixture:<case>:before`, `fixture:<case>:after` | actual normalized report bags equal independent authored expectations | `fail` on disagreement; `error` on validation/protocol failure |
| shacl-repair | `fixture:<case>:retention` | explicit shape targets remain targeted before and after | `fail` when an obligation is absent or erased |
| shacl-repair | `fixture:<case>:immutable` | declared immutable predicate/triple facts are preserved exactly | `fail` on missing obligations or changed hard facts |
| shacl-repair | `fixture:<case>:edits` | finite add/remove costs recorded separately from hard obligations | descriptive pass only; never overrides any other failed gate |
| shacl-repair | `fixture:execution` | never passes | `error` on incomplete real pySHACL execution or output validation |
| reason | `v5:closure:<h>` | the OWL closure pairs equal the closure table | `fail`; `not_run` (`closure_not_passed`, `cycle_edges_dropped`, `closure_table_missing`, `inconsistent`, `owl_not_run`, `resource_limit`) |
| reason | `v5:defined:<name>` | the members of the defined class equal its SQL definition (a class without members on either side passes and is listed in `not_proven`) | `fail`; `not_run` (as `v5:closure:<h>`; `contains_class` reads the closure table) |
| reason | `v5:witness:<name>` | opt-in synthetic expected memberships agree with both HermiT and the configured SQL translation; includes nonmember controls under open-world semantics | `fail` on any disagreement; `error` on execution failure; `not_run` for a configuration gap, an impossible positive, inconsistent synthetic ontology or resource limit; `<name>` is `scope` when no definitions are configured |
| reason | `scope:shacl`, `scope:owl` | never passes | `not_run` (`excluded_by_only`): the part `--only` left out |
| cube | `cube:model` | the model was generated (a generator error is exit 2 instead) | none |
| cube | `cube:gold_members:<gold>` | every member the gold entry names exists in the model and is public | `fail` (`missing`, `hidden`, `malformed` counts) |
| cube | `cube:meta` | Cube's `/meta` lists exactly the public members of the model | `fail` (`missing`, `extra`); `error` (HTTP status, connection error, unexpected response). Every outcome records `cube_version` and `sql_planner` of the adapter that answered |
| verify | `v1:<gold>` | the first binding matches exactly | `fail`; `error` (gold or Cube side: error type, SQLSTATE, HTTP status, `cube` `timeout`/`row_limit`/`query_error`; `members_not_in_model`; `bindings_failed`); `not_run` (`closure_not_passed`, `no_binding`) |
| verify | `v2:<gold>:<stratum>` | every binding of the stratum matches exactly; an empty stratum passes (`empty_stratum`) and is listed in `not_proven` | as `v1:<gold>`, without `no_binding` |
| verify | `v3:total:<member>` | the Cube total equals SQL computed from the configuration (or the closure table) | `fail`; `error`; `not_run` (`closure_not_passed`) |
| verify | `v3:additive:<member>:<member>` | the groups of the measure by the ontology class column of a cube joined through many_to_one / one_to_one steps only, the NULL group included, add up to its total, and the NULL group equals independent SQL over the measure source (`null_group_equal`: an anti-join, or the matched rows without a class when Cube roots the query at the dimension's cube) | as `v3:total:<member>`; a join path with a one_to_many or unknown step gets no check, only a `not_proven` line (not applicable) |
| verify | `v3:pair:<member>+<member>` | the two measures queried together equal the same measures queried alone, per key | as `v3:total:<member>`; since 0.9.0 also `not_run` (`keys_not_answered`): a chunked test whose Cube answers left out keys it asked for |
| verify | `v3w:pair_witness:<member>+<member>` | since 0.11.0, opt-in (`verify.existence_witness`), for a sampled or chunked `v3:pair:<member>+<member>` test: every asked key the together answer holds but an alone answer leaves out has no row satisfying the measure's `cube_gen.where_sql` in two PostgreSQL reads that bracket Cube's queries, and its together value is NULL (`absent_no_facts`); passes with nothing absent when every key was answered | `fail` (`absent_with_facts`: a row in both reads, for the closure `qty` a closure row; `witness_value_mismatch`: no row but a non-empty together value); `not_run` (`witness_incomplete`: a key missing from the together answer, a failed cast or statement, a change between the two reads; `pair_error`: the witnessed pair test is an `error` after its key read). The pair test's own status, detail and V4 role are unchanged, also when the witness fails; an unfiltered pair test (or one whose key read failed) gets no witness, not even when it errors |
| verify | `v3:closure_oracle:<h>` | raw edges independently recomputed in Python match every stored closure pair's exact quantity, logical path count and minimum/maximum depth in one read-only REPEATABLE READ snapshot | `fail` on differences, error-mode cycles or an exceeded configured depth; `error` on query failure or an internal oracle defect (`scc_removal_left_cycle`); `not_run` for a failed build gate, unsupported keys/quantities or exceeded resource bounds. In `on_cycle = report` mode the oracle removes the edges inside strongly connected components itself and records `cycle_handling`, `dropped_cycle_edges` and `cycle_nodes` (informational, never compared with build's statistics); a pass that removed edges is listed in `not_proven` |
| verify | `v4:<operator>:<target>` | the mutant was killed: a suite item failed against it (`killed_by`) | `fail` (it survived); `error` (`suite_error`, `startup`, `cube`, `write`, `model_mismatch`, `witness`); `not_run` (`closure_not_passed`, `suite_incomplete`). Equivalent and not applicable mutants get no check, only a `not_proven` line |
| verify | `v4:suite` | never passes | `not_run` (`no_passing_suite_item`): no suite item passes on the unmutated model |
| verify | `scope:v1`, `scope:v2`, `scope:v3`, `scope:v4`, `scope:gold:<gold>` | never pass | `not_run` (`excluded_by_only`, `excluded_by_gold`): the part or gold query a narrowed run left out |
| report | `stage:<name>` | the stage (`doctor`, `demo`, `build`, `reason`, `cube`, `verify`) ran, is current and passed | its own status; `not_run` with `reason` `missing` (a required stage without a result) or `stale` |
| cube-install | `cube_install:npm_ci`, `cube_install:native_sha256`, `cube_install:extract`, `cube_install:smoke` | each install step passed | `fail` or `error`; the steps after a failed one are `not_run` |
| cube-install | `cube_install:already_installed` | an install matching the pins and this checkout's adapter files exists, so nothing was reinstalled | none |

The standalone CLI can additionally emit `diagnostic:output` as an error when an output cannot be written safely. It does not expose the path or turn completed checks into a passing stage. Repair case ordinals and six worker gates are composed dynamically; the catalogue test resolves their format-prefix concatenation.

## 7. Verification semantics

**Independent runtime checks.** Standalone `cube-replay`, `tlp` and `shacl-corpus` use synthetic inputs and
retain separate Result stages. Their successes never promote real-data V4, operational V1–V5 or the global
report's coverage. Each preserves explicit `not_proven` scope limits, and missing engines, partial
comparisons, timeouts and malformed output cannot pass.

Cube replay uses the existing compiler and mutation implementation, runs both actual Cube models, and
requires original/gold agreement plus mutant/gold disagreement. Its dedicated database is selected only
through `SXC_CUBE_REPLAY_DSN` or `--dsn-env`; explicit `--allow-scratch-schema` permits creating and
removing this invocation's private synthetic schemas. Schema names and OIDs are recorded for scoped
cleanup. A fixture is saved only after successful comparison and cleanup; hashes bind its operator,
generator, seed and exact data. This does not certify arbitrary supplied SQL or production models.

TLP evaluates a strict versioned relation/predicate/COUNT-or-SUM specification on bounded synthetic
fixtures, materializing each relation and predicate for a stable comparison. TRUE, FALSE and NULL
partitions must preserve duplicates and NULL-sensitive aggregates, including empty/all-NULL SUM.
Exact decimals never pass through floating point. Spec and replay hashes are checked before retaining
evidence. Trusted SQL and custom functions retain the counterexample trust boundary: a read-only
transaction and fixture CTEs do not constitute a SQL sandbox or prove business-query correctness.

The SHACL corpus uses the production shape generator and RDF exporter, with independent literal
expectations for class, bounds, NULL/no-triple, decimal, combined class+bounds and colliding value-group
cases. Actual pySHACL and Jena must each match focus/path/component/severity/value multisets; result
blank-node names, source-shape labels and diagnostic messages are ignored. Missing reports are distinct
from valid conforming empty reports. Both engines have bounded stdout/stderr, a per-case deadline and
owned descendant cleanup. Jena is an external optional runtime; the verified Jena 6.2.0 test distribution
uses a Java 21-compatible runtime, while HermiT's main Java 11 minimum is unchanged. Since
0.9.0, `--w3c-suite` takes expected reports from the W3C SHACL core test suite instead, so they cannot
share a mistake with the fixed corpus. Both engines validate the suite's own shapes and data graphs: the mode
checks engine conformance on the components sxc emits, not sxc's exporter, so it complements the fixed corpus
(run both) and says so in `not_proven`. Only approved entries inside the sxc profile run: the components and
severities `shapes.shapes_ttl()` emits (sh:class and the three numeric bounds, one Violation or Warning
severity; since 0.11.0, also sh:lessThan and sh:lessThanOrEquals with an IRI value on property shapes,
which `reason` generates for `verify.shacl_pairs`) on IRI focus nodes, through sh:targetClass, sh:targetNode, implicit class targets or node-level
shapes, with no path or one IRI path; everything else is `not_run` with a reason id. The suite is pinned by a
SHA-256 over the sorted relative paths and bytes of the files read; a different digest runs no engine unless
`--allow-unpinned-suite`, and then the `not_run` check `w3c:suite` keeps the stage from passing. It is read
at test time, never vendored. Details and scope
are in `docs/cube-replay.md`, `docs/tlp.md` and `docs/shacl-corpus.md`.

**Literal expected rows (`expect`).** When the gold SQL and the configuration share a
mistake they agree and V1 passes. `[[gold.expect]]` cases add rows a person wrote from the business
specification for a small fixture, the literal expected-row form of dbt and SQLMesh unit tests; closures are
recursive and cannot be mocked, so both sides really run. `expect` is the first stage that runs the operational
gold entries and model on other data, so it keeps the standalone rule: it is reported separately, `report` never
reads it, and no pass widens operational V1–V5 or report coverage.

- Guards before anything connects: a configuration without cases is `expect:cases` `not_run`; otherwise
  `--allow-fixture-database` is required (exit 2), the `--dsn-env` value must be a variable name (exit 2) and the
  variable (`SXC_EXPECT_DSN`) must be set (exit 3). The fixture DSN is refused (exit 2, no value printed) when it
  is, equals, or names the same host, port and database as, an operational DSN variable (`source.dsn_env`,
  `source.owner_dsn_env`, `cube.dsn_env`) set in this environment. Another name for the same server is not
  detected, so the fixture must be a dedicated database: `expect:fixture` records `operational_variables_compared`,
  and `not_proven` says what was compared and names the variables that were not set.
- The user loads the fixture (the source tables and a few rows, for example `examples/expect/fixture.sql`); sxc
  never runs fixture SQL and creates no scratch schema. The fixture's source tables are fingerprinted first
  (`fixture_sha256`); a case whose optional `fixture_sha256` pin differs is `not_run` (`fixture_mismatch`). A copy of
  the configuration whose DSN variables all name the fixture variable (data revision and runtime probes off) runs
  `stage_build.run` and `stage_cube.run` into `<out>/expect/`: build writes its views and closure tables into the
  fixture database's output schema (what the consent permits), where they stay until the next run rebuilds them, and
  the operational `build/` and `cube/` results are never touched. All cases of all gold entries share the one fixture
  database. `expect:build` mirrors the closure checks (shape results are counted, not gated) and `expect:cube` the
  `cube:meta` check with its Cube version and planner.
- The inner cube stage starts Cube for `cube:meta`; one more Cube process then serves the cases. For each case the gold SQL (READ ONLY, bound parameters) and the
  gold's `cube_query` run with the case's `params`; a Cube row without a requested member is an error. Each side is
  compared with the expected rows by `compare.compare_rows`, typed by the model's member kinds: gold strictly (its
  SQL coalesces the `zero_if_null` columns itself, and a column it does not return never matches a NULL), Cube
  with the declared `zero_if_null` equivalence, and gold with Cube as in V1. A partial case compares only the
  columns its rows name.
- `expect:<gold>[<i>]` passes only when both sides equal the expected rows. It fails with `reason`
  `common_mode` when gold and Cube agree with each other but not with the rows of an authored case, `regression`
  when they agree but not with captured rows (only authored rows with a `spec` decide `common_mode`), and
  `gold_mismatch`, `cube_mismatch` or `both_mismatch` when they disagree; `gold_equals_expected`,
  `cube_equals_expected` and `gold_equals_cube` say which side matched. Counts follow `compare_rows` per pair
  (`gold_vs_expected`, `cube_vs_expected`, `gold_vs_cube`). A side that cannot run is `error` (type, SQLSTATE or
  HTTP status); so are expected rows whose keys are equal in their typed form (`expected_duplicate_keys`). A case is
  `not_run` when a closure it uses did not pass (`closure:<h>` or `closure_apply:<h>`; `closure_not_passed`), Cube
  did not serve the model (`cube_not_passed`) or its members did not pass `cube:gold_members:<gold>`
  (`gold_members_not_passed`); every `not_run` case gets a `not_proven` line. The next action for `common_mode`
  starts with checking that the fixture rows and the expected rows follow the cited specification: a wrongly loaded
  fixture gives the same verdict, and changing the gold SQL and the configuration to match it would create the shared
  mistake this stage looks for. Captured rows need not cite a specification and come from a run, so the next action
  for `regression` starts with checking that the loaded fixture is the one the rows were captured on (compare
  `expect:fixture`'s `fixture_sha256` with the case's pin, or pin it).
- `expect:fixture` hashes the row count and an in-database md5 of every source table before the build, and of every
  source and built closure table before and after the cases, each digest in one REPEATABLE READ snapshot. While the
  cases run, a second session holds SHARE locks on those tables (as `cube-replay` does), so a writer waits; locks
  that cannot be taken (for example a role without UPDATE, DELETE, TRUNCATE or MAINTAIN, or a conflicting lock held
  past `source.statement_timeout_ms`) are recorded (`locked: false`) and disclosed in `not_proven`. A changed digest
  (`fixture_changed`) or a failed fingerprint (`fingerprint_failed`) is an error and turns every passing case into
  `not_run`. The digests show equality at those points, not one shared snapshot, cover only the entity and built
  closure tables, and the gold SQL and Cube read in separate sessions; `not_proven` says so. `fixture_sha256` covers
  the source tables only and is not salted, so the result can confirm guessed fixture rows: keep the fixture
  synthetic. A case stores it as a pin compared across runs and databases, so each digest orders the row text by
  `COLLATE "C"` and fixes `TimeZone`, `DateStyle`, `IntervalStyle`, `extra_float_digits`, `bytea_output` and
  `lc_monetary` with `SET LOCAL` in its transaction: a pin depends on the source tables' names, their rows and the
  order and types of their columns (and on the database encoding for text outside ASCII), not on the database's
  default collation or the session's settings. The digest format is versioned (`sxc-expect-fixture-v2`).
- Captured passes carry `independent: false` and a `not_proven` line: their rows come from a run, not from a
  specification, and a pass of captured cases only says so in its next action. Authored rows are independent only as
  far as they follow the cited specification. Results hold counts, ids and the fixture hash, never rows, parameter
  values or DSNs. The cases live in the operational `crosscheck.toml`: editing one changes `config_sha256`, so
  `report` treats earlier operational results as stale until they are rerun.

**Independent closure oracle.** V3 also reads raw edge lines and the closure table in a single
read-only REPEATABLE READ transaction, before starting Cube. A reverse-topological Python fold
computes expected results with exact rational arithmetic, without invoking the builder's SQL or
reading its temporary tables. Duplicate parent/child lines form one logical edge with summed
quantity: path counts count paths through those logical edges. SQL SUM ignores NULL products and
an all-NULL sum remains NULL. Null endpoints are omitted, as in build. Both minimum and maximum
depth are compared. Empty input is explicitly disclosed as lacking nonempty coverage.

This check is bounded, not sampled: limits are 50,000 raw lines, 100,000 stored/expected pairs,
10,000 nodes and 2,000,000 counted operations, with numeric-size bounds in `closure_oracle.Limits`.
No matching prefix can pass when a limit is exceeded. A raw cycle fails in `on_cycle = error` mode.
In `on_cycle = report` mode the oracle reproduces build's rule independently: an iterative (non-recursive)
Tarjan search, charged to the operation budget, finds the strongly connected components of the logical
edge graph; every logical edge whose parent and child share a component (self-loops included) is removed
and the rest is folded as above, depth limit and NULL semantics unchanged. A cycle left after removal is
an internal `error`, never a pass. `dropped_cycle_edges` and `cycle_nodes` are informational; the verdict
is the exact closure comparison, not agreement with build's counts, and a pass discloses the removal in
`not_proven`. Only compatible built-in endpoint types with deterministic SQL equality are supported;
character padding, citext, domains and nondeterministic collation are refused. These database-only checks are not replayed as V4
Cube mutation suite items. They do not create a shared snapshot with Cube or other stages.

**Synthetic OWL definition witnesses.** `reason --synthetic-witnesses` adds separate small graph
checks for the configured definition kinds. Expected memberships and closure pairs are specified
independently of both translations; SQL relation bindings become bound-value CTEs and HermiT keeps
the configured ontology axioms. These are logical translation checks on synthetic text keys,
not tests of all production column types. Nonmember controls express lack of entailment, never
explicit OWL negation. An impossible positive example is not repaired by removing an axiom.
Synthetic success leaves real-data empty-class `not_proven` entries intact.

**Operator-directed SQL fixtures.** `counterexample --mutation-operator` uses small deterministic
templates on the fixed four-table synthetic schema, instead of the ordinary scenario generator.
Original and mutant SQL must return different successful answers to establish a distinction.
When `--gold-sql` is supplied, gold observation also requires original/gold agreement and
mutant/gold disagreement. The same condition is preserved while shrinking; query errors and
invalid fixtures are not detections. SQL, operator, generator, seed and exact integrity-profile
bytes are bound in replay. This command neither verifies that supplied queries came from the
selected Cube operator nor changes the real-data V4 verdict. Qualified external references in
trusted SQL can still read real relations; use a dedicated test database.

**V1/V2 compare.** Gold rows come from the gold SQL (read as UTF-8 with or without a BOM, one prepared
statement, bound parameters, read-only transaction; the parameter names `stream` and `types` are reserved). Cube
rows come from `cube_query` after substituting every string equal to `:<param>` (also inside lists and tables).
`map` renames Cube members to gold columns and must cover exactly the queried members. Each mapped column is
compared in the form of its Cube member type in `cube/model.json`:

| Member type | Canonical form |
|---|---|
| `string` | text exactly as written: `'1.10'` differs from `'1.1'`, `'1e3'` from `'1000'`, `'true'` stays text; a gold value that is not text takes its untyped form |
| `number` | exact decimals: `'2.50'` equals `2.5`, `'1e3'` equals `1000`; no tolerance |
| `time` | `YYYY-MM-DDTHH:MM:SS.ffffff` in UTC: a date equals its midnight, an aware timestamp is converted to UTC, Cube's `YYYY-MM-DDTHH:MM:SS.mmm` is parsed (naive values count as UTC) |
| `boolean` | booleans; the texts `true`/`false` and `t`/`f` become booleans |

Measures are numbers unless their type is `string`, `time` or `boolean`; members without a known type are
compared untyped (decimals normalised, numeric-looking text folded, other text exact). A value the type cannot
read keeps its untyped form and so differs from every value the type did read. Rows are matched on the canonical
key and a duplicate key on either side fails. For matched rows every column must be canonically identical; the
only declared equivalence is `zero_if_null` (a Cube NULL counts as 0 for those columns). Both sides empty is a
pass with `empty = true`. A gold query whose Cube members belong to a hierarchy whose `closure:<h>` did not pass
in `build` is `not_run`. Detail keys: `gold_rows`, `cube_rows`, `matched_rows`, `missing_in_cube`,
`extra_in_cube`, `duplicate_keys_gold`, `duplicate_keys_cube`, `mismatched_columns`, `empty`,
`declared_null_as_zero` (V2 adds `bindings`, `failed_bindings`, `error_bindings`, `empty_bindings`,
`rows_compared` and the positions of failed bindings).

**Bindings.** No parameters: one binding in stratum `all`. `param_sets`: stratum `param_sets` with each set.
One swept parameter: `values` (stratum `values`), `values_of = "entity.column"` (every distinct non-null value,
stratum `values`), or `strata` sampled from the hierarchy (k = ceil(sweep_k / number of strata) per stratum,
deterministic order `md5(id || seed) COLLATE "C", id`):

| Stratum | Nodes (node ids = node-entity keys plus edge endpoints) |
|---|---|
| `top` | has children, no parents |
| `mid` | has parents and children |
| `leaf` | has parents, no children |
| `leaf_parent` | has children, and none of its children has children |
| `deep` | has children; ordered by the depth of its subtree (deepest first) |
| `shared` | has at least two distinct parents |
| `empty` | node-entity keys without children |

V1 compares the first binding; V2 compares every binding of each stratum. An empty stratum is `not_proven`, not
a failure; a gold query without any binding is `not_run` (`no_binding`) in V1.

**V3 query shapes** (derived from the model): `total` compares each measure's Cube total with SQL computed from
the configuration (`cube_gen.where_sql`), and the closure cube's `qty`/`pairs` with the closure table (a NULL
count total counts as 0, flagged `null_as_zero`); `additive` checks that the groups by the ontology class column
add up to the total; `pair` checks that measures queried
together (the closure `qty` with each closure-joined measure, and every two measures joined to the same node
cube) equal the same measures queried one at a time, per key, which exposes join fan-out. Pair-test keys are
typed like V1/V2 keys. A pair test whose key source (the node table key, or the closure end) has more than
`shape_q.PAIR_KEY_CAP` = 2,000 distinct values compares a deterministic sample of 2,000 keys (ordered by
`md5(key || seed) COLLATE "C", key`), read once (the key count and the sample in one `REPEATABLE READ` snapshot)
and filtered with an `equals` filter in each of its queries; the detail gains `sampled_keys` and `total_keys`, and
`not_proven` says `compared 2000 of N keys`. The cap is fixed. The detail also gains the sample's detection bound
`d95_keys`, the smallest D >= 1 with C(N - D, n) / C(N, n) <= 0.05 for n sampled out of N keys (zero-acceptance
hypergeometric sampling, computed exactly on integers), and `d95_rate` = D / N; for n = 2,000 the bound is 6 at
N = 5,000, 29 at N = 20,000, 149 at N = 100,000 and 1,496 at N = 1,000,000. The `not_proven` line keeps its prefix
and adds that a mismatch affecting at least D keys (r%) is detected with 95% probability, assuming the mismatching
keys are independent of the fixed md5 sample order, and that fewer mismatching keys may be missed. Since 0.9.0
it also says that the NULL key group is not compared in a sample: the `equals` filter leaves it out, so
the bound covers the non-NULL keys only. Since 0.10.0, sampled V3 and V4 replay require every requested
key in the together and every alone answer. Confirmed mismatches/duplicates still fail; otherwise missing answers
are `not_run` (`keys_not_answered`). `answered_keys` counts requested keys present in every response, with each
dimension's canonical type. Incomplete samples carry no D95 bound and their not-proven note reports requested
versus fully answered keys without claiming a detection probability. A complete sample retains the existing bound.

The completeness contract is intentionally conservative: standalone measure cubes may legitimately omit node
keys with no matching fact rows. Above the sampling cap, such sparse data can therefore become `not_run` even
without a Cube defect. The harness cannot distinguish legitimate absence from a missing answer without independent
existence evidence. It does not restore a pass by substituting NULL; the unfiltered path at or below the cap is unchanged.
The opt-in existence witness below supplies that evidence as a separate check; the pair test's own status stays.

**Existence witness** (since 0.11.0; limitation M5). With `verify.existence_witness = true` (default
`false`, which changes no output) the baseline V3 run gives every sampled or chunked pair test a separate check
`v3w:pair_witness:<member>+<member>` (`shape_q.PairWitness`). An alone query reads the measure cube by its own key
column (`SELECT by, agg(column) FROM table t WHERE where_sql GROUP BY by`, no join), so its answer has a row for a
key exactly when a source row with that key satisfies `cube_gen.where_sql(m)`. The witness asks PostgreSQL that
condition directly: `SELECT DISTINCT t.<by> FROM <table> t WHERE <where_sql> AND t.<by> IN (:k0, ...)`, binding
the texts Cube's `equals` filter receives, untyped as Cube's PostgreSQL driver sends them, so PostgreSQL casts them
to the key column's type as it does for Cube (`'01'` finds the integer 1); answers are canonicalised with the alone
dimension's kind. The first read runs in the key-read `READ ONLY REPEATABLE READ` transaction of `pair_keys`, one
statement per alone measure and slice of 2,000 asked keys, each under a savepoint so a failed statement does not
abort the key read. The second runs after the sample's or chunk's Cube queries, in a new read-only `REPEATABLE
READ` transaction, for the absent keys only. The closure cube's `qty` gets no first statement: its alone query reads
the closure table by the column the keys came from, without a filter, so in the key-read snapshot every asked key
has closure rows and the key read is its first read. Its second read asks the closure table again (`SELECT DISTINCT
t.<column> FROM <closure table> t WHERE t.<column> IN (...)`), so a closure rebuilt between the reads, for example
by a concurrent `build`, is `witness_changed`, not a failure. Only asked keys present in the together answer and
missing from an alone answer are classified, one label per key and measure whose alone answer left the key out (an
absence): `absent_no_facts` (no row in either read and an empty together value), `witness_value_mismatch` (no row
but a non-empty together value), `absent_with_facts` (a row in both reads) and undecided (`witness_type`: a failed
cast, SQLSTATE class 22; `witness_error`: another failed statement, a failed savepoint command or second-read
transaction, or a lost connection; `witness_changed`: the two reads disagree; `not_witnessed`: no configuration
measure backs the member). An asked key missing from the together answer is labelled once, per key
(`together_absent`): a fact-table witness cannot tell why the together query left it out. The empty together value
is NULL for every aggregate, since count is re-aggregated as a sum of per-key counts (`cube_gen.measure_cube`);
`tests/test_pair_witness_pg.py` pins it through Cube 1.8.3 and PostgreSQL for sum and count measures on a node pair
and on the closure pair (a row with a NULL value, and no alone row). The check passes when every absence is
`absent_no_facts` (with nothing absent it passes with zero counts), fails on any `absent_with_facts` or
`witness_value_mismatch`, and is otherwise `not_run` (`witness_incomplete`) when a key is undecided; a known failure
wins. A witnessed pair test that is an `error` after its key read leaves it `not_run` (`pair_error`) and discards
what earlier chunks found, since the pair test's error already makes verify an error. Its detail holds `mode`
(`sampled` or `chunked`) and counts only, never keys: `asked_keys`, `incomplete_keys`, `undecided_keys` (keys with an
undecided absence or `together_absent`, once each) and `together_absent` count keys; the other labels count
absences, so a key missing from both alone answers is one incomplete key and two absences. The witness checks one
direction: a key an alone answer holds is not witnessed, even when no row qualifies for it (a measure cube that
dropped a filter is not seen here). Its `not_proven` line says that the witness reuses `crosscheck.toml`'s
`where_sql`, so it shows only that the asked keys Cube left out of an alone answer have no qualifying row under the
same filters, not that the data is complete or the filters right (common mode); that keys an alone answer holds are
not witnessed; that the NULL key group is outside it; and that the two reads bracket the per-query Cube snapshots
without sharing them, so a change undone between the reads is not seen. Whatever the witness's own work raises
(`compare.expected_errors()`, a lost connection included) stays in the witness as `witness_error`: the pair test's
status, reason, counts, D95 bound and V4 baseline are those of the option-off run, a failure to end the key-read
transaction after a failed witness savepoint is the witness's, and the chunked `elapsed_ms` leaves the witness reads
out. The witness does not restore D95 (a later decision). The unfiltered path at or below the cap gets no witness,
not even when it errors (it already compares an absence as NULL), and neither does a pair test whose key read
failed. V4 replays run no witness and keep treating `not_run` as an error, not a kill; reusing the first read in V4
replays is left for later. In chunked mode the witness adds about ceil(N / 2,000) statements per measure to the key
read and one per measure and chunk with absent keys afterwards.

**Chunked pair tests** (since 0.9.0). `verify.pair_key_budget` (default 2000, at least the cap) is the
largest key count the baseline V3 run compares in full; the default keeps the sample above the cap. A pair test with
more keys than the cap and no more than the budget reads the key count and every non-NULL key, in the same md5 order,
in one `REPEATABLE READ` snapshot (the keys in one statement under `source.statement_timeout_ms`, a timeout making the
test an `error`, and held in memory for the baseline run), splits them into consecutive chunks of 2,000 keys (the last
chunk holds the rest)
and runs the same `equals`-filtered together and alone queries per chunk, so the cost grows linearly with the keys
(ceil(N / 2,000) times that of the sample) instead of with their square. The NULL key group, which the unfiltered
queries of at most 2,000 keys include and an `equals` filter leaves out, is then compared with a `notSet` filter
(together on the grouped member, alone on each alone dimension). Mismatched keys, duplicate keys and alone-only keys
add up over the chunks and the NULL key group; the test passes only when every chunk and the NULL key group pass and
the together and (since 0.10.0) every alone answer held every key the chunks asked for (`answered_keys`,
equal to `compared_keys`). A key missing there is never fully compared by value, so without a mismatch or a
duplicate key the test is then `not_run` (`keys_not_answered`), never a pass. A query that cannot run in any chunk
makes it an `error` whose detail says `mode`, `chunks` and `chunks_compared` (the chunks that finished; all of them
when the NULL key group failed). The detail of a finished comparison gains `mode = "chunked"`, `chunks`,
`compared_keys` (the keys the chunks asked for), `answered_keys`, `total_keys`, `null_key_group` (the NULL key group's
own `keys`, `mismatched`, `duplicate_keys` and `alone_only_keys`, which the top-level counts include) and `elapsed_ms`
(from the key read to the last answer; since 0.11.0, without the existence witness's reads), and
carries no `sampled_keys`, `d95_keys` or `d95_rate`. The alone queries are filtered to the chunk's keys, so the chunked `alone_only_keys` count keys of the key table only, and
`duplicate_keys` counts duplicates within one chunk (two keys equal in canonical form in different chunks are each
compared with their own alone value). Its `not_proven` line says that every key was compared in C chunks plus the
NULL key group (or, when the answers held fewer, how many of the keys asked for they held), that the key set comes
from one `REPEATABLE READ` snapshot at the start, and that each Cube query (the together query and every alone query
of every chunk and of the NULL key group) reads its own snapshot, shared with no other query and not with SQL: the
longer the run, the longer that unshared window. Above the budget the sample and its bound stay, the
detail gains `pair_key_budget`, and the `not_proven` line adds that the key count exceeds the budget. V4 replays a
chunked test against its sample, the first chunk of the same key read, never every chunk: the item enters the suite
on its chunked V3 status, and the `sample_blind_possible` marking and the V4 `not_proven` line are unchanged.

**V3 additivity over join paths.** Predefined joins inflate totals when a row matches several rows of the
joined cube ("Aggregation Consistency Errors in Semantic Layers and How to Avoid Them", HILDA 2023,
arXiv 2307.00417). An `additive` test exists for every sum/count measure (each configuration measure with `agg`
`sum` or `count` and no `date_column`, and the closure cube's `qty` and `pairs`; `min`, `max` and any other
aggregation are not additive) and every cube exposing `ontology.class_column` that Cube can join to the measure's
cube on its own. Cube joins the cubes of a query from one of them through declared joins, so the join path
(`shape_q.join_path`, shortest, joins in declaration order) runs from the measure's cube to the dimension's cube
with the relationships as declared, or from the dimension's cube to the measure's with each step inverted, and is
read from the measure's side. Only a path made of `many_to_one` and `one_to_one` steps gets a test (Cube's
`belongs_to`/`has_many`/`has_one` spellings are folded); a path with a `one_to_many` step or a relationship Cube
does not define (many-to-many) gets none, because a row legitimately counts in several groups, and is listed in
`not_proven` as not applicable. In a generated model the tests are the node cube of each measure with a node
(joined one-to-one) and, per hierarchy, `qty` and `pairs` over the ancestor and descendant copies (joined
many-to-one), so the demo has seven; the class column bounds the number of groups. Each test sums every group,
the NULL group included, against the measure's Cube total (`equal`), and compares the NULL group with SQL from the
configuration (`null_group_equal`): the measure over the source rows (the measure's entity table with its WHERE
clause, or the closure table) that Cube puts in the NULL group. Which rows those are depends on the cube Cube roots
the grouped query at, recorded per test as `root`. When the measure's cube reaches the dimension's cube itself (a
forward path, the closure ends: `root = measure`), Cube left-joins the dimension to every measure row, and the NULL
group is the rows for which no dimension row with that key has a non-null class: the rows with no dimension row at
all (an anti-join, `NOT EXISTS`) and the rows whose dimension row has no class. When only the dimension's cube
reaches the measure's (a backward path, a generated node cube's one-to-one join to its measure cube:
`root = dimension`), Cube starts from the dimension rows and drops the measure rows with no dimension row instead
of grouping them, so the NULL group is only the matched rows whose class is NULL. A count's empty NULL group
counts as 0 on both sides. The test fails unless both hold; its detail adds `null_group_rows` and `orphan_rows`
(the source rows of the SQL NULL group and those without any dimension row; under a backward path the orphans are
counted but are not in the NULL group), never the values. Measure rows with values that Cube drops (a measure key
missing from the node table, which Cube's node-rooted join leaves out) fail the sum; orphan rows that add up to 0
change neither side and pass. A join declared many-to-one that matches several rows (a repeated node key) fails the sum.
The SQL side is read once per test and reused by the V4 replays. An additive path no configuration entry
describes (a hand-edited model) has no independent SQL, gets no test and is listed in `not_proven`.

**V4 mutation tests.** Each operator patches a deep copy of the model; a fresh Cube process serves each mutant
(from `verify/mutants/<operator>__<target>/`) and the suite runs against it. The suite replays V1 (the first
binding of every gold query), V2 (the first `verify.mutant_bindings` bindings of every stratum) and V3, but only
the items that passed against the unmutated model (the others are counted in a `not_proven` line), with gold rows
computed once. It stops at the first item that does not pass:

- a **failing** item kills the mutant: the check passes and `killed_by` names the item (`v1:<gold>`,
  `v2:<gold>:<stratum>[<position>]` with the position in the deterministic binding order, or a V3 check
  id);
- an item that **errors** is retried once; if it still errors, the check is `error` with `reason=suite_error` and
  `item`, because a query that could not run proves nothing about the mutant (a suite that raises gives
  `reason=suite_error` with `exception`);
- when every item passes the mutant **survived** and the check fails (`killed_by` is empty).

Before Cube starts, a witness SQL query counts what the mutation changes on the current data, independent of the
suite; witness 0 means the mutant is equivalent here and goes to `not_proven` without a check. The closure
operators (and `closure_join_swap`, through the hierarchy its measure joins) are gated like V1–V3: when build's
`closure:<h>` did not pass, their checks are `not_run` (`closure_not_passed`) and no Cube process starts. When
suite items are missing because a closure gated them, their gold query could not be planned
(`members_not_in_model`, `bindings_failed`) or `--gold` left it out, a surviving mutant is `not_run`
(`suite_incomplete`, detail `excluded`) instead of failed: the missing items might have killed it. A kill by part
of the suite is still a pass. A closure operator whose closure cube is not in the model is listed in
`not_proven` (not applicable). Every V4 detail holds `witness` and the run's `summary` counts (`killed`,
`survived`, `equivalent`, `error`, `not_applicable`, `not_run`). The detail of a mutant that survived a suite
replaying a sampled V3 pair test (fewer keys compared than exist) also holds `sample_blind_possible = true` and
`sampled_items` (those V3 check ids): the mutant may change only keys outside the samples. The marker changes no
status.

| Operator | Applies to | Mutation | Witness (rows of the current data) |
|---|---|---|---|
| `closure_depth_shift` | every hierarchy | `min_depth + 1` | closure rows |
| `closure_path_count_one` | every hierarchy | `path_count` = 1 | pairs with `path_count > 1` |
| `closure_qty_single_path` | every hierarchy | `qty / path_count` | pairs with `path_count > 1` and `qty <> 0` |
| `measure_filter_drop` | measures with `filter` | the exact filter removed | rows selected by exactly one of the two WHERE clauses whose value can move the measure (non-null; non-zero for `sum`) |
| `measure_normalization_drop` | measures with `filter_normalized` | the normalised filter compared on raw values | as `measure_filter_drop` |
| `measure_agg_to_max` | `sum` and `count` measures | the value measure becomes `max` | on the measure cube's own row SQL: keys whose rows give a different sum (count) than max, plus 1 when the total differs, plus 1 when two keys queried together would differ (two non-zero values, or a negative value next to another) |
| `dated_shift_one_day` | measures with `date_column` | `ts + 1 day` | rows of the measure cube's row SQL with a non-null `ts` |
| `closure_join_swap` | measures with `closure_join` | the measure joined on the other end of the pair | pairs whose two ends have different measure values (missing counts as 0 for `sum`/`count`) |
| `closure_end_swap` | every hierarchy | the descendant copy joined on `ancestor` | pairs whose two ends are different node rows (one of them may be missing from the node table) |

A model whose element an operator patches does not look as the configuration generates it (another
configuration, a hand edit) gives an `error` check with `reason=model_mismatch`, never a silently different test.

**SHACL vs SQL.** `build` writes `shapes.ttl` from the configuration: a node shape per configuration shape
targeting the entity class, with one property shape (`sh:class`, `sh:minExclusive`, `sh:minInclusive`,
`sh:maxInclusive`) and the configured severity. A shape that combines `class` with bounds gets two property shapes
in its node shape, both with its severity, because one RDF value cannot be a row IRI and a number at once (SHACL
2017-07-20, 4.3: a value node that cannot be compared with a bound violates it): `<iri>shape/<id>/p` with
`sh:class` on the column's predicate `<iri><entity>.<column>`, and `<iri>shape/<id>/b` with the bounds on the value
predicate `<iri><entity>.<column>/value` (`naming.rdf_value_predicate`; no other generated IRI can equal it, since
identifiers contain neither `.` nor `/`). The export writes, for every non-NULL value of such a column, both the
referenced row's IRI on the column's predicate and the same value as a bound literal on the value predicate,
whether or not the referenced row exists, so the bounds apply as in the view, which ORs the dangling reference
with each bound; NULL writes neither. The results of both property shapes count for the shape, so its SHACL keys
are the union of their focus nodes. `reason` exports the needed rows as RDF in memory (target rows
with the shape paths; referenced entities with their types) in one read-only `REPEATABLE READ` transaction,
counting them first against `verify.shacl_max_rows`, runs pySHACL without inference and compares, per shape, the
set of focus-node keys with the keys in `v_shape_<id>` (both in `canon.key` form).

One RDF predicate carries one kind of value, and shapes on the same target column can need different kinds (a
`class` shape and a bound shape, or `class` shapes naming different entities): in one graph each value would
violate the other shape's constraint. The compared shapes are therefore split into value groups
(`shacl_check.groups`): each shape goes, in configuration order, to the first group in which its column
(target, path) carries no other kind of value (`bound` literals or `class:<entity>` row IRIs; a shape with `class`
and bounds needs `class:<entity>`, since its literals go to the value predicate, which carries nothing else, so it
shares a group with `class` shapes naming the same entity and is split from bound shapes on its column), and a shape whose
column carries another kind in every group so far opens a new group. The first group is `main`, the others are
`extra:1`, `extra:2`, ... in the order they are opened. Every shape that needs no split stays in `main`, so a
configuration without a column shared by two value kinds (the demo included) exports one graph with exactly the
rows of 0.3.0; only the colliding shapes move to extra groups. Each group gets its own data graph with only what
its shapes need (their target rows and paths; the typed keys of the class entities they reference), and pySHACL
validates every group graph once with the same shapes graph. The focus nodes of a group graph are read only for
that group's shapes, so every group has the single-kind semantics above. Counting, exporting every group and
reading the views stay in the one transaction. The row limit applies to the sum over the groups (a table that two
groups read counts twice, so an extra group adds the rows it exports again), so it does not grow with the number
of groups; above it, every planned shape leaves a `not_run` check with `reason=resource_limit`, and the
explanation also appears in `not_proven`. When validating one group graph fails (pySHACL returns no results graph
or raises any exception), only that group's shapes are `error` (`reason=validation_failure`; `exception` holds a
type name only, the raised exception's or the type of pySHACL's non-graph answer, never a message); the other
groups are still compared. The row cap and
database errors occur before validation and still decide every shape. Each `shacl:<id>` check names its
`value_group` and `group_rows`; `rows_exported` is the sum over the groups.

One kind of shape is not compared and is listed in `not_proven` instead ("checked only in SQL"):

- `sql_violation`: any shape with an `sql_violation` predicate, also when it has `class` or bounds as well
  (no SHACL form).

**Property pairs (opt-in, since 0.11.0).** With `verify.shacl_pairs = true`, a shape whose only
constraint is an `sql_violation` that is, as a whole, `t.<a> <op> t.<b>` (op `>`, `>=`, `<` or `<=`; SQL
whitespace only; two different identifiers that pass `ident.check`) is also compared with SHACL 2017 section 4.5.
Anything else keeps the line above: comments, parentheses, casts, constants, quoted or upper-case names, `=`, `<>`
and `!=` (`sh:equals` and `sh:disjoint` are never used), and shapes that also have `path`, `class` or bounds. The
violation predicate maps to the property shape the valid rows satisfy (`shapes.PAIR_OPS`): `a > b` to `sh:path`
V(a) with `sh:lessThanOrEquals` V(b), `a >= b` to V(a) `sh:lessThan` V(b), `a < b` to V(b) `sh:lessThanOrEquals`
V(a) and `a <= b` to V(b) `sh:lessThan` V(a), where V(c) is the value predicate `<iri><entity>.<c>/value`, which
carries literals only. The pair shapes graph (node shape `<iri>shape/<id>`, property shape `<iri>shape/<id>/pair`,
the shape's severity) is generated at reason time and never written to `shapes.ttl`, so build's output, the
`shapes.ttl` comparison and every `shacl:<id>` check are unchanged; it passes the same shacl-shacl preflight first
(failure: every pair check `not_run`, `generated_shapes_invalid`). A pair check needs a view that build created
from the predicate as configured now: `shape:<id>` passed or failed (else `shape_not_built`) and `build/build.sql`
holds the view statement the configuration generates now (else `view_stale`).

`shacl_check.run_pairs` then reads, in its own read-only `REPEATABLE READ` transaction (after the shape
comparison's, so a pair query can never turn a `shacl:<id>` check into an error): the view keys (`view_missing`
when the view is gone); both column types from `pg_attribute`, which must be the base types `smallint`, `integer`,
`bigint` or `numeric` (`column_type` otherwise, with the type names; `real`, `double precision`, text, dates,
booleans and domains over the four types included); the rows holding NaN or an infinity in a numeric column (`non_finite_values`); and the rows
repeating a target key (`duplicate_keys`). pySHACL 0.40.1 compares rdflib Literals: rdflib orders any comparison
involving NaN as less-than in both directions, so a NaN pair never violates, while SHACL makes an incomparable
pair a violation and PostgreSQL orders NaN above every number; literals of unrelated datatypes are ordered by
datatype IRI (an `xsd:date` against an `xsd:integer` conforms one way round and violates the other). `tests/test_shacl_pairs.py` pins these behaviours
so a pySHACL change is noticed before the preconditions are relaxed. A repeated key would give one RDF node the
values of several rows and pair them across rows. NULL needs no rule: no triple, no pair, no violation, as the SQL
predicate is unknown there. The target rows of the remaining shapes are counted; the rows the shape comparison
counted plus these may not exceed `verify.shacl_max_rows` (else every remaining pair check is `not_run`,
`resource_limit`, while the `shacl:<id>` checks keep their own outcome). The rows are exported as one pair data
graph (`rdf:type` and the non-NULL literals of the compared columns, `literal()` typing), a key repeated among the
exported RDF nodes is still `duplicate_keys`, and pySHACL validates it without inference after the transaction.
A database error in that transaction, even on one target entity, makes every pair check that reached it `error`
with its SQLSTATE (all or nothing, as the shape comparison is after a database error).
`shacl:pair:<id>` passes iff the focus-node keys of `<iri>shape/<id>/pair` equal the keys in `v_shape_<id>`; its
detail holds the operator, constraint, column names and types, counts (`violations`, `shacl_focus_nodes`,
`shacl_only`, `sql_only`, `rows_exported`), severity and view, never a value. Only when it passes or fails does the
shape's not-proven line change to name the pair check, with one note that a pass shows agreement on this snapshot,
not that the rule is right; a `not_run` keeps the line and adds its reason. `reason` runs pySHACL only (Jena runs in
`shacl-corpus`). With the option off none of this runs.

Releases up to 0.3.0 left every shape on a column shared by two value kinds uncompared (`shared_path`); value
groups replace that exclusion. Releases up to 0.5.0 also left a shape combining `class` with bounds uncompared
(`class_and_bounds`); the second property shape on the value predicate replaces that exclusion. Its `shapes.ttl`
text changed, so a build output written before cannot be reasoned on (exit 2: rerun `build`), and the engine
fingerprint marks the earlier results stale in `report`.

`shapes.ttl` must equal what the configuration generates now; otherwise `reason` stops with exit 2 (rerun
`build`).

**OWL (V5).** `reason` builds an in-memory ontology: a class for the node entity, one subclass per
`ontology.classes` value (pairwise disjoint), the edge property `<h>` and its transitive super-property
`<h>_plus`, individuals for node keys and edge endpoints (endpoints missing from the node table are plain
`Thing`s), `no_children` axioms (`C SubClassOf not (<h> some Thing)`) and defined classes:

| kind | OWL definition | SQL definition (node-table rows only) |
|---|---|---|
| `has_child` | Node and (`<h>` some Thing) | parents of at least one edge |
| `has_parent` | Node and (inverse `<h>` some Thing) | children of at least one edge |
| `contains_class` | Node and (`<h>_plus` some C) | ancestors in the closure of a node of class C |
| `class_with_child` | C and (`<h>` some Thing) | parents of class C |
| `class_used_as_part` | C and (inverse `<h>` some Thing) | children of class C |

HermiT runs with `infer_property_values=True` in a private temporary directory that is removed afterwards; the
Pellet reasoner bundled with owlready2 is never called. The OWL closure is the inferred `<h>_plus` values united
with the asserted `<h>` edges. `v5:closure:<h>` and `contains_class` need build's `closure:<h>` to have passed
with no cycle edges dropped; an inconsistent ontology entails everything, so the V5 checks are then `not_run`.
Above `verify.owl_max_individuals`, `owl:consistency` and every configured V5 comparison are `not_run` with
`reason=resource_limit`. Size limits never turn a selected engine into a successful untested part of a run.

## 8. Report

`report` reads the results of `doctor`, `demo`, `build`, `reason`, `cube` and `verify` in the output directory and
never touches the database.

- **Required stages** are `build`, `cube` and `verify`, plus `reason` when the configuration has an `[ontology]`
  or any `[[shapes]]`. A required stage without a result is `stage:<name>` `not_run` (`reason=missing`).
- **Staleness.** Dependencies: `build` after `demo`; `reason` and `cube` after `build`; `verify` after `build`
  and `cube`. A stage that started before a dependency finished its latest run is stale (`why=newer`), so is a
  stage that depends on a stale one (`stale_dependency`) and one whose run times cannot be read
  (`no_timestamp`). A stale required stage counts as at least `not_run` (`reason=stale`, `after`, `why`,
  `last_status`).
- **Cube runtime.** A cube result whose `cube:meta` recorded another `cube_version` or `sql_planner` than
  `cube_run.runtime_facts` of the configured (pinned) Cube version is stale (`after=Cube runtime`,
  `why=cube_runtime_changed`); one whose `cube:meta` lacks either value is stale too (`why=cube_runtime_missing`,
  for example a result from a release that did not pin the planner). verify then follows as `stale_dependency`. A
  cube result without `cube:meta` (the stage stopped before Cube answered) is left to its own error.
- **Optional stages** (`doctor`, `demo`): a present result that passed, failed or errored is mirrored (a failing
  doctor or demo fails the report); one that is `not_run` or stale gets only a `not_proven` line. A missing
  `doctor` adds the `not_proven` line that the check role's privileges were not verified.
- Each stage's status is recomputed from its checks (fail-closed; a hand-edited `status` can only make it
  worse). A stage whose own status is `not_run` (a run narrowed with `--only`, `--gold` or `--hierarchy`) is
  `stage:<name>` `not_run`.
- The overall status (and exit 0) is `pass` only when every required stage ran, is current and passed.
- `report.json` holds `status`, `generated_at`, `config` (file name), `required`, `missing_required`, `stages`
  (per stage: `status`, `counted_as`, `required`, `stale`, `counts`, `failing`, `not_proven`,
  `next_safe_action`, `started_at`, `finished_at`), `not_run`, `stale`, `not_proven`, `trusted_sql` and
  `next_safe_action`. An unreadable `result.json` becomes a synthetic `result_file` error for that stage.
  `report.md` renders the same: a status table, missing or stale stages, the checks that did not pass, every
  not-proven line, every stage's next action and the trusted code (`sql_violation` shapes and gold SQL files).
- Inputs: `sxc.py` records `result.inputs_fingerprint(cfg)` (SHA-256 of `crosscheck.toml` and of the gold SQL
  files) in every result, taken before the stage runs. `report` marks build, reason and cube stale when the
  configuration changed since they ran, and verify when the configuration or a gold file changed
  (`reason=stale`, `why=inputs_changed`); a result without a fingerprint cannot be shown to be current.

### Synthetic counterexamples and data provenance

The optional `counterexample` command is independent of the configured pipeline. It always ignores
the working-directory config and rejects an explicit `--config`. Two trusted SQL files run against
generated CTE fixtures using bound values in PostgreSQL read-only transactions. NULLs, duplicate join
keys, BOM diamond paths and date boundaries have deterministic generators. Successful unequal exact
row multisets are witnesses; query errors cannot be witnesses. Bounded agreement is `not_run` and
never proves equivalence. The shrinker preserves a mismatch while deleting rows and reports whether
it completed a deletion-1-minimal pass or exhausted its budget. Optional `--constraints FILE` validates
PK/UNIQUE/NOT NULL/FK/allowlisted CHECK rules before every SQL comparison; inadmissible deletions
are rejected without cascading. Minimality is then relative to admissible single-row deletions.
Profiles bind exact file bytes and mode in replay; constrained text uses explicit `C` collation.
Only the separate synthetic replay
artifact contains generated row values; results and reports contain statistics, IDs and hashes.

`--metamorphic-sql FILE` replaces the two-file comparison with a gold-independent check of one trusted
closure SELECT (columns `ancestor`, `descendant`, `qty`) over seeded `bom_diamond` fixtures (or the bounded
`bom_exhaustive` enumeration described below), with or without
`--constraints`. Q(a, d) is the path-product quantity: the sum over all paths of the product of `qty_per`. Each
admissible case runs the SQL on the original fixture and on four transformed fixtures, each in its own read-only
REPEATABLE READ transaction with bound values: `mr1_unfold` (shared sub-assemblies copied into a forest; summed
over copies, every instance answers like its original), `mr2_scale` (one multi-path line times K = 2:
Q'(a, d) = Q(a, d) + (K-1)·q·Q(a, u)·Q(v, d)), `mr3_disjoint` (a fresh component leaves original pairs unchanged
and adds no cross pair) and `mr4_unit_node` (a line split through a fresh unit node: original pairs unchanged,
Q'(a, w) = Q(a, u)·q, Q'(w, d) = Q(v, d)). Expected values use exact decimal arithmetic and the `number`
canonical form; absent pairs are 0, Q(x, x) = 1 only inside the formulas, and reflexive answer rows are not
compared. Every relation of a case is `not_run` when its fixture has no BOM line (`no_bom_lines`), a NULL parent,
child or quantity (`null_bom_value`), a quantity outside `numeric(18,6)` (`invalid_bom_value`) or a cycle
(`cycle`); a single relation is `not_run` without a diamond (`no_diamond`), a multi-path line
(`no_multi_path_edge`) or a quantity other than 1 (`no_non_unit_quantity`) where it needs one, when its
transformed fixture leaves the type domain or the profile (`type_domain`, `integrity_profile`), exceeds the row
bound (`size_bound`), needs an expected value exact arithmetic cannot represent (`inexact_arithmetic`), or
compares no pair (`no_pairs`); never `pass` without a compared pair. A relation `not_run` in every case gives the
check reason `no_admissible_generated_examples` (no case was checked) or `vacuous_for_every_case`. Database,
driver and connection errors are `counterexample:query` errors; any other exception propagates to `stage`
(error). The search stops at the first case that
violates any relation and saves the original rows, the failed relations and every transform specification in
`fixture.json` (format `sxc-synthetic-metamorphic`); replay recomputes the transforms and refuses a file whose
recorded ones differ. Results carry counts, the seed and transform hashes. These relations cannot see errors
invariant under them (edge direction, a wrong quantity column, a filter mapping) and test only the given SQL:
the generated closure is tied to gold through V1/V3/V5, and a passing relation upgrades no other check. The idea
follows EDC (Detecting Logic Bugs in DBMSs via Equivalent Data Construction, PACMMOD 3(6), 2025); no code is used.

The seeded `bom_diamond` fixtures share one diamond topology, so a closure that uses `UNION` (set semantics) instead
of `UNION ALL` and collapses equal-product paths is seen only where the seeded quantities collide: with seed 7 the
original diamond has two equal-product paths in 2 of the 64 default cases (19 and 39, caught by unfolding) and
scaling creates one in 4 more. `--scenario bom_exhaustive` (with `--metamorphic-sql` only) replaces sampling by a
bounded-exhaustive enumeration (`bom_exhaustive.py`): every connected acyclic BOM of 2 to 4 parts with 1 to 5
`bom_line` rows, at most 2 rows per (parent, child) pair and `qty_per` in {1, 2}, one canonical representative per
isomorphism class (1,743 structures: 5, 153 and 1,585 for 2, 3 and 4 parts), each under two label placements
because ids are values in SQL: `topological` (parents get the smaller labels) and `reverse` (children do), 3,486
cases in a fixed order (case i is structure i // 2 under placement i % 2). Each original answer is compared with
`closure_oracle.expected_quantities`, the same exact Fraction fold that judges generated closures (duplicate lines
summed), in check `counterexample:metamorphic:oracle` (absent pairs 0, reflexive rows ignored, `Decimal` answers
converted exactly); the four relations also run on every case where they are not vacuous. The enumeration is not
sampled: `--max-examples` must be omitted or cover every case, and the oracle passes only when every admissible
case was compared. No check passes partially: a resource bound (`size_bound`, `inexact_arithmetic`) in an original
answer makes every check `not_run`; one hit only in a relation's own transform, transformed answer or comparison
makes only that relation `not_run`; at the first failure and in a one-case replay the failed checks `fail` and the
others are `not_run`; statement timeouts stay `counterexample:query` errors.
Inadmissible cases under `--constraints` are counted (`invalid_domain_examples`) and named in `not_proven`. The
first failing case stops the run and `fixture.json` also binds the case index, placement, structure index and the
SHA-256 of the whole enumeration; replay requires exactly that case. With the reference SQL a full run took 28 s
(26 s with the example profile) on a loopback PostgreSQL 17.5. The `UNION` closure fails deterministically at case 4
(two parallel `A -> B` lines with `qty_per` 1), the `min` candidate at the same case and a filter
`parent_id < child_id` passes every `topological` case but fails case 1 (`reverse`). Only the stated scope, both
placements and the `qty` column are tested; synthetic fixtures do not establish correctness on real data. The method
follows Marinov, Andoni, Daniliuc, Khurshid and Rinard, "An Evaluation of Exhaustive Testing for Data Structures"
(MIT CSAIL technical report, 2003); no code is used.
See [counterexample reference](../skills/semantic-crosscheck/references/counterexamples.md).

Optional `source.data_revision_sql_file` supplies a trusted, read-only single-text-scalar probe.
The operator must advance a never-reused revision atomically with every relevant source change.
Start/end probes bind build/reason/cube/verify evidence to the hashed revision; if Cube uses a different
DSN, probes on both endpoints must agree. The report checks current bindings only when the probe is
configured; otherwise revision checking remains database-free and explicitly lists data freshness as unbound.
Missing or different bindings and changed engine/probe inputs invalidate evidence. A revision contract
does not provide a shared MVCC snapshot, and cannot detect omitted or reused revisions. SQL probes
and reference queries remain trusted code, not a security boundary. The engine digest covers plugin
Python/CLI sources, adapter files/pins and Python/direct Python package-version metadata. Optional
`source.runtime_evidence=true` separately binds actual PG endpoint versions/semantic settings,
Node/Java executable versions, the used HermiT JAR/classpath and the selected Cube installation's consumed
contents, with the pinned Cube version and the SQL planner the runner fixes (`cube_version`, `sql_planner`;
configured values, not read back from Cube) next to its package versions, so evidence recorded under another
planner no longer matches. It requires the complete installed runtime, adds bounded metadata/version/hash probes,
and invalidates missing/errored/changed evidence. With it disabled, no new runtime probes occur.
Historical results without current fingerprints must be rerun. These are observed boundary facts,
not cryptographic authentication, runtime attestation or a shared cross-session snapshot.

## 9. Demo reference (`expected.json`)

`demo` writes `demo/expected.json` in the output directory (`demo/.sxc/demo/expected.json` for the shipped
demo configuration): the numbers its independent Python implementation computed, grouped by what they are
compared with: `rows` (both variants; `demo:rows:<table>` and `demo:copy_on_error`), `graph` (both variants; edges,
orphan child references, `has_cycle`), `closure` (clean variant only: pairs, path counts, multi-path pairs,
depth histogram and a quantity checksum, compared with `closure:has_component`, `v5:closure:has_component` and
gold query `q04_depth_histogram`; the dirty variant has a cycle, so build refuses that closure and the section
says `compared: false`) and `planted_defects` (dirty variant: each defect's count and the check that reports it,
or the checks it exercises without being reported). See `commands/xc-demo.md` for the list.

## 10. Tests

- `python -m unittest discover -s tests -v` from the repository root (stdlib unittest only).
- Unit tests need no database. Integration tests are skipped unless `SXC_TEST_ADMIN_DSN` names a superuser
  connection; `tests/pgfixture.py` creates a throw-away database, the loader/check roles, the schemas and loads
  the demo, and `tests/test_docs_setup_roles.py` runs `docs/setup-roles.sql` through a minimal psql interpreter.
  Cube tests are also skipped unless an adapter installed from this checkout is present
  (`cube_run.installed(cube_home())`), OWL tests unless Java is available (`SXC_JAVA` or `java` on `PATH`).
- `tests/test_packaging.py` and `tests/test_docs_*.py` keep the documents, manifests and CI workflow in line with
  the code: stages, options, check ids, configuration keys and defaults, minimum versions and pins.
- The demo's `expected` numbers come from an independent Python implementation and are the reference for the
  closure, the shapes and the dirty-data findings.


## Opt-in structured and finite-domain counterexample search

The two-query mode adds `--shrink-strategy structured` and `--witness-schema FILE`.
The existing row-only path and version 1 replay remain the default. Structured reduction
attempts row deletion, reverse-FK closures and finite-rank non-key value simplifications
under one trial budget; each accepted operation descends the row-count/cell-rank measure
and restarts row deletion. The final single-row minimality flag is never global minimality.

A strict user declaration supplies types, finite domains and row bounds; optional Z3
encodes only the existing integrity language. It does not introspect operational schema
or encode SQL query conditions. One `--solver-timeout-ms` deadline covers compilation
and lexicographic model enumeration. Candidate models are independently domain-validated
and evaluated by the existing PostgreSQL read-only REPEATABLE READ comparator. The same
finite-domain and row-bound validator applies to reduction and replay steps.

Extended fixture version 2 binds exact declaration/profile and SQL bytes, generator,
strategy, seed and original synthetic input. Structured accepted traces are regenerated
from that input and checked before DB connection; row-only reductions must be subsets.
Saved statistics are untrusted: replay reevaluates SQL and recomputes shrink claims.
Only fixture files contain rows and transformation values; count-only results omit them.
Input drift and size limits prevent saving. No-model/no-disagreement/solver-unavailable
outcomes are not_run plus not_proven, never correctness or equivalence proofs.
See [the full input and replay contract](../skills/semantic-crosscheck/references/counterexamples.md#structured-reduction-and-typed-finite-domain-inputs).

## Optional diagnostic contracts

The [diagnostic design](diagnostic-contracts.md) defines three bounded opt-in paths. [SHACL repair](shacl-repair.md) binds independent reports and explicit target/immutable obligations to the same supported shapes before and after. [Grain contracts](grain.md) bind intended operation/key provenance independently of configuration and check real visible key/join assumptions. [OWL explanations](owl-explanations.md) preserve inconsistency and localize sufficient generated-axiom groups using the same reasoner; incomplete searches never certify minimality, and only finished full-set checks report a unique explanation over those groups.

Standalone grain and shacl_repair results are not added to PIPELINE or promoted into default report success. Existing source revision probes and default semantics remain unchanged. Alias guards run before cleanup/failure writes; unverified aliases suppress writes. New stages return counts/ids/digests only. These features do not discharge current general-grain, shared-snapshot, full-SHACL or independent-reasoner limitations.

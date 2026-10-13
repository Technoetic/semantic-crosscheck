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

Common options:

- `--config PATH` names the configuration. When it is given, the file must exist, for every stage, `doctor` and
  `cube-install` included: `--config <name>: no such file (check the path; nothing was run)`, exit 2. A typo must
  not quietly turn a check off. Without `--config`, `./crosscheck.toml` is used; only `doctor` and `cube-install`
  run without one, every other stage stops with exit 2 (`no crosscheck.toml in the current directory: pass
  --config PATH`).
- `--out DIR` is the output directory. Default: `.sxc` next to the configuration, or `./.sxc` in the current
  directory when there is no configuration (`doctor` and `cube-install` without `--config`).
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
4. The configuration is loaded and validated (exit 2 on any malformed entry, see config.md).
5. The stage module runs: `run(cfg, args, out: Path) -> Result` (`cfg` is `None` only for `doctor` and
   `cube-install` without a configuration). `sxc.py` writes the result atomically (`result.json.part`, then a
   rename) and prints the summary.

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
| 2 | configuration or usage error | a malformed `crosscheck.toml` entry, an explicit `--config` that does not exist, no `./crosscheck.toml` for a stage that needs one, an unknown `--require`, `--only`, `--gold` or `verify.mutation_operators` value, `--scale` below 1, a missing prerequisite result (`run sxc.py <stage> first`), `build/shapes.ttl` older than the shapes in the configuration, a configured table or column that does not exist, demo tables with other columns than the demo defines, a `--native-file` that does not exist, a `--home` directory that is neither empty nor an adapter install |
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
  only in SQL, a vacuous defined class, a sampled pair test, a component that is not installed). It does not
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
  (`db.read_only`). `build` reads the source tables inside its write transactions (it counts the shape views and
  copies the edges into temporary tables there) and relies on the check role's privileges; it writes only to
  the output schema and to temporary tables. Each build transaction first takes
  `pg_advisory_xact_lock(hashtext('semantic-crosscheck build <output_schema>'))`, so two builds of one output
  schema wait for each other. `demo` writes with the loader role under its own advisory lock, in one
  transaction with a 10-minute statement timeout.
- **Cube sessions are read-only and bounded.** Cube connects with the check role (or `[cube].dsn_env`), with
  `PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=<cube.query_timeout_s * 1000>'` and
  `CUBEJS_DB_QUERY_TIMEOUT=<cube.query_timeout_s>`. Cube's Postgres driver sets `statement_timeout` from the
  latter on every connection (its default is 10 minutes, and it is also the queue's execution timeout), so a
  query the client gave up on stops in PostgreSQL as well.
- **The Cube child's environment** is the inherited one minus every `CUBEJS_*`, `PG*` and `SXC_*` variable,
  `NODE_OPTIONS`, every variable whose value contains `postgres://` or `postgresql://` and the configured DSN
  variable names; plus the connection parsed from the Cube DSN, a random per-run API secret, development mode
  and telemetry off, `CUBEJS_LOG_LEVEL=error`, TLS settings from the DSN's `sslmode`, `SXC_PORT`,
  `SXC_MODEL_DIR` and, only when it is `1`, `SXC_CUBE_DEBUG`. `node` is looked up on `PATH` only.
- **The adapter listens on 127.0.0.1 only** and requires an HS256 JWT signed with the per-run secret. The client
  talks to it through an opener without proxies, so `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and the system
  proxy settings never see the JWT or filter values, and no `NO_PROXY` entry is needed. `cube-install` downloads
  still honour proxy settings (their content is hash-pinned).
- **Adapter logs are redacted.** `server.js` drops request logs (they contain filter values) and writes each
  error as one line, `SXC_CUBE_ERROR <event>: <class> [code] (category)`: no message text at all, because
  PostgreSQL quotes values in a style that depends on its message language ('x', »x«, « x »). The code is the
  SQLSTATE when known; the category is `timeout` or `model`, and only model compile errors keep their text
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
| `db` | `connect(env_name, require_pg17=True)` (doctor passes `False`), `parse_dsn`, `redact`, `rows(conn, sql, **params) -> list[dict]`, `scalar`, `read_only(conn, ms)`, `write_tx(conn, ms)`, `server_version_num` |
| `procutil` | `which(name)`: absolute path of a program on `PATH`, never the current directory |
| `naming` | every generated name: closure/cycles tables, shape views, cube and member names, RDF IRIs, OWL property names |
| `closure` | `build(conn, cfg, h) -> (checks, sql_log)` inside the caller's transaction, which holds the output schema's build lock |
| `cube_gen` | `generate(cfg, conn) -> (model, notes)`, `members(model)`, `to_yaml`, `write_model(model, dir)`, `load_model(out)`, `where_sql(measure)`, `measure_cube(cfg, m, cols)`, `columns(conn, table)` |
| `cube_run` | `cube_home()` (absolute), `install_state(home) -> 'ok'\|'missing'\|'incomplete'\|'stale'`, `installed(home)`, `adapter_hashes()`, `serve(cfg, run_dir, log_path) -> CubeClient` (context manager), `ADAPTER_DIR`, `MIN_NODE` |
| `cube_client` | `CubeClient.load(query) -> list[dict]` (polls "Continue wait", explicit limit 50,000, never `renewQuery`, no proxies), `meta()` |
| `compare` | `run_binding(conn, client, cfg, gold, params, cache=None, types=None)`, `compare_rows(gold, g_rows, c_rows, types=None)`, `member_kinds(model)`, `gold_kinds(gold, kinds)` |
| `sweep` | `bindings(conn, cfg, gold)`, `strata_names(gold)`, `md5_order(column)` |
| `shape_q` | `tests(cfg, model)`, `run_test(...)`, `pair_keys(conn, cfg, test)`, `sampled(test)`, `PAIR_KEY_CAP = 2000` |
| `mutate` | `OPERATORS`, `CLOSURE_OPERATORS`, `OUTCOMES`, `selected(cfg)`, `run_mutants(cfg, out, conn, model, suite, closure=None)`, `verdict(status, item)`, `closure_status(cfg, out)` |
| `demo` | `build(dirty, seed, scale) -> {"parts","edges","orders","stock","malformed","expected"}`, `load(conn, data)`, `reference(expected, variant, seed, scale)`, `TABLES`, `TableMismatch` |

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
property shapes (`<iri>shape/<id>/p`).

## 6. Check catalogue

Every check id a result can hold. `<entity>`, `<h>`, `<id>`, `<gold>`, `<name>` and `<target>` are
configuration ids; `<member>` is a Cube member (`cube.member`); `<stratum>` is a stratum name, `values`,
`param_sets` or `all`.

| Stage | Check id | Passes when | Other outcomes |
|---|---|---|---|
| any | `stage` | never passes | `error`: the stage stopped before its own checks (`message` for exit 2 or 3, `reason=interrupted`, or `exception` and `sqlstate`) |
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
| reason | `shacl:<id>` | pySHACL's focus nodes equal the keys in `v_shape_<id>` | `fail` (`shacl_only`, `sql_only`); `error` (SQLSTATE or `validation_failure`); `not_run` (`shape_not_built`, `view_missing`) |
| reason | `owl:consistency` | HermiT finds the ontology consistent | `fail`; `error` (SQLSTATE or `reasoner_error`) |
| reason | `v5:closure:<h>` | the OWL closure pairs equal the closure table | `fail`; `not_run` (`closure_not_passed`, `cycle_edges_dropped`, `closure_table_missing`, `inconsistent`, `owl_not_run`) |
| reason | `v5:defined:<name>` | the members of the defined class equal its SQL definition (a class without members on either side passes and is listed in `not_proven`) | `fail`; `not_run` (as `v5:closure:<h>`; `contains_class` reads the closure table) |
| reason | `scope:shacl`, `scope:owl` | never passes | `not_run` (`excluded_by_only`): the part `--only` left out |
| cube | `cube:model` | the model was generated (a generator error is exit 2 instead) | none |
| cube | `cube:gold_members:<gold>` | every member the gold entry names exists in the model and is public | `fail` (`missing`, `hidden`, `malformed` counts) |
| cube | `cube:meta` | Cube's `/meta` lists exactly the public members of the model | `fail` (`missing`, `extra`); `error` (HTTP status, connection error, unexpected response) |
| verify | `v1:<gold>` | the first binding matches exactly | `fail`; `error` (gold or Cube side: error type, SQLSTATE, HTTP status, `cube` `timeout`/`row_limit`/`query_error`; `members_not_in_model`; `bindings_failed`); `not_run` (`closure_not_passed`, `no_binding`) |
| verify | `v2:<gold>:<stratum>` | every binding of the stratum matches exactly; an empty stratum passes (`empty_stratum`) and is listed in `not_proven` | as `v1:<gold>`, without `no_binding` |
| verify | `v3:total:<member>` | the Cube total equals SQL computed from the configuration (or the closure table) | `fail`; `error`; `not_run` (`closure_not_passed`) |
| verify | `v3:additive:<member>:<member>` | the groups of the measure by the ontology class column add up to its total | as `v3:total:<member>` |
| verify | `v3:pair:<member>+<member>` | the two measures queried together equal the same measures queried alone, per key | as `v3:total:<member>` |
| verify | `v4:<operator>:<target>` | the mutant was killed: a suite item failed against it (`killed_by`) | `fail` (it survived); `error` (`suite_error`, `startup`, `cube`, `write`, `model_mismatch`, `witness`); `not_run` (`closure_not_passed`, `suite_incomplete`). Equivalent and not applicable mutants get no check, only a `not_proven` line |
| verify | `v4:suite` | never passes | `not_run` (`no_passing_suite_item`): no suite item passes on the unmutated model |
| verify | `scope:v1`, `scope:v2`, `scope:v3`, `scope:v4`, `scope:gold:<gold>` | never pass | `not_run` (`excluded_by_only`, `excluded_by_gold`): the part or gold query a narrowed run left out |
| report | `stage:<name>` | the stage (`doctor`, `demo`, `build`, `reason`, `cube`, `verify`) ran, is current and passed | its own status; `not_run` with `reason` `missing` (a required stage without a result) or `stale` |
| cube-install | `cube_install:npm_ci`, `cube_install:native_sha256`, `cube_install:extract`, `cube_install:smoke` | each install step passed | `fail` or `error`; the steps after a failed one are `not_run` |
| cube-install | `cube_install:already_installed` | an install matching the pins and this checkout's adapter files exists, so nothing was reinstalled | none |

## 7. Verification semantics

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
count total counts as 0, flagged `null_as_zero`); `additive` checks that groups by the ontology class column add
up to the total for sum/count measures joined one-to-one to their node cube; `pair` checks that measures queried
together (the closure `qty` with each closure-joined measure, and every two measures joined to the same node
cube) equal the same measures queried one at a time, per key, which exposes join fan-out. Pair-test keys are
typed like V1/V2 keys. A pair test whose key source (the node table key, or the closure end) has more than
`shape_q.PAIR_KEY_CAP` = 2,000 distinct values compares a deterministic sample of 2,000 keys (ordered by
`md5(key || seed) COLLATE "C", key`), read once and filtered with an `equals` filter in each of its queries; the
detail gains `sampled_keys` and `total_keys`, and `not_proven` says `compared 2000 of N keys`. The cap is fixed.

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
`survived`, `equivalent`, `error`, `not_applicable`, `not_run`).

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
`sh:maxInclusive`) and the configured severity. `reason` exports the needed rows as RDF in memory (target rows
with the shape paths; referenced entities with their types) in one read-only `REPEATABLE READ` transaction,
counting them first against `verify.shacl_max_rows`, runs pySHACL without inference and compares, per shape, the
set of focus-node keys with the keys in `v_shape_<id>` (both in `canon.key` form). Three kinds of shape are not
compared and are listed in `not_proven` instead ("checked only in SQL"):

- `sql_violation`: any shape with an `sql_violation` predicate, also when it has `class` or bounds as well
  (no SHACL form);
- `class_and_bounds`: a shape that combines `class` with bounds (its value cannot be a row IRI and a number at
  once in RDF; no SHACL form);
- `shared_path`: shapes with a SHACL form on the same target column that need different kinds of RDF value (a
  `class` shape and a bound shape, or `class` shapes naming different entities). One RDF predicate carries one
  kind of value, so none of those shapes is compared. Adding a shape can therefore remove another shape's SHACL
  comparison.

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

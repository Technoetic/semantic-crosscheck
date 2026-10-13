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
the generated SQL or YAML.

## 1. Stages and command line

```
python skills/semantic-crosscheck/scripts/sxc.py <stage> --config crosscheck.toml [--out DIR]
```

| Stage | Role | Writes |
|---|---|---|
| `doctor` | Python >= 3.11, pg8000, DSN, PostgreSQL >= 17, check-role privileges, optional components | `doctor/result.json` |
| `demo` | loads the synthetic demo (`--variant clean|dirty`) with the loader role | `demo/result.json`, `demo/expected.json` |
| `build` | shape violation views, hierarchy closures | `build/result.json`, `build/build.sql`, `build/shapes.ttl`, `build/fk_review.sql` |
| `reason` | SHACL vs SQL, OWL consistency, OWL vs SQL (V5) | `reason/result.json` |
| `cube` | generates the Cube model, starts Cube, checks metadata | `cube/result.json`, `cube/model/*.yml`, `cube/model.json`, `cube/server.log` |
| `verify` | V1 strict compare, V2 strata sweep, V3 query shapes, V4 mutation tests | `verify/result.json`, `verify/mutants/*/` |
| `report` | summary of every stage | `report/result.json`, `report.md`, `report.json` |
| `cube-install` | installs the pinned Cube adapter once per user | `cube_install/result.json` |

`--out` defaults to `.sxc` next to the configuration. Exit codes: 0 pass, 1 a check failed or could not
run, 2 configuration or usage error, 3 environment missing (DSN, PostgreSQL, Java, Node, adapter).

Every stage module exposes `run(cfg, args, out: Path) -> Result` (`cfg` is `None` only for `doctor` and
`cube-install` without a configuration). `sxc.py` writes the result, prints a summary and maps
`StageError(message, exit_code)` to exit codes 2/3.

## 2. Result contract (`result.py`)

- `Result(stage)`; `res.add(check_id, status, config_ref, **detail)` with status `pass|fail|error|not_run`.
- Stage status is fail-closed: any `error` > any `fail` > any `not_run` > all `pass`; no checks = `not_run`.
- `detail` holds counts, ids of configuration entries and small enums only. **Never row values** (no keys,
  names, quantities from the database), never DSNs or secrets. Messages of database errors are not stored
  (they can quote values); store the SQLSTATE instead.
- `res.not_proven` lists what the run could not prove (an empty stratum, an equivalent mutant, a shape with no
  SHACL form, a component that is not installed). It does not change the status; `report` shows it.
- `res.next_safe_action` is one sentence the agent can follow.
- Prior stage results: `load_result(out, stage)`, `check_status(out, stage, check_id)`, `require_stage(out, stage)`.

## 3. Security model

- Two database roles. The **loader** (`SXC_PG_OWNER_DSN`) is used only by `demo`. The **check role**
  (`SXC_PG_DSN`) has SELECT on the source tables and owns the output schema (`source.output_schema`), where
  all generated tables and views live. `doctor` verifies that the check role cannot write source tables.
- Reads of source data run in `READ ONLY` transactions with `statement_timeout` (`db.read_only`).
- Cube connects with the check role (or `[cube].dsn_env`) and `PGOPTIONS=-c default_transaction_read_only=on`,
  so every Cube session is read-only. The adapter listens on 127.0.0.1 only, with a random per-run API secret
  that exists only in the child's environment. Request logs are dropped (they contain filter values).
- DSNs come only from environment variables; the configuration refuses credentials.
- The Cube adapter is installed with `npm ci --ignore-scripts` from a committed lockfile; the native module is
  downloaded from the Cube GitHub release and accepted only if its SHA-256 matches `native-sha256.json`.
- Generated SQL uses validated identifiers (`ident.py`) and bound parameters; `sql_violation` predicates and
  gold SQL files are trusted code from the configuration and are labelled as such in reports.
- Optional `output.show_rows` (default 0) is the only way to write example rows, into a local samples file.

## 4. Shared modules (stable contracts)

| Module | Contract |
|---|---|
| `ident` | `check`, `quote`, `qualified('schema.table')`, `split` |
| `canon` | `canon(v)`: strict canonical form (decimals normalised, ISO dates, booleans, exact text); `same(a, b)` |
| `config` | `load(path) -> Config` (dataclasses; see the module); `cfg.hierarchy(name='')`, `cfg.entity(name, ref)`, `cfg.cube_dsn_env()` |
| `db` | `connect(env_name)`, `rows(conn, sql, **params) -> list[dict]`, `scalar`, `read_only(conn, ms)`, `write_tx(conn, ms)`, `server_version_num` |
| `naming` | every generated name: closure/cycles tables, shape views, cube and member names, RDF IRIs, OWL property names |
| `closure` | `build(conn, cfg, h) -> (checks, sql_log)` inside the caller's transaction |
| `cube_gen` | `generate(cfg, conn) -> (model, notes)`, `members(model)`, `to_yaml`, `write_model(model, dir)`, `load_model(out)`, `where_sql(measure)`, `measure_cube(cfg, m, cols)`, `columns(conn, table)` |
| `cube_run` | `cube_home()`, `installed(home)`, `serve(cfg, run_dir, log_path) -> CubeClient` (context manager), `ADAPTER_DIR` |
| `cube_client` | `CubeClient.load(query) -> list[dict]` (polls "Continue wait", explicit limit, never `renewQuery`), `meta()` |
| `demo` | `build(dirty, seed, scale) -> {"parts","edges","orders","stock","malformed","expected"}`, `load(conn, data)` |

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

RDF/SHACL: `naming.rdf_*` helpers define the IRIs of entity classes, rows (reversible key encoding),
predicates (`<iri><entity>.<column>`), node shapes and property shapes (`<iri>shape/<id>/p`).

## 6. Check catalogue

| Stage | Check id | Pass when |
|---|---|---|
| doctor | `python`, `pg8000`, `dsn`, `connect`, `pg_version` | basics present; server_version_num >= 170000 |
| doctor | `privileges:select:<entity>`, `privileges:no_write:<entity>`, `output_schema` | check role can read, cannot write sources, can create in the output schema |
| doctor | `reason:owlready2`, `reason:pyshacl`, `reason:java`, `cube:node`, `cube:adapter`, `cube:native` | only for components named in `--require`; others go to `not_proven` when missing |
| demo | `demo:rows:<table>`, `demo:copy_on_error` | loaded rows equal the generator; `ON_ERROR ignore` skipped exactly the malformed rows |
| build | `shape:<id>` | no violations (a `warning` shape passes and reports its count) |
| build | `closure:<h>`, `closure_apply:<h>` | acyclic (or `on_cycle = "report"`) within `max_depth`; table equals recomputation |
| reason | `shacl:<shape>` | pySHACL focus nodes == SQL violation keys (set equality) |
| reason | `owl:consistency` | HermiT finds the ontology consistent |
| reason | `v5:closure:<h>` | OWL transitive closure pairs == SQL closure pairs |
| reason | `v5:defined:<name>` | members of each defined class == the SQL definition |
| cube | `cube:model`, `cube:meta`, `cube:gold_members:<gold>` | model generated; Cube metadata has every public member; gold members exist |
| verify | `v1:<gold>` | first binding of each gold query matches exactly |
| verify | `v2:<gold>:<stratum>` | every sampled binding of the stratum matches exactly |
| verify | `v3:total:<member>`, `v3:additive:<measure>:<dimension>`, `v3:pair:<m1>+<m2>` | totals equal SQL; groups add up; measures queried together equal measures queried alone |
| verify | `v4:<operator>:<target>` | the mutant is killed by the gold suite (observable mutants only) |
| report | `stage:<name>` | mirrors each stage status (overall exit code) |

## 7. Verification semantics

**V1/V2 compare.** Gold rows come from the gold SQL (bound parameters, read-only transaction). Cube rows come
from `cube_query` after substituting every string equal to `:<param>` (also inside lists). `map` renames Cube
members to gold columns and must cover exactly the queried members. Rows are matched on the canonical key;
duplicate keys fail. For matched rows every column must be canonically identical; the only declared
equivalence is `zero_if_null` (a Cube NULL counts as 0 for those columns). Both sides empty is a pass that is
counted as `empty_bindings`. A gold query whose Cube members belong to a hierarchy whose `closure:<h>` did not
pass in `build` is `not_run`.

**Bindings.** No parameters: one binding `all`. `param_sets`: one stratum `param_sets` with each set.
One swept parameter: `values` (stratum `values`), `values_of = "entity.column"` (every distinct non-null value,
stratum `values`), or `strata` sampled from the hierarchy (k = ceil(sweep_k / number of strata) per stratum,
deterministic order `md5(id || seed), id`):

| Stratum | Nodes (node ids = node-entity keys plus edge endpoints) |
|---|---|
| `top` | has children, no parents |
| `mid` | has parents and children |
| `leaf` | has parents, no children |
| `leaf_parent` | has children, and none of its children has children |
| `deep` | has children; ordered by the depth of its subtree (deepest first) |
| `shared` | has at least two distinct parents |
| `empty` | node-entity keys without children |

An empty stratum is `not_proven`, not a failure.

**V3 query shapes** (derived from the model): `total` compares each measure's Cube total with SQL computed from
the configuration (`cube_gen.where_sql`), and the closure cube's `qty`/`pairs` with the closure table;
`additive` checks that groups by the ontology class column add up to the total for measures joined to the node
cube; `pair` checks that measures queried together (same cube or joined cubes) equal the same measures queried
one at a time, per key, which exposes join fan-out.

**V4 mutation tests.** Each operator patches a copy of the model; a fresh Cube process serves each mutant and
the suite (V1, V2 with `mutant_bindings` per stratum, V3) runs until the first failure (killed). Before
running, a witness SQL counts rows where the mutation changes results on the current data; witness 0 means
the mutant is equivalent here and goes to `not_proven`. A mutant with witness > 0 that survives fails.

| Operator | Mutation | Witness |
|---|---|---|
| `closure_depth_shift` | `min_depth + 1` | closure rows |
| `closure_path_count_one` | `path_count` = 1 | pairs with path_count > 1 |
| `closure_qty_single_path` | `qty / path_count` | pairs with path_count > 1 and qty <> 0 |
| `measure_filter_drop` | exact filter removed | rows excluded by the filter with a non-null (non-zero for sum) value |
| `measure_normalization_drop` | normalised filter compared on raw values | rows whose raw value differs from the normalised match |
| `measure_agg_to_max` | sum/count measure becomes max | at least two keys with non-zero values |
| `dated_shift_one_day` | `ts + 1 day` | filtered rows dated on a window end, or the day before a window start |
| `closure_join_swap` | measure joined on the other end of the pair | pairs whose two ends have different measure values |
| `closure_end_swap` | descendant copy joined on `ancestor` | pairs whose ends differ in the ontology class column (or all pairs) |

**SHACL vs SQL.** `build` writes `shapes.ttl` from the configuration: a node shape per configuration shape
targeting the entity class, with one property shape (`sh:class`, `sh:minExclusive`, `sh:minInclusive`,
`sh:maxInclusive`) and the configured severity. `reason` exports the needed rows as RDF in memory (target rows
with the shape paths; referenced entities with their types), runs pySHACL without inference and compares, per
shape, the set of focus-node keys with the keys in `v_shape_<id>`. Shapes defined only by `sql_violation` have
no SHACL form and are listed in `not_proven`.

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
with the asserted `<h>` edges.

## 8. Tests

- `python -m unittest discover -s tests -v` from the repository root (stdlib unittest only).
- Unit tests need no database. Integration tests are skipped unless `SXC_TEST_ADMIN_DSN` names a superuser
  connection; `tests/pgfixture.py` creates a throw-away database, the loader/check roles, the schemas and loads
  the demo. Cube tests are also skipped unless the adapter is installed (`cube_run.installed(cube_home())`),
  OWL tests unless Java is available (`SXC_JAVA` or `java` on PATH).
- The demo's `expected` numbers come from an independent Python implementation and are the reference for the
  closure, the shapes and the dirty-data findings.

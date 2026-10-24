# crosscheck.toml reference

`crosscheck.toml` is the single source of truth for every stage. The generated SQL, SHACL shapes, Cube
model and OWL ontology are derived from it; when a check fails, fix this file (or the gold SQL, or the
data), never the generated output. The demo configuration `demo/crosscheck.toml` uses every section.

Validation runs before any stage does work. An invalid entry stops the run with exit code 2 and a
message that names the entry where it can, for example `crosscheck.toml [measures.on_hand]: ...`.
Unknown option keys are rejected at the top level (only the tables below exist) and in `[source]`,
`[entities.*]`, `[hierarchies.*]`, `[[shapes]]`, `[measures.*]`, `[ontology]`, `[[ontology.defined]]`,
`[cube]`, `[verify]`, `[[gold]]`, `[gold.params.*]` and `[[gold.expect]]`. User-defined keys in mappings such as
`ontology.classes`, measure filters, `gold.map` and parameter bindings remain supported; they are not option
names. The file is read as UTF-8, with or without a byte order mark.

## Conventions

- **Identifiers** (entity, hierarchy, measure, shape and gold ids, schema, table and column names) must
  be plain lower-case SQL identifiers: `^[a-z_][a-z0-9_]{0,62}$`. They are quoted when used in SQL;
  mixed-case or quoted names in the database are not supported.
- **Tables** are schema-qualified: `"schema.table"`.
- **Cube members** are written `cube.member`, for example `"m_on_hand.value"`. Cube names come from
  the configuration: `<entity>`, `<h>_closure`, `<h>_ancestor`, `<h>_descendant` and `m_<measure>`.
- **Credentials are never stored here.** `source.dsn_env`, `source.owner_dsn_env` and `cube.dsn_env`
  hold the NAME of an environment variable (`[A-Za-z_][A-Za-z0-9_]*`) that holds the DSN. Any other value
  is refused without being repeated in the message (which adds a hint when the value looks like a DSN),
  and a key named `dsn` or containing `password` is rejected in `[source]` and `[cube]`.
- **Integers** are type-checked: booleans, floats and strings are refused, and the limits in the tables
  below apply.
- Paths (`gold.sql`) are relative to the directory of `crosscheck.toml`. The output directory defaults
  to `.sxc` next to it.

## Top level

| Key | Type | Default | Meaning |
|---|---|---|---|
| `version` | integer | required | Format version. Only `1` is supported. |

The other top-level keys are the tables `source`, `entities`, `hierarchies`, `shapes`, `measures`,
`ontology`, `cube`, `verify` and `gold`; any other key is exit 2.

## `[source]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `dsn_env` | string | `"SXC_PG_DSN"` | Name of the environment variable holding the check role's DSN (`postgresql://user@host:port/database`). |
| `owner_dsn_env` | string | `"SXC_PG_OWNER_DSN"` | Name of the environment variable holding the loader role's DSN; used only by `demo`. |
| `schemas` | list of strings | required | Source schemas. Every entity table must be in one of them. |
| `output_schema` | string | `"sxc_out"` | Schema owned by the check role, where closures, cycle examples and `v_shape_<id>` views are created. Must differ from every source schema. |
| `statement_timeout_ms` | integer | `60000` | `statement_timeout` for the stages' database transactions; at least 1000. A build waiting for another build of the same output schema counts against it. |
| `data_revision_sql_file` | string | `""` | Optional configuration-relative trusted SQL file returning exactly one nonempty text revision. Start/end probes and report freshness checks record only its hash. |
| `runtime_evidence` | boolean | `False` | Opt into actual full-pipeline runtime binding at build/reason/cube/verify boundaries and report. Requires both PG roles, Node, Java, HermiT and the selected installed Cube adapter. Missing or changed evidence fails closed. |

When `data_revision_sql_file` is configured, the source owner must update a never-reused, monotonically
advancing revision in the same transaction as **every** relevant data change. The probe must cover all
referenced source data; a partial counter or a manually changed environment value is insufficient.
If Cube uses a different DSN, both endpoints must return the same revision. Changed/missing bindings
invalidate evidence; probe failure cannot certify freshness. The contract does not share an MVCC
snapshot between independent SQL/Cube sessions and depends on the owner's revision discipline.
Without a probe, existing verdicts remain compatible and data freshness is explicitly `unbound` /
`not_proven`. Engine and probe-file fingerprints also participate in evidence freshness.
The engine fingerprint covers plugin Python/CLI sources, adapter source/lock inputs, Python version
and direct Python package-version metadata. By itself it does not identify the actual PostgreSQL,
Node.js or Java executable version, or establish an authenticated/tamper-proof evidence chain. Results produced
before this fingerprint existed must be rerun; compatibility applies to new runs without a revision probe.

### Actual runtime binding (opt-in)

```toml
[source]
# Keep the other required source keys.
runtime_evidence = true
```

This option observes each connected PostgreSQL source/Cube endpoint's `server_version_num` and
hashes allowlisted session/database semantics (timezone, date/interval style, encoding, collation,
database locale/ICU rules and collation versions, string/float/bytea settings, text-search
configuration and search path). Nullable locale/rule metadata is preserved exactly in the hash. The two
endpoint observations must agree. It records normalized versions from the actual selected Node and
Java executables, hashes the HermiT JAR and actual classpath contents used by owlready2, and hashes the selected Cube
adapter, native artifact and installed dependency contents. Package pins alone are insufficient.
Only normalized versions, fixed identifiers, counts and hashes are retained; no DSNs, hostnames,
local paths, raw process output or database setting values are saved.

The contract covers the **complete pipeline**, including a `build` run in this mode: all listed
components must already be available. No dependency install or service startup occurs. Version probes
have short deadlines and bounded output; artifact traversal has file/size limits and unsupported
links fail closed. npm `.bin` launch conveniences are excluded because the adapter starts Node and
`server.js` directly. Full installed-content hashing adds I/O at each stage boundary and report.
In this mode, nonempty `JAVA_TOOL_OPTIONS`, `JDK_JAVA_OPTIONS` or `_JAVA_OPTIONS` are rejected with
a fixed reason code: otherwise injected JVM options/agents could affect HermiT without appearing in
the selected classpath evidence. Their contents are never retained. The default disabled mode keeps
the existing environment behavior.

A missing, errored or changed probe cannot preserve a previous PASS. Reports reject legacy evidence
without the runtime binding when this option is enabled. With `false` (default), no new runtime
database/subprocess probes occur, and the report explicitly labels actual runtime binding `unbound`.
Other operations, such as configured data-revision probes or normal stage work, still perform their
own existing I/O. Runtime fingerprints are observed boundary evidence, not binary attestation,
protection against between-probe replacement, or a shared PostgreSQL/Cube MVCC snapshot.

A DSN may carry the password in the URL (percent-encoded: reserved characters such as `/ # ? @ : %` in the
user name or password must be encoded); when it does not, `PGPASSWORD` is used. TLS settings go in the
DSN: `?sslmode=disable|allow|prefer|require|verify-ca|verify-full` and `sslrootcert=<file>` (or
`system`); see SECURITY.md for what each mode verifies. `sslcert`, `sslkey`, `sslpassword`, `sslcrl`,
`sslcrldir` and `sslcertmode` are refused. A malformed DSN is exit 3 naming only the variable.

## `[entities.<name>]`

One table per entity. `<name>` is an identifier and becomes the entity's Cube name and RDF class.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `table` | string | required | `"schema.table"`; the schema must be listed in `source.schemas`. |
| `key` | list of strings | required | Key columns identifying a row. A single-column key becomes the Cube primary key; a composite key gets a hidden synthetic key. Node entities and `class` targets need a single-column key. |

## `[hierarchies.<name>]`

A parent/child relation stored in an edge entity. `<name>` names the closure table `<name>_closure`,
the cycles table `<name>_cycles`, the Cube cubes `<name>_closure`, `<name>_ancestor` and
`<name>_descendant`, and the OWL properties `<name>` and `<name>_plus`.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `edge` | string | required | Entity holding one row per parent/child line. Several lines between the same pair form one edge whose quantity is their sum. |
| `node` | string | required | Entity whose single-column key identifies the nodes. |
| `parent` | string | required | Parent column of the edge entity. |
| `child` | string | required | Child column of the edge entity. |
| `quantity` | string | required | Quantity column of the edge entity. The closure's `qty` is the sum over all paths of the product of the edge quantities. |
| `max_depth` | integer | `32` | Longest path the closure may contain, 1 to 64. A longer path fails `closure:<h>` instead of being truncated. |
| `on_cycle` | string | `"error"` | `"error"`: a cycle fails `closure:<h>` and leaves the closure table untouched. `"report"`: edges inside each cycle are dropped, the rest is closed, and the cycles are reported. |

## `[[shapes]]`

Data rules. Each shape becomes a SQL view `v_shape_<id>` with the key columns of the violating rows and,
when it has a SHACL form, a node shape in `build/shapes.ttl`. A shape needs at least one of `class`, a
bound, or `sql_violation`; when it has several, a row violating any of them violates the shape.

`reason` compares a shape with SHACL only when it has a separate SHACL form. A shape with `sql_violation`
(also when it has `class` or bounds as well) has none and is reported as not proven, "checked only in SQL",
instead of compared. A shape that combines `class` with bounds is compared: `build/shapes.ttl` gives it
two property shapes (`sh:class` on the column, the bounds on the same value written as a number), and a
row that violates either one violates the shape, as in its view. Its target and class-entity rows count
against `verify.shacl_max_rows` (releases up to 0.5.0 did not export such shapes).

Shapes on the same `target` and `path` that need different kinds of RDF value (a `class` shape and a
bound shape, or `class` shapes naming different entities; a shape with `class` and bounds counts as a `class`
shape here, since its bounds read a separate value predicate) are all compared: `reason` puts each shape in
the first value group in which its column carries no other kind of value, and validates each group on its own
data graph. Without such a shared column every shape is in one group (`main`) and the export is the same
as without groups. A shape that collides opens an extra group (`extra:1`, `extra:2`, ...), which exports
its target rows (and referenced keys) again; `verify.shacl_max_rows` counts the rows of all groups
together, so adding a colliding shape can push the export over the limit, and then every SHACL check,
not only the new shape's, is `not_run` (`resource_limit`).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `id` | string | required | Identifier, unique among shapes. |
| `target` | string | required | Entity whose rows are checked. |
| `path` | string | `""` | Column of the target checked by `class` and the bounds. |
| `class` | string | `""` | Referential rule: a non-null `path` value must be a key of this entity (single-column key). SHACL form: `sh:class`. |
| `min_exclusive` | number | none | `path` must be greater than this. SHACL form: `sh:minExclusive`. |
| `min_inclusive` | number | none | `path` must be at least this. SHACL form: `sh:minInclusive`. |
| `max_inclusive` | number | none | `path` must be at most this. SHACL form: `sh:maxInclusive`. |
| `sql_violation` | string | `""` | Trusted SQL predicate over the alias `t` that is true for violating rows, for example `"t.qty_open > t.qty_total"`. It has no SHACL form, so `reason` lists the shape as not proven. Reports label it as trusted SQL. |
| `severity` | string | `"violation"` | `"violation"` fails `shape:<id>` when rows violate it; `"warning"` passes and reports the count. |
| `fk_ddl` | string | `"none"` | `"review_only"` writes a `NOT VALID` foreign-key statement for a `class` shape to `build/fk_review.sql`, for a person to review. It is never executed. |

Bounds accept integers, floats or numeric strings and are compared as exact decimals. NULL values never
violate `class` or bound rules (SHACL treats a missing value as no value).

## `[measures.<name>]`

Aggregated values per node key, exposed as the Cube cube `m_<name>` with the dimension `key` and the
measure `value`.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `entity` | string | required | Entity holding the rows to aggregate. |
| `column` | string | required | Column to aggregate. |
| `agg` | string | required | `"sum"`, `"count"`, `"min"` or `"max"`. |
| `by` | string | required | Grain column; its values are node keys. Rows with a NULL grain are not part of the measure. |
| `filter` | table of lists | `{}` | Exact match on raw values, for example `{ status = ["FIRM"] }`. Values are strings or integers; each list is non-empty. |
| `filter_normalized` | table of lists | `{}` | Match on `upper(btrim(column))`. Values must already be upper-case and trimmed strings. A column cannot be in both filters. |
| `date_column` | string | `""` | Makes the measure row-level with a time dimension `ts`, for date-window gold queries. Date and timestamp-without-time-zone columns are exposed as UTC timestamps. |
| `closure_join` | string | `""` | `"ancestor"` or `"descendant"`: join the measure to that end of the closure cube. Needs exactly one hierarchy; not allowed with `date_column`. |
| `node` | string | `""` | Entity whose key `by` refers to. Defaults to the node entity of the only hierarchy. |

## `[ontology]`

Optional. When present, `reason` builds an OWL ontology for one hierarchy (and `report` then requires
`reason`, as it does when there are `[[shapes]]`).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `iri` | string | `"https://example.org/sxc#"` | Base IRI for classes, properties, rows and shapes; an http(s) IRI ending with `#` or `/`. |
| `hierarchy` | string | required | Hierarchy modelled in OWL. |
| `class_column` | string | `""` | Column of the node entity holding the class value. V3 `additive` tests group the sum/count measures by it on every cube Cube joins through many-to-one or one-to-one steps (the node cube and the closure's ancestor and descendant copies); without it there are none. |
| `classes` | table of strings | `{}` | Column value to OWL class name, for example `{ raw = "RawMaterial" }`. Names match `^[A-Za-z_][A-Za-z0-9_]*$` and are distinct; the classes are pairwise disjoint subclasses of the node class. |
| `no_children` | list of strings | `[]` | Class values (keys of `classes`) whose items must not have children; each becomes the axiom `C SubClassOf not (<h> some Thing)`. |
| `defined` | list of tables | `[]` | Defined classes, written as `[[ontology.defined]]` entries (below). |

### `[[ontology.defined]]`

Defined classes compared with their SQL definition (`v5:defined:<name>`).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | OWL class name, distinct from the `classes` names. |
| `kind` | string | required | `"has_child"`, `"has_parent"`, `"contains_class"`, `"class_with_child"` or `"class_used_as_part"`. |
| `class` | string | `""` | Key of `classes`; required for `contains_class`, `class_with_child` and `class_used_as_part`. |

| kind | OWL definition | SQL definition (node-table rows only) |
|---|---|---|
| `has_child` | Node and (`<h>` some Thing) | parents of at least one edge |
| `has_parent` | Node and (inverse `<h>` some Thing) | children of at least one edge |
| `contains_class` | Node and (`<h>_plus` some C) | ancestors in the closure of a node of class C |
| `class_with_child` | C and (`<h>` some Thing) | parents of class C |
| `class_used_as_part` | C and (inverse `<h>` some Thing) | children of class C |

## `[cube]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `version` | string | `"1.8.3"` | Cube release. Must equal the release this version of semantic-crosscheck pins (the adapter's lockfile and native hashes belong to it). |
| `port` | integer | `0` | Port on 127.0.0.1, 0 to 65535; `0` lets the adapter pick a free port. |
| `dsn_env` | string | `""` | Name of the environment variable with the DSN Cube uses; empty means `source.dsn_env`. Sessions are forced read-only either way. |
| `startup_timeout_s` | integer | `90` | Seconds to wait for the adapter to become ready; at least 1. |
| `query_timeout_s` | integer | `120` | Seconds to wait for one Cube query, including "Continue wait" polling; at least 1. It is also the `statement_timeout` of every Cube database session and Cube's own query timeout, so an abandoned query stops in PostgreSQL too. |

## `[verify]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `sweep_k` | integer | `40` | Parameter values per gold query in V2, spread over its strata (k = ceil(sweep_k / strata) per stratum). At least 1. |
| `seed` | integer | `7` | Seed of the deterministic sampling order `md5(id \|\| seed), id` (strata and the 2,000-key sample of large V3 pair tests). Any integer. |
| `mutation_operators` | string or list | `"default"` | `"default"` or a list of V4 operator names: `closure_depth_shift`, `closure_path_count_one`, `closure_qty_single_path`, `measure_filter_drop`, `measure_normalization_drop`, `measure_agg_to_max`, `dated_shift_one_day`, `closure_join_swap`, `closure_end_swap`. An unknown name is exit 2 as soon as the verify stage starts, before any check runs. |
| `mutant_bindings` | integer | `3` | Bindings per stratum replayed against each mutant. At least 1. |
| `shacl_max_rows` | integer | `200000` | Upper bound on the rows exported to RDF for the SHACL comparison; at least 1. Above it each planned shape is `not_run` (`resource_limit`), with an explanation in `not_proven`; reason and report cannot pass. |
| `owl_max_individuals` | integer | `50000` | Upper bound on the individuals (node keys and edge endpoints) given to HermiT; at least 1. Above it OWL consistency and V5 checks are `not_run` (`resource_limit`), with an explanation in `not_proven`; reason and report cannot pass. |
| `pair_key_budget` | integer | `2000` | Unreleased (after 0.8.0). The largest distinct key count for which the baseline V3 run of a pair test compares every key, in chunks of 2,000 plus the NULL key group, instead of the 2,000-key sample; at least 2000, the default, which keeps the sample. The Cube queries grow with the number of chunks, and every key up to the budget is read in one statement under `source.statement_timeout_ms` (a timeout makes the test an `error`) and held in memory, so size it to the memory and time available; each Cube query reads its own snapshot (listed in `not_proven`) and V4 replays keep the sample. |

## `[[gold]]`

Hand-written reference queries. The gold SQL is trusted code; it should compute its answer
independently (for example with its own recursive CTE instead of reading the generated closure table),
otherwise a shared mistake goes unnoticed.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `id` | string | required | Identifier, unique among gold queries. |
| `sql` | string | required | Path of the SQL file, relative to `crosscheck.toml`, read as UTF-8 with or without a byte order mark. One statement; parameters are written `:name` and bound as values (`stream` and `types` are reserved names); the query runs in a read-only transaction. |
| `key` | string or list of strings | required | Gold column(s) forming a unique row key. Each must be in `map`. |
| `cube_query` | table | `{}` | The Cube query: `dimensions`, `measures`, `filters`, `timeDimensions` (a `dateRange`, no `granularity`). Every string equal to `:<param>` is replaced by the binding, also inside lists. |
| `map` | table of strings | required | Gold column to Cube member. Must cover exactly the `cube_query` dimensions and measures. Each column is compared in the form of its member's Cube type: `string` as exact text, `number` as an exact decimal, `time` as a UTC timestamp, `boolean` as a boolean. |
| `zero_if_null` | list of strings | `[]` | Gold columns the gold SQL coalesces to 0; a Cube NULL counts as 0 there. The only declared equivalence. |
| `params` | table | `{}` | At most one swept parameter, as `[gold.params.<name>]` with exactly one of `strata` (list of `top`, `mid`, `leaf`, `leaf_parent`, `deep`, `shared`, `empty`; optional `hierarchy`), `values_of` (`"entity.column"`: every distinct non-null value) or `values` (explicit list). |
| `param_sets` | list of tables | `[]` | Explicit bindings for multi-parameter queries, for example `[{ date_from = "2026-01-01", date_to = "2026-03-31" }]`. Not together with `params`. |
| `expect` | list of tables | `[]` | Literal expected-row cases, as `[[gold.expect]]` (below). Only the `expect` stage reads them. |

Without `params` and `param_sets` a gold query has one binding, `all`. An empty stratum is reported as
not proven, not as a failure.

### `[[gold.expect]]`

After 0.8.0, not released yet. Literal expected rows of the gold query for a small fixture, in the form of dbt and
SQLMesh unit tests: a third oracle for a gold SQL and a configuration that share one mistake. The `expect` stage runs
the gold SQL and the Cube query on a dedicated fixture database (`SXC_EXPECT_DSN`, see
[xc-expect](../../../commands/xc-expect.md)) and compares both with these rows; no other stage reads them.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `source` | string | required | `"authored"`: the rows were written from the business specification named by `spec`, the only independent evidence. `"captured"`: the rows were taken from an earlier run; they detect regressions only, never count as independent and are listed in `not_proven`. |
| `rows` | list of tables | required | The expected output rows, keyed by gold columns (the keys of `map`); `rows = []` expects no rows. Every row lists every gold column unless `partial = true`. Values are strings, numbers, booleans, dates or times; TOML has no NULL, so a NULL cell is written `{ null = true }`, which a `zero_if_null` column cannot expect. Write exact decimals as integers or strings (`"2.50"`). Keys must be unique. Rows are compared like V1: by key, in the form of each column's Cube type, order ignored. |
| `spec` | string | `""` | Where in the business specification the rows come from (a document and section). Required for `authored`. |
| `params` | table | `{}` | The gold query's parameter values for this case: exactly the names of `params`, or the names of an entry of `param_sets`; empty for a gold query without parameters. Values are scalars. |
| `partial` | boolean | `False` | `true` compares only the columns the rows name; every row then names the same columns, every key column among them. |

A malformed case is exit 2 naming `gold[<n>] <id> expect[<i>]` (and `rows[<r>]`); messages name keys and
positions, never values.

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `SXC_PG_DSN` | every database stage, Cube | Check role DSN (name configurable with `source.dsn_env`). |
| `SXC_PG_OWNER_DSN` | `demo` | Loader role DSN (name configurable with `source.owner_dsn_env`). |
| `PGPASSWORD` | database stages, Cube | Password used when the DSN has none (`~/.pgpass` is not read). |
| `SXC_JAVA` | `doctor`, `reason` | Absolute path of the `java` executable for HermiT, or a command name on `PATH`, when `java` itself is not on `PATH`. |
| `SXC_CUBE_HOME` | `cube-install`, `doctor`, `cube`, `verify` | Install directory of the Cube adapter (default: a per-user cache); a relative value is resolved against the current directory. |
| `SXC_CUBE_DEBUG` | `cube`, `verify` | `1` writes the full Cube error text to `server.log` for local debugging; the log can then hold row values. |
| `SXC_DEBUG` | every stage | `1` prints the full traceback of an unexpected exception for local debugging. |
| `SXC_EXPECT_DSN` | `expect` | DSN of the dedicated fixture database for `[[gold.expect]]` (name configurable with `--dsn-env`); refused when it names the database of `source.dsn_env`, `source.owner_dsn_env` or `cube.dsn_env`. |

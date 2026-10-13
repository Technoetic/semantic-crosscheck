# crosscheck.toml reference

`crosscheck.toml` is the single source of truth for every stage. The generated SQL, SHACL shapes, Cube
model and OWL ontology are derived from it; when a check fails, fix this file (or the gold SQL, or the
data), never the generated output. The demo configuration `demo/crosscheck.toml` uses every section.

Validation runs before any stage does work. An invalid entry stops the run with exit code 2 and a
message that names the entry where it can, for example `crosscheck.toml [measures.on_hand]: ...`.
Unknown keys are rejected in `[source]`, `[hierarchies.*]`, `[[shapes]]`, `[measures.*]`, `[cube]`,
`[verify]` and `[[gold]]`. In `[entities.*]`, `[ontology]`, `[[ontology.defined]]`, `[gold.params.*]`
and `[output]` unknown keys are ignored, so check the spelling there.

## Conventions

- **Identifiers** (entity, hierarchy, measure, shape and gold ids, schema, table and column names) must
  be plain lower-case SQL identifiers: `^[a-z_][a-z0-9_]{0,62}$`. They are quoted when used in SQL;
  mixed-case or quoted names in the database are not supported.
- **Tables** are schema-qualified: `"schema.table"`.
- **Cube members** are written `cube.member`, for example `"m_on_hand.value"`. Cube names come from
  the configuration: `<entity>`, `<h>_closure`, `<h>_ancestor`, `<h>_descendant` and `m_<measure>`.
- **Credentials are never stored here.** DSNs come from environment variables named in `[source]` and
  `[cube]`; a `[source]` key named `dsn` or containing `password` is rejected.
- Paths (`gold.sql`) are relative to the directory of `crosscheck.toml`. The output directory defaults
  to `.sxc` next to it.

## Top level

| Key | Type | Default | Meaning |
|---|---|---|---|
| `version` | integer | required | Format version. Only `1` is supported. |

## `[source]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `dsn_env` | string | `"SXC_PG_DSN"` | Environment variable holding the check role's DSN (`postgresql://user@host:port/database`). |
| `owner_dsn_env` | string | `"SXC_PG_OWNER_DSN"` | Environment variable holding the loader role's DSN; used only by `demo`. |
| `schemas` | list of strings | required | Source schemas. Every entity table must be in one of them. |
| `output_schema` | string | `"sxc_out"` | Schema owned by the check role, where closures, cycle examples and `v_shape_<id>` views are created. Must differ from every source schema. |
| `statement_timeout_ms` | integer | `60000` | `statement_timeout` for the stages' database transactions. |

A DSN may carry the password in the URL (percent-encoded); when it does not, `PGPASSWORD` is used.

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
bound, or `sql_violation`; when it has several, a row violating any of them violates the shape. A shape
with `sql_violation`, or one that combines `class` with bounds, has no SHACL form and is reported as not
proven by `reason` instead of compared.

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

Optional. When present, `reason` builds an OWL ontology for one hierarchy.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `iri` | string | `"https://example.org/sxc#"` | Base IRI for classes, properties, rows and shapes; an http(s) IRI ending with `#` or `/`. |
| `hierarchy` | string | required | Hierarchy modelled in OWL. |
| `class_column` | string | `""` | Column of the node entity holding the class value. |
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
| `dsn_env` | string | `""` | Environment variable with the DSN Cube uses; empty means `source.dsn_env`. Sessions are forced read-only either way. |
| `startup_timeout_s` | integer | `90` | Seconds to wait for the adapter to become ready. |
| `query_timeout_s` | integer | `120` | Seconds to wait for one Cube query, including "Continue wait" polling. |

## `[verify]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `sweep_k` | integer | `40` | Parameter values per gold query in V2, spread over its strata (k = ceil(sweep_k / strata) per stratum). At least 1. |
| `seed` | integer | `7` | Seed of the deterministic sampling order `md5(id \|\| seed), id`. |
| `mutation_operators` | string or list | `"default"` | `"default"` or a list of V4 operator names: `closure_depth_shift`, `closure_path_count_one`, `closure_qty_single_path`, `measure_filter_drop`, `measure_normalization_drop`, `measure_agg_to_max`, `dated_shift_one_day`, `closure_join_swap`, `closure_end_swap`. |
| `mutant_bindings` | integer | `3` | Bindings per stratum replayed against each mutant. At least 1. |
| `shacl_max_rows` | integer | `200000` | Upper bound on the rows exported to RDF for the SHACL comparison. |
| `owl_max_individuals` | integer | `50000` | Upper bound on the individuals (node keys and edge endpoints) given to HermiT. |

## `[output]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `show_rows` | integer | `0` | Number of example rows written to a local samples file. `0` writes none. It is the only way rows reach a file; results and reports never contain row values. Do not share or commit the samples file. |

## `[[gold]]`

Hand-written reference queries. The gold SQL is trusted code; it should compute its answer
independently (for example with its own recursive CTE instead of reading the generated closure table),
otherwise a shared mistake goes unnoticed.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `id` | string | required | Identifier, unique among gold queries. |
| `sql` | string | required | Path of the SQL file, relative to `crosscheck.toml`. Parameters are written `:name` and bound as values; the query runs in a read-only transaction. |
| `key` | string or list of strings | required | Gold column(s) forming a unique row key. Each must be in `map`. |
| `cube_query` | table | `{}` | The Cube query: `dimensions`, `measures`, `filters`, `timeDimensions` (a `dateRange`, no `granularity`). Every string equal to `:<param>` is replaced by the binding, also inside lists. |
| `map` | table of strings | required | Gold column to Cube member. Must cover exactly the `cube_query` dimensions and measures. |
| `zero_if_null` | list of strings | `[]` | Gold columns the gold SQL coalesces to 0; a Cube NULL counts as 0 there. The only declared equivalence. |
| `params` | table | `{}` | At most one swept parameter, as `[gold.params.<name>]` with exactly one of `strata` (list of `top`, `mid`, `leaf`, `leaf_parent`, `deep`, `shared`, `empty`; optional `hierarchy`), `values_of` (`"entity.column"`: every distinct non-null value) or `values` (explicit list). |
| `param_sets` | list of tables | `[]` | Explicit bindings for multi-parameter queries, for example `[{ date_from = "2026-01-01", date_to = "2026-03-31" }]`. Not together with `params`. |

Without `params` and `param_sets` a gold query has one binding, `all`. An empty stratum is reported as
not proven, not as a failure.

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `SXC_PG_DSN` | every database stage, Cube | Check role DSN (name configurable with `source.dsn_env`). |
| `SXC_PG_OWNER_DSN` | `demo` | Loader role DSN (name configurable with `source.owner_dsn_env`). |
| `PGPASSWORD` | database stages | Password used when the DSN has none. |
| `SXC_JAVA` | `doctor`, `reason` | Path of the `java` executable for HermiT when `java` is not on `PATH`. |
| `SXC_CUBE_HOME` | `cube-install`, `cube`, `verify` | Install directory of the Cube adapter (default: a per-user cache). |

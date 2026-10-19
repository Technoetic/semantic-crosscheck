# Pitfalls behind common failures

Each layer has traps that make two correct-looking formulations disagree. The harness already avoids
the ones listed here; this page explains them so a failing check can be traced to its cause, and so the
gold SQL and `crosscheck.toml` avoid them too.

## Cube

- **`renewQuery` is rejected.** Cube 1.8 refuses `/load` requests that set `renewQuery`. The client
  strips it and polls while Cube answers `"Continue wait"`. Do not add it to a gold `cube_query`.
- **The default row limit is 10,000.** Without an explicit `limit` Cube silently returns at most
  10,000 rows. The client always sends `limit` (50,000) and fails a query whose result reaches it
  (an `error` check with `cube=row_limit`) instead of comparing a truncated answer. The limit is not
  configurable: a query that needs more rows must be narrowed (filters, parameters) rather than compared
  partially. V3 pair tests group by every key, so above 2,000 keys they compare a deterministic sample of
  2,000 keys instead; their detail and not proven line give the sample's 95% detection bound
  (`d95_keys`), and a mismatch on fewer keys can be missed.
- **A many-to-one join that is one-to-many in the data inflates totals.** Cube trusts a declared
  `many_to_one` (or `one_to_one`) relationship and sums without deduplication, so a repeated key in the
  joined table counts the measure rows once per match. V3 `additive` tests sum the groups by the class
  column over such joins (the NULL group included) and compare them with the total, and compare the NULL
  group with independent SQL (an anti-join when Cube starts from the measure's cube; the matched rows
  without a class when it starts from the node cube and drops measure rows without a node row, which then
  fail the sum); give the node entity's key a primary key or unique constraint. A path with a
  `one_to_many` step is not additive by nature and gets no test (listed as not applicable).
- **Primary keys are hidden.** A primary-key dimension is not public unless it says `public: true`;
  querying a hidden member fails (HTTP 500). The generator makes single-column keys public. The
  synthetic `sxc_pk` of a composite-key cube stays hidden, so gold queries cannot use it and `/meta`
  does not list it.
- **`schemaPath` is relative to the working directory.** Cube resolves the model directory against the
  process's current directory, not against `server.js`. The runner starts the adapter with the run
  directory as working directory and the model in `model/`; starting `server.js` from elsewhere loads
  no model (or the wrong one).
- **The native module is required.** Cube Core 1.x loads `@cubejs-backend/native` at startup. Its npm
  postinstall would download the binary without an integrity check, so `cube-install` runs
  `npm ci --ignore-scripts` and fetches the release tarball itself, accepting it only when its SHA-256
  matches `native-sha256.json`. Without the module Cube does not start; a platform without a pinned
  tarball cannot run the Cube stages.
- **Date ranges are UTC; dates are not.** Cube applies a `dateRange` as UTC timestamps. A `date` or
  `timestamp without time zone` column compared in a session whose `TimeZone` is not UTC shifts the
  window by the offset, so a day can drop out or slip in at either end. The generator exposes such
  columns as `(col::timestamp AT TIME ZONE 'UTC')`, which selects whole calendar days whatever the
  session time zone is. Write gold date windows as inclusive dates (`BETWEEN CAST(:from AS date) AND
  CAST(:to AS date)`).
- **Read-only sessions come from `PGOPTIONS`.** The check role can write to the output schema, so Cube
  is not read-only by privilege alone. The runner removes every inherited `PG*`, `CUBEJS_*` and `SXC_*`
  variable, `NODE_OPTIONS` and every variable holding a `postgres://` URL, and sets
  `PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=...`, which makes every Cube
  transaction read-only. Starting the adapter by hand without it, or with another role, loses that
  guarantee.
- **The SQL planner is pinned by the runner.** Cube 1.8.3 chooses between its Tesseract and legacy SQL
  planners from `CUBEJS_TESSERACT_SQL_PLANNER` (default `true`). The runner always sets `true` and drops an
  inherited value with the other `CUBEJS_*` variables, and `cube:meta` records `cube_version` and
  `sql_planner`. A cube result that recorded another planner or version, or none, is stale in `report`
  (`cube_runtime_changed`, `cube_runtime_missing`): rerun cube and verify. Starting the adapter by hand
  without the variable leaves the choice to Cube's default.
- **Cube overrides `statement_timeout`.** Cube's Postgres driver runs `SET statement_timeout` on every
  connection from `CUBEJS_DB_QUERY_TIMEOUT` (default 10 minutes, also its queue's execution timeout),
  which replaces the value from `PGOPTIONS`. The runner sets both from `cube.query_timeout_s`, so a query
  the client gave up on stops in PostgreSQL instead of running on and holding Cube's queue.
- **Proxy variables reach loopback requests.** Python's default URL opener sends requests to 127.0.0.1
  through `HTTP_PROXY` or the system proxy unless `NO_PROXY` lists it, which would hand the JWT and the
  filter values to the proxy. The client uses an opener without proxies, so no `NO_PROXY` entry is
  needed.
- **Error messages quote values.** PostgreSQL and Cube errors echo the values they could not handle (an
  id compared with a number column, for example), quoted in a style that depends on the server's message
  language. The adapter therefore logs each error only as its class, SQLSTATE and a coarse category
  (model compile errors keep their text), and results keep only the SQLSTATE or HTTP status.
  `SXC_CUBE_DEBUG=1` shows the full text, locally.
- **Cube reads timestamps to the millisecond.** Time values are compared at millisecond resolution
  (truncated, as Cube's PostgreSQL type parser does); finer differences cannot be checked through Cube.
- **min and max keep their column's kind.** A min or max over a text column is compared as text
  (`'007'` is not `'7'`) and over a date or timestamp column as time; sums and counts as numbers.

## PostgreSQL 17

- **Recursive CTE anchor typmod mismatch.** In `WITH RECURSIVE` the non-recursive term fixes each
  column's type including its typmod. An anchor of type `numeric(18,6)` with a recursive term computing
  `w.qty * e.qty_per` (plain `numeric`) fails with "column ... has type numeric(18,6) in non-recursive
  term but type numeric overall". Cast the anchor to plain `numeric`, as the demo gold SQL does with
  `sum(qty_per)::numeric`. The same happens with `integer` against `bigint` (for example `count(*)`).
- **`CYCLE` marks the row, it does not drop it.** `CYCLE node SET is_cycle USING path` emits the row
  that revisits a node with `is_cycle = true` and stops expanding it; filter with `WHERE NOT is_cycle`.
  It detects a node repeated on the same path only, so a node reached by two different paths (a
  diamond) is not a cycle, which is what multi-path counting needs. The cycle columns must identify the
  node alone; adding the depth makes every row unique and no cycle is ever found. Enumerating paths is
  exponential on graphs with many diamonds, which is why `build` aggregates level by level instead.
- **`MERGE ... WHEN NOT MATCHED BY SOURCE` and `merge_action()` need PostgreSQL 17.** `MERGE` exists
  since 15, but deleting target rows absent from the source and `RETURNING merge_action()` (used to
  count inserts, updates and deletes) are new in 17. Older servers fail with a syntax error; `doctor`
  checks `server_version_num >= 170000`.
- **`COPY ... ON_ERROR ignore` needs PostgreSQL 17.** It skips rows whose values cannot be converted to
  the column types (text and CSV formats only) and reports how many were skipped. Rows with the wrong
  number of columns and other errors still abort the `COPY`. Keep the default `LOG_VERBOSITY`:
  `verbose` reports each skipped row and can quote its input.

## OWL

- **Open world.** A missing assertion is not a false one. An item without asserted children is not
  inferred to have none, so `no_children` axioms only make the ontology inconsistent when a child is
  actually asserted, and SQL's closed-world answer to "has no parent" has no OWL counterpart without
  closure axioms. The harness compares only definitions whose OWL and SQL meanings coincide (the
  existential kinds in `ontology.defined`).
- **Inferred property values omit the asserted pairs.** With `infer_property_values=True`, the values
  HermiT adds for the transitive `<h>_plus` do not repeat the pairs already asserted through the
  sub-property `<h>`. The OWL closure is the inferred `<h>_plus` values united with the asserted `<h>`
  edges; comparing only one of them with the SQL closure reports false differences.
- **HermiT needs Java.** owlready2 runs HermiT as a Java process. Set `SXC_JAVA` to the absolute path
  of the `java` executable when it is not on `PATH`; Java 11 or later. The JVM heap is limited, so keep
  `verify.owl_max_individuals` bounded. HermiT runs in a private temporary directory that is removed
  afterwards.
- **Never call the bundled Pellet.** owlready2 also ships the Pellet reasoner (AGPL-3.0) with
  different defaults and reasoning behaviour. semantic-crosscheck only calls HermiT; do not switch the
  reasoner to make a check pass.
- **An inconsistent ontology entails everything.** Once `owl:consistency` fails, every class membership
  follows, so the V5 comparisons cannot prove anything until the contradiction is fixed.

## SHACL

- **NULL means no value.** A SQL NULL becomes no triple in RDF, and SHACL value constraints
  (`sh:class`, `sh:minExclusive`, ...) only test values that exist. A NULL is therefore never a
  violation, and the SQL views test `IS NOT NULL` to match. A rule that a value must be present needs a
  different formulation (for example `sql_violation = "t.col IS NULL"`), which has no SHACL form.
- **`sh:class` needs `rdf:type`.** pySHACL runs without inference, so `sh:class ex:part` is satisfied
  only when the referenced node itself carries `rdf:type ex:part` in the data graph. The export types
  only the rows that exist in the referenced entity, so a dangling reference is a node without a type,
  which is exactly the violation the SQL view finds with its anti-join.
- **Bounds compare typed literals.** Bounds are written as `xsd:decimal`, and the compared values must
  be numeric literals too. A number stored as text is not comparable with a decimal bound and violates
  it in SHACL, so a column used with bounds must have a numeric type in PostgreSQL.
- **One RDF predicate carries one kind of value.** A column is one predicate in the exported RDF. A
  `class` shape needs its values as row IRIs and a bound shape as numbers, so when shapes on the same
  column need different kinds of value (or `class` shapes name different entities), each value would
  violate the other shape's constraint in one graph. `reason` therefore puts each shape in the first value
  group in which its column carries no other kind of value (`main`, then `extra:1`, `extra:2`, ... for the
  colliding shapes), validates each group on its own data graph and reads a group's results only for its
  own shapes; the check detail names the `value_group`. Without a colliding shape there is only `main`.
  One shape with both `class` and bounds is not such a collision: its bounds read the same value written
  as a number on a second predicate (`<entity>.<column>/value`), so it needs only row IRIs on the column.
  Each extra group exports its target rows again, and `verify.shacl_max_rows` limits the sum over all
  groups, so splitting one column across value kinds can push a large export over the limit (every SHACL
  check is then `not_run`).

## Comparing values

- **Text that looks like a number is still text.** V1 and V2 compare each column in the form of its Cube
  member type. In a `string` column `'1.10'` and `'1.1'` are different values (and different keys), so a
  gold query that formats a number differently from the source fails. Cast in the gold SQL rather than
  relying on a numeric reading. `number` columns compare exact decimals (`2.50` equals `2.5`); there is
  no tolerance.
- **Times are compared in UTC.** `time` members are compared as UTC timestamps at microsecond precision:
  a date equals its midnight, an aware timestamp is converted to UTC, and Cube's
  `YYYY-MM-DDTHH:MM:SS.mmm` output is read as UTC (which is how the generated time members render).

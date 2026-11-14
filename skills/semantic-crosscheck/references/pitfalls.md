# Pitfalls behind common failures

Since 0.10.0, `keys_not_answered` means requested pair-test keys were absent from the together or an
alone response; do not treat missing rows as NULL-valued answers or use an incomplete sample's requested size for
D95. `shacl:meta:generated` checks the generated shape graph against only the available shacl-shacl syntax subset:
correct syntax does not establish the intended rule. `tlp --oracle norec` compares bounded predicate counts only;
it is not full result-set equivalence and its replay must use the same oracle.
Standalone measure queries can legitimately omit keys with no fact rows. Requested-key comparisons conservatively
leave these sparse cases `not_run` too: independent existence evidence is required to distinguish legitimate absence
from an incomplete response, and substituting NULL would hide that uncertainty. Since 0.11.0,
`verify.existence_witness = true` supplies that evidence as a separate check, `v3w:pair_witness:<member>+<member>`,
read with the measure's own `where_sql`; it leaves `keys_not_answered` as it is and shares the configuration's
filters, so a wrong filter in `crosscheck.toml` stays invisible to it. It reads only the keys an alone answer left
out: a key an alone answer holds without a qualifying row (a measure cube that dropped a filter) is not witnessed.

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
  (`d95_keys`), and a mismatch on fewer keys can be missed; a sample never compares the NULL key group
  (its equals filter leaves it out), which not proven says since 0.9.0. Since 0.9.0,
  raising `verify.pair_key_budget` to at least the key count makes the baseline run compare
  every key in chunks of 2,000 keys plus the NULL key group, each chunk as small as the sample; every
  Cube query reads its own snapshot, shared with no other query. Every key up to the budget is read in
  one statement under `source.statement_timeout_ms` (a timeout is an `error`) and held in memory, and a
  chunked test whose Cube answers leave out keys it asked for is `not_run` (`keys_not_answered`).
- **A malformed answer is not an empty answer.** The client accepts a `/load` answer only in Cube's load
  envelope, a JSON object whose `data` is a list of row objects, and a gold query's rows must hold every
  mapped member (Cube answers each requested dimension and measure, NULL included). Any other HTTP 2xx
  answer without an `error` member, or a gold-query row without a mapped member, makes the check an `error`
  (`CubeResponseError`, or `MissingMember` for such a row), never zero rows, a NULL or a `zero_if_null` 0. A
  V1 or V3 check carries `cube=invalid_response` and names the adapter in its next action; a V2 stratum
  check counts such bindings by type under `errors`. In V4 the suite item is retried once, and when the
  retry gets one too the mutant is left unjudged (`suite_error`) instead of killed.
- **A many-to-one join that is one-to-many in the data inflates totals.** Cube trusts a declared
  `many_to_one` (or `one_to_one`) relationship and sums without deduplication, so a repeated key in the
  joined table counts the measure rows once per match. V3 `additive` tests sum the groups by the class
  column over such joins (the NULL group included) and compare them with the total, and compare the NULL
  group with independent SQL (an anti-join when Cube starts from the measure's cube; the matched rows
  without a class when it starts from the node cube and drops measure rows without a node row, which then
  fail the sum); give the node entity's key a primary key or unique constraint. A path with a
  `one_to_many` step is not additive by nature and gets no test (listed as not applicable).
- **A measure on the one side of a many_to_one join is counted once per group.** When a query reaches a
  measure's cube through another cube's `many_to_one` join (the closure cube's join to a measure with
  `closure_join`), Cube 1.8.3 aggregates the measure over the distinct primary keys of the measure's cube in
  each group, not once per joined row: the group of an ancestor with several descendants counts the
  ancestor's value once. V3 pair tests rely on this, and the V4 `closure_join_swap` witness counts the pairs
  whose two ends join different measure rows: equal values at both ends can still change a grouped sum, and
  always change which measure row (whose key is a public dimension) the pair joins.
  The previous bullet is the other direction: the measure's own cube joins a table whose key repeats.
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
  session time zone is. Write the gold date window so that it covers the same whole days, the last one
  included. On a `date` column that is an inclusive range:
  `col BETWEEN CAST(:from AS date) AND CAST(:to AS date)`. On a `timestamp without time zone` column
  that holds times of day the same form ends at midnight at the start of the last day, so the rows later
  that day are left out (a row at 12:00 on the last day is not counted). Compare the date instead,
  `col::date BETWEEN CAST(:from AS date) AND CAST(:to AS date)`, or write the window half-open,
  `col >= CAST(:from AS date) AND col < CAST(:to AS date) + 1`.
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
- **Property pairs compare finite numbers only.** Since 0.11.0, `verify.shacl_pairs = true`
  compares a rule `t.<a> <op> t.<b>` as `sh:lessThan` or `sh:lessThanOrEquals` (`shacl:pair:<id>`). pySHACL
  compares rdflib literals, not as SHACL or PostgreSQL do: rdflib orders any comparison involving NaN as
  less-than in both directions, so a NaN pair never violates (PostgreSQL orders NaN above every number, SHACL
  counts it as a violation), and literals of unrelated datatypes (a date or text against a number) are ordered by
  datatype IRI. A repeated key would give one RDF node the values of several rows and pair them across rows. So
  the check is `not_run` unless both columns are of the base types `smallint`, `integer`, `bigint` or `numeric`
  (`column_type`: `real`, `double precision`, text, dates and domains are refused), neither holds NaN or an
  infinity (`non_finite_values`) and the target key is unique (`duplicate_keys`). Change the column type or the
  data, or leave the option off: a `not_run` pair check keeps `reason` from passing.

## Comparing values

- **Text that looks like a number is still text.** V1 and V2 compare each column in the form of its Cube
  member type. In a `string` column `'1.10'` and `'1.1'` are different values (and different keys), so a
  gold query that formats a number differently from the source fails. Cast in the gold SQL rather than
  relying on a numeric reading. `number` columns compare exact decimals (`2.50` equals `2.5`); there is
  no tolerance.
- **Times are compared in UTC.** `time` members are compared as UTC timestamps at microsecond precision:
  a date equals its midnight, an aware timestamp is converted to UTC, and Cube's
  `YYYY-MM-DDTHH:MM:SS.mmm` output is read as UTC (which is how the generated time members render).

## Data rules

The `data-rules` stage evaluates each rule in PostgreSQL and in Python and gives a verdict only when both agree
([docs/data-rules.md](../../../docs/data-rules.md)). These are the traps that make the two disagree, or make both agree
on something the rule's author did not mean.

- **Text compares exactly.** No trimming, case folding or Unicode normalization: `'tmp-1'` does not start with
  `'TMP-'`, a value with a trailing space differs from the same value without it, and a name typed in decomposed
  Unicode (NFD) differs from the composed form (NFC). Clean the data or list every spelling in the set; never rely on
  a collation to make values equal.
- **bpchar pad spaces are not part of the value.** PostgreSQL compares `character(n)` values without their trailing
  pad spaces, and a cast to text drops them. Both evaluations read bpchar that way (every compiled reference and the
  export cast it to text), so a padded `char(n)` key equals the same key in a text column. In a `text` or `varchar`
  column a trailing space stays significant.
- **Nondeterministic collations are refused.** An ICU collation created with `deterministic = false` makes `=`, `IN`
  and `GROUP BY` treat different strings as equal in PostgreSQL, while Python compares code points. Rules that read
  such a column are `not_run` (`collation`); cast the column to a deterministic collation such as `COLLATE "C"` in a
  database view, together with every column it is compared with (next point), and point the entity at the view.
- **Compared text columns need one collation.** The node key and the hierarchy parent and child columns, and a
  `not_in_ref` column and its ref column, are compared with each other. When two of them carry different
  deterministic collations (say the node key `COLLATE "C"` and the edge endpoints an ICU collation, or a view that
  casts only one side), PostgreSQL cannot choose a collation: a rule comparing them fails with SQLSTATE 42P22 and is
  an `error` without a verdict, and parent and child columns of different collations already fail the height gate of
  a `stored_metric` rule with 42P21, which ends the snapshot so that no rule is decided. Never a pass, but the type
  check does not catch it before the run. Cast every compared column to the same collation in the views the entities
  point at.
- **Floating-point columns are refused.** `real` and `double precision` hold binary approximations, so equality and
  sums can differ between the engines. Rules that read them are `not_run` (`column_type`), as for dates, domains and
  every other type outside `int2`, `int4`, `int8`, `numeric`, `text`, `varchar`, `bpchar` and `bool`. Store
  quantities as `numeric`, or cast in a view.
- **NaN and infinities stop a rule.** `numeric` accepts `'NaN'`, `'Infinity'` and `'-Infinity'`. PostgreSQL orders NaN
  above every number, while Python's Decimal refuses to order it. A numeric column a rule reads must hold none of them,
  or the rule is `not_run` (`non_finite_values`).
- **Two regular-expression engines.** PostgreSQL's advanced regular expressions and Python's `re` differ: alternation
  can pick another match, `\d`, `\w` and `\s` cover other characters (Python's are Unicode-aware), Python's `$` also
  matches before a final newline, and from the leftmost position where a match starts PostgreSQL takes the longest
  overall match while Python takes the first match its backtracking finds, so the whole match and the captures can
  differ (`([0-9]+)(?:[0-9]g)?` on `123g` captures `12` in PostgreSQL and `123` in Python). `text_quantity` patterns
  therefore stay inside a subset both read alike: no alternation or letter escapes, `\d` and `\s` expanded to ASCII
  classes, `$` written as `\Z` for Python, no repetition over a group that contains a quantifier. The subset removes
  the first three differences, not the last: keep neighbouring items disjoint (`([0-9]+)g`) or anchor the pattern
  when the whole text is the match (`^([0-9]+)(?:[0-9]g)?$` captures `12` in both), which avoids that example's
  difference but is not shown to avoid every one. A difference that remains and changes the rule's output (which
  rows it flags, or their numerator or divisor) shows up as a failing `rule_xcheck:<id>`, never as a pass; one that
  changes no output leaves the verdict the same under either reading.
- **Overlapping quantifiers backtrack slowly in Python.** The subset excludes nested repetition, not neighbouring
  quantified items that can match the same characters: `([0-9]+)[0-9]*[0-9]*g` is accepted, and Python's `re` tries
  every split of a long digit run that is not followed by `g`, which takes polynomial time with no time limit on the
  Python side. Keep neighbouring quantified items disjoint (`\s*([0-9.]+)\s*g`) and `max_text_length` small.
- **COPY turns an unquoted empty CSV field into NULL.** When the source tables are loaded with `COPY ... (FORMAT
  csv)`, an unquoted empty field becomes NULL and a quoted `""` stays an empty string, so a blank code can arrive as
  NULL. A comparison with NULL is false (`ne`, `not_in` and `not_in_ref` included): a rule that must report blank or
  missing codes needs an explicit `is_null` branch (`any = [[... not_in_ref ...], [... is_null ...]]`). Load with
  every field quoted, or with `FORCE_NOT_NULL`, when blanks must stay blank.
- **Exact numbers only.** Write constants and example values as decimal strings or integers: TOML floats are refused,
  because `0.1` as a binary float is not 0.1. Numbers compare by value (`2.0` equals `2.000000`), but an example value
  must fit its column exactly: `"0.1234567"` for a `numeric(18,6)` column is exit 2, so neither evaluation rounds.
- **NULL is a violation only when asked for.** A NULL endpoint has no node row to compare, an `all` quantifier needs at
  least one neighbour, and NULL key parts form no duplicate group. Use `is_null` or an `exists = false` endpoint atom
  when a missing value or a dangling reference is the defect.

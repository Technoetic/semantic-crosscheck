# Changelog

## [Unreleased]

### Fixed

- `CubeClient.load` accepts a Cube `/load` answer only in Cube's load envelope, a JSON object whose `data` is a list
  of row objects. Any other answer without an `error` member (an empty object, `results` instead of `data`, `data`
  that is an object or a string, a body or row that is not an object, a body that is not JSON) is an `error`, never
  zero rows: a V1 or V3 check carries `cube=invalid_response` and its next action points at the adapter, while a V2
  stratum check counts such bindings by type under `errors` (`CubeResponseError`) and keeps the general next action,
  as for its other errors. Read as no rows, such an answer passed V1 and V2 vacuously against empty gold rows, passed
  a V3 pair test of at most 2,000 keys (the key cap) over no keys whatever the source data held, and counted as a V4
  kill against non-empty gold rows; the other malformed JSON answers stopped the verify stage with an unexpected
  exception (in V4 they left the mutant unjudged without the retry). A body that is not JSON was already an `error`,
  but named `JSONDecodeError` or `UnicodeDecodeError`, and a V1 or V3 next action pointed at the gold SQL instead of
  the adapter.
- The Cube rows of a gold query in V1, V2 and their V4 replays must hold every mapped member, as `expect` already
  required: a row without one is an `error` (`MissingMember`; `cube=invalid_response` on a V1 check and an `expect`
  case, counted under `errors` on a V2 check), never a NULL or, under `zero_if_null`, a 0 that could pass. A V4 suite
  item that gets either kind of answer is retried once; when the retry gets one too, the mutant is left unjudged
  (`suite_error`) instead of killed.
- V4 `closure_join_swap` no longer calls an observable mutant equivalent. Its witness compared the measure values at
  the two ends of each closure pair (a missing value counting as 0 for `sum`/`count`), but Cube 1.8.3 aggregates a
  measure reached through the closure's `many_to_one` join over the distinct primary keys of the measure cube in each
  group, so equal values on a fork or diamond still change grouped answers; the measure's `key` is also a public
  dimension, and V1 tells NULL from 0 unless `zero_if_null`. Such a mutant was skipped as "equivalent on this data"
  with no check, which could hide a survivor from V4. The witness now counts the pairs whose two ends join different
  measure rows, which is 0 only when every query answers the same; such mutants are served, and one that survives
  fails V4. A verify that passed 0.13.0 with `v4:closure_join_swap:<m>` listed as equivalent can now fail V4 when no
  gold query observes the change, for example when only the joined measure row changes and the gold queries read only
  values.

### Added

- `reason --explain-inconsistency` reports `uniqueness` (`unique`, `not_unique` or `not_proven`) for a completed subset-minimal explanation. One extra same-HermiT check per retained group removes only that group from the full generated set, within the existing call cap and deadline, using the intersection-of-justifications criterion of "Union and Intersection of all Justifications" (arXiv 2109.11216). `unique` holds only among explanations over the generated axiom groups; caps, deadlines, worker errors or a repeated `ontology.no_children` value or edge leave it `not_proven` and keep the verified minimality.

### Changed

- A repeated value in `ontology.no_children` is now a configuration error. A repeat generated the same axiom twice and would imitate a second explanation.

## [0.13.0] - 2026-10-29

Release notes: [docs/releases/v0.13.0.md](docs/releases/v0.13.0.md).

### Added

- Optional authored synthetic `shacl-repair` fixtures with real before/after pySHACL, retained-target and immutable-fact obligations, independent normalized report bags and separate edit costs.
- Optional `grain` authored operation/key contracts for undated preaggregate-to-node measures with bounded read-only PostgreSQL key/null/orphan/fanout evidence.
- Optional `reason --explain-inconsistency` bounded same-HermiT generated-axiom explanations, verified subset-minimality only on completed checks and preserved original failures. No new dependencies or default pipeline stages.


All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

### Release compatibility

- Register the Windows Job Object containment test only on Windows; Linux integration still requires zero skipped tests. Platform-specific coverage remains in the corresponding CI jobs.

## [0.12.0] - 2026-10-28

Release notes: [docs/releases/v0.12.0.md](docs/releases/v0.12.0.md).

### Added

- Three independently authored, SHA-bound `expect` row cases in `examples/expect-boundaries/`: a parent
  without stock keeps raw NULL, facts excluded by a filter use an explicitly declared zero policy, and
  identical labels in different regions retain composite result keys. The fixture specification gives
  hand-calculated rows and provenance; temporary-copy mutations test wrong authored rows, a shared wrong
  filter and label-only grouping. Evidence covers these synthetic PostgreSQL 17+ / Cube 1.8.3 cases only,
  separate from operational V1-V5, and runtime errors are never accepted as passes.
- Opt-in `counterexample --constraint-audit FILE` with a strict version 1 manifest binding the original
  constraint bytes, caller-claimed provenance for every source declaration and explicit relaxation selectors.
  The same trusted SQL pair is searched in a declared arm and a projected relaxed arm; a relaxed witness
  must violate the full original constraint profile and be confirmed by PostgreSQL's existing typed bag
  comparison. A declared-domain disagreement is `fail`; an outside-domain disagreement is diagnostic
  `not_run`, and bounded no-witness or incomplete outcomes are also `not_run`. No audit outcome is `pass`;
  none proves equivalence, causal necessity, a false constraint or an actual production defect.
- Separate versioned audit replay binds exact SQL, finite specification, original/projected constraints,
  manifest, selected declarations, arm classification and budgets. Reduction and replay support `rows`
  shrinking only, preserving original finite values, types and minimum/maximum row bounds. Both arms get
  their own search/solver/shrink budgets; their bounded prefixes can differ. Counts/IDs/hashes stay in
  results, while local synthetic replay artifacts may contain synthetic rows. See
  [docs/constraint-audit.md](docs/constraint-audit.md) and [examples/constraint-audit/](examples/constraint-audit/).

Existing defaults, dependency pins, ordinary counterexample formats and operational report verdicts are unchanged.

## [0.11.0] - 2026-10-27

Release notes: [docs/releases/v0.11.0.md](docs/releases/v0.11.0.md).

### Added

- Opt-in `verify.shacl_pairs = true` (default `false`): `reason` translates a shape whose `sql_violation` is
  exactly `t.<a> <op> t.<b>` (`>`, `>=`, `<`, `<=`; no comment, cast, parenthesis, constant, `=` or `<>`, and no
  `path`, `class` or bound on the shape) into a SHACL property-pair shape (`a > b` as `sh:lessThanOrEquals`,
  `a >= b` as `sh:lessThan`, `<` and `<=` with the columns swapped) and adds the separate check
  `shacl:pair:<id>`: pass when pySHACL's focus nodes on a separate pair export equal the keys in `v_shape_<id>`.
  The pair shapes are generated at reason time and pass the generated-shape preflight; `build/shapes.ttl`, its
  comparison and every existing check stay unchanged. Both columns must be of the base types `smallint`,
  `integer`, `bigint` or `numeric` (a domain over them is `column_type`) without NaN or infinities and the target
  keys unique, otherwise the check is `not_run` (`column_type`, `non_finite_values`, `duplicate_keys`), because
  pySHACL 0.40.1 orders NaN and mixed datatypes unlike SHACL and PostgreSQL; while a pair check is `not_run`,
  reason and report cannot pass: change the column type or the data, or set `verify.shacl_pairs = false`. The
  pair rows count against `verify.shacl_max_rows` after the shape export. With the option on and a pair-form
  shape configured, `reason` needs rdflib and pySHACL (exit 3) and a `build/shapes.ttl` that matches the
  configuration (exit 2), even when no other shape is compared. A pass shows agreement on one snapshot, not that
  the rule is right. With the option off nothing changes.
- Opt-in `verify.existence_witness` (default `false`): each sampled or chunked V3 pair test gains a separate
  check `v3w:pair_witness:<member>+<member>`. For every asked key that Cube's together answer holds but an alone
  answer leaves out, PostgreSQL is asked whether the measure has a row satisfying its configured `where_sql` (for
  the closure `qty`, whether the closure table still has the key), binding the texts Cube's equals filter receives:
  once in the key-read REPEATABLE READ snapshot and again after the Cube queries. Keys without such rows and with
  an empty (NULL) together value pass (`absent_no_facts`); keys with rows in both reads fail (`absent_with_facts`),
  as do keys without rows but with a non-empty together value (`witness_value_mismatch`); when no key fails, a key
  missing from the together answer, a failed cast, another failed statement (`witness_error`), a change between the
  two reads or a member no configuration measure backs (`not_witnessed`) leaves the check `not_run`
  (`witness_incomplete`), since a failing key takes precedence, and a witnessed pair test that is an `error` after
  its key read leaves it `not_run` (`pair_error`). Details hold counts only: `asked_keys`, `incomplete_keys`,
  `undecided_keys` and `together_absent` count keys, the other labels count absences (one per key and measure an
  alone answer left out). The pair test's own status, detail (the chunked `elapsed_ms` leaves the witness reads
  out), D95 bound and V4 role are unchanged, also when the witness's statements or connection fail; the unfiltered
  path at or below 2,000 keys gets no witness, not even when it errors, and V4 replays do not run it. The witness
  shares the configuration's filters with the measure cubes and reads only the keys an alone answer left out, so a
  pass shows that those keys have no qualifying row under the same filters; keys an alone answer holds are not
  witnessed, and data completeness is not shown. With the option off every output is unchanged.

### Changed

- `shacl-corpus --w3c-suite` profile: `sh:lessThan` and `sh:lessThanOrEquals` with an IRI value on property
  shapes (the parameters `verify.shacl_pairs` generates). On the pinned suite `core/property/lessThan-001`,
  `lessThan-002` and `lessThanOrEquals-001` are now in profile (9 of 98 instead of 6), and pySHACL 0.40.1 and
  Apache Jena 6.2.0 both match their expected reports (observed 2026-10-26). No option controls this change: it
  changes the stage's default result on the pinned suite (six more passing checks); it still cannot pass as a
  whole.

## [0.10.0] - 2026-10-25

Release notes: [docs/releases/v0.10.0.md](docs/releases/v0.10.0.md).

### Fixed

- Sampled V3 pair tests and V4 replay require every requested key in the together and every alone response.
  Confirmed disagreements still fail; otherwise missing answers are `not_run` (`keys_not_answered`). Incomplete
  samples do not claim a D95 bound or describe requested keys as compared. Chunked tests now apply the same
  coverage guard to missing alone rows, including NULL-valued measures.

### Added

- Generated SHACL preflight `shacl:meta:generated` in `reason`, once before source export, using the pinned
  pySHACL shacl-shacl syntax subset without logging invalid-shape reports. Failure blocks SHACL comparisons,
  while independent OWL can proceed. This is not a proof of complete syntax or business-rule correctness.
- Optional `tlp --oracle norec`: bounded, independently evaluated WHERE-count and CASE/SUM-count consistency,
  restricted to `count(*)`, with full-row diagnostic stability checks, strict counts and oracle-bound replay.
  Default TLP and existing TLP artifacts remain compatible. This remains separate from operational V1-V5.

These changes alter the existing engine digest; rerun build, reason, cube, verify and report after upgrading.

## [0.9.0] - 2026-10-24

Release notes: [docs/releases/v0.9.0.md](docs/releases/v0.9.0.md).

### Added

- `verify.pair_key_budget` (default 2000, at least 2000): a V3 pair test with more keys than the
  2,000-key cap and no more than the budget compares every key in the baseline run. The key count and
  every non-NULL key are read in md5 order in one REPEATABLE READ snapshot (one statement; every key is
  held in memory) and compared in consecutive chunks of 2,000 keys through the existing filtered
  queries, plus the NULL key group through a `notSet` filter, which the sample left out. A mismatch in
  any chunk or in the NULL key group fails the test, a query error in any of them makes it an error, and
  keys missing from Cube's together answers make it `not_run` (`keys_not_answered`), never a pass. The
  detail says `mode = "chunked"` with `chunks`, `compared_keys`, `answered_keys`, `total_keys`,
  `null_key_group` and `elapsed_ms`, without sample or d95 fields, and not proven states that each Cube
  query reads its own snapshot, shared with no other query and not with SQL. Above the budget the sample
  and its d95 bound stay and not proven names the budget; a sampled not proven line now also says that
  a sample never compares the NULL key group. V4 still replays the 2,000-key sample (the first chunk).
  The default keeps the previous comparison.
- Literal expected-row oracle for a gold SQL and a configuration that share one mistake: optional
  `[[gold.expect]]` cases on a `[[gold]]` entry (`source` `authored` with a `spec` location, or `captured`;
  `params`, `rows` with `{ null = true }` for NULL, `partial`, an optional `fixture_sha256` pin) and a new `expect`
  stage. It reads the operational configuration but runs build, cube, the gold SQL and Cube only on a dedicated
  fixture database the user loaded (`--dsn-env`, default `SXC_EXPECT_DSN`, with explicit `--allow-fixture-database`)
  and never runs fixture SQL. A fixture DSN is refused when it equals, or names the same host, port and database as,
  an operational DSN variable set in this environment; another name for the same server is not detected, so the
  fixture must be a dedicated database (not proven names the unset ones). `expect:<gold>[<i>]` passes only when both
  sides equal the expected rows; agreeing sides that differ from them fail as `common_mode` (authored) or
  `regression` (captured), a disagreement as `gold_mismatch`, `cube_mismatch` or `both_mismatch`; a case whose
  pinned `fixture_sha256` differs is `not_run` (`fixture_mismatch`). The digest orders the row text by
  `COLLATE "C"` and fixes TimeZone, DateStyle, IntervalStyle, extra_float_digits, bytea_output and lc_monetary,
  so a pin depends on the rows and the column types (and on the database encoding for text outside ASCII), not
  on the database's default collation or session settings. `expect:fixture` compares digests of the fixture
  taken before the build and around the cases while a second session holds SHARE locks on its source and closure
  tables when they can be taken (not, for example, when the role lacks UPDATE, DELETE, TRUNCATE or MAINTAIN, or a
  conflicting lock outlasts `source.statement_timeout_ms`; then `locked: false` and a not proven line says so).
  The build objects stay in the fixture database's output schema (no cleanup; the next run rebuilds them; all
  cases share that database). Captured passes are
  regression evidence only. The result stays out of `report` and operational V1-V5 coverage; editing a case changes
  `crosscheck.toml`, so `report` needs the operational stages rerun. Example: `examples/expect/`.
- `shacl-corpus --w3c-suite [DIR]` (or `SXC_W3C_SHACL_SUITE` with a bare `--w3c-suite`) compares pySHACL and Apache Jena
  with the expected reports of the W3C SHACL core test suite instead of the fixed corpus. Only approved `sht:Validate`
  entries inside the sxc profile run (the components and severities sxc's exporter emits: `sh:class` and the three
  numeric bounds, one Violation or Warning severity, on IRI focus nodes with no path or one IRI path); every other
  entry is `not_run` with a reason id. Check ids are `w3c:<engine>:<case>`. The engines validate the suite's own
  graphs, so this mode does not exercise sxc's exporter and complements the fixed corpus (run both). The suite is
  pinned at w3c/data-shapes `1dfcd9f` by a digest over the files read; an unpinned suite runs no engine unless
  `--allow-unpinned-suite`, which adds a `not_run` `w3c:suite` check, so the W3C mode never passes as a whole. On the
  pinned suite both engines matched all 6 in-profile cases (pySHACL 0.40.1 and Apache Jena 6.2.0, 2026-10-24).
- The integration CI job fetches the pinned suite tarball, verifies its SHA-256 and the content digest, and runs the
  W3C subset; the suite is read at test time under the W3C Software and Document License and never vendored.

## [0.8.0] - 2026-10-23

Release notes: [docs/releases/v0.8.0.md](docs/releases/v0.8.0.md).

### Added

- Optional standalone `cube-replay` runs the existing synthetic mutation templates through actual compiled original/mutant Cube models and independently authored gold SQL. It requires a dedicated test DSN and explicit private-schema write opt-in; it does not promote real-data V4 coverage.
- Optional `tlp` checks bounded nonrecursive trusted SQL through true/false/NULL partitions, exact COUNT/SUM and multiset comparisons, including strict generated-fixture replay and observable input stability checks.
- Optional `shacl-corpus` compares independently specified nonrecursive reports with pySHACL and separately installed Apache Jena. Missing/malformed/partial engines cannot pass; the operational reason stage is unchanged.

## [0.7.0] - 2026-10-21

Release notes: [docs/releases/v0.7.0.md](docs/releases/v0.7.0.md).

### Added

- Opt-in two-query `--shrink-strategy structured`: shared-budget single-row reduction, reverse
  foreign-key dependency group deletion and finite-rank non-key value simplification. Every accepted
  trial preserves the declared integrity domain and actual SQL disagreement. Version 2 fixtures
  bind the original synthetic input and validated transformation trace; legacy row-only replay remains.
- `--witness-schema FILE`: a strict version 1 declaration of typed finite domains and row bounds,
  with optional existing integrity profiles. Optional pinned Z3 generates models within
  `--solver-timeout-ms`; PostgreSQL still compares both queries. Missing solver, bounded UNSAT,
  unknown, timeout or no disagreement is never a correctness or equivalence proof.

### Fixed

- Validate numeric range with exact `Decimal.copy_abs()` so valid 29..38-digit declared
  boundaries are not rejected by a caller's lower-precision Decimal context.
- The Windows descendant cleanup/handle-leak regression triggers cleanup after the real
  child reports readiness; its separate actual deadline test remains unchanged.

## [0.6.0] - 2026-10-20

Release notes: [docs/releases/v0.6.0.md](docs/releases/v0.6.0.md).

### Added

- counterexample: `--metamorphic-sql` gains `--scenario bom_exhaustive`, a bounded-exhaustive enumeration of
  every connected acyclic BOM with 2-4 parts, 1-5 `bom_line` rows, at most 2 rows per (parent, child) pair and
  `qty_per` in {1, 2} (1,743 isomorphism classes x 2 label placements = 3,486 cases). The new check
  `counterexample:metamorphic:oracle` compares every case's answer exactly with the independent closure-oracle
  fold (`closure_oracle.expected_quantities`). It passes only when every admissible case of the enumeration was
  compared (with `--constraints`, inadmissible cases are counted and noted in `not_proven`). No check passes
  partially: at the first failure and in a one-case replay the failed checks are `fail` and the others `not_run`;
  a resource bound in an original answer makes every check `not_run`; one hit only in a relation's own transform,
  transformed answer or comparison makes only that relation `not_run`. `--max-examples` must be omitted or cover
  every case. The scenario is rejected outside `--metamorphic-sql`. A `UNION` closure now fails deterministically
  (case 4), while with seed 7 the seeded `bom_diamond` run sees it in only 6 of its 64 default cases (method after
  Marinov et al., 2003).

### Changed

- The V3 independent closure oracle now verifies `on_cycle = "report"` hierarchies instead of reporting them
  `not_run`: an iterative Tarjan search (charged to the operation budget) removes every logical edge inside a
  strongly connected component, self-loops included, then folds the rest exactly. `dropped_cycle_edges` and
  `cycle_nodes` are recorded for information only, a pass that removed edges is listed in `not_proven`, and a
  cycle left after removal is an `error` (`scc_removal_left_cycle`). The `cycle_not_supported` reason is gone. The
  configuration still accepts only `error` and `report`; called directly, the oracle answers any other `on_cycle`
  value with `not_run` (`unsupported_on_cycle`), whatever the graph.
- reason now compares shapes that combine `class` with bounds against their SQL view through SHACL. The node shape
  has two property shapes: `sh:class` on the column predicate and the bounds on a new value predicate
  `<entity>.<column>/value`, which carries the same column value as a literal. A row flagged by either property
  shape counts for the shape. Only `sql_violation` shapes remain checked only in SQL. `build/shapes.ttl` changes
  for configurations with such shapes, and the engine fingerprint changes: rerun the whole pipeline (`build` first, then
  `reason`, `cube` and `verify`); earlier results are reported stale. These shapes are now exported, so their target and class-entity rows count against
  `verify.shacl_max_rows`, and next to a bound-only shape on the same column (or a class shape naming another
  entity) such a shape opens an extra value group that exports its target rows again. An unchanged configuration
  can therefore exceed the limit, which leaves every `shacl:<id>` check `not_run` (`resource_limit`): check the
  reported row count after upgrading, or raise `verify.shacl_max_rows`.

## [0.5.0] - 2026-10-19

Release notes: [docs/releases/v0.5.0.md](docs/releases/v0.5.0.md).

### Added

- A bounded V3 independent closure oracle reads raw edges and the stored closure in one read-only
  REPEATABLE READ snapshot. Python exact arithmetic checks quantities, logical path counts and depth
  bounds without reusing the generated closure SQL. Unsupported inputs and resource limits are
  explicitly not run; duplicate BOM lines follow the existing summed-edge contract, with path counts
  over logical edges. Error-mode cycles and an exceeded configured maximum depth fail.
- Operator-directed synthetic SQL fixtures through `counterexample --mutation-operator`, supporting
  all nine V4 operator names, optional independent `--gold-sql`, integrity-constrained shrinking and
  hash-bound replay. Gold observation requires original/gold agreement and mutant/gold disagreement. A
  synthetic disagreement does not change a real-data V4 verdict or prove a Cube runtime defect.
- Opt-in `reason --synthetic-witnesses` exercises configured OWL definitions on small, independently
  specified graphs and compares HermiT and the SQL translation with expected memberships. These
  checks preserve real-data vacuity disclosures and OWL's open-world semantics.

## [0.4.0] - 2026-10-18

Release notes: [docs/releases/v0.4.0.md](docs/releases/v0.4.0.md).

### Added

- V3 `additive` tests now cover every sum/count measure (configuration measures and the closure's
  `qty`/`pairs`) grouped by `ontology.class_column` on every cube that Cube joins through
  many_to_one / one_to_one steps only (4 new checks in the demo, 42 -> 46). Each test also compares
  Cube's NULL group with independent SQL over the measure source; a mismatch fails. The SQL follows
  the cube Cube roots the query at: from the measure's cube (the closure ends) an anti-join, rows
  without a dimension row with a class; from the dimension's cube (a node cube's one_to_one join to
  its measure cube) only the matched rows without a class, as Cube drops the measure rows without a
  node row (they fail the sum unless they add up to 0, and are counted as `orphan_rows`). Join
  paths with a one_to_many or unknown step get no test and are listed in not_proven as not
  applicable (aggregation-consistency errors, HILDA 2023, arXiv 2307.00417).
- Sampled V3 pair tests report their 95% detection bound: the detail gains `d95_keys` and `d95_rate`
  (zero-acceptance hypergeometric sampling, computed exactly), and the not_proven line keeps
  "compared 2000 of N keys" and appends the bound and its independence assumption. A V4 mutant that
  survives a suite replaying a sampled pair test carries `sample_blind_possible` and `sampled_items`;
  no status changes.
- `counterexample --metamorphic-sql FILE` checks one trusted closure SQL (columns ancestor,
  descendant, qty) over seeded bom_diamond fixtures against four BOM metamorphic relations without a
  gold query: `mr1_unfold`, `mr2_scale`, `mr3_disjoint`, `mr4_unit_node`. Comparison is exact decimal,
  vacuous relations are `not_run`, and a violation fails with exit 1 and writes a replayable
  `fixture.json` (format `sxc-synthetic-metamorphic`). `--constraints` is supported. The principle
  follows EDC (PACMMOD 3(6), 2025); no code is borrowed.
- `cube:meta` records `cube_version` and `sql_planner` on every outcome, and the selected-Cube
  runtime component carries them when `source.runtime_evidence = true`.

### Changed

- The Cube child always gets `CUBEJS_TESSERACT_SQL_PLANNER=true`; an inherited value, including
  `false` for the legacy planner, is dropped with every other `CUBEJS_*` variable. The Cube pin stays
  at 1.8.3 (GHSA-cfx6-94gc-h774, a legacy-planner SQL injection published 2026-09-21, affected 1.7.x
  and earlier only). `report` treats a cube result that recorded another Cube version or planner
  (`cube_runtime_changed`), or none (`cube_runtime_missing`, e.g. results from 0.3.0), as stale;
  verify follows as a stale dependency. Rerun cube and verify after upgrading.
- reason: shapes on the same column that need different RDF value kinds (a class shape and a bound
  shape, or class shapes naming different entities) are no longer skipped as `shared_path`. Each shape
  goes, in configuration order, to the first value group in which its column carries no other kind of
  value: `main` holds every shape that needs no split, so a configuration without such a column (the
  demo included) exports one graph with the same rows as 0.3.0, and only a colliding shape opens an
  extra group (`extra:1`, `extra:2`, ... in the order they are opened). All groups are exported in the
  same READ ONLY REPEATABLE READ snapshot and validated with the same shapes graph.
  `verify.shacl_max_rows` limits the sum of the rows over all groups (an extra group adds the rows it
  exports again), so a configuration with a shared column near the limit can become `not_run`
  (`resource_limit`) where those shapes were skipped before. If validating one group fails (pySHACL
  returns no results graph or raises any exception), only that group's shapes are `error`
  (`validation_failure`, with a type name only). `shacl:<id>` details gain `value_group` and
  `group_rows`; `shacl_check.export_graph` is replaced by `export_graphs`. `class_and_bounds` remains
  SQL-only.
- The `v3:additive` detail gains `null_group_equal`, `null_group_rows` and `orphan_rows`; a test passes
  only when the groups add up and the NULL groups agree.
- `counterexample`: `--reference-sql` and `--candidate-sql` are enforced by the stage (still exit 2,
  now with a result.json), and the default `--scenario` depends on the mode (nulls, or bom_diamond
  with `--metamorphic-sql`).

### Fixed

- SQL reads that one check combines from several statements share one snapshot: the V3 pair-test key
  count and key sample, and the information_schema reads the cube stage builds the model from, run in
  one READ ONLY REPEATABLE READ transaction (`db.read_only(..., isolation="repeatable read")`).
  Single-statement reads keep READ COMMITTED. Cube sessions and stage boundaries still have no shared
  snapshot and rely on the data_revision contract.

## [0.3.0] - 2026-10-17

Release notes: [docs/releases/v0.3.0.md](docs/releases/v0.3.0.md).

### Added

- Optional `counterexample --constraints FILE` profiles enforce the declared PK, UNIQUE, NOT NULL,
  MATCH SIMPLE FK and allowlisted CHECK domain before SQL and during every row-deletion trial.
  Replay binds the exact profile bytes and mode. Minimality covers admissible single-row deletions
  only; a search with no valid generated cases remains `not_run`.
- Optional `source.runtime_evidence = true` binds the actual PostgreSQL endpoints/settings,
  Node/Java versions, used HermiT JAR and selected Cube installation at stage boundaries and report.
  Missing, failed or changed runtime evidence prevents reuse of prior PASS results. The default is
  off and performs no new runtime probes; enabling it requires the complete pipeline runtime.
- Actual full-stack runtime CI exercises an opted-in clean pipeline and changed, unavailable and
  missing runtime evidence. Platform-specific process cleanup coverage is registered on the
  applicable OS, preserving the integration job's zero-skip requirement.

## [0.2.0] - 2026-10-16

Release notes: [docs/releases/v0.2.0.md](docs/releases/v0.2.0.md).

### Added

- Optional synthetic SQL counterexample generation, bounded row-deletion shrinking and hash-bound
  replay. NULL, join fan-out, BOM diamond and date-boundary scenarios run in read-only PostgreSQL
  transactions. Bounded agreement is not a proof; query errors are never mismatch witnesses.
- Optional data-revision SQL probes bind stage evidence to hashed source revisions. Reports reject
  missing or changed bindings and changed engine/probe inputs. Legacy configurations explicitly
  report unbound data freshness without a database probe. No common cross-session snapshot is claimed.
- Forty-nine tests cover the new counterexample and provenance capabilities, bringing the suite to
  562 tests. The local PostgreSQL baseline completed 539 with 23 optional-engine skips; this is not
  an all-engines CI result. Release-commit public CI evidence is recorded separately.

### Changed

- Evidence fingerprints include the harness/adapter inputs and selected Python runtime/dependency
  metadata. Results without current fingerprints must be regenerated after upgrading. Actual
  PostgreSQL, Java and Node.js runtime versions are not covered by this engine fingerprint.

## [0.1.1] - 2026-10-14

Release notes: [docs/releases/v0.1.1.md](docs/releases/v0.1.1.md).

### Fixed

- A selected SHACL or OWL check exceeding its resource limit now leaves explicit `not_run` checks, so
  neither `reason` nor the full `report` can pass when only one engine was skipped. The next action
  directs the user to review the limits and rerun reasoning.
- Numeric canonicalisation preserves every decimal digit independently of the active Decimal context;
  distinct high-precision values and numeric keys no longer compare equal through rounding.
- Unknown option keys in entities, ontology definitions and gold parameter specifications stop with
  a configuration error instead of silently dropping a rule. User-defined mapping keys remain supported.

### Changed

- Integration CI fetches the full history and fails if any test is skipped or no tests run.
- Release document checks cover every published version, including this patch release.

## [0.1.0] - 2026-10-14

First release. Release notes: [docs/releases/v0.1.0.md](docs/releases/v0.1.0.md).

### Added

- `sxc.py` with the stages `doctor`, `demo`, `build`, `reason`, `cube`, `verify`, `report` and
  `cube-install`, one `result.json` per stage (counts and ids only, fail-closed status, `not_run` never a
  pass, `not_proven`, `next_safe_action`) and exit codes 0 pass, 1 a check failed or did not run,
  2 configuration or usage error, 3 environment missing. An explicit `--config` must exist; results are
  written atomically and the previous result is removed before a stage runs; new output directories get a
  `.gitignore` of `*`; `SXC_DEBUG=1` prints tracebacks for local debugging.
- `crosscheck.toml` version 1 as the single source of truth: entities, hierarchies, shapes, measures,
  ontology, Cube, verification and gold queries, validated before any stage works (unknown keys,
  credential keys in `[source]` and `[cube]`, DSNs in the `*_dsn_env` keys and out-of-range numbers are
  configuration errors).
- `doctor`: Python, pg8000, the check-role DSN, PostgreSQL 17, `privileges:temporary`,
  `privileges:select:<entity>`, `privileges:no_write:<entity>` (write privileges, TRIGGER, table and
  schema ownership, CREATE on the schema), `output_schema`, and the optional reason and Cube components,
  with install hints that work from any directory.
- PostgreSQL 17 hierarchy closure built level by level (exact path counts and summed path-product
  quantities without enumerating paths), cycle examples with the `CYCLE` clause, and
  `MERGE ... WHEN NOT MATCHED BY SOURCE ... RETURNING merge_action()` followed by an equality proof;
  concurrent builds of one output schema wait for each other.
- Data rules as SQL violation views and generated SHACL shapes; `reason` compares pySHACL with SQL per
  shape (shapes without a separate SHACL form are reported as not proven) and checks an OWL ontology
  with HermiT (consistency, transitive closure, defined classes).
- Cube 1.8.3 adapter installed with `npm ci --ignore-scripts` from a committed lockfile and a size- and
  SHA-256-pinned native module (Node.js 22 or later; glibc checked on Linux); generated Cube model with
  metadata and gold-member checks; read-only, time-limited Cube sessions on 127.0.0.1 with a per-run API
  secret, a scrubbed child environment, proxy-free client requests and redacted adapter logs
  (`SXC_CUBE_DEBUG=1` for full local error text).
- `verify`: V1 strict compare and V2 strata sweep in the form of each column's Cube member type, V3 query
  shapes (totals, additivity, measure pairs, sampled above 2,000 keys) and V4 mutation tests with witness
  queries for equivalent mutants, where only a failing suite item kills a mutant; `--only` and `--gold`
  leave `scope:*` checks so a narrowed run never passes.
- `report` with `report.md` and `report.json`: statuses recomputed from the checks, required stages,
  stale and missing stages counted as not run.
- Synthetic parts / BOM / orders / stock demo with `clean` and `dirty` variants that can be reloaded at
  any time, and `expected.json` reference numbers from an independent Python implementation that name the
  check showing each planted defect.
- TLS for the Python stages and the Cube child following the DSN's `sslmode` (`verify-ca` and
  `verify-full` verify the server certificate).
- Claude Code plugin (`/xc-*` commands and a skill), Codex plugin and marketplace manifests.
- `tools/leak_scan.py` (secrets, e-mail addresses outside `tools/leak-allow.txt`, a private deny-list,
  encoded and compressed content, history, path and ref names) and git hooks in `.githooks/` for commits
  and pushes.
- Documentation: README, design document (`docs/design.md`), configuration reference, pitfalls, Codex
  usage, security policy, third-party licences, release notes and a roles template for psql
  (`docs/setup-roles.sql`).
- CI: unit tests on Linux, Windows and macOS with Python 3.11 and 3.13; integration tests against a
  PostgreSQL 17 service with Java 21 and Node.js 22, after `docs/setup-roles.sql` is run with psql,
  followed by the clean and dirty demo end to end through the command line; whitespace check and a leak
  scan of the working tree and the whole history.

[0.9.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.9.0
[0.8.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.8.0
[0.7.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.7.0
[0.6.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.6.0
[0.5.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.5.0
[0.4.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.4.0
[0.3.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.3.0
[0.2.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.2.0
[0.1.1]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.1.1
[0.1.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.1.0

[0.10.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.10.0
[0.12.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.12.0
[0.11.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.11.0

[0.13.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.13.0

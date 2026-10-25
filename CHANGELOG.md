# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

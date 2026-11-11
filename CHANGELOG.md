# Changelog

## [0.23.0] - 2026-11-12

Release notes: [docs/releases/v0.23.0.md](docs/releases/v0.23.0.md).

What a pass means. Every check now has an evidence kind, a passing V4 mutant check says what kind of item detected
the mutant, the parts of measures and hierarchies without authored evidence are named, and a gold query that compared
no row is disclosed. By default this is disclosure: check ids, check counts and exit-code rules do not change (apart
from the new exit 2 for an identifier that ends with a line break, under "Changed"), and statuses change only in the
one suite-order case under "Changed".

### Added

- Evidence kinds (`evidence.py`, docs/design.md section 8, "Evidence kinds"): `authored` (one side is a gold SQL file
  a person wrote, with no generated text in it), `recomputed` (recomputed from source rows by code that imports no
  generator; today only `v3:closure_oracle:<h>`), `generated` (both sides generated from `crosscheck.toml`: every
  other `v3:*` check, and also the OWL closure check `v5:closure:<h>`), `structural` (one side), `mutation` (V4) and
  `unclassified` (no rule names the check; never counted as authored or recomputed). A unit test keeps the table in
  docs/design.md equal to the registry.
- verify: a top-level `evidence` key in `verify/result.json` (only verify writes it): per gold query its kind, the
  generated text found in it, the members it compares and those it uses only in filters, its comparisons, those with
  rows, and per compared member the matched rows whose gold value is not NULL, 0 or empty (`value_rows`); on
  full-scope runs every facet of a measure, hierarchy and `ontology.class_column` with its status (`compared`,
  `detected`, `accepted`, `gap`) and reason (`not_in_model`, `no_values`, `no_rows_compared`, `gold_not_run`,
  `generated_text`, `filter_only`, `no_gold_reads`; a set class column that no entity cube of the model holds is the
  one facet `*.<class_column>`, a `not_in_model` gap); the V1/V2 checks that compared no row; the gold queries whose
  every comparison had zero rows; and the mutant split (`operators_selected`, `operators_not_selected`,
  `killed_authored`, `killed_generated_only`, `unattributed`, `accepted`, `survived`, `not_judged`, `equivalent`,
  `not_applicable`, `generated_only`).
- verify: each V4 check with `killed_by` gains `detection` (`authored`, `generated_only`, `unattributed`, `none`),
  `facet` and `gold_comparing`. `verify/mutation-coverage.json` (still `schema_version` 1) gains `detection` on rows
  with `killed_by` and `kind` on every receipt item.
- verify: not_proven lines for gold SQL that names the output schema, names a generated relation (`<h>_closure`,
  `<h>_cycles`, `v_shape_<id>`, `v_rule_<id>`, `v_rules_subjects`; any rule view is the one item `relation:v_rule_*`,
  never the text that matched; case folds in ASCII only) or contains a generated measure filter clause
  verbatim; for a gold query whose every comparison that ran had zero rows on both sides; and for gaps and kills
  accepted by `verify.accepted_gaps`. In the default mode also for an element whose facets have no authored evidence
  (one line per element, with reasons and the gold queries concerned), for a V4 kill that only a check between
  generated layers made, with a remedy for its operator (for `measure_agg_to_max`: a gold query that aggregates the
  measure over more than one key), and for a kill by an item of no known kind; in require mode the
  `gold_evidence:<element>` checks and the failing V4 checks with their next actions take the place of these three.
- report: with a verify result, a `report.json` `evidence` key (with `mutation_reason`: `not_recorded`,
  `not_included`, `v4_not_run`), a `## Evidence` section in `report.md` before `## Not proven`, and
  `  EVIDENCE  ` console lines after the NOT PROVEN lines (not with `--quiet`): checks by kind, the mutant split, the
  facets without authored evidence, zero-row comparisons, gold SQL with generated text, stages not included. A verify
  result without a readable `evidence` object (for example one written by an earlier release) is shown as "observation not
  recorded", and its V1/V2 checks count as unclassified. Readable also means closed values (reasons, operator names,
  plain ids without spaces or line breaks) and a mutant split that follows from the V4 checks of the same result
  (docs/contract-report.md).
- Configuration (`[verify]`, both part of the configuration hash): `gold_observation` (`"disclose"`, the default, or
  `"require"`) and `accepted_gaps` (a list of elements, facet ids or `v4:<operator>:<target>` ids of this
  configuration; anything else is exit 2).
- Opt-in require mode (`gold_observation = "require"`): a V4 kill whose `detection` is `generated_only` or
  `unattributed` and that `accepted_gaps` does not list is `fail` with that `reason` (`killed_by` kept), or `not_run`
  (`suite_incomplete`) when suite items were left out; on full-scope runs one check `gold_evidence:<element>` per
  measure, hierarchy and `ontology.class_column` (pass, `not_run` when every gap is `gold_not_run` or
  `not_in_model`, else fail with the first gap's reason); a passing `v1:<gold>` of a gold query whose every
  comparison had zero rows becomes `not_run` (`no_rows_compared`), while an `error` or `fail` stays; a not_proven
  line names the operators `verify.mutation_operators` leaves out, and every `gold_evidence:<element>` detail lists
  them. Only ever a pass becomes a fail or `not_run`.
- Unit tests (no database) for these rules, among them `v4_pass_is_not_gold_detection` and the helper
  `evidence.v4_violations`, which fail if a V4 pass can again be read as a gold detection, and
  `tests/test_docs_evidence.py`, which holds README, SKILL.md, docs/design.md and the command documents to the
  registry and the line texts.

### Changed

- A passing `v4:<operator>:<target>` no longer reads as "the gold query detects this". It means that a replayed suite
  item failed against the mutant; the suite also holds V3 items, which compare generated layers. Read `detection`:
  only `authored` means a gold item failed. README, SKILL.md, docs/design.md, the command documents and SECURITY.md
  say so.
- V4 suite order: the items of gold queries whose SQL contains generated text now run after the items of authored
  gold queries (a stable sort). Without such a gold query the order, every V4 status and every `killed_by` are as in
  0.22.1. With one, `killed_by` and V4 statuses can change in the default mode too: another item can be the first to
  fail, or an item that errors twice or the `verify.mutant_max_items` budget can be met first. This goes either way:
  `error` or `not_run` can become `pass`, and `pass` can become `error` or `not_run`; the coverage receipts change
  with it.
- The next action of a surviving V4 mutant reads "no replayed suite item detects this kind of error" instead of "no
  gold query observes this kind of error". The `shape_q.measure_total_sql` docstring no longer calls the V3 total SQL
  independent of the Cube model: it uses the same `cube_gen.where_sql`.
- verify prints more NOT PROVEN lines for most configurations, so scripts that count them see more: on the clean demo
  verify prints 8 instead of 4 (the three `measure_agg_to_max` kills by generated checks only and the facet
  `part+m_firm_open` without authored evidence), and the whole quick start 18 instead of 14. report's console prints
  no new NOT PROVEN line of its own, but `report.md` "## Not proven" and the not_proven count of the `stage:verify`
  detail include verify's new lines. Console parsers should ignore lines that start with `  EVIDENCE  `.
- Configuration: an identifier (gold ids, measure and entity names, the class column and the other names checked
  against `^[a-z_][a-z0-9_]{0,62}$`) or a Cube member of `cube_query` or `map` that ends with a line break is now exit 2
  (`invalid identifier: ...`, `bad Cube member ...`); before, the pattern's `$` let it through.

### Upgrading

- Engine code changed, so the engine fingerprint changed: every earlier build, reason, cube and verify result is
  stale (`inputs_changed`). Rerun the whole pipeline after upgrading.
- No configuration needs editing, unless an identifier or Cube member ends with a line break (above); the default
  mode changes no status apart from the suite-order case above. Build,
  reason and cube outputs, check ids and reports without a verify result are unchanged apart from the engine
  fingerprint (the compatibility goldens hold this).
- No new runtime dependency.

### Not solved

- Gold SQL that is wrong in the same way as `crosscheck.toml` still agrees: authored says who wrote a side, not that
  it is right. Paraphrased copies of generated SQL, generated SQL read through a view the user made, and dynamic SQL
  are not detected.
- Attribution names the first detecting item and its kind only (the suite stops there), and it is exact only for the
  replayed bindings. Facets cover measures, hierarchies, the joins of the generated model and the class column, not
  other entity dimensions; facets without an operator can be compared but never detected. Compared with rows that
  hold values is not "a wrong value would be seen".
- Other vacuous passes (`shacl:<id>` with no keys, `v5:closure:<h>` with no pairs, an unfiltered V3 pair test over
  zero keys) are not disclosed. Measures still have no independent recomputation. `expect`, `data-rules` and the
  diagnostics stay outside report.

[0.23.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.23.0

## [0.22.1] - 2026-11-11

Release notes: [docs/releases/v0.22.1.md](docs/releases/v0.22.1.md).

Documentation corrections and unit tests. No stage, check, option, result format, configuration key or dependency
changes; the only product-code change is the version constant.

### Changed

- README: the title, the purpose and the intended users open the file. The release notes that preceded the title
  are in a "Recent changes" section near the end, their text unchanged. SKILL.md starts with the core description;
  its four release notes are in "Optional offline diagnostics", unchanged.
- README: the "결과 읽기" and "검증 한계" anchors sit above their own headings.
- README: the clean demo prints 14 NOT PROVEN lines, not 13; the breakdown now accounts for all of them.
- README: all 21 slash commands are listed, core pipeline first, each linked to its command document; the stage
  table points to the appendix table that holds the other four stages.
- README: one paragraph on what agreement between the generated layers shows, what the independent evidence is
  (hand-written gold SQL, authored expected rows, the independent closure recomputation), and that existing Cube
  models, ontologies and SHACL files are not inputs.
- README: "실패한 실행 읽기" shows a failing build and report, taken from the compatibility goldens, and how to
  read them.
- README: the `git archive` command that makes the release zip from the tag, and under it a dated record of the
  comparison with the published v0.22.0 asset (2026-11-11, git 2.55.0 on Windows, time zone UTC+9:
  byte-identical). The record is about that one asset and does not change when the command's version changes.
- README and references/config.md: `SXC_DEBUG` is documented for the stages that read it. The diagnostic stages,
  `counterexample --constraint-audit` and `expect` with `spec_source` cases print a fixed reason code only.
- CONTRIBUTING and README: unit tests need `requirements/core.txt` and `requirements/witness.txt`; without
  z3-solver some tests fail or error instead of skipping (CONTRIBUTING gives the count at v0.22.0).
- Plugin manifests: the marketplace entry and the Codex short description say "check" instead of "prove"; the
  Codex long description starts with what the tool is and what a pass means.
- commands/xc-grain.md, commands/xc-shacl-repair.md, commands/xc-expect.md and references/codex.md name example
  files from the plugin root, so the examples resolve from a user's project folder; the grain example writes its
  result to `.sxc-grain` in the working directory.

### Added

- Eight docs and packaging tests pin the above: the command list against `commands/`; manifest and command
  descriptions without "prove", "guarantee" or an unnegated "proof" or "proven" (the word "certify", which the
  data-rules help text uses, is not covered); example paths from the plugin root; anchors above their headings;
  the NOT PROVEN counts in the prose; the failing example against the goldens; the version in the archive
  command; and the dated record under it, which must name a release with a dated changelog entry and must not be
  dated before that release.
- Six unit tests pin fail-closed rules: a result or a stage without any check is `not_run` (exit 1); a relative
  `PATH` entry is never searched for node, npm or java; an entity table outside `source.schemas` is a
  configuration error; in verify a gold query without any binding is `not_run` (`no_binding`) and an empty
  stratum passes only with a `not_proven` line. No database is needed.

The version constant is in a file the engine fingerprint hashes, so the fingerprint changes: rerun operational
stages after upgrading. No other engine code changed, so they produce the same checks and verdicts as 0.22.0.

[0.22.1]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.22.1

## [0.22.0] - 2026-11-09

Release notes: [docs/releases/v0.22.0.md](docs/releases/v0.22.0.md).

### Added

- Optional `data-rules` stage and `/xc-data-rules`: typed rules in a separate file (`[data_rules] file`) with six
  kinds (row, duplicate, mode_outlier, same_signature, stored_metric, text_quantity). Every rule is evaluated twice
  on one READ ONLY REPEATABLE READ snapshot, as compiled PostgreSQL SQL with bound parameters and as an independent
  standard-library Python evaluation of the rows exported in the same transaction. `rule:<id>` reports a verdict
  by severity only when `rule_xcheck:<id>` shows equal outputs; a disagreement leaves the rule `not_run`.
- Authored rule examples (`[[rule.example]]`): small typed rows and the expected output written from the business
  rule, evaluated by both engines without touching tables (`rule_example:<id>`, `common_mode` when both agree but
  differ from the authored rows). Required by default (`require_examples = true`).
- Database lookup relations: with `views = true` (default) build creates `v_rule_<id>` (output rows with `ord`) and
  `v_rules_subjects` (one row per subject key with worst severity, rule count and rule ids); data-rules proves they
  return exactly the agreed rows in canonical order (`rule_lookup:<id>`, `rule_subjects:lookup`).
- Gates and budgets that are never a pass: column types and collations, server encoding, node-key uniqueness,
  NaN and infinities, NULL edge quantities, unbounded heights, long text, `max_export_rows`, `max_rule_rows`.
- `docs/data-rules.md` (rule file, semantic contract, lookups, checks and limits), a clean-room synthetic example
  in `examples/data-rules/`, and data-rule pitfalls in the skill references.

### Changed

- build creates the rule views when configured (`rule_view:<id>`, `rule_subjects:view`); configurations without
  `[data_rules]` produce byte-identical build output, results and reports apart from the engine fingerprint. report
  adds one not_proven line when `[data_rules]` is configured and never reads the data-rules result. expect builds its
  fixture without rule views.

Results keep counts, ids and enums only; no file holds row values. Agreement of the two evaluations does not show
that a rule or its constants are right; authored examples pin only the cases they contain. Engine code changed:
rerun operational stages after upgrading.

[0.22.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.22.0

## [0.21.1] - 2026-11-08

Release notes: [docs/releases/v0.21.1.md](docs/releases/v0.21.1.md).

### Fixed

- Compare canonical selected paths in the contract/report portability tests.
  Windows short temporary names and macOS `/private/var` aliases legitimately
  resolve to a different spelling; a permission-denial test must intercept the
  actual canonical file and assert the injection occurred.
- Retain the production input/output/link guards and evidence semantics. This
  supersedes the tagged v0.21.0 candidate, whose Windows/macOS CI was not green;
  no formal v0.21.0 release assets were published.

The opt-in contract report evidence, separate PostgreSQL structural diagnostic
and independent bag fixtures are described below and in this release's notes.

[0.21.1]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.21.1

## [0.21.0] - 2026-11-08

Release notes: [docs/releases/v0.21.0.md](docs/releases/v0.21.0.md).

### Added

- Explicit paired contract/plan selection for current preflight evidence in
  operational report freshness. Non-opted-in reports retain their behavior;
  changed, missing, mismatched or unsuccessful evidence cannot satisfy the gate.
- Optional offline `sql-structure` and `/xc-sql-structure`, backed by pinned
  PostgreSQL17 pglast rather than regex. Strict bounded structural expectations,
  redacted results and an explicitly unsupported-query path; no DB execution.
- Independently authored synthetic bag/DISTINCT/EXCEPT/EXCEPT ALL expected
  fixtures, PostgreSQL companion tests and a pinned formal-provenance
  counterexample illustrating the difference between annotations and returned rows.

Data Contract CLI/ODCS inform the report evidence boundary, not a full-format
adapter. pglast is optional and separately installed; no parser code is vendored.
No Lean/ProvSQL runtime or company database change is included. Definition or
structure agreement does not establish business truth, SQL equivalence,
authenticated approval or a shared cross-engine MVCC snapshot.

[0.21.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.21.0

## [0.20.0] - 2026-11-06

Release notes: [docs/releases/v0.20.0.md](docs/releases/v0.20.0.md).

### Added

- Optional offline `semantic-preflight` and `/xc-preflight`: compare explicit
  business-definition contracts, existing measure/Gold/Cube declarations and
  structured plans before execution. Missing/ambiguous/candidate selections
  require clarification; complete conflicts fail.
- Bounded verified-byte config/contract/plan capture, strict JSON budgets,
  guarded atomic publication, hash/index-only evidence and a synthetic example.
- Captured-byte `config.loads` shares the unchanged configuration validation.

Review, unit/population/denominator/time and period declarations are authored
assertions, not proof of SQL or answer truth. No DB, model, network or SQL-body
read is performed by this diagnostic. Defaults/dependencies/report authority are
unchanged; contract-to-operational-report binding remains deferred. New engine
code still requires rerunning operational stages after upgrade.

[0.20.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.20.0

## [0.19.1] - 2026-11-05

Release notes: [docs/releases/v0.19.1.md](docs/releases/v0.19.1.md).

### Fixed

- Support macOS's exact `/var` and `/tmp` system temporary-root aliases while
  retaining checks against declared input/output links, user-directory links,
  Windows reparse points and input/output aliases. The original v0.19.0 path
  guard rejected the platform's normal temporary directory before generation.
- Add platform-independent regression probes and retain the full Intel macOS
  Python 3.11/3.13 matrix and strict zero-skip actual-engine integration gates.

### Release scope

This is the formal bundle of the evidence-boundary and opt-in diagnostic
features below. The v0.19.0 annotated tag is preserved, but its macOS boundary
path tests failed and no formal release was published. Its results and archive
checks do not count as v0.19.1 verification.

## [0.19.0] - 2026-11-05

Release notes: [docs/releases/v0.19.0.md](docs/releases/v0.19.0.md).

### Fixed

- Validate saved result envelopes, version/type/status/check IDs and duplicate
  IDs; malformed checks are not silently removed. Invalid optional-stage files
  remain errors even if normal freshness checks also mark them stale.
- Check live SHACL report roots, typed conformance, linked results, cardinality
  and usable generated source/focus structure. Preserve warning/info and valid
  other-group projection semantics, with sanitized errors.

### Added

- Standalone offline `boundary-schema --spec FILE` creates an existing witness-v1
  finite schema from one supported exact integer/numeric/date scalar comparison.
- Explicit `tlp --shrink --max-shrink-steps N` connects structured reduction to
  TLP/NoREC failures. Closed source-bound v2 replay rechecks original, accepted
  steps and final evidence, preserving only the declared discrepancy signature.

Default stages, dependency/runtime pins and operational report authority remain
unchanged. Final-source validation and publication evidence are recorded only
after execution; actual-model effects, company DB verification and general SQL
equivalence are not claimed.

## [0.18.1] - 2026-11-04

Release notes: [docs/releases/v0.18.1.md](docs/releases/v0.18.1.md).

### Fixed

- Select the standard `macos-15-intel` unit runner after hosted macOS arm64
  capacity failures left all four Mac jobs on the v0.18.0 main/tag runs
  cancelled without a runner or executed steps. Linux/Windows latest and Python
  3.11/3.13 retain six unit jobs; strict real-engine integration and hygiene
  retain the eight-job workflow. No skip, gating, step or action-pin change.
- Add a regression contract for that matrix and unchanged strict integration.

### Release scope

This is the first formal release bundle of the boundary fixes documented below
for v0.18.0. That tag remains immutable, but no formal v0.18.0 release was
published after the capacity failure. The only new patch behavior is CI runner
selection; package version metadata and release documentation advance to 0.18.1.
Product behavior and Cube, Jena, PostgreSQL and dependency pins are unchanged.
Mac unit coverage targets macOS 15 Intel (x64); arm64 is not claimed tested.
Actual new-source CI and test totals must be recorded after execution.

## [0.18.0] - 2026-11-04

Release notes: [docs/releases/v0.18.0.md](docs/releases/v0.18.0.md).

### Fixed

- Cube load responses are decoded from raw JSON before duplicate keys can be
  overwritten or fractional numbers rounded. The strict checker policy rejects
  duplicate object keys at every depth and nonfinite constants; fractional
  numbers use exact Decimal values. Invalid JSON, encoding and Decimal parse
  failures become sanitized invalid-response errors.
- Before returning decoded Cube JSON, sum potential fixed-point characters of
  raw Decimal numbers across nested values without formatting or rounding.
  Exceeding the additional `max_response_bytes` character budget produces a
  sanitized response error in load, replay and metadata paths. Zero counts as
  one; other lengths include signs and fractional trailing zeros. Admitted
  values and canonical/RDF/hash representations are unchanged; numeric strings
  and global heap/CPU limits remain outside this guard.
- Gold SQL returned metadata is validated before tuple-to-dictionary conversion:
  invalid or duplicate labels, missing mapped columns even on zero-row results,
  and mismatched row widths are rejected. Distinct extra unmapped columns and
  valid empty results remain allowed.
- Normal and replay Cube loads share a monotonic elapsed acceptance budget across
  polls and final success, and bound successful and HTTP-error source bodies at
  16 MiB by default. Late results are rejected when control returns; this does
  not guarantee hard cancellation of DNS, headers or HTTP framing, or cap total
  heap use. See [Cube HTTP bounds](docs/cube-http.md).

### Compatibility

Previously accepted lossy or malformed answers may now be errors; they cannot
establish equality, a confirmed witness or a mutation kill. Repair the response
or SQL metadata rather than treating an error as a mismatch. The CubeClient
constructor requires a finite positive nonboolean numeric timeout and a positive
nonboolean integer `max_response_bytes` override, which also sets the separate
numeric expansion character allowance. Native metadata JSON/Decimal parse
exceptions remain intact; its numeric-budget rejection is `CubeResponseError`.
Dependency and runtime pins, default stages and report authority are unchanged.
Rerun operational stages after
upgrading; installation does not refresh evidence.

## [0.17.0] - 2026-11-03

Release notes: [docs/releases/v0.17.0.md](docs/releases/v0.17.0.md).

### Fixed

- V3 validates every requested dimension and measure in each Cube response row
  before canonicalization, across totals, additive groups, pair chunks, NULL
  groups and V4 replays. Explicit NULL remains valid; a missing member is an
  invalid response, never a passing baseline or a V4 mutation kill.
- The trusted-SQL lexical gate recognizes quoted and schema-qualified known
  volatile calls while preserving supported relation aliases, column lists and
  literals. Context controls include ONLY, adjacent AS, NOT, qualified OPERATOR,
  VALUES and ROWS FROM. No parser dependency or arbitrary SQL sandbox is added.
- Undated filter-drop and normalization-drop witnesses compare the original and
  mutated generated key/value bags with bidirectional EXCEPT ALL. NULL-to-zero,
  missing keys, cancellation and MIN/MAX changes are classified by exposed
  outputs rather than changed source-row counts. Dated witnesses are unchanged;
  this is current-data observability, not general or future-data equivalence.

## [0.16.0] - 2026-11-02

Release notes: [docs/releases/v0.16.0.md](docs/releases/v0.16.0.md).

### Added

- Per-mutant execution coverage with safe item ids, attempts and explicit unexecuted reasons in
  `verify/mutation-coverage.json` and mutant details. Positive `verify.mutant_max_items` (default 10000)
  bounds logical replay items; budget exhaustion is unjudged (`not_run`, `suite_budget`).
- Optional bounded enumeration of alternative subset-minimal OWL generated-group cores through
  `--explain-max-explanations` (default 1, ceiling 32) and `--explain-max-search-nodes` (default 256,
  ceiling 4096). Shared check/time budgets and explicit partial/complete state preserve the original
  failure, first core and uniqueness fields. No dependency or report authority is added.

### Changed

- V4 replays every eligible bounded V3 pair chunk and its NULL group instead of only the first chunk,
  reusing baseline key partitions in memory. Above `pair_key_budget` it still discloses sampling.
  Early kills retain unexecuted items in the receipt; baseline failures cannot establish survivor verdicts.

## [0.15.0] - 2026-11-01

Release notes: [docs/releases/v0.15.0.md](docs/releases/v0.15.0.md).

### Added

- Optional authored `gold.expect.spec_source` binds opaque source bytes with a
  project-relative path and SHA-256 pin. Missing, changed, blocked or unavailable
  sources stop fixture cases before DB access; completion drift demotes passes.
  Public metadata uses states and hashes only. Legacy unbound cases keep their
  behavior; source equality is not semantic approval. See [scope](docs/knowledge-sources.md).
- The diagnostic `okf` exports explicitly selected metadata or previews a local
  bundle using closed profile `sxc-verification-contract-metadata/v1`, OKF 0.2.
  Preview applies nothing; receipts and raw source/SQL/rows/paths are omitted.
  Integrity is not authenticity. See [profile and bounds](docs/okf.md).
- Standalone `knowledge-evolution` replays the bundled T0-T4 synthetic source
  transitions through actual config/source/fingerprint/report APIs. Its fixed
  26 contracts retain missing observations, restore without case promotion, and
  reject stale synthetic receipts on retirement. No DB, engine or model executes,
  and semantic coverage is not measured. See [offline replay](docs/knowledge-evolution.md).

All three additions are opt-in and outside operational report requirements.
Dependency pins and default pipeline stages remain unchanged.

### Fixed

- Result publication uses a unique exclusively created temporary file instead
  of truncating a fixed `.part` path. Source-bound expectation and OKF error
  paths preserve inputs after observed configuration drift; preview retains
  its validated inventory through final publication.
- Windows junction fixtures are registered only on Windows, preserving real
  junction checks there without introducing unavoidable skips in the strict
  Linux integration runner.

## [0.14.0] - 2026-10-31

Release notes: [docs/releases/v0.14.0.md](docs/releases/v0.14.0.md).

### Fixed

- `CubeClient.load` accepts a Cube `/load` answer only in Cube's load envelope, a JSON object whose `data` is a list
  of row objects. Any other HTTP 2xx answer without an `error` member (an empty object, `results` instead of `data`,
  `data` that is an object or a string, a body or row that is not an object, a body that is not JSON) is an `error`,
  never zero rows: a V1 or V3 check carries `cube=invalid_response` and its next action points at the adapter, while
  a V2 stratum check counts such bindings by type under `errors` (`CubeResponseError`) and keeps the general next
  action, as for its other binding errors. Read as no rows, such an answer passed V1 and V2 vacuously against empty
  gold rows, passed a V3 pair test of at most 2,000 keys (the key cap) over no keys whatever the source data held,
  and counted as a V4 kill against non-empty gold rows; the other malformed JSON answers stopped the verify stage
  with an unexpected exception (in V4 they left the mutant unjudged without the retry). A body that is not JSON was
  already an `error`, but named `JSONDecodeError` or `UnicodeDecodeError`, and a V1 or V3 next action pointed at the
  gold SQL instead of the adapter.
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
  measure rows, which for a model generated from `crosscheck.toml` is 0 only when every query answers the same; such
  mutants are served, and one that survives fails V4. A verify that passed 0.13.0 with `v4:closure_join_swap:<m>`
  listed as equivalent can now fail V4 when no gold query observes the change, for example when only the joined
  measure row changes and the gold queries read only values.

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
[0.15.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.15.0
[0.14.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.14.0
[0.16.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.16.0
[0.17.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.17.0
[0.18.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.18.0
[0.18.1]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.18.1

[0.19.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.19.0

[0.19.1]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.19.1

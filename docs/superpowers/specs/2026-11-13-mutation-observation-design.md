# Mutation observation and authored join/aggregate fixtures

The user authorized implementation and public GitHub release of the proposed first package: full observable mutation-test records and small independently authored duplicate-join/aggregate fixtures. Independent operational measure evaluation and SQL text reduction remain later research.

## Design

1. Add `verify.mutant_observation = "first" | "all"`, default `"first"`. Reuse the existing bounded mutation suite and its per-mutant `mutant_max_items` budget. In `all` mode continue selected baseline-passing suite items after the first non-pass, while returning the same first non-pass verdict as the legacy suite. Diagnostic observations never upgrade authoritative detection, evidence facets, report status or exit codes. Existing one-error retry remains per item.
2. Emit `verify/mutation-matrix.json` only in `all` mode, using safe IDs, evidence kinds, categorical statuses and counts. Include selected mutant/item denominator, executed and not-run cells, all observed mismatch detectors, unique/redundant observed detector sets, excluded baseline items and partial/budget/skip reasons. A cell with error, timeout, startup failure, equivalent data or missing execution is never a detection. Item IDs identify the existing exact replay binding by stratum/index; never serialize parameter values. Unobserved cells cannot imply pass or uncovered business semantics.
3. The usual report continues using the first-detector result. `all` mode is an optional bounded diagnostic with increased runtime. Remove obsolete matrix artifacts at a new verify attempt, including default mode and early failure, so a previous run cannot masquerade as current. Use atomic artifact replacement and bind diagnostic output to available run/config/model identities without introducing a new authority path.
4. Add a separate small offline integer/text/NULL inner-join and aggregate reference module and authored literal fixture corpus. Preserve the existing one-column provenance corpus. Support only explicit bounded inputs and fixed operations: SQL-like equijoin (NULL never equals NULL), duplicate multiplicity, grouped/global `COUNT(*)`, `COUNT(value)` and `SUM(value)`, including all-NULL and empty aggregate rules. No general SQL evaluator, outer join, decimals/floats, arbitrary expressions, new runtime dependency or CLI stage.
5. Compare hand-written expected results with independent Python arithmetic and actual PostgreSQL 17 in the existing disposable integration environment. Include distinguishing wrong variants: deduplicating join inputs, confusing row/non-NULL counts, and SUM-to-MAX. These fixtures remain diagnostic/test evidence, not automatic authored Gold for operational datasets.

## Approaches and limits

The selected approach extends the current suite's receipt path and adds an isolated corpus. A separate mutation engine would add an unnecessary language/runtime dependency; changing the default suite to run all items would increase every user's cost. A new measure evaluator and general SQL reducer need independent semantics/parser work and are outside this release.

StrykerJS `disableBail` and Mutation Testing Elements distinguish observed detections from mere execution coverage. SPES supplies bag-semantics background. These are design references; their engines/code are not bundled. Pin public source references and preserve licenses/attribution. Do not imply that SPES proves PostgreSQL/Cube equivalence or that full observation covers every bug.

## Acceptance

- Default first-mode statuses and receipts remain compatible. All-mode returns the same first result for an identical observed prefix and records later outcomes without changing report authority.
- Budget, errors/retries, partial runs, skipped/equivalent mutants, baseline exclusions, generated/authored classifications and stale-file removal are exercised.
- Integer/NULL duplicate-join fixtures have independently authored literal expectations; integration tests require actual PG17 in provisioned CI.
- Repository tests, compile check, packaging/document checks, leak and whitespace scans pass. All required Linux/Windows/macOS unit jobs and provisioned zero-skip integration pass before release.
- Publish version 0.24.0 with source archive, checksum and verification evidence following the repository's existing release format. Verify public assets independently and update Reference Atlas with pinned implementation citations.

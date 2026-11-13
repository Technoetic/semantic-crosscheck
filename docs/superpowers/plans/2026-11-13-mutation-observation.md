# Mutation observation implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development for isolated tasks and independent reviews. Steps use checkbox syntax for tracking.

**Goal:** Ship optional complete observation of the selected mutation suite and authored duplicate-join/aggregate examples.

**Architecture:** Keep authoritative first-detector V4 behavior; extend bounded receipts and publish a diagnostic matrix. Keep the new synthetic corpus separate from operational verification and existing provenance fixtures.

**Tech Stack:** Python 3.11+, existing PostgreSQL 17/Cube integration, standard library only for new code.

**Spec:** `docs/superpowers/specs/2026-11-13-mutation-observation-design.md`

## Global constraints

- Default mode `first`; optional mode `all`; existing `mutant_max_items` applies per mutant.
- IDs, categorical states and counts only in diagnostics; no SQL parameter/row values or exception messages.
- Matrix never changes authoritative evidence/report interpretation. Unsupported/missing execution never passes.
- New corpus is bounded integer/text/NULL synthetic inner-join/aggregate arithmetic, not a general SQL engine.
- No new runtime dependencies or operational DB/MES changes.

## Review focus

- A first mismatch followed by a timeout retains the legacy verdict but records uncertainty in later cells.
- Budget exhaustion after a mismatch does not imply complete matrix coverage.
- An all-mode artifact followed by default/failed/filtered verify cannot be mistaken for current evidence.
- Duplicate and NULL join inputs retain SQL cardinality and NULL semantics.
- Empty global aggregates differ from empty grouped results; all-NULL SUM differs from zero.

### Task 1: bounded mutation observation

Files: config.py, mutation_coverage.py, stage_verify.py, mutate.py only as needed; new/related tests; docs/mutation-matrix.md. Do not edit release manifests, README, THIRD_PARTY or shared design docs.

- [ ] Write and observe failing tests for mode validation, default receipt compatibility, continued all-mode cells, retries/error isolation, first-verdict parity, per-mutant budgets and categorical safe metadata.
- [ ] Implement minimal mode/receipt/matrix support with atomic writes and stale diagnostic cleanup. Keep the existing external suite tuple interface.
- [ ] Add stage-level tests for baseline filtering, data-equivalent/startup/unrun rows, authored/generated annotations, selected bindings and output authority. Include an actual-runtime integration test using existing disposable fixtures.
- [ ] Run focused tests, the full available local suite, compile and whitespace checks. Record skips/environment prerequisites accurately. Commit only task files.

### Task 2: authored join/aggregate corpus

Files: new `join_aggregate_fixtures.py`, `examples/join-aggregate-fixtures/`, tests/test_join_aggregate_fixtures.py and companion PG17 integration test, docs/join-aggregate-fixtures.md. No edits to task1 or release metadata.

- [ ] Author literal expected results and tests before implementing the arithmetic. Cover duplicate joins, NULL join keys, mixed/all-NULL values, zero/negative values, two groups, empty grouped input and empty global aggregate.
- [ ] Observe expected failing tests, implement narrow validated arithmetic, and compare all cases with literal expectations.
- [ ] Add actual PG17 fixture comparisons plus wrong-query/incorrect-operation witnesses for deduplication, count confusion and sum/max.
- [ ] Run focused and full available suite, record skipped PG tests locally, and commit only task files.

### Task 3: integrate, review and publish

- [ ] Integrate isolated task commits; independently review code and execute targeted tests.
- [ ] Document exact operation/scope in README, skill/reference/command docs, design and THIRD_PARTY; update all manifest/runtime versions to 0.24.0 and changelog/release notes.
- [ ] Run repository suite/compile/leak/whitespace checks; push a review branch and run required CI. Fix failures with regressions. Merge only the verified reviewed result.
- [ ] Tag the exact verified main commit and verify tag CI. Build/check source ZIP, SHA256SUMS and verification evidence, create/publish release with all assets, then independently re-download and compare.
- [ ] Update and verify Reference Atlas pinned citations/catalog/publication using existing tools; record vault note/index/log/ADR and local commit without pushing the vault.

The two implementation tasks use disjoint files in separate worktrees so they can run concurrently. The primary agent owns integration and publication. Evidence and task ledgers stay outside the public source except this design and plan.

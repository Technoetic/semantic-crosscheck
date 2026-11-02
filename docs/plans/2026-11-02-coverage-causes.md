# Coverage and alternative explanations implementation plan

**Goal:** Expand V4 replay to every eligible pair-test chunk and NULL group, and optionally enumerate multiple verified subset-minimal OWL causes.
**Architecture:** Preserve existing engines and verdict authority. Retain bounded V3 key partitions in memory for replay; expand passing V4 items to explicit chunk/NULL items and record per-mutant execution coverage. Extend the existing OWL oracle with bounded hitting-set enumeration after the existing first-core path.
**Tech stack:** Python 3.11+, existing PostgreSQL/Cube and HermiT adapters. No new dependencies.
**Authorization:** User requested implementation of the two recommended features on 2026-11-02; routine API defaults are controller decisions. No publishing, installation update, company DB access or remote push.

## Global constraints
- Counts, safe ids and safe descriptors only; no rows, key values, DSNs or exception messages in receipts.
- A missing execution cannot establish survival or complete coverage. A verified early kill stays a kill with remaining items explicitly not_run.
- Existing source pins, runtime/report authority, reason consistency failure and first-core minimality/uniqueness semantics remain intact.
- Full SQL equivalence, globally smallest explanations, automatic repair and independent certification are not claimed.
- Work only in D:/worktrees/sxc-coverage-causes-20261102; preserve source main until reviewed local integration.

## Task 1: V4 chunk replay and coverage (controller)
Files: stage_verify.py, shape_q.py, mutate.py, config.py; new mutation_coverage.py if needed; tests/test_mutation_coverage.py, tests/test_pair_chunks.py; relevant docs.
- [x] Write regression tests proving a mutant affecting only a later chunk or NULL group is killed; watch old code fail.
- [x] Retain original in-memory keys after baseline and expand V4 into deterministic chunk ids and NULL-group id. Reuse baseline key read and preserve sample disclosure when pair_key_budget was exceeded.
- [x] Add verify.mutant_max_items integer >=1, default 10000; bounds logical replay items per mutant, not network retries. Retries remain one per erroring item.
- [x] Record all mutant instances and each selected replay item in verify/mutation-coverage.json: executed status/attempt count, unexecuted reason, equivalent-on-data/not-applicable/gated outcomes. Include executed receipts in mutant result detail. Explicit budget exhaustion is not_run, never survived or killed.
- [x] Preserve killed_by with the exact chunk id; baseline failures are excluded and prevent surviving incomplete suites from being judged.
- [x] Test baseline failure, later-chunk mismatch, NULL mismatch, all-pass chunks, sampled fallback, early kill, retry/error, budget reset, gate/equivalent/not-applicable, privacy and configuration validation.

## Task 2: Bounded alternative OWL cores (delegated isolated files)
Files: owl_explain.py and optional new focused helper; CLI reason flags; tests/test_owl_explain.py/new test_owl_alternatives.py; docs/owl-explanations.md and commands/xc-reason.md.
- [x] Write and run failing tests before product code.
- [x] Add explain(max_explanations=1,max_search_nodes=256); CLI --explain-max-explanations (1..32, default 1), --explain-max-search-nodes (1..4096, default 256).
- [x] Preserve default first-core status/reason/core/minimality/uniqueness/call order. All phases share existing check/deadline budgets.
- [x] Opt-in deterministic bounded hitting-set traversal; every added core has observed inconsistency and final one-deletion subset-minimal verification. Deduplicate, preserve duplicate-axiom guard.
- [x] Add explanations, explanations_found, enumeration_status (not_run/partial/complete), enumeration_complete, enumeration_reason, enumeration_checks_completed, search_nodes_visited and requested caps. Complete requires frontier exhaustion or already established first-core uniqueness.
- [x] Tests: independent/overlapping causes, deterministic results, call/time/node/count limits, exceptions/duplicates, CLI validation, safe output, actual HermiT when available.

## Task 3: Review and local completion
- [x] Run full unittest suite, compile checks, packaging/docs/leak checks. Log unavailable real engines as skips, never live passes.
- [x] Independent read-only code review and rerun focused checks; resolve important findings.
- [x] Commit owned source/doc files, locally integrate into main if clean/fast-forward only. No remote push.
- [x] Create/update NS implementation record with six handoff fields, index/log and own shared-state entries only; commit only owned vault deltas, preserve concurrent edits.

## Review focus
- Cached key partitions must never leak into receipts, and V4-only runs must obtain baseline partitions before expansion.
- Chunk ids must correspond to the same bounded baseline key snapshot; no shared Cube/SQL MVCC snapshot is claimed.
- A retry or second mutant must not reuse a previous mutant receipt or budget.
- Interrupted enumeration cannot imply uniqueness/completeness or erase an already verified first core.
- Enumeration frontier and response sizes must remain bounded by the stated search and result caps.

## Verification outcome

2026-11-02: full unittest 1440 run, 1174 passed, 266 environment/platform skips, zero failures/errors.
Compile, docs, packaging, staged leak scan and three independent reviews passed. PostgreSQL/Cube/HermiT
live integration was unavailable; synthetic replay/oracle checks do not stand in for live engine results.
Local integration/receipt paths and commit are retained in the NS task handoff; publication is not authorized.

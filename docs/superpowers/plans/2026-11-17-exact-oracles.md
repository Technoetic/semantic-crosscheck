# Exact arithmetic and bounded diagnostics implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Root coordinates shared interfaces, commits and publication.

**Goal:** Publish a verified v0.28.0 fixing context-sensitive additive verdicts and adding two explicit bounded diagnostics.

**Architecture:** Preserve the existing pipeline and result authority. Add a pure exact numeric helper and separate optional measure/plan diagnostic modules; root owns CLI, shared docs and release integration.

**Tech Stack:** Python 3.11+, existing pg8000/Cube adapter; PostgreSQL 17 fixture databases; existing full-stack CI.

**Spec:** `docs/superpowers/specs/2026-11-17-exact-oracles-design.md`

## Global constraints

- No new runtime dependency, arbitrary SQL in plan specs, operational data changes or active plugin install.
- Results contain counts, identifiers, hashes and fixed reasons only. Diagnostics do not add default report authority.
- All rejected/unsupported/budgeted cases fail closed. Only root stages, commits, pushes, merges or publishes.
- Base public source cf620038d33cae5cf81d8f2e2fc0e16d5f7a424f; isolated branch feat/v028-exact-oracles.

## Review focus

Exact arithmetic with hostile contexts; proof of oracle independence; genuine physical plan diversity and full bags; stale receipts/input aliasing; default pipeline compatibility. Each is assigned to the corresponding tests below.

## Tasks

### A. Exact arithmetic

Files: new `exact_numeric.py`, `_sum` in `shape_q.py`, `tests/test_exact_numeric.py`, `tests/test_shape_additive_exact.py`.

- [ ] Add failing production-judge precision/nonfinite/budget and exact-sum tests; record RED.
- [ ] Implement `exact_sum` and `NumericLimit` as specified; integrate the legacy parser.
- [ ] Run new and existing additive/compare/shapes tests; inspect bounds and no context mutation.
- [ ] Independent read-only source review before release.

### B. Independent measure oracle

Files: new `measure_oracle.py`, scoped unit/PG tests, `docs/measure-oracle.md`, `examples/measure-oracle/`.

Consumes A's exact_sum. Produces `run(cfg,args,out)->Result('measure_oracle')`, `prepare_run(args,out)`, closed spec documentation and synthetic fixtures.

- [ ] RED tests for wrong shared generator filter, NULL/count, precision, empty/limits, invalid types, changed inputs and value redaction.
- [ ] Implement raw export/Python predicate/exact calculation and existing Cube lifecycle with receipt validation.
- [ ] Add real PostgreSQL tests and verify with a throwaway cluster.
- [ ] Review module/import boundaries and diagnostic-only result scope.

### C. Differential plan diagnostic

Files: new `plan_compare.py`, scoped unit/PG tests, `docs/plan-compare.md`, `examples/plan-compare/`.

Produces `run(None,args,out)->Result('plan_compare')`, `prepare_run(args,out)`. Args: spec, dsn_env (SXC_TEST_ADMIN_DSN), statement_timeout_ms (1000), quiet.

- [ ] RED tests for typed spec, full bags, structural plans, missing diversity, cap/empty/budget, rollback/timeout and changed inputs.
- [ ] Implement declarative TEMP fixture, bound values, EXPLAIN-confirmed comparison and result redaction.
- [ ] Verify integer and timestamp sentinel cases on real PostgreSQL, preserving exact supported scope.
- [ ] Review no SQL sandbox claims, no reducer/minimality and no production table writes.

### D. Controller integration and publication

Files: `sxc.py`, CLI security tests, default report scope/docs, version manifests, CHANGELOG, README, SKILL, reference/command docs and `docs/releases/v0.28.0.md`.

- [ ] Add failing CLI dispatch/alias/stale-result/usage-redaction tests for both new stages.
- [ ] Integrate parser/diagnostic preflight and protected publication; preserve per-module inputs.
- [ ] Update documentation and pinned conceptual references; run all unit/scoped PG/full-stack tests and leak checks.
- [ ] Independent broad review, address findings, repeat affected tests.
- [ ] Push candidate/PR; require all CI jobs including strict integration. Merge only reviewed candidate; annotate v0.28.0 tag.
- [ ] Build release ZIP from Git blobs with SHA256SUMS/verification receipt, independently verify draft and published assets, then publish authorized release.
- [ ] Update Reference Atlas from pinned published citations; independently verify registry/site and record vault handoff locally.

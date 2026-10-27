# Boundary fixtures and constraint audit implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add independent boundary fixtures and an optional, replay-bound constraint sensitivity diagnostic.

**Architecture:** Reuse the existing gold.expect runner for three independently specified boundary cases. Keep constraint sensitivity in a separate module reached only through explicit CLI flags, with two bounded search arms and conditional findings.

**Tech Stack:** Python 3.11+, PostgreSQL 17+, Cube 1.8.3, existing bounded Z3 generator.

**Spec:** docs/superpowers/specs/2026-10-27-boundaries-constraint-audit-design.md

## Global Constraints

- Existing default behavior and report/replay contracts remain unchanged when the audit is off.
- PostgreSQL 17+, Cube 1.8.3, existing dependencies only; synthetic data only.
- No arbitrary joins, expected-error-as-PASS DSL, copied float comparator or SQL equivalence proof.
- Summaries contain counts, safe identifiers and hashes; never SQL, row values, secrets or local paths.
- Each feature must fail closed and retain actual PostgreSQL/Cube confirmation as applicable.

## Review Focus

- Missing fact aggregate NULL must not silently become zero; test both explicit policies.
- Filtered-out facts must differ from absent parents; test independent expected rows and a wrong filter.
- Same dimension label in different groups must remain separate under composite keys; test a collapsing mutation.
- Relaxing a primary-key component must preserve declaration and FK consistency; test overlapping rules.
- Audit replay must bind both domains and refuse stale/mismatched provenance, SQL, bounds and selections.

### Task 1: Independently authored boundary corpus

**Files:** Create `examples/expect-boundaries/` with fixture.sql, fixture-spec.md, crosscheck.toml, gold SQL; create `tests/test_expect_boundaries.py` and `tests/test_expect_boundaries_pg.py`; update example documentation.

**Interfaces:** Consumes existing config.load / expect.run; produces three named gold.expect cases: parent_without_facts, facts_filtered_out, same_label_different_region. Reuse supported node/measure link and composite keys. No runtime change unless a test exposes an actual supported-contract bug, which must first be reported to the controller.

- [x] Write tests before corpus; demonstrate missing corpus fails and wrong authored rows are rejected.
- [x] Author a tiny part/stock synthetic fixture and a standalone specification calculating expected rows; use raw NULL and explicitly declared zero_if_null as needed.
- [x] Implement three supported cases, with SHA-bound fixture, independent gold rows and no upstream code copy. Document inspiration and pinned source, current NULL-grain policy and bounded scope.
- [x] Run focused config/unit checks plus real PostgreSQL/Cube tests; inject wrong expectations and a supported wrong-model/filter/composite grouping to prove the checks detect them.
- [x] Commit only owned files and report commands/results plus remaining limitations.

### Task 2: Optional constraint provenance and sensitivity audit

**Files:** Existing constraints/witness/search/CLI integration plus a focused constraint_audit module and tests; docs and a synthetic example.

**Interfaces:** Final flag and artifact contract follows the code-based C2 design review in the task brief. Ordinary counterexample dispatch remains untouched without the explicit audit flag.

- [x] Pin public options, versioned manifest with source-declaration relaxation and per-declaration provenance, dual-arm classification and replay bindings in the task brief before implementation.
- [x] Write failing tests for opt-in dispatch, validation, relaxation dependencies, masked COUNT witness, no-witness/incomplete cases and replay mismatches.
- [x] Implement the smallest bounded two-arm diagnostic reusing existing PostgreSQL witness confirmation and generator; never reinterpret a relaxed witness as an ordinary domain violation.
- [x] Run focused unit/integration tests and default-mode regression tests, document usage, provenance limitations and budget costs.
- [x] Commit only owned files; report exact evidence.

### Task 3: Integration and independent verification

- [ ] Review each task's full diff against its brief, fix substantive findings and re-review.
- [ ] Run the complete provisioned test suite without skips, compile, CLI/docs/package checks and leak/whitespace scans.
- [ ] Have an independent reviewer inspect the integrated diff and rerun the important feature checks.
- [ ] Record evidence and unresolved limits, commit the implementation locally and update the vault handoff. No remote push or release.

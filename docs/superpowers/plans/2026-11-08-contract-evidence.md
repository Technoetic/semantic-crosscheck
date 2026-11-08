# Contract evidence implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task.

**Goal:** Deliver current contract-bound report evidence, opt-in PostgreSQL AST diagnostics and independent provenance-scope fixtures, then publish verified source and catalog uses.

**Architecture:** Existing report authority changes only for configurations explicitly selecting paired contract/plan sidecars. SQL structure and bag fixtures remain separate supported-subset tools/tests.

**Tech stack:** Python 3.11+, existing PostgreSQL17/HermiT/Cube/Jena integration matrix; optional exact-pinned PG17 pglast parser.

**Spec:** `docs/superpowers/specs/2026-11-08-contract-evidence-design.md`.

## Global constraints

- Counts, hashes and fixed reasons only in emitted diagnostics; never SQL/row/secret/path values.
- No company DB access, active installation change, Lean/ProvSQL runtime or Sites hosting.
- Parser remains optional; defaults and legacy non-opted-in reports retain their authority.
- Preserve strict input bounds, observed path/change checks and fail-closed statuses.
- Unsupported SQL and unproven theory/SQL mappings must not become PASS.

## Review focus

- Saved preflight PASS for different sidecars or a changed evaluator must not satisfy the report gate.
- Missing/failed/not_run preflight and malformed result must not pass opted-in reports.
- SQL aliases/CTEs/casts/complex predicates cannot silently pass a narrow structural checker.
- Parser absence/version mismatch and hostile SQL/input errors must remain redacted non-passes.
- Nat annotations, SQL NULL behavior and bag multiplicity must remain separate and independently tested.

## Task 1: Bind contract evidence into an opted-in report

Files: `config.py`, `semantic_preflight.py`, `report.py`, optional focused
`contract_evidence.py`, `tests/test_contract_evidence*.py`,
`docs/contract-report.md`, `examples/contract-report/`.

- [ ] Write and run failing negative tests for opted-in missing/stale/mismatched/failed evidence and unchanged legacy reports.
- [ ] Add paired config selection and current-byte/evaluator binding, recomputed status and safe next actions.
- [ ] Run focused report/preflight/config suites and commit only owned files.

## Task 2: Add bounded offline PostgreSQL SQL structure diagnostics

Files: `sql_structure.py`, `sxc.py`, `tests/test_sql_structure*.py`,
`docs/sql-structure.md`, `commands/xc-sql-structure.md`,
`requirements/sql-structure.txt`, `examples/sql-structure/`.

- [ ] Write and run failing supported-query, mismatch, unsupported-query and redaction/input-guard tests.
- [ ] Define strict v1 expectations and implement actual parser-backed bounded checks; missing parser is not_run.
- [ ] Run real parser and no-parser tests, update help/command docs, commit only owned files.

## Task 3: Add independent bag/provenance-scope fixtures

Files: `provenance_fixtures.py`, `examples/provenance-fixtures/`,
`tests/test_provenance_fixtures*.py`, `docs/provenance-fixtures.md`.

- [ ] Write failing tests using literal independently reviewed expected bags and the exact pinned Dedup/Diff counterexample.
- [ ] Implement bounded offline reference operations; add throw-away PostgreSQL companion tests and proof-scope documentation.
- [ ] Run offline cases and provisioned integration through CI; commit only owned files.

## Task 4: Integrate, review, release and publish references

Files: version manifests, README/CHANGELOG/SKILL/design/reference docs,
`docs/releases/v0.21.0.md`, CI optional parser setup and packaging/docs contracts.

- [ ] Align public docs and license/dependency notices with actual supported scope.
- [ ] Run full local suite, compile and leak scans; independent reviewers rerun new tests and inspect negative boundaries.
- [ ] Publish feature source through existing GitHub workflow; require exact final-source matrix and zero-skip provisioned integration.
- [ ] Build committed-source archive, verification receipt and SHA256SUMS; independently verify downloads before and after publication.
- [ ] Publish annotated v0.21.0 tag/release, confirm source/tag/asset identities.
- [ ] Update existing Reference Atlas with pinned actual uses, independently verify, publish through its existing Pages pipeline.
- [ ] Record task artifact paths, actual checks, assumptions, unresolved limits and next safe action in the vault without unrelated edits.

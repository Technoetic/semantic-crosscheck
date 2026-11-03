# Response-contract and mutation-witness hardening implementation plan

> For agentic workers: use superpowers:subagent-driven-development. Work on the shared isolated branch only in assigned files; the controller owns integration, documentation and commits.

**Goal:** Close V3 missing-member acceptance and quoted known-volatile-function gaps, and validate aggregate filter witnesses on disposable PostgreSQL/Cube before changing their supported undated scope.

**Architecture:** Reuse existing missing-member/error contracts and the conservative SQL gate. For undated aggregate mutations, compare the actually exposed original/mutant key-value bags after a real-engine reproduction. Keep dated witnesses and unsupported SQL outside that extension.

**Tech Stack:** Python 3.11+, pinned pg8000, PostgreSQL 17, existing Cube 1.8.3 adapter, stdlib tests.

**Spec:** This file, Specification section.

## Specification

- C1: every returned V3 row must contain each dimension/measure requested for its query. Present NULL remains valid; an absent key is an invalid response, including NULL groups, chunked and mutant replay paths.
- C3: a quoted or schema-qualified invocation of a known volatile function must not escape the existing unsupported-input gate. Quoted non-function identifiers and string literals retain existing behavior; unsupported SQL remains a conservative boundary, not a sandbox.
- C2: first reproduce NULL-to-zero, cancellation and non-extremal MIN/MAX on disposable synthetic PostgreSQL/Cube fixtures. Implement only source-confirmed undated observability corrections; keep a failed/unavailable experiment explicit and do not substitute SQLite for actual-engine evidence.
- User authorization covers this first research bundle and necessary local verification/integration. Broader Gungnir/MC, new reasoners, deployment, releases and active installations are outside this change.

## Global constraints

- No company DB/data; synthetic part/bom_line/stock/sales_order only.
- No new runtime dependency, default network/model call, operational report authority, release tag or package version bump.
- Never emit source row values, DSNs, secrets or machine paths in normal program errors/results.
- Missing/invalid responses are error/not_run, not a killed mutant or pass.
- Preserve baseline key/item/time budgets, explicit NULL semantics, count-empty conventions and data-revision contracts.
- Separate RED/GREEN evidence from baseline and integration runs.
- Keep actual environment and skip counts explicit. No GitHub/NS remote push.

## Review focus

1. Present NULL versus absent requested member in each V3 response mode.
2. Missing rows/member errors inside V4 must not become false mutation kills.
3. Quoted aliases/schema identifiers that resemble SQL keywords must keep existing support.
4. Undated NULL, cancellation, duplicate/value grouping and MIN/MAX observability; no arbitrary equivalence proof.
5. Runtime processes/data/ports isolated from user services; verify and stop owned test resources.

## Tasks

### Task 1: V3 response members
Files: shape_q.py and focused new V3 member tests; controller updates docs.
- [ ] Run existing pair/envelope baseline.
- [ ] Add meaningful missing-member/explicit-NULL and all affected mode regressions; record expected RED.
- [ ] Reuse typed invalid-response handling and validate every requested member before canonicalization.
- [ ] Run focused pair/chunk/coverage/mutation tests; record GREEN and privacy/error semantics.
- [ ] Independent review before integration.

### Task 2: Quoted volatile calls
Files: tlp.py, focused TLP/NoREC lexical regression tests; controller updates docs.
- [ ] Run existing TLP/NoREC baseline.
- [ ] Add quoted/schema-qualified calls and non-function/literal controls; record expected RED.
- [ ] Separate quoted invocation names from literals without changing unrelated keyword behavior.
- [ ] Verify both builders, accepted identifiers, malformed/escaped lexical forms and no dependency change.
- [ ] Independent review before integration.

### Task 3: Aggregate filter witness experiment and correction
Files: mutate.py, dedicated PG/integration filter witness tests and a disposable experiment record.
- [ ] Discover existing disposable engine tools/caches; do not use company services.
- [ ] Execute original SQL and actual Cube on NULL-to-zero, cancellation and non-extremal aggregate fixtures; retain evidence.
- [ ] Only after a confirmed observation, add RED source-level/live regressions.
- [ ] For undated supported measures, use NULL-aware bidirectional exposed key/value bag differences; retain dated semantics.
- [ ] Verify mutation coverage statuses and unrelated closure witness tests.
- [ ] Independent review of observability and scope.

### Task 4: Integration and local delivery
- [ ] Update README/design and scoped docs to match supported behavior.
- [ ] Run full appropriate suite once after code freeze, exact counts/skips and focused real-engine probes.
- [ ] Run compile, docs/package and staged leak checks; independently review final diff.
- [ ] Commit scoped changes and fast-forward the local main if still at the authorized baseline.
- [ ] Record important six-field handoff, independent evidence and owned NS metadata with partial staging; no remote push.

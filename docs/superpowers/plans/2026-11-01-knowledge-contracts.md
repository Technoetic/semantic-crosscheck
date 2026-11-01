# Knowledge contracts implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add authored-source version binding, selected limited OKF export/preview and an offline multi-revision regression diagnostic.

**Architecture:** Optional source-binding logic is shared by expect and the two diagnostics. Operational report authority stays unchanged. Bounded metadata operations use only existing Python/std-library facilities.

**Tech Stack:** Python 3.11+, unittest, existing pg8000/witness development dependencies, no new feature runtime dependency.

**Spec:** `docs/superpowers/specs/2026-11-01-knowledge-contracts-design.md`.

## Global constraints

Read the spec's Global constraints verbatim. No output of bodies/rows/SQL/params/DSNs/env names/absolute paths or exception messages. No new live/model/network effects. No release/version/installed-host changes. The user authorized implementation and local integration; public push/tag/release is outside this task.

## Review focus

- Source/output hardlinks, symlinks or junctions must not let CLI cleanup destroy or read outside selected inputs.
- Mutation of an earlier source during a later capture or a running case must invalidate old pass evidence.
- Preview input remains untrusted even after matching hashes; forged pass/trust fields never reach operational report authority.
- Existing unbound expect and report behavior must remain compatible; diagnostic source metadata must survive the CLI's input stamping.
- Exact-byte restoration is current by hash but creates no new case PASS or real-engine evidence; synthetic coverage must remain explicit.

### Task 1: Authored source bindings

**Read first:** spec.md S1 and Global constraints. Work only in the assigned worktree. Do not spawn subagents.

**Files:** Create `skills/semantic-crosscheck/scripts/semantic_crosscheck/knowledge_sources.py`, `tests/test_knowledge_sources.py`, `docs/knowledge-sources.md`; modify `config.py`, `expect.py`, `result.py`, `skills/semantic-crosscheck/scripts/sxc.py`, related expect CLI/config/result tests, `skills/semantic-crosscheck/references/config.md` and `commands/xc-expect.md` as needed. Update the existing SKILL/README command guidance only for this capability. Copy the finalized spec/plan into the public docs/superpowers/specs and plans locations when production edits begin.

**Interfaces:** SourceBinding; validate_spec_source(value,ref); preflight(cfg,out); capture(cfg,cases,out=None)->SourceSnapshot with ok/public(); finish(snapshot,result)->SourceSnapshot. Config Expect gains optional spec_source. Cases retain (gold_index,gold,case_index,case). Public schema/limits/states are fixed by S1 spec. CLI preserves only spec_sources for bound expect. Do not alter global report inputs or grain.

- [ ] Write a behavioral test that a valid authored spec_source is accepted, with controlled missing/changed evidence stopping before DB. Run against baseline and record the expected failure, not a syntax/import typo.
- [ ] Add the eight S1 behavior groups: legacy compatibility, closed schema, current pin/privacy, missing/changed/blocked/unavailable, path/alias/no destruction, cross-file scan mutation, during-case mutation with worse-status preservation, and limit boundaries. Also reproduce Result.write truncating a synthetic source through an existing fixed .part hardlink; preserve it with unique exclusive temporary output and verify cleanup/final writes.
- [ ] Implement the smallest source module and expect/config/CLI integration. Do not read/emit arbitrary labels or source text. Preserve fixed namespace through CLI. Validate output protections before cleanup and in direct calls.
- [ ] Run focused source/expect/CLI/config/packaging/doc tests. Record commands and exact actual counts/skips. Read diff for default behavior or result leaks.
- [ ] Commit only Task 1 files through normal hooks. Write `.sxc/knowledge-20261101/task-1-report.json` with commit range, red/green evidence, tests, assumptions and unresolved findings.

### Task 2: Limited OKF export and preview

**Read first:** spec.md S2 and Global constraints. Work only in the assigned worktree. Do not spawn subagents.

**Files:** Create `semantic_crosscheck/okf.py`, `tests/test_okf.py`, `docs/okf.md`, `commands/xc-okf.md`; modify `sxc.py`, SKILL, README, config reference/packaging/docs tests as needed. Paths to semantic_crosscheck modules are beneath `skills/semantic-crosscheck/scripts/`.

**Interfaces:** Reuse capture(cfg,cases,out) and SourceSnapshot.public() for selected Gold cases. Implement run(cfg,args,out)->Result and CLI okf with --export/--import-preview, --select required for export. Use exact S2 layout/schema/profile/limits. Metadata-only concept type is SXC Verification Contract. Result namespace is okf_profile; no operational report dependency changes.

- [ ] Write export subset and roundtrip preview tests; demonstrate red because the stage/profile does not exist.
- [ ] Add eight groups: selected subset and no sensitive strings, unchanged preview/no writes, business-source drift, forged verdict fields, untrusted metadata/no execution, existing/alias destinations, exact inventory/path tampering, malformed/over-limit/deep input rejection.
- [ ] Implement bounded manifest/frontmatter profile, guarded metadata generation/preview, stable pre/post observations and no-clobber publication. Keep source metadata and raw computations private with explicit losses. Unsupported SXC profile is not invalid general OKF.
- [ ] Register command and document operation PASS vs runtime/correctness/authenticity not_proven. Run focused and integration tests with Task 1; confirm config/bundle bytes unchanged and no DB/model/network calls.
- [ ] Commit only Task 2 files and write `.sxc/knowledge-20261101/task-2-report.json` with actual red/green/focused evidence.

### Task 3: Offline multi-revision evaluation and final integration

**Read first:** spec.md S3 and Global constraints. Work only in the assigned worktree. Do not spawn subagents.

**Files:** Create `semantic_crosscheck/knowledge_evolution.py`, `semantic_crosscheck/resources/knowledge-evolution-v1.json`, `tests/test_knowledge_evolution.py`, `docs/knowledge-evolution.md`, `commands/xc-knowledge-evolution.md`; modify CLI, SKILL, README/design/changelog/references and packaging/doc tests as required for the final capability set.

**Interfaces:** run(None,args,out)->Result diagnostic-only; actual config/fingerprint/source/report pure APIs; fixed T0-T4 fixture operations/states as S3 spec. Namespace knowledge_evolution preserves fixture+engine hashes. No external fixture path/config and no operational artifact generation.

- [ ] Write fixed five-checkpoint behavior tests with independent literal expectations; run red before the module/stage exists.
- [ ] Add no-op, irrelevant file vs whole-input change, stale synthetic pass, restore/no new receipt/no promotion, retire/no cases/no vacuous success, corrupted fixture and always-current classifier mutation, counts/coverage and no side-effect tests.
- [ ] Implement fixed bounded replay and safe public counts/IDs. Exercise actual existing APIs; do not compute oracle expectations from the same classifier. Distinguish synthetic harness-check success from engine/data/semantic verification.
- [ ] Complete final user-facing docs and command inventory. Keep release version at 0.14.0 and label new source unreleased. Record exact supported profile and source-binding scope.
- [ ] Run focused CLI smoke/metadata tests and the full project suite in the prepared environment, compile sources and leak scan. Do not hide initial failures or environment skips. Commit task files and write task-3-report.json plus final acceptance results.
- [ ] After root's independent whole-branch review and any required fixes, root fast-forwards clean local main to the verified feature head. No push/release/install.

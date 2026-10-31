# Bounded diagnostic contracts Implementation Plan

> For agentic workers: execute with superpowers:subagent-driven-development and test-first regression checks.

**Goal:** Implement opt-in SHACL repair fixtures, independent measure grain checks and bounded same-reasoner OWL explanations.
**Architecture:** Three focused modules follow Result/fail-closed/read-only conventions; root wires CLI and documentation.
**Tech Stack:** Python3.11+, unittest, pySHACL, owlready2/HermiT, PostgreSQL17, existing venv.
**Spec:** docs/diagnostic-contracts.md

## Global constraints

No new dependency; no default pipeline/output behavior change; counts/ids/hashes only; no operational writes; no release/version bump. Work only in a separate worktree. Feature agents do not commit or edit shared CLI/docs. Root stages/commits the reviewed integration.

## Review focus

- Empty or erased target sets must not pass retention obligations.
- Wrong/duplicate grain keys, nullable grain exclusions and orphan/fanout joins must be diagnosed under explicit policy.
- Explanation limits/errors must preserve original inconsistency and never claim unverified minimality.
- Malformed/unknown manifests and source/output aliases cannot leave stale successful results or overwrite inputs.
- RDF/Java/SQL errors must not disclose row values, private paths or secrets.

### Task1: Repair fixtures

- [x] Write failing tests in tests/test_shacl_repair.py: successful numeric/class/pair repair, retained-target deletion, hard mutation, exact expected report mismatch, unsupported shapes, malformed schema, limits and alias protection.
- [x] Execute red tests and record failure.
- [x] Implement semantic_crosscheck/shacl_repair.py run(cfg,args,out), dedicated synthetic examples/shacl-repair and docs/shacl-repair.md. args.spec and args.timeout_ms are public interface.
- [x] Execute green tests using actual pySHACL when installed; record limitations.
- [x] Root independent review before integration commit.

### Task2: Grain contract

- [x] Write failing tests in tests/test_grain.py and tests/test_grain_pg.py for source/group/output operation and independent key contract mismatch, supported success, duplicate key, orphan/null evidence, unsupported date/closure fragment, strict schema/limits and no leak.
- [x] Execute red tests and record failure.
- [x] Implement semantic_crosscheck/grain.py run(cfg,args,out), args.spec/args.max_rows, examples/grain and docs/grain.md using quoted identifiers and existing db.read_only.
- [x] Execute green pure and actual dedicated-PG tests when prerequisites exist.
- [x] Root independent review before integration commit.

### Task3: OWL explanation

- [x] Write failing tests in tests/test_owl_explain.py for real no_children/disjoint inconsistency, sufficient core, verified subset-minimal core, capped partial result, skipped/consistent cases, timeout/error and no leaks.
- [x] Execute red tests and record failure.
- [x] Implement semantic_crosscheck/owl_explain.py and bounded axiom-subset support in owl.py; stage_reason.run/_owl threads args only in opt-in branch. args.explain_inconsistency/explain_max_axioms/explain_max_checks/explain_timeout_ms match spec defaults.
- [x] Execute green tests with actual HermiT; document process timeout and same-reasoner boundary in docs/owl-explanations.md.
- [x] Root independent review before integration commit.

### Task4: CLI, documentation and integration

- [x] Add failing tests for three CLI paths, missing/invalid options, option exclusions, no default diagnostics and safe failure/stale cleanup.
- [x] Wire sxc.py parse/config/dispatch/fingerprint/summary/cleanup without promoting standalone checks to the default pipeline. Update command docs, SKILL references, README and CHANGELOG consistently.
- [x] Execute each runnable synthetic example and targeted tests. Run complete suite with available provisioned runtimes, compilation and tools/leak_scan.py.
- [x] Independent spec/code review; fix findings and rerun affected tests.
- [x] Commit reviewed source locally, fast-forward clean local main; record exact checks and important six-field vault handoff. No network push/release.

## Execution record

All four tasks were implemented and independently reviewed. The full provisioned run executed1307 tests with zero skips;1305 passed and two documentation assertions failed. Documentation and its static template checker were corrected, then all37 documentation tests passed on the real runtime. Production Python, dependencies, schemas and examples remained unchanged through this correction. Separate real-PG/pySHACL/HermiT, original-graph core/deletion, process containment and input-protection regressions are recorded in the external task evidence. Local source integration is recorded by the implementation commit; no release was published.

# Evidence boundaries implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reject malformed evidence, generate useful bounded scalar domains,
and reduce TLP/NoREC discrepancies without increasing operational authority.

**Architecture:** Independent result and RDF boundaries; a standalone offline
domain builder feeding the existing witness API; opt-in reduction connected to
the existing TLP/NoREC evaluator. Default diagnostic and operational contracts
remain distinct.

**Tech Stack:** Python3.11+ stdlib, existing pinned optional reason/witness
requirements, PostgreSQL17/Cube1.8.3/Jena6.2.0 integration.

**Spec:** `docs/superpowers/specs/2026-11-05-evidence-boundaries.md`.

## Global constraints

- No company database, `.env` read, installed-plugin change or new runtime dependency.
- No general SQL parser/equivalence, model-performance or globally minimal bug claim.
- Values appear only in explicit synthetic fixture artifacts; errors are sanitized.
- No tag rewrite, force push, hook bypass or skipped required integration checks.

## Review focus

- Malformed check entries, IDs, statuses, duplicates and version/type confusion.
- SHACL warning/info conformance flags and valid cross-group results.
- SQL-like text outside the boundary grammar and numeric/date representation limits.
- Reduction accepting a different mismatch, error or unstable fixture.
- Forged/stale replay, changed input sources, partial output and budget exhaustion.

### Task1: Saved-result validation (independent worker)

Files: `result.py`, `report.py`, optionally `result_contract.py`; focused result/
report regression tests. Public contract: existing valid `Result` files and
report helpers remain supported; malformed persisted evidence yields error.

- [ ] Reproduce malformed entries and write failing contract tests.
- [ ] Implement minimal explicit version/type/ID checks and sanitized failures.
- [ ] Run focused tests, compatible legacy tests and record red/green evidence.

### Task2: Live SHACL report validation (independent worker)

Files: `shacl_check.py`, focused SHACL tests; reuse corpus/meta helper semantics
where appropriate without changing their supported profiles.

- [ ] Add failing malformed-root/conformance/result/cardinality tests.
- [ ] Add actual pySHACL conforming, violating, warning/info and pair/group checks.
- [ ] Implement envelope validation preserving allow-warnings and group isolation.
- [ ] Run focused reason/meta/corpus tests and record red/green.

### Task3: Boundary-schema experiment (independent worker)

Files: new `boundary_schema.py`, tests and small public examples; root owns
`sxc.py` CLI registration and final release docs. Interfaces: `run(cfg,args,out)`
returns normal Result; `load_spec`/`derive` produce the existing witness v1
document from the exact scalar declaration described in the spec. New command
name and Result stage: `boundary-schema` / `boundary_schema`.

- [ ] Write tests for integer/numeric/date boundaries and full grammar rejection.
- [ ] Implement exact, bounded derivation with hashed source and output artifacts.
- [ ] Round-trip output through `witness.load` and prove no DB/solver call.
- [ ] Add supported-subset docs/examples and focused tests.

### Task4: TLP/NoREC shrink/replay integration (root)

Files: `tlp.py`, optional new `tlp_reduction.py`, `sxc.py` flags, focused unit and
PostgreSQL tests. Reuse `reduction.py` public interfaces; coordinate changes if
an actual limitation requires modifying the shared reducer.

- [ ] Pin exact mismatch-signature and opt-in replay contract with failing tests.
- [ ] Connect bounded reduction and rechecks; keep default/v1 compatibility.
- [ ] Prove error/instability/signature changes cannot count as accepted witnesses.
- [ ] Add actual PostgreSQL round-trip and edited replay tests.

### Task5: Review and integration

- [ ] Review every worker diff and red/green evidence against the spec.
- [ ] Register CLI flags/stage and verify default commands remain valid.
- [ ] Obtain independent adversarial source review; fix concrete findings.
- [ ] Update version0.19.0 manifests, docs, third-party research acknowledgments,
      source-bound native discovery and release-metadata checks.
- [ ] Freeze source, run full local suite and static/leak/native checks.
- [ ] Fast-forward main, annotate/push new tag with user publication authority.
- [ ] Require both main/tag CI to pass8 jobs including zero-skip full integration.
- [ ] Build exact Git-blob ZIP and sanitized verification/checksum assets.
- [ ] Independently inspect unpublished draft downloads, publish immutable Latest,
      verify anonymous downloads and release/asset attestations independently.
- [ ] Record scoped NS evidence/decisions/index/log/anchors, local commit/mirror;
      preserve unrelated edits and never network-push the NS vault.

# Verification Hardening Implementation Plan

> For agentic workers: use executing-plans inline with independent research and final review. The user's implementation request authorizes execution of the three previously recommended changes.

**Goal:** close incomplete pair-comparison passes and add generated-shape syntax and bounded NoREC evidence.

**Architecture:** extend `shape_q`, preflight generated shapes in `stage_reason`, and select a NoREC branch inside the existing standalone TLP runner.

**Tech Stack:** Python 3.11+, PostgreSQL 17+, pinned pySHACL 0.40.1; existing unittest framework.

**Spec:** `docs/superpowers/specs/2026-10-25-verification-hardening.md`.

## Global Constraints

- Python 3.11+, PostgreSQL 17+, current pinned pySHACL 0.40.1 and Cube 1.8.3; no new runtime dependency.
- Only synthetic, disposable PostgreSQL data for tests; no production or NAS access.
- Results/console contain counts, IDs and fixed codes, never source row values, SQL, connection strings or local paths.
- Generated fixtures may occur only in explicit replay artifacts, as before.
- Keep version 0.9.0 and document new behavior under Unreleased; no remote push/release.

## Review Focus

- Missing-alone NULL-valued results must not pass when together results exist.
- Incomplete responses must not regain a D95 assertion through `sample_note` fallback.
- Meta-SHACL errors must not emit pySHACL's full report or collide with user shape IDs.
- NoREC must not reuse one materialized predicate for both arms or accept equal malformed counts.
- Replay artifacts and saved evidence must distinguish TLP from NoREC, preserving old TLP replay compatibility.

## Task 1: complete pair-response accounting

**Files:** `shape_q.py`, new `tests/test_pair_response_coverage.py`, affected existing pair tests and sampled documentation.

- [ ] Add zero/partial/missing-alone sample, mismatch precedence, canonical key and no-D95/no-false-compared-note regressions; run and observe failure.
- [ ] Extend `_compare` requested-key coverage across all query responses. Make sampled/chunked incomplete results `not_run`; preserve known mismatches as fail. Keep old unsampled behavior and complete-sample bounds.
- [ ] Run pair, snapshot and mutation tests; update affected fixtures that previously omitted requested responses.
- [ ] Commit the verified task.

## Task 2: generated-shape meta-SHACL preflight

**Files:** `shacl_check.py`, `stage_reason.py`, new `tests/test_shacl_meta.py`, applicable reason tests.

- [ ] Add real positive/negative meta-shapes and stage tests proving no source export after failure, sanitized errors, once-per-run invocation and independent OWL behavior; observe failures.
- [ ] Add lazy preflight returning only conformity/counts. Wire `shacl:meta:generated` and blocked per-shape checks into `reason`, including actionable failure text and non-crashing `next_action`.
- [ ] Verify full generated corpus plus reason/report tests with existing optional-dependency behavior.
- [ ] Commit the verified task.

## Task 3: bounded NoREC oracle

**Files:** `tlp.py`, `sxc.py`, new `tests/test_norec.py` and `tests/test_norec_pg.py`, CLI tests, example count spec and TLP documentation.

- [ ] Add strict count-only CLI/spec, malformed answer, oracle-bound replay and full-row stability tests; observe failure.
- [ ] Add default-preserving oracle selection; independent WHERE/CASE arms with bounded relation and diagnostics. Bind oracle in results/replay and keep TLP artifacts unchanged.
- [ ] Add real PostgreSQL NULL/empty/duplicate/timeout and deliberately broken CASE-transform tests; verify failure detection and safe replay.
- [ ] Document scope and run TLP/NoREC/CLI/documentation tests; commit.

## Task 4: integrate, verify and hand off

- [ ] Update Unreleased/README/design/skill/command/reference docs and execute all required local checks, full-stack suite and leak scan.
- [ ] Obtain one independent whole-branch review; resolve substantive findings with regression tests and rerun relevant checks.
- [ ] Integrate into local main if clean and unchanged, record vault handoff with exact evidence, and leave owned changes committed. Publishing remains separate.

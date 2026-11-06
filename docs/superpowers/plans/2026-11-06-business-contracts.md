# Business contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Publish an optional offline business-definition and structured-plan preflight diagnostic.

**Architecture:** A pure contract evaluator consumes captured JSON documents and an existing Config. A dedicated guarded CLI route parses bounded verified bytes and publishes only redacted Result evidence. Existing operational stages remain unchanged.

**Tech Stack:** Python standard library, existing Config/Result/naming APIs, unittest, existing eight-job GitHub CI.

**Spec:** `docs/superpowers/specs/2026-11-06-business-contracts.md`

## Global Constraints

- No company DB/model/network use in the diagnostic; no generated/executed SQL.
- Keep report pipeline, statuses and default behavior unchanged; C3 deferred.
- Strict bounded explicit inputs; fixed errors, digest/count-only output.
- Release and GitHub Pages are authorized; Sites hosting is prohibited.

## Review Focus

- Alias collisions and inconsistent explicit metric IDs cannot silently select a metric.
- Wrong binding or denominator cannot pass because labels look alike.
- Input/output aliases or changed captured bytes cannot authorize cleanup/publication.
- Malformed CLI and parser content cannot leak paths, values or secrets.
- macOS standard directory aliases must work without accepting arbitrary links.

### Task 1: Pure evaluator

**Files:** create `skills/semantic-crosscheck/scripts/semantic_crosscheck/business_contracts.py`, `tests/test_business_contracts.py`.
**Interfaces:** consumes Config plus decoded dicts; produces `evaluate(cfg, contract_document, plan_document) -> Result` per spec.

- [ ] Write failing counterexample and normal-case tests.
- [ ] Run the focused unittest module and confirm a real missing-implementation failure.
- [ ] Implement strict bounded semantic validation and redacted checks.
- [ ] Run focused tests; review spec compliance and code quality; commit owned files.

### Task 2: Verified-byte Config parser

**Files:** modify `config.py`, create `tests/test_config_bytes.py`.
**Interfaces:** produces `config.loads(raw: bytes, path: str|Path) -> Config`, same validations without rereading TOML.

- [ ] Write and run failing same-semantics/BOM/malformed/no-read tests.
- [ ] Extract shared parse validation without changing `load` behavior.
- [ ] Run focused and existing config tests; review and commit owned files.

### Task 3: Guarded diagnostic CLI

**Files:** create `semantic_preflight.py`, modify `sxc.py`, create `tests/test_semantic_preflight_cli.py`.
**Interfaces:** consumes Task 1 evaluate and Task 2 loads; produces dedicated early `cli(argv, parser, summary)` route.

- [ ] Write failing alias/input-change/redaction/malformed-argv/no-external-read CLI tests.
- [ ] Implement bounded descriptor input capture, strict JSON budgets and guarded atomic Result publication.
- [ ] Run focused CLI and existing diagnostic tests; independently review IO and redaction.

### Task 4: Docs, examples and release

**Files:** new command/docs/examples, README/SKILL/design/THIRD_PARTY/CHANGELOG/release notes, four manifests and VERSION.

- [ ] Document exact scope, schema, statuses and authored-review limitations with one synthetic example.
- [ ] Run complete local suite/native plugin/leak checks and independent whole-branch review; fix actual findings.
- [ ] Integrate main, pass exact-main/tag CI, build and independently verify draft assets, publish immutable release and verify public downloads/signatures serially.
- [ ] Update and independently verify/publish Reference Atlas with actual pinned NVIDIA uses; record NS evidence and local-only commit/mirror.

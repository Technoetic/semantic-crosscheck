# Independent runtime checks implementation plan

**Goal:** Implement the user's accepted three additions after v0.7.0: actual synthetic Cube replay, SQL TLP consistency checks, and an independent SHACL corpus with Apache Jena.

**Architecture:** Add three optional standalone stages beside the existing operational pipeline. Each returns the existing Result contract and uses synthetic data. Existing build/reason/cube/verify/report verdicts retain their meaning.

**Tech stack:** Python 3.11+, PostgreSQL 17+, existing pinned Cube 1.8.3 adapter, existing rdflib/pySHACL, optional Apache Jena with Java. No new mandatory Python dependency.

**Authorization:** The user said "implement" in response to these three concrete proposed additions. Routine interface and isolation choices are settled here and executed without another approval round. Remote publication is a separate action.

## Global constraints

- Never put source rows, SQL answer values, DSNs, secrets, or local paths in Result or normal console output. Synthetic fixtures may be saved in explicitly labelled local replay artifacts.
- Missing engine, malformed report, timeout, unsupported input, partial comparison, or error must never pass. Preserve not_proven disclosures and exit conventions.
- Use dedicated synthetic test connections. Cube replay may create and remove only its own randomly named scratch schema after explicit CLI write opt-in; never fall back to a production config/DSN.
- Auxiliary synthetic checks must not promote operational V1-V5 or global report coverage.
- Preserve exact decimal, multiset, NULL, integrity and hash-bound replay behavior of v0.7.
- Work occurs in separate feature worktrees. Root integrates shared CLI/documentation after module contracts settle. Keep v0.7.0 version; record additions in Unreleased.

## Interfaces and ownership

- `cube-replay` -> `semantic_crosscheck.cube_replay.run(cfg, args, out)` -> `Result('cube_replay')`. No operational config; fixed synthetic mutation templates. CLI options: required `--mutation-operator`, `--seed` (7), `--dsn-env` (`SXC_CUBE_REPLAY_DSN`), explicit `--allow-scratch-schema`, `--statement-timeout-ms`, optional `--replay` if a safe hash-bound replay contract is provided. Module owns scratch lifecycle and real compiler/runtime calls. Unsupported named operator must remain explicit, never silently substitute unrelated SQL.
- `tlp` -> `semantic_crosscheck.tlp.run(cfg, args, out)` -> `Result('tlp')`. Required `--spec FILE`, `--dsn-env` (`SXC_TLP_DSN`), `--seed` (7), `--max-examples` (64), `--statement-timeout-ms`, optional `--replay`. Strict versioned spec for nonrecursive relation/predicate/count-or-sum. Trusted SQL has the same explicit boundary as existing trusted counterexample SQL. Reuse bounded synthetic fixtures and read-only execution; materialize a relation per comparison if needed for stability. Undefined transformation preconditions are not evidence of correctness.
- `shacl-corpus` -> `semantic_crosscheck.shacl_corpus.run(cfg, args, out)` -> `Result('shacl_corpus')`. Fixed independently specified nonrecursive corpus, optional external `--jena-home` (`SXC_JENA_HOME`) and `--timeout-ms`. Compare pySHACL and actual Jena against independently stated normalized expected reports. No production DB access or recursive SHACL expansion.
- Root owns `sxc.py`, shared README/SKILL/references/catalogue/CHANGELOG and packaging tests. Feature agents own only new modules, directly supporting helpers, their new tests and a unique feature doc.

## Task 1: Actual Cube replay

- [x] Write and run failing tests for real model/fixture binding, original/gold agreement and mutant/gold disagreement, malformed/changed replay, forbidden scratch write, dependency failure and owned cleanup.
- [x] Implement replay using the existing generator, compiler, mutation machinery and Cube runtime. Freeze the generated data inside the owned scratch schema and compare actual runtime answers.
- [x] Test unit behavior and actual PostgreSQL/Cube execution with known distinguishing operators. Cover every advertised supported operator and label unsupported scope.
- [x] Commit module/tests/doc and hand off exact commands and results.

## Task 2: TLP consistency checks

- [x] Write and run failing tests for NULL predicate outcomes, duplicate rows, COUNT, SUM with empty/all-NULL partitions, exact decimals and excluded NULL partition detection.
- [x] Implement bounded synthetic TLP evaluation, spec binding/replay, timeout/error sanitation and inconclusive outcome handling.
- [x] Execute actual PostgreSQL tests proving a valid partition passes and an intentionally wrong partition is detected; do not equate this with business-query correctness.
- [x] Commit module/tests/doc and hand off exact commands and results.

## Task 3: Independent SHACL corpus

- [x] Write and run failing tests for canonical reports, constraint-level mismatches, blank-node independence, missing/malformed Jena output, timeout and missing dependency.
- [x] Implement independently expected class, bounds, NULL/missing triple, decimal boundary and combined/value-group cases, plus pySHACL/Jena adapters.
- [x] Run every corpus case through actual pySHACL and Apache Jena; record engine version/hash evidence without private paths.
- [x] Commit module/tests/doc and hand off exact commands and results.

## Task 4: Integration and independent review

- [x] Add standalone CLI parsers, configuration rejection, stale-result removal and CLI tests. Integrate feature commits without overwriting shared work.
- [x] Synchronize README, SKILL, references, command help and Unreleased changelog; run docs/packaging/leak checks.
- [x] Run complete suite with dedicated PostgreSQL, Java/HermiT and Cube available; record exclusions separately, exercise Jena actual corpus and examples.
- [x] Independent reviewer reruns meaningful commands and checks fail-closed/cleanup/privacy/semantic edge cases. Fix findings and recheck affected scope.
- [x] Commit complete implementation and integrate locally into main if safe. Update scoped vault research/handoff/index/log/decision records and commit only owned changes. No GitHub push/release.

## Review focus

- Destructive cleanup targets only the exact scratch schema created by this invocation, including exceptional paths.
- A successful synthetic query pair does not prove the supplied operator matches a compiled Cube mutation; require actual model mutation and actual Cube comparison.
- SUM empty/all-NULL behavior and multiset duplicates retain PostgreSQL semantics.
- SHACL report parsing must distinguish a valid empty report from missing or invalid output; bnode/message differences alone are not violations.
- Auxiliary replay artifacts and normal output must never expose real data, secrets or silently reuse stale success.

## Verification record

Actual Windows integration run: 979 tests passed, zero skips, failures or errors (1320.570 seconds), with PostgreSQL 17.5, Cube 1.8.3, Java 21 and Jena 6.2.0. An independent reader reran 59 feature/CLI tests. Five actual CLI runs passed. A long Windows dependency-cache path was replaced with a verified identical short-path copy; the deadline test dropped an unused file write while preserving its process/pipe/time assertions. That fixture passed 80 repeats plus 20 independent repeats. No production cleanup code was relaxed. Host integration records are maintained separately; no remote publication is part of this implementation.

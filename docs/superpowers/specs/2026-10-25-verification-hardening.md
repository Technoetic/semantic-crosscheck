# Verification hardening after v0.9.0

User approval: implement the recommended sampled-response guard, generated SHACL preflight, and bounded NoREC check (2026-10-25). Implementation is local; publishing is separate.

## Design

1. Every requested non-NULL pair-test key must be answered by the together query and each alone query. Confirmed mismatches/duplicates still fail; otherwise incomplete coverage is `not_run` with `keys_not_answered`. Apply this to sampled V3, V4 replay and the existing chunked comparison. Do not report D95 or describe requested keys as compared when responses are incomplete. Count coverage without retaining row values.
2. Run a generated-shapes meta-SHACL preflight once in `reason`, after optional components are available and before source export. Use check ID `shacl:meta:generated` (configured shape IDs cannot contain colons). Confirmed nonconformance fails; parse/engine/malformed-report errors are sanitized errors. Block affected SHACL comparisons explicitly, while independent OWL may proceed. Use the pinned `pyshacl.entrypoints.meta_validate` helper; do not enable the pySHACL wrapper that logs complete invalid-shape reports. This is the standard's available syntax subset, not a proof of business semantics. External W3C corpus handling stays independent.
3. Add `tlp --oracle {tlp,norec}`, default `tlp`. NoREC supports exactly `count(*)` and rejects other aggregates before connecting. Materialize only the bounded relation; evaluate the predicate independently in the WHERE-count arm and CASE/SUM arm. Empty relations and all-false/NULL predicates count zero. Keep bounded full-row/predicate diagnostics and a second evaluation for observable stability. Require complete, exact, nonnegative integer counts within relation bounds. Reuse synthetic generation, trusted-SQL boundaries, read-only repeatable-read, timeouts, response limits, sanitized errors and changed-input checks. NoREC artifacts use `sxc-synthetic-norec` plus `oracle: norec`; existing TLP artifacts remain compatible. Results identify the oracle and never promote operational V1–V5 coverage.

## Constraints

- Python 3.11+, PostgreSQL 17+, current pinned pySHACL 0.40.1 and Cube 1.8.3; no new runtime dependency.
- Only synthetic, disposable PostgreSQL data for tests; no production or NAS access.
- Results/console contain counts, IDs and fixed codes, never source row values, SQL, connection strings or local paths.
- Generated fixtures may occur only in explicit replay artifacts, as before.
- Keep version 0.9.0 and document new behavior under Unreleased; no remote push/release.
- Keep source/commands/design/skill/reference documentation consistent and verify existing reports become stale through the existing engine digest.

## Alternatives

The selected approach extends existing comparison and TLP mechanisms. A separate NoREC stage would duplicate standalone dispatch and replay machinery. A third SHACL engine and DQP are later research candidates rather than this implementation's scope.

## Verification

Regression tests must fail before implementation and then pass. Cover zero/partial/missing-alone answers, canonical key handling, known mismatch precedence, truthful sampled notes, generated-shape positive/negative and sanitized error paths, NoREC malformed documents and cross-oracle replay, real PostgreSQL NULL/duplicate/empty cases and deliberately broken transformations. Run the complete stack suite with the isolated runtime, then independent read-only code review and focused verification. Record exact counts and any skipped prerequisites.

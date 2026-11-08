# Contract evidence, PostgreSQL structure diagnostics and independent bag fixtures

## Intent and authorization

The user requested implementation and public GitHub publication of the three
recommendations reviewed on 2026-11-08. This continues that approved scope;
routine implementation choices below are the implementer's decisions. No company
database, active plugin installation or unrelated application is changed.

## Approach

Keep the existing Python harness and make new authority explicit. A persistent
optional `[semantic_preflight]` configuration section selects contract and plan
sidecars. Only an opted-in report requires their current successful preflight.
Absent this section, the existing report behavior is retained. A separately
selected `sql-structure` diagnostic uses the PostgreSQL parser through an optional
pinned Python binding. Synthetic bag fixtures remain independent of the default
pipeline and provide literal expected results plus PostgreSQL integration tests.

Alternatives considered: adding a full external data-contract framework as a
default dependency introduces unnecessary runtime surface; treating every
standalone preflight as operational evidence silently changes existing authority;
regex SQL matching does not meet the proposed actual-parser boundary. None is
used. Data Contract CLI/ODCS inform the evidence seam; no claim of full ODCS
import/export compatibility is made. libpg_query is used through pglast only in
the explicitly selected diagnostic. The formal provenance sources inform tiny
independently authored fixtures rather than installing Lean or ProvSQL.

## Behavioral boundaries

- Configuration selects both contract and plan, with strict fields and paths.
  Missing, invalid, changed or mismatched inputs cannot satisfy the report gate.
  Recompute saved result status from checks, preserve worse status, and bind the
  current config, contract, plan and evaluator. A caller cannot substitute a
  successful run for different sidecars. Failed and unexecuted checks cannot pass.
- Preserve the bounded, redacted and path/change-guarded preflight capture.
  Diagnostic validation reads no Gold SQL, DB, model or network.
- `sql-structure --sql FILE --expect FILE` consumes bounded local SQL and a
  strict independently authored v1 structural expectation. Support only a
  documented single-relation SELECT subset with direct grouping, projection,
  aggregates and conjunction comparisons. Unsupported constructs, unavailable or
  wrong-version parsers remain explicit non-passes. Parsing is not name/type
  resolution, parameter-value binding, SQL equivalence or business truth.
- Independently literal bag fixtures cover multiplicity, DISTINCT, EXCEPT and
  EXCEPT ALL, with carefully separate NULL equality/set semantics. Include the
  pinned Lean Dedup/Diff counterexample without treating Nat annotations as SQL
  row multiplicities or asserting a PostgreSQL bug.
- No default parser dependency, production extension installation, remote custom
  quality checks or authenticated-approval claim. Local hashes do not establish
  authorship. Matching declarations can share a mistake. No shared Gold/Cube
  MVCC snapshot is added.

## Success criteria

Meaningful negative tests fail before the implementation, then pass. Full local
tests and exact-source CI pass with the project's strict provisioned integration
gate. Fresh independent reviews inspect the final changes. Release metadata,
help, examples, docs, dependency/license notices and pinned source references
agree. Publish v0.21.0 with truthful verification evidence and checksums, then
update the existing Reference Atlas with pinned public uses and independently
verify that catalog update. Publication does not update the user's installed
plugin or MES deployment.

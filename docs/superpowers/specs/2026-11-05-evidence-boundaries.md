# Evidence boundaries and bounded diagnostic inputs

The user requested implementation and public release of the recommended next
bundle after the v0.18.1 research: saved-result and live SHACL report validation,
a small SQL-boundary generation experiment, and connecting existing reduction
to TLP/NoREC failures. This is implementation/publication authorization; it does
not authorize modifying a company database or updating active installations.

## Scope and compatibility

- Keep PostgreSQL17, Cube1.8.3, HermiT/pySHACL and Jena pins and required CI gates.
- No new runtime dependency, PostgreSQL extension, default pipeline stage,
  automatic skill/model call, operational database verification or business-truth claim.
- Preserve existing bag semantics. Ordered-query contracts and ProvSQL remain
  later investigations, not part of this release.
- Preserve legacy valid saved results and default TLP replay behavior. Malformed
  inputs must not establish a pass, witness, or successful reduction.
- Only explicit synthetic fixtures may contain generated values. Results and
  diagnostics remain count/hash/identifier oriented and redact untrusted text.
- Final source is frozen before full regression, main/tag CI, native discovery,
  source ZIP checks, independent draft review and immutable publication.

## A. Saved result contract

Validate persisted result envelopes and every check before aggregation instead
of silently dropping malformed list entries. Check mappings, nonempty IDs,
duplicate IDs, known status strings, supported version, and field types. Retain
valid legacy files with documented version handling. Report invalid evidence as
a sanitized result-file error, not a passing subset. Metadata fingerprints and
existing freshness/provenance checks retain their authority and limitations.
Structural checks do not authenticate a maliciously rewritten artifact.

## B. Live SHACL report contract

Check the engine's returned result shape and RDF report envelope before focus
projection: report root, typed conformance value, linked results, necessary
cardinalities and usable source/focus structure. Preserve existing group
isolation and property-pair behavior. Treat warning/info options consistently
with the pinned pySHACL engine; do not equate its allow-warnings API boolean
with an absence of report results. Do not mistake legitimate results belonging
to another planned group for malformed reports. Errors remain sanitized.

## C. Offline boundary-schema experiment

Add an explicit standalone `boundary-schema --spec FILE` command. Input v1 is a
small declaration containing `version`, `table`, `column`, `type`, `rows`, and
`predicate_sql`. It describes exactly one scalar column, its supported type,
finite row bounds, and a supported atomic comparison of that column and a
literal. Supported types are integer, numeric(p,s), and date. Derive the
literal and representable immediate neighbors (integer1, numeric quantum, or
one day), plus NULL, using exact representations. Reject functions, compound
predicates, subqueries, comments, unrelated identifiers, unknown fields and
unsupported or unrepresentable values explicitly. A small fully consuming
grammar is a supported-subset parser, never a PostgreSQL parser or SQL sandbox.

Output `boundary_schema/witness-schema.json` must load under the existing
version1 `--witness-schema` contract. A normal Result records source/generator/
output hashes and counts without literal values. The tool does not connect to
PostgreSQL, invoke Z3 or execute SQL. Actual two-query witness search remains
PostgreSQL-based and opt-in. Derived finite domains are heuristics, not complete
SQL semantics or proof that the declared predicate matches a caller's query.

## D. Opt-in TLP/NoREC reduction

Add explicit `tlp --shrink` and a bounded `--max-shrink-steps`; default execution
and old valid replay files retain existing contracts. Reuse structured reduction
over the known synthetic schema. Original and reduced fixtures, operation trace,
input bindings and reduction scope are retained in an explicit replay artifact.
The selected mismatch signature must survive accepted changes; disclose exactly
what that signature preserves, without calling it proof of the same optimizer
bug. Errors or instability cannot be accepted as a smaller witness. Recheck the
final fixture with actual evaluator semantics and revalidate source/replay
bindings before writing evidence. Bound exhaustion does not imply minimality.
Replay validates trace, schema, fixture identity, oracle and spec/generator
identity; edited or stale replay must not establish a pass.

## Verification and release

Each component starts with failing regression tests and focused green checks.
Use actual pinned pySHACL for live conformance and Warning/Info cases; mocks are
only fault injection of impossible/malformed engine responses. Add genuine
PostgreSQL tests for new query boundary artifacts and TLP/NoREC reduction/replay,
including errors, signature switches and nondeterminism. CI integration must
continue to reject all skips, failures, errors and an empty suite. Record local
environment skips separately. Independently review final code and artifacts;
publish source/tag/release only after required evidence is concrete.

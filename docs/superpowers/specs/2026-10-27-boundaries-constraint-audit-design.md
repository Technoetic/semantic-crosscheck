# Boundary fixtures and constraint sensitivity audit

The approved fifth research recommendation adds two safeguards to v0.11.0. The user requested implementation; deployment remains separate.

## Goals

1. Independently authored fixture expectations expose sparse facts, filtered-out facts, and duplicate dimension labels under different composite keys.
2. An opt-in two-query bounded-search diagnostic compares a declared constraint domain with a deliberately relaxed domain. A relaxed-only PostgreSQL-confirmed witness is conditional evidence outside the declared domain, not proof that the constraint is false or that production contains a defect.

## Constraints

- Existing default behavior and report/replay contracts remain unchanged when the audit is off.
- PostgreSQL 17+, Cube 1.8.3, existing dependencies only; synthetic data only.
- Fixtures use the existing gold.expect mechanism and authored rows, exact numeric values and composite keys. Missing aggregates explicitly distinguish NULL and zero. NULL measure-grain keys retain the existing exclusion policy.
- No arbitrary entity joins, expected-error-as-PASS DSL, copied float comparator, SQL equivalence proof, LLM constraint mining or default network calls.
- The audit keeps both SQL hashes, finite domains, row bounds and data types fixed; selected declaration groups are relaxed explicitly. Primary-key implications and foreign-key dependencies must remain well defined.
- Provenance is user-declared evidence classification (catalog, human, observed or inferred), never automatically certified truth.
- Summaries contain counts, safe identifiers and hashes; SQL, cell values, credentials and local paths stay out.
- Audit artifacts must be replayable with exact domain/selection/provenance bindings and PostgreSQL confirmation; incomplete search is not_run or bounded no-witness, never equivalence.

## Approaches

Reusing gold.expect and the existing bounded generator minimizes new semantics and preserves the pinned engine contracts. Importing SLayer's full runner would add incompatible engines, comparison and success semantics. Inferring constraints or adding a separate SQL verifier would expand scope without improving the declared-domain audit's reliability.

## Acceptance

The three fixture cases pass on the pinned real engines and fail for independently injected wrong expectations/models. The optional audit finds the COUNT(nullable) versus COUNT(key) witness only after relaxing an authored NOT NULL constraint, rejects malformed/stale/mismatched inputs before database work, respects budgets, and replays only in the correct domain. Existing tests stay green with the flag absent.

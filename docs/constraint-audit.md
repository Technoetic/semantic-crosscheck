# Opt-in constraint sensitivity audit

`counterexample --constraint-audit FILE` runs the same two trusted SQL queries
over two finite integrity domains. The declared arm enforces the exact original
constraint profile. The relaxed arm removes explicitly selected source
declarations and accepts diagnostic witnesses only **outside** the full original
profile. Actual PostgreSQL disagreement, using the existing typed bag comparison,
is the witness predicate. Solver satisfiability alone is never a witness.

Without the flag, ordinary search, constraint JSON and replay formats are unchanged.
The audit requires `--witness-schema`, `--constraints`, `--reference-sql` and
`--candidate-sql`. Explicit `--scenario`, mutation/gold/metamorphic modes, and
`--shrink-strategy structured` are rejected before connecting. Version 1 supports
`rows` shrinking only; it preserves all finite values, data types, schemas and
original minimum/maximum row bounds throughout reduction and replay.

## Synthetic COUNT example

Use the existing optional solver dependency (`requirements/witness.txt`) and a
dedicated synthetic PostgreSQL database named by `SXC_PG_DSN`:

```sh
python skills/semantic-crosscheck/scripts/sxc.py counterexample \
  --reference-sql examples/constraint-audit/reference.sql \
  --candidate-sql examples/constraint-audit/candidate.sql \
  --witness-schema examples/constraint-audit/schema.json \
  --constraints examples/constraint-audit/constraints.json \
  --constraint-audit examples/constraint-audit/manifest.json \
  --seed 7 --max-examples 24 --max-shrink-steps 128 \
  --solver-timeout-ms 3000 --statement-timeout-ms 1000 --out .sxc
```

The primary key makes `id` non-null. With the explicit `qty` NOT NULL declaration,
`COUNT(id)` and `COUNT(qty)` agree. Removing only that declaration permits a row
whose `qty` is NULL; PostgreSQL then confirms an outside-domain disagreement.
The result is diagnostic `not_run`, exit **1**, with no correctness claim.

Add `--replay .sxc/counterexample/audit/relaxed-fixture.json` to the identical
command to recheck that arm. Replay requires the same options and exact input
bytes. It replays one arm and labels the other `not_replayed`; it does not rerun
the two-arm comparison. Either arm's artifact can be replayed repeatedly.

## Strict version 1 manifest

```json
{
  "version": 1,
  "constraints_sha256": "exact lowercase SHA-256 of original constraint file bytes",
  "provenance": [
    {"rule": "/tables/items/primary_key", "source": "catalog"},
    {"rule": "/tables/items/not_null/0", "source": "observed"}
  ],
  "relax": ["/tables/items/not_null/0"]
}
```

The file is bounded to 256 KiB. Duplicate JSON keys, nonfinite numbers, boolean
versions, unknown fields, missing/duplicate/unknown selectors and noncanonical
indices are rejected. There may be at most 128 source declarations. Provenance
must cover every source declaration exactly once; its source is exactly
`catalog`, `human`, `observed` or `inferred`. These are **caller claims**. No
catalog is automatically inspected and no category independently verifies truth
or automatically selects a relaxation. There is no free-text evidence field.

Selectors address the original profile, before any declaration is removed:

| Source declaration | Exact selector |
| --- | --- |
| Entire primary key, including implied UNIQUE and NOT NULL | `/tables/<table>/primary_key` |
| One explicit UNIQUE entry | `/tables/<table>/unique/<index>` |
| One explicit NOT NULL column | `/tables/<table>/not_null/<index>` |
| One foreign key | `/foreign_keys/<index>` |
| One CHECK | `/checks/<index>` |

`<table>` uses RFC6901 escaping (`~` → `~0`, `/` → `~1`); only real declarations
in the loaded profile resolve. Indices are zero-based canonical decimal integers
with no leading zero, sign or whitespace. Parent objects, arbitrary JSON
pointers and aliases never select rules. The finite schema's existing identifier
restrictions still apply.

`relax` is nonempty and unique. Effective rules are reconstructed by removing
source declarations from a copy of the original JSON, then loading it through
the unchanged version 1 constraint loader. Removing a PK removes its implied
contributions while separately declared NOT NULL rules remain. Removing explicit
NOT NULL cannot bypass a retained PK. Existing version 1 restrictions, including
rejection of identical PK/UNIQUE keys, still apply. A retained FK requires its
exact retained target PK/UNIQUE; select the dependent FK explicitly or the
projection is rejected. There is no automatic cascade.

A selection with **zero normalized rule contribution delta** is rejected. This
compares reconstructed NOT NULL/UNIQUE/FK/CHECK rule sets, including deduplicated
PK implications; it is not a general logical implication checker. Selected
source count and effective normalized deltas are reported separately. The audit
does not prove that every removed contribution changes the finite reachable
domain or is causally necessary for the SQL disagreement.

## Bounds and statuses

Both arms receive the same Spec object, SQL bytes, configured seed, maximum
examples, solver budget, statement timeout and shrink budget. The existing
finite solver orders models deterministically; the recorded seed does not change
that solver's fixed ordering. Every generated relaxed candidate consumes the
example budget, including candidates skipped because they still satisfy the
declared profile. Both effective profiles are non-None, even for an empty
projection, so declared text keeps identical C collation and exact equality.

`max_examples`, `solver_timeout_ms` and `max_shrink_steps` apply **per arm**.
`statement_timeout_ms` applies to each evaluated SQL statement. Total work can
approach twice one search/reduction, plus final witness confirmations. Both arms
run even when the first finds a disagreement. Their enumeration order and bounded
prefixes can differ. Their found/not-found outcomes do not establish causal
necessity or exhaustive disappearance. Solver timeouts, unavailable/unknown
solvers or invalid generation leave that arm incomplete; interrupted generation
prefixes are not published as audit witnesses.

| Evidence | Check status | Classification/reason | CLI exit |
| --- | --- | --- | --- |
| Declared-domain PostgreSQL disagreement | `fail` | `declared_domain_disagreement` | 1 |
| Relaxed, outside-declared-domain disagreement | `not_run` | `outside_declared_domain`, `found=true` | 1 |
| No witness in bounded complete generation | `not_run` | `bounded_no_witness` and termination reason | 1 |
| Solver interrupted/unavailable | `not_run` | `incomplete` and termination reason | 1 |
| Query/reduction/confirmation or changed-input error | `error` | Fixed sanitized reason | 1 |
| Invalid or mismatched configuration/replay | `error` | Configuration rejection before connecting | 2 |

Error dominates fail; declared fail dominates diagnostic `not_run`. No audit
outcome is `pass`. No-witness, including finite `space_exhausted`/`unsatisfiable`,
proves neither equivalence nor correctness. Query exceptions are errors and are
never accepted as witnesses. Outside evidence does not automatically mean the
constraint is false, production contains such rows, or production is defective.

## Artifacts and replay contract

The single summary is `counterexample/result.json`. It and CLI output contain
fixed reason/classification enums, counts, opaque `r0001` rule IDs (from sorted
canonical selectors), limits and SHA-256 digests; no SQL, row values, names,
selectors, secrets or local paths. Provenance counts contain only the four fixed
categories. Synthetic local replay artifacts may contain synthetic rows.

`counterexample/audit/relaxed-constraints.json` is a canonical projected v1 profile
that the existing solver can revalidate against its real file source. Its exact
hash is separate from the original profile. Witnesses are stored, if confirmed,
as `declared-fixture.json` and `relaxed-fixture.json` in that directory.

Audit replay uses format `sxc-synthetic-constraint-audit`, `schema_version: 1`,
separate from ordinary v1/v2 counterexamples. It binds:

- Both exact original SQL hashes, exact finite spec/hash/schema and original
  constraint profile/hash.
- Exact manifest hash, selected canonical declarations and opaque IDs, source
  address catalog hash, projected profile/hash, arm and domain classification.
- Seed, example/solver/statement/shrink bounds, row shrink strategy and scope.
- Original solver fixture and final row-deletion subset, both validated against
  finite values, original row bounds and the arm profile.

Relaxed original and final fixtures must additionally violate the full original
profile. Saved status, found/minimality flags and shrink statistics do not
authorize evidence; reduction statistics are recomputed. No structured trace is
accepted. Ordinary replay rejects audit files, and audit replay rejects ordinary
or mismatched files before database connection. A fingerprint detects changed
bytes; it is not an authenticity signature or a proof of original solver origin.

Each witness is reconfirmed in PostgreSQL after reduction and again after both
searches, followed by exact original SQL/spec/profile/manifest and projected
profile revalidation, before atomic publication and fixture hash release. At the
start of a run, stale ordinary/audit witnesses and summaries are removed. Replay
bytes are preserved first when input/output paths coincide. An input aliasing an
output artifact is rejected to avoid overwriting it.

Each evaluated fixture compares the queries in a read-only REPEATABLE READ
transaction. Different fixtures/arms do not share a whole-run snapshot. Trusted
SQL can explicitly read external relations; synthetic CTEs are not a SQL sandbox.
The audit adds no catalog/network access, LLM call, new dependency, SQL condition
encoding or equivalence engine and does not change the pipeline report verdict.

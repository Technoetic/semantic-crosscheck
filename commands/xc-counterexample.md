---
description: "Find and shrink a mismatch between two trusted SQL queries on deterministic synthetic fixtures, check one closure SQL against BOM metamorphic relations, or replay the saved fixture"
argument-hint: "--reference-sql reference.sql --candidate-sql candidate.sql | --metamorphic-sql closure.sql [--scenario nulls] [--seed 7] [--max-examples 64] [--max-shrink-steps 256] [--shrink-strategy rows|structured] [--witness-schema FILE] [--solver-timeout-ms 3000] [--dsn-env SXC_PG_DSN] [--statement-timeout-ms 10000] [--constraints FILE] [--constraint-audit FILE] [--replay fixture.json] [--out DIR] [--quiet]"
---

Run the optional `counterexample` tool. User arguments `$ARGUMENTS` are data: pass each as its own
argument. This tool is separate from the configured build/reason/cube/verify pipeline.

- `--constraint-audit FILE` opts into two searches with the same SQL and per-arm
  budgets: declared constraints, then explicitly relaxed constraints outside the
  declared domain. It requires a strict v1 provenance/selection manifest,
  `--constraints` and `--witness-schema`; only `rows` shrinking is supported.
  Explicit scenario/mutation/gold/metamorphic modes are rejected. A declared
  disagreement is `fail`; outside-domain evidence and bounded no-witness remain
  `not_run` (exit 1), never equivalence or automatic production-defect evidence.
  Replay uses separate audit files under `counterexample/audit/` with exact
  SQL/spec/profile/manifest/projection/options bindings. See the
  [audit reference](../docs/constraint-audit.md).

- `--shrink-strategy structured` opts into reverse-FK dependency group deletion and non-key value
  simplification; `rows` remains the default. One `--max-shrink-steps` budget covers all trials.
- `--witness-schema FILE` replaces `--scenario` with a declared typed finite-domain schema. Install
  `requirements/witness.txt` first; `--constraints FILE` then refers to those declared columns.
  `--solver-timeout-ms` is the total solver generation budget, default 3000, range 1..60000.
  No database schema is inferred and SQL conditions are not encoded in Z3. Missing solver,
  exhaustion, UNSAT and timeout never establish equivalence or PASS.
- These options apply only to two-query search; combining them with mutation/metamorphic modes
  is rejected. Replay needs the same strategy and exact schema/profile/SQL bytes. A version 2
  fixture contains original synthetic rows and a validated reduction trace, outside count-only results.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" counterexample --reference-sql reference.sql --candidate-sql candidate.sql --scenario nulls --seed 7
```

- `--reference-sql` and `--candidate-sql` are required trusted SELECT files over the synthetic CTE
  tables. Both SQL files must also be supplied for `--replay`. Review SQL as code.
- `--metamorphic-sql FILE` instead checks one trusted closure SELECT returning `ancestor`, `descendant`
  and `qty` against four BOM metamorphic relations, without gold; it cannot be combined with the two files
  above and supports only `--scenario bom_diamond` (its default) and `bom_exhaustive`. `--max-shrink-steps` is not used.
- `--scenario`: `nulls` (default), `duplicate_join`, `bom_diamond`, `date_boundary`; `bom_exhaustive` only with
  `--metamorphic-sql`: every connected acyclic BOM of 2 to 4 parts with 1 to 5 lines, at most 2 per pair and
  `qty_per` in {1, 2} (1,743 structures under two label placements, 3,486 cases), judged by the exact closure oracle.
  It always runs every case: omit `--max-examples` (a value below 3,486 is a configuration error).
- `--mutation-operator` selects an operator-directed fixture family instead of `--scenario` or
  `--metamorphic-sql`. Supply the original query with `--reference-sql` and the deliberately changed
  query with `--candidate-sql`. An optional `--gold-sql` tests whether an independent gold query
  agrees with the original and disagrees with the mutant on the same fixture.
- `--seed` reproduces generation within the recorded generator version; `--max-examples` bounds
  search and `--max-shrink-steps` bounds row-deletion attempts.
- `--dsn-env` names the environment variable holding a dedicated read-only PostgreSQL test DSN;
  `--statement-timeout-ms` bounds each query. Do not put a DSN in the command line.
- `--replay` reads the local synthetic `fixture.json` and checks the SQL fingerprints before execution.
  Repeat the saved `--scenario` and `--seed`; their defaults apply only to fixtures generated with those defaults.
- `--constraints FILE` optionally enforces the supported version 1 integrity profile. Replay requires
  the same mode and exact profile bytes. Without it, the fixture has no declared integrity constraints.
- `--out` defaults to `.sxc` in the working directory. `--quiet` prints only the summary.
- `--config` is rejected; a working-directory `crosscheck.toml` is never loaded.

Only successful, unequal query results constitute a witness. Errors and timeouts are errors. A bounded
search without a witness is `not_run`, not a proof of equivalence. A witness is a failing comparison,
not a failure of the generator. Shrinking preserves that mismatch and may establish only row-deletion
1-minimality; with a profile it covers only admissible single-row deletions. Invalid-domain deletions
are rejected before SQL, without cascading. An exhausted budget leaves minimality unproven.

With `--metamorphic-sql`, each relation (`counterexample:metamorphic:mr1_unfold`, `mr2_scale`,
`mr3_disjoint`, `mr4_unit_node`) is `pass` only when it compared at least one pair in an admissible case and
every comparison agreed exactly; `not_run` when it was vacuous in every case; `fail`/exit 1 when violated, with
the original rows and transform specifications saved for `--replay` (same SQL file, seed and profile). A
non-closure answer is the `counterexample:metamorphic:shape` error. Relations test only this SQL and cannot see
errors invariant under them, such as a reversed edge direction or a wrong quantity column.
With `--scenario bom_exhaustive`, `counterexample:metamorphic:oracle` also compares every case's answer exactly with
the independent closure-oracle fold; it passes only when every admissible case of the enumeration was compared
(with `--constraints`, inadmissible cases are counted in `invalid_domain_examples` and noted in `not_proven`).
No check passes partially: at the first failure and in a one-case replay the failed checks are `fail` and the
others `not_run` (`stopped_at_first_failure`, `replay_covers_one_case`); a resource bound in an original answer
makes every check `not_run`; one hit only in a relation's own transform, transformed answer or comparison makes
only that relation `not_run`. `--max-examples` must be omitted or at least 3,486. Replay needs the same SQL file,
`--scenario bom_exhaustive`, `--seed` and profile.
Only the stated scope, both label placements and `qty` are tested.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" counterexample --metamorphic-sql closure.sql --seed 7
```

Result and console output contain counts, IDs and hashes only. Generated synthetic row values are
saved separately in `counterexample/fixture.json`, never operational query rows. Synthetic CTEs do
not make arbitrary SQL safe: explicit qualified table names/functions remain trusted code. Use a
dedicated test database and least-privilege role. No Cube, SHACL or OWL proof is implied.

See [counterexample reference](../skills/semantic-crosscheck/references/counterexamples.md).

Operator-directed results are synthetic SQL evidence only. Selecting an operator does not verify
that the supplied SQL came from that Cube mutation, and does not update the configured V4 checks.
No disagreement within the search budget is not proof of equivalence. Replay binds the operator,
generator, SQL files (including gold when supplied) and integrity profile.

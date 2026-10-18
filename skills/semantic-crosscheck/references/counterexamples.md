# Synthetic SQL counterexamples

The optional `counterexample` command runs two trusted SELECT files on the same generated fixture.
It uses PostgreSQL read-only transactions and bound values in CTE tables, without creating or loading
source tables. Use a dedicated test database; arbitrary SQL can explicitly reference other objects.

The built-in scenarios target NULL aggregation, duplicate join keys, two paths through a BOM diamond,
and date-window boundaries. The fixture schema and runnable reference/candidate pairs are in
`examples/counterexamples/`. Both query outputs must have matching column names and equal exact
row multisets to agree; row ordering is irrelevant and duplicate multiplicity matters.

```text
python skills/semantic-crosscheck/scripts/sxc.py counterexample --reference-sql examples/counterexamples/nulls-reference.sql --candidate-sql examples/counterexamples/nulls-candidate.sql --scenario nulls --seed 7 --out .sxc
```

Supply the same SQL files, repeat the saved `--scenario` and `--seed`, and add
`--replay .sxc/counterexample/fixture.json` to rerun a saved case.
Replay checks format, the supported synthetic schema and both SQL hashes. Generated values are local
fixture data, not evidence extracted from a production database. Keep a fixture with its SQL files to
reproduce the comparison; hashes do not preserve the SQL content themselves.

Search and shrinking are bounded. The reducer tries deleting individual rows in a deterministic order
and accepts a deletion only after a successful comparison still shows a difference. It restarts after
an accepted deletion. A complete last pass can establish **1-minimality under row deletion**, not the
globally smallest fixture or minimality under value changes. A budget limit leaves minimality unproven.
Query errors never establish a counterexample and are not accepted as shrink witnesses.
If a deletion cannot execute, the valid witness is retained and minimality stays unproven.

## Optional integrity profile

Add `--constraints examples/counterexamples/constraints.json` to enforce a declared domain. The
profile is not inferred from a production schema. It accepts only the four fixed synthetic tables
and their existing columns, format `version: 1`, and the following rules:

- `tables.<table>.primary_key`: one nonempty column list; implies NOT NULL and uniqueness.
- `tables.<table>.unique`: a list of column lists; PostgreSQL default NULLS DISTINCT semantics.
- `tables.<table>.not_null`: a column list.
- `foreign_keys`: source `table`/`columns` and `references.table`/`references.columns`. Column types
  must match exactly and the referenced list must be a declared primary/unique key. MATCH SIMPLE
  ignores a child key containing any NULL.
- `checks`: `table`, `column`, `op`, `value`. Operators are `eq`, `ne`, `lt`, `le`, `gt`, `ge`, `in`;
  `in` takes a nonempty list. A NULL column satisfies CHECK unless separately declared NOT NULL.
  Text supports only `eq`/`ne`/`in`; ordered text predicates are rejected.

Numeric fixture values are rounded to the declared PostgreSQL precision/scale before validation,
including rounding overflow. Numeric CHECK constants retain their exact precision; decimal strings
are convenient in JSON. Integers are signed 32-bit and dates are ISO `YYYY-MM-DD`, years 0001–9999.
Constrained fixture text expressions use explicit PostgreSQL `C` collation, so this domain does not
model application-specific or nondeterministic collation behavior. Explicit collation overrides in
trusted SQL are outside the profile contract.

Unknown fields, duplicate JSON keys, nonfinite numbers, invalid types and unsupported rules fail
before DB access. Profiles are limited to 256 KiB and 128 compiled rules. Arbitrary SQL CHECKs,
column-to-column expressions, functions, MATCH FULL, deferred constraints and NULLS NOT DISTINCT
are unsupported. The profile is not a PostgreSQL DDL interpreter.

Every generated case and shrink trial must satisfy the profile. Invalid cases are counted separately
and never run as SQL; an all-invalid search is `not_run`, not PASS. A parent-row deletion that would
orphan a child is rejected without a cascade. A completed shrink pass proves at most **1-minimality
among admissible single-row deletions**. Simultaneous deletions might still yield a smaller witness.

The fixture records `{mode, version, sha256}` for the exact profile bytes, including whitespace.
Replay needs the same SQL, scenario, seed and exact profile file. A changed mode or profile is rejected
before connecting; a profile changed during a witnessed run prevents saving that witness. Legacy
fixtures without the binding remain replayable only without `--constraints`.

```text
python skills/semantic-crosscheck/scripts/sxc.py counterexample --reference-sql examples/counterexamples/nulls-reference.sql --candidate-sql examples/counterexamples/nulls-candidate.sql --constraints examples/counterexamples/constraints.json --scenario nulls --seed 7 --out .sxc
```

The opt-in approach is informed by integrity-constrained bounded SQL checking in
[VeriEQL](https://doi.org/10.1145/3649849). No VeriEQL source is copied or bundled; its
CC BY-NC-SA code is not incorporated into this MIT project.

A discovered mismatch is `fail`/exit 1; no mismatch within the bound is `not_run`/exit 1, not global
equivalence. Invalid arguments or replay files are configuration errors. Runtime query failures are
errors. `result.json` contains statistics and identifiers only; `fixture.json` alone contains the
generated rows, together with generator/replay metadata and SQL fingerprints.

## Operator-directed mutation fixtures

`--mutation-operator` selects a deterministic fixture family for one of the nine configured V4
operator names, instead of `--scenario` or `--metamorphic-sql`. The supplied reference is the original
SQL and the candidate is the deliberately changed SQL, adapted to the fixed CTE tables above. This
tool does not compile Cube models or verify that the files correspond to that operator.

```text
python skills/semantic-crosscheck/scripts/sxc.py counterexample --mutation-operator measure_agg_to_max --reference-sql examples/counterexamples/mutation_agg-original.sql --candidate-sql examples/counterexamples/mutation_agg-mutant.sql --gold-sql examples/counterexamples/mutation_agg-gold.sql --constraints examples/counterexamples/constraints.json --out .sxc
```

Families cover closure depth/path count/quantity, exact and normalized filters, sum/count to max,
one-day shifts and asymmetric join/end selection. Only an executed SQL disagreement establishes
`synthetic_distinguished`. Selecting a family never establishes that a particular fault was present.
All queries for a fixture execute in the same read-only REPEATABLE READ transaction. With an
independent `--gold-sql`, `gold_observes` is true only when original and gold agree and mutant and
gold differ. Original/gold disagreement is a separate failed check. Without gold, its observation
is explicitly not proven. Gold cannot be supplied without this operator-directed mode.

The same distinction and gold-observation condition must survive shrinking. Every generated case
and deletion still satisfies the optional integrity profile. No successful mismatch within the
budget is `not_run`, never equivalence. Unsupported SQL relation/column schemas are explicitly
not run; other query failures are errors, never detections.

Replay format `sxc-synthetic-mutation` binds the selected operator, generator version, seed, original,
mutant and optional gold hashes, plus exact profile bytes. Repeat those inputs and add `--replay
.sxc/counterexample/fixture.json`. SQL files are trusted code, and qualified references can bypass the
fixture CTEs: use a dedicated test database. Results do not prove real-data V4 coverage, Cube runtime
behavior or business correctness. Fixture generation borrows the distinguishing-input principle
from [Polygon](https://arxiv.org/abs/2504.06542); no solver or third-party source is bundled.

## BOM metamorphic relations (one SQL, no gold)

A two-file comparison cannot see an error that the reference SQL shares with the candidate, for example
when gold and configuration were written from the same misunderstanding. `--metamorphic-sql FILE` checks a
single trusted closure SELECT against relations that follow from the BOM path-product meaning alone: Q(a, d) is
the sum over all paths a -> d of the product of `qty_per`. The SQL must return one row per (ancestor, descendant)
pair with the columns `ancestor`, `descendant` and `qty` (more columns are ignored), like
`examples/counterexamples/bom_diamond-reference.sql`.

```text
python skills/semantic-crosscheck/scripts/sxc.py counterexample --metamorphic-sql examples/counterexamples/bom_diamond-reference.sql --seed 7 --out .sxc
```

The mode uses only the `bom_diamond` generator (also with `--constraints`) and cannot be combined with
`--reference-sql`/`--candidate-sql`. For every admissible case the SQL runs on the original fixture and on four
transformed fixtures, each in its own read-only REPEATABLE READ transaction with bound values:

| Check | Transform | Expected answer |
|---|---|---|
| `counterexample:metamorphic:mr1_unfold` | copy shared sub-assemblies so the DAG becomes a forest; copies get fresh ids and copied part rows | for every instance i of part x and original part d, the sum of Q'(i, c) over the copies c of d equals Q(x, d) |
| `counterexample:metamorphic:mr2_scale` | multiply one line (u, v, q) on a multi-path by K = 2 | Q'(a, d) = Q(a, d) + (K-1)·q·Q(a, u)·Q(v, d) for every pair of either answer |
| `counterexample:metamorphic:mr3_disjoint` | add a fresh component containing a diamond | pairs of original ids unchanged; no pair crosses the components |
| `counterexample:metamorphic:mr4_unit_node` | replace (u, v, q) by (u, w, q) and (w, v, 1) through a fresh part w | original pairs unchanged; Q'(a, w) = Q(a, u)·q and Q'(w, d) = Q(v, d) |

An absent pair counts as 0. Q(x, x) = 1 (the empty path) is used only inside the formulas; reflexive answer
rows are never compared. Expected values are computed with exact decimal arithmetic and compared in the exact
`number` form (`6.000` equals `6`, nothing is rounded); a float `qty` cannot be checked exactly and is an error.
The line chosen for scaling or splitting is deterministic in the seed and case index.

A relation is `not_run` for a case when it would compare nothing or could not be computed exactly. Every
relation of a case is `not_run` when the fixture has no BOM line (`no_bom_lines`), a line with a NULL parent,
child or `qty_per` (`null_bom_value`), a `qty_per` that is not a valid `numeric(18,6)` value
(`invalid_bom_value`), or lines that form a cycle (`cycle`). A single relation is `not_run` with no diamond
(`no_diamond`), no line on a multi-path (`no_multi_path_edge`), no quantity other than 1 to split
(`no_non_unit_quantity`), a transformed fixture outside the column types or the profile (`type_domain`,
`integrity_profile`), more than 10,000 rows in a fixture or answer (`size_bound`), an expected value that exact
decimal arithmetic cannot represent (`inexact_arithmetic`), or no compared pair (`no_pairs`). `not_run_reasons`
counts them. A relation passes only when it compared pairs in at least one case and every comparison agreed; one
that ran in no case is `not_run` with `reason` `no_admissible_generated_examples` (no case was checked) or
`vacuous_for_every_case`. Unrun relations make the result `not_run` unless another relation failed; nothing is
proven for them.

The search stops at the first case that violates any relation: the violated relations `fail` (exit 1, like a
comparison mismatch) and `fixture.json` (format `sxc-synthetic-metamorphic`) holds the original generated rows,
the failed relation ids and every transform specification. Results hold pair counts, the seed, K and SHA-256
hashes of the transform specifications and fixture. Replay needs the same SQL file, `--seed` and profile;
the transforms are recomputed from the saved rows and a file whose recorded transforms differ is refused before
connecting. A missing `ancestor`/`descendant`/`qty` column, a duplicate pair, NULLs, non-text ids or a
non-numeric `qty` is the `counterexample:metamorphic:shape` error; database, driver and connection failures stay
`counterexample:query`. Any other exception is a fault of the tool, not a statement about the SQL, and is
recorded as `stage` error.

What the relations do not cover: errors that every transform preserves, such as a reversed edge direction, the
wrong quantity column or a mis-mapped filter; correctness of the business rule; and any SQL other than the given
file. The generated closure is tied to gold through V1/V3/V5 on real data; a passing relation upgrades no other
check. With the shipped examples, the `sum` reference passes all four relations, while the `min` candidate fails
unfolding (and scaling whenever the scaled line lies off the minimal path), and a `UNION` instead of `UNION ALL`
fails unfolding when two paths have the same product and depth.

The principle (transform the data so that the expected result is known) follows EDC, "Detecting Logic Bugs in
DBMSs via Equivalent Data Construction", PACMMOD 3(6), 2025; no EDC code is used.

This capability adopts generated-test-input and shrinking ideas described by
[Hypothesis](https://github.com/HypothesisWorks/hypothesis), and the counterexample/reduction approach
in [SQLancer](https://github.com/sqlancer/sqlancer). Neither project is bundled or copied, and no new
runtime dependency is added. A comparison detects disagreement between the supplied trusted queries;
it does not prove either query expresses the intended business rule or detect every DBMS defect.

# Authored stock boundary specification (EB-1)

This is a standalone specification for synthetic parts and stock. The expected rows
below were calculated before running gold SQL or Cube; they are not captured outputs.
The tables, SQL, TOML and tests were independently authored for semantic-crosscheck.

## 1. Fixture and arithmetic

There are five parts. `part_id` identifies a part; its region and label are dimensions.
`P-EMPTY` belongs to NORTH with label `No stock` and has no stock rows. `P-FILTER`
belongs to NORTH with label `Filtered stock`. `R-N1` and `R-N2` belong to NORTH;
`R-S1` belongs to SOUTH. All three R parts have the label `Relay`.

| stock_id | part_id | status | qty (exact numeric) |
| --- | --- | --- | ---: |
| 1 | P-FILTER | QUARANTINE | 9.00 |
| 2 | R-N1 | USABLE | 1.25 |
| 3 | R-N1 | USABLE | 2.75 |
| 4 | R-N2 | USABLE | 6.00 |
| 5 | R-S1 | USABLE | 7.00 |
| 6 | R-S1 | USABLE | 13.00 |
| 7 | NULL | USABLE | 1000.00 |

`on_hand` sums stock quantity by non-null `part_id`. `usable` sums only stock rows
with status exactly `USABLE`, again by non-null `part_id`. Both declare `node = "part"`
and use the existing node-to-preaggregated-measure link. Region and label live on
the part entity; no arbitrary entity join or new runtime behavior is required.

The existing NULL-grain policy excludes stock row 7 from either measure: a NULL
measure grain cannot join to a node. Its 1000.00 is outside every expected total.
This is a declared exclusion policy, not a promise to preserve orphan facts through
an arbitrary LEFT JOIN. No new NULL-grain semantics are introduced by this corpus.

## 2. parent_without_facts

For parameter `part = "P-EMPTY"`, retain the parent part and report raw missing
`on_hand` as SQL NULL. The one expected row is `part_id = "P-EMPTY", qty = NULL`.
There is no `zero_if_null` declaration for this query. Returning 0, dropping the
parent, or treating an absent response member as NULL cannot satisfy this contract.

## 3. facts_filtered_out

For parameter `part = "P-FILTER"`, its one stock row contributes 9.00 to `all_qty`.
It contributes nothing to `usable` because QUARANTINE is not USABLE. For this query
the presentation policy deliberately displays that missing usable aggregate as 0:
gold SQL explicitly coalesces `usable_qty`, TOML declares only `usable_qty` under
`zero_if_null`, and the authored value is literal 0. The expected row is
`part_id = "P-FILTER", all_qty = 9, usable_qty = 0`.

The nonzero `all_qty` distinguishes filtered-out facts from an actually empty parent.
The gold side must itself return 0; `zero_if_null` normalizes only the Cube side.

## 4. same_label_different_region

For parameter `label = "Relay"`, group by the complete key `(region, label)`.
NORTH contains R-N1's 1.25 + 2.75 and R-N2's 6.00, totaling exactly 10.00.
SOUTH contains R-S1's 7.00 + 13.00, totaling exactly 20.00. The two expected rows are
`("NORTH", "Relay", 10)` and `("SOUTH", "Relay", 20)`.

Grouping by label alone would produce one quantity 30 and lose a region. That is
a different query and fails this case. The entity key remains the single part ID;
the *result* key is composite, using the existing gold/expect comparison contract.

## 5. Provenance, fixture binding and bounded scope

Boundary ideas were inspired by SLayer's comparison corpus: a parent with no facts,
facts removed by a filter, and one dimension label in two regions. The reviewed
source is [MotleyAI/slayer examples/comparisons at
4c3330aa1a507f5a4b2cc836048ca42a785b0141](https://github.com/MotleyAI/slayer/tree/4c3330aa1a507f5a4b2cc836048ca42a785b0141/examples/comparisons).
No upstream data, SQL, runner, comparator or expected rows were copied. In particular,
the upstream DuckDB/Cube 1.7.46 outcomes are not asserted to equal PostgreSQL 17+ /
Cube 1.8.3 outcomes. The fixture pin binds the reviewed source tables using expect's
`fixture_sha256` v2 digest; it is not the hash of this SQL file. Record the pin only
after loading exactly these rows and checking this specification.

The supported inventory is these three authored row cases, exact numeric aggregates,
the existing node/measure link, literal NULL, explicit zero normalization and composite
result keys. This is not a port of the upstream 54-feature corpus. Generic multi-hop
entity joins, rolling windows, time spines, multi-stage/derived measures and
expected-error-as-PASS probes are outside this corpus. Runtime errors remain errors;
missing engines or unrun cases remain skipped/not_run rather than evidence of PASS.
The fixture exercises neither all possible NULL-grain queries nor all production
inputs. An expect pass is fixture-only evidence, separate from operational V1–V5
and report coverage, and is not an SQL equivalence proof.

## 6. Run and sensitivity checks

Use an empty dedicated synthetic database and the pinned engines. Load the SQL
yourself, then set `SXC_EXPECT_DSN` to that dedicated database and run:

```text
psql -d <dedicated-fixture-db> -v ON_ERROR_STOP=1 -f examples/expect-boundaries/fixture.sql
python skills/semantic-crosscheck/scripts/sxc.py expect --config examples/expect-boundaries/crosscheck.toml --allow-fixture-database
```

Never point it at an operational database. expect builds output views in that fixture
database. Source rows are unchanged. Normal results contain counts/IDs/hashes, not
fixture values or DSNs; local engine/build artifacts can contain synthetic data.

`tests/test_expect_boundaries.py` compares against hand-calculated literal rows,
rejects wrong authored rows and label-only grouping, and distinguishes NULL from
explicit zero. `tests/test_expect_boundaries_pg.py` creates only a process-specific
database and executes three real-engine runs: the authored baseline; changed authored
rows, which must fail as common_mode while SQL/Cube agree; and a shared wrong
QUARANTINE filter plus a gold query grouped only by label, which must fail as
common_mode and gold_mismatch respectively. These mutations are confined to temporary
copies; no source or operational database is used.

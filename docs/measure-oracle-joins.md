# Check whether a join multiplies fact amounts

A sales order worth 15.25 has two matching lines. If the business rule counts
each posted order with a line once, those two lines qualify the order for one
contribution of 15.25. A second order for the same amount contributes another
15.25. Deduplicating amounts would lose that second order.

The version-2 `measure-oracle` spec expresses this eligibility rule with
`related_exists`. It supports global SUM and COUNT(column). Both use the
existing fact-side `where` predicates and a separately authored Cube query.

```json
{
  "schema_version": 2,
  "entity": "sales_orders",
  "column": "amount",
  "aggregation": "sum",
  "where": [{"column": "state", "operator": "eq", "type": "text", "value": "posted"}],
  "related_exists": {"entity": "sales_lines", "fact_key": "id", "foreign_key": "order_id"},
  "cube_query": {"measures": ["m_eligible.value"]},
  "max_rows": 20
}
```

## Which rows contribute

The oracle reads the configured fact and related tables separately in one
read-only, repeatable-read transaction. It projects the fact identity,
measure and predicate columns from the first table, and only the foreign key
from the second. The same `max_rows` applies to **each** table. Exceeding it
on either side yields `not_run/row_limit`. The Cube query runs separately
after that transaction closes.

- The spec author identifies the stable, single-column fact key. Every visible
  fact must have a unique non-NULL value, including facts excluded by the
  predicate. A duplicate or NULL identity yields `not_run/invalid_fact_identity`.
- Keys on both sides must have the same supported kind, either PostgreSQL
  built-in integer or deterministic text/varchar. Equality preserves case and
  leading zeros in text. NULL related keys and orphan related rows do not
  qualify a fact.
- At least one matching row qualifies the fact for the fact-side predicate.
  Further matches add no extra contributions. SUM ignores NULL measure values
  and returns NULL for an empty or all-NULL selection. COUNT(column) counts
  qualifying non-NULL values and returns zero for an empty selection.
- The receipt records `related_rows`, `matched_facts`, `multi_match_facts`
  (two or more related rows), `null_foreign_keys` and `orphan_related_rows`.
  These counts cover the complete reads before the fact predicate. For
  example, `source_rows - matched_facts` counts facts without any match.
  `qualifying_rows`, `qualifying_multi_match_facts` and `non_null_values`
  describe the final selection. The receipt discloses when no qualifying fact
  has more than one related row, even if excluded facts had several matches.

## Run the example

Use the existing check/owner roles in a disposable database with an
`sxc_demo` schema. Load
[`fixture.sql`](../examples/measure-oracle-joins/fixture.sql) as the owner,
grant the check role schema USAGE and SELECT on the two tables and two views,
set the normal DSN variables, and install the Cube adapter.

```console
python skills/semantic-crosscheck/scripts/sxc.py cube --config examples/measure-oracle-joins/crosscheck.toml --out .sxc-join
python skills/semantic-crosscheck/scripts/sxc.py measure-oracle --config examples/measure-oracle-joins/crosscheck.toml --spec examples/measure-oracle-joins/eligible.json --out .sxc-join
python skills/semantic-crosscheck/scripts/sxc.py measure-oracle --config examples/measure-oracle-joins/crosscheck.toml --spec examples/measure-oracle-joins/multiplied.json --out .sxc-join
```

The independent expected total is 15.25 + 15.25 − 4.50 = **26.00** for both
runs. The first Cube measure reads an EXISTS-based view and passes. The second
reads a row-per-line join view and sums **41.25**, producing
`fail/value_mismatch`. Only the selected Cube member differs between the two
specs. Each run replaces `measure_oracle/result.json` in the output directory.

A mismatch establishes that the global totals differ. The receipt alone
cannot identify the cause. Aggregate equality can also conceal duplication
when multiplied amounts cancel, equal zero or contain only NULLs. A per-line
measure requires a different authored fact grain. Multi-hop joins, related-row
predicates, composite keys, grouped results and time windows are outside this
contract. The normal source/Cube snapshot and privacy limitations apply.
See the [measure oracle contract](measure-oracle.md).

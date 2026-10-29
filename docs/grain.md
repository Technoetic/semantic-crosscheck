# Independent grain contracts

`grain` is an opt-in diagnostic for the existing undated measure preaggregation followed by its node join:

```text
python skills/semantic-crosscheck/scripts/sxc.py grain --config examples/grain/crosscheck.toml --spec examples/grain/contract.json --max-rows 100000 --out .sxc-grain
```

Set the environment variable named by `source.dsn_env` to the dedicated database's read-only check connection. Credentials stay out of the config, manifest, command arguments and results. The stage performs no DDL, writes, config mutation or Cube execution. It does not join the default pipeline.

Write the manifest from the intended specification independently of `crosscheck.toml`. A matching manifest is an authored claim; it does not certify business truth. The stage derives the configured operations independently, compares the claim, and then checks actual visible source/node rows in one read-only, repeatable-read PostgreSQL snapshot. It accepts ordinary and partitioned source/node tables; views and foreign tables are unsupported. Dated row-level measures, closure joins, multi-hop joins and general DAG claims are explicitly unsupported. A selected subset of measures is allowed, and all unselected measures remain outside the reported scope.

## Exact version-1 schema

All fields below are required, including empty filter objects. No additional fields are accepted at any nesting level. Files must be nonempty strict JSON, at most 65,536 bytes; duplicate object keys, nonfinite numbers, booleans used as integers, repeated measures and empty scope are refused. There must be 1–32 contracts. Identifiers follow the existing plain lower-case SQL identifier policy, and tables must be schema-qualified. SQL text, file references, date or closure assertions cannot be placed in this schema.

```json
{
  "schema_version": 1,
  "contracts": [{
    "measure": "total",
    "source": {"entity": "fact", "table": "sxc_demo.grain_fact", "key": ["fact_id"]},
    "group_by": ["node_id"],
    "aggregate": {"function": "sum", "column": "qty"},
    "filters": {"exact": {}, "normalized": {}},
    "output": {"cube": "m_total", "key": ["key"], "value": "value", "reaggregate": "sum"},
    "node": {"entity": "node", "table": "sxc_demo.grain_node", "key": ["node_id"]},
    "join": {"relationship": "one_to_one", "node_key": ["node_id"], "output_key": ["key"]},
    "null_policy": {"operation": "exclude", "allow_excluded_rows": false}
  }]
}
```

`source` and `node` declare entity, physical table and configured key provenance. Source keys may be composite; the supported node key and grouping column are single-column. Nonempty key arrays contain at most 32 distinct identifiers. `aggregate.function` accepts `sum`, `count`, `min` or `max`; `count` counts the declared column's non-null values. `output` declares the generated `m_<measure>` cube, its `key` grain dimension, `value` member and reaggregation operation. Counts reaggregate with `sum`; other aggregates retain their operation.

`filters.exact` and `filters.normalized` map columns to nonempty arrays of strings or integers. The normalized form uses `upper(btrim(column))`, so authored values must already be uppercase and trimmed. A column cannot appear in both forms. Each filter object has at most 32 columns and each array at most 256 values. IN-list order and duplicate members do not change comparison. Values are bound PostgreSQL parameters, never executable SQL.

`join` describes the node-to-preaggregate operation: the node key joins to the output key with a `one_to_one` relationship. A `many_to_one` or `one_to_many` assertion is a recognized disagreement and fails the contract. `null_policy.operation` must match the configured exclusion of rows whose grain column is NULL. A `retain` claim is a recognized disagreement. `allow_excluded_rows` separately states whether observed null-grain exclusions are acceptable. With `false`, any otherwise filter-matching null-grain row fails. With `true`, their count is reported and accepted, while an entirely erased selected scope still cannot pass.

## Evidence, limits and statuses

Each contract uses at most five evidence statements: two physical-table catalog checks, two row guards, and one aggregate/key/join count query. The read-only transaction setup and rollback are additional fixed control statements. `--max-rows` defaults to 100,000 and accepts integers 1–1,000,000. The limit applies separately to the complete source table and complete node table, including rows outside measure filters. Row guards inspect at most limit+1 rows; overflow stops that contract before any sampled or partial evidence could pass. Every statement uses the configured `source.statement_timeout_ms`, capped at 30,000 ms. Multiple contracts share the same snapshot.

Once within the row bound, actual SQL checks both tables' entire key domains for nulls and duplicate groups. It applies configured filters, counts null-grain exclusions and non-null selected rows, and materializes the actual grouping and aggregate. It counts output groups, groups with NULL aggregate values, missing referenced nodes, groups with multiple matching nodes, joined rows, and nodes without an output group. Join multiplicities are counted without expanding a potentially quadratic fanout result. Aggregate values, source rows and keys never leave PostgreSQL; the result contains only counts, opaque contract ids, hashes and fixed diagnostic reasons. Null measure values are counted, preserving SQL aggregate semantics; they are not key violations.

An authored operation/provenance disagreement fails before database execution. Duplicate/null source or node keys, orphan groups, join fanout, and disallowed null exclusion fail actual evidence. Empty visible source/node tables, an empty selected scope, row overflow and unsupported relation kinds produce `not_run`. Unsupported configured fragments and malformed manifests are configuration errors. SQL errors, timeouts, connection problems, input changes during execution and incomplete cleanup produce sanitized errors. All required obligations must complete and pass for the stage to pass.

`<out>/grain/result.json` is written through the normal Result flow. Contract ids are positional `c0001`, `c0002`, and so on; check ids end in `:contract` or `:evidence`. The result includes manifest/config SHA-256 bindings and explicit `not_proven` boundaries. It does not independently certify aggregate values, actual generated Cube model/runtime behavior, future snapshots or business meaning. Input/output aliases, including hardlinks and symlinks, are rejected before stale result removal. Invalid subsequent runs clear an older result when safe.

## Synthetic examples

Load `examples/grain/fixture.sql` into a fresh disposable database using a loader connection, then grant its check role schema USAGE and table SELECT. Point `SXC_PG_DSN` at the check connection and run the command above. The authored contract expects 3 source rows, 2 node rows, 2 output groups and 2 joined rows; it should pass.

Use `examples/grain/wrong-group.json` with that fixture to diagnose the independently asserted `fact_id` grouping: the configured operation groups by `node_id`, so it fails before connection. Apply `fixture-violations.sql` only to that disposable fixture database and rerun the original contract to observe one duplicate source-key group, one orphan output group, one null-grain exclusion and one fanout group. These are real data checks, independent of operation-label agreement.

For a null-policy example, reload a fresh clean fixture and insert a fourth row with a new `fact_id` and NULL `node_id`. The original contract fails; `allow-null-exclusion.json` accepts and counts the exclusion. If all source grain values are NULL, both contracts remain nonpassing. The diagnostic itself never applies fixture SQL or operational repairs.

The Python entry point is `semantic_crosscheck.grain.run(cfg, args, out: Path) -> Result`, where `cfg` is a validated `Config`, `args.spec` is the manifest path and `args.max_rows` is the row limit. `prepare_run(args, out)` provides a read-only CLI alias preflight before artifact cleanup; `run` repeats complete input protection and removes safe stale result artifacts. CLI integration must also protect config/gold/revision inputs before writing error results.

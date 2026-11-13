# Mutation observation matrix

`verify.mutant_observation` controls how much of each selected mutation suite is observed:

```toml
[verify]
mutant_observation = "all"  # optional; default "first"
mutant_max_items = 10000   # existing logical-item budget, separately for each mutant
```

`first` keeps the existing early-stop behavior and receipt format. `all` continues after the first non-pass,
within the same per-mutant budget. Each item still gets one retry on error; retries consume attempts within
that item, not another logical item. The callable suite still returns the **first non-pass** `(status, item)`.
For example, mismatch then timeout returns the mismatch; error then mismatch returns the error. Budget
exhaustion after a mismatch keeps that verdict and leaves the remaining cells unexecuted. Without a prior
non-pass, exhausting the budget yields the existing `suite_budget` not-run verdict.

Extra observations cannot upgrade authoritative `detection`, authored evidence facets, mutation buckets,
report status or exit codes. In particular, a later authored mismatch after a first error cannot certify a
mutant. Source and Cube requests still use separate snapshots. Running more items increases requests and
runtime; the amount depends on the selected suite and has not been benchmarked.

## Artifact and scope

Only `all` writes `verify/mutation-matrix.json`. It is an atomic diagnostic sidecar with `schema_version: 1`
and `diagnostic_only: true`. Report does not read it. A new verify attempt removes the old matrix before
argument validation, prerequisite checks, database/Cube startup, or CLI TOML parsing. Default mode and
runs excluding V4 leave no matrix. Cleanup refuses a configuration alias or a verify directory resolving
outside the chosen output directory. Atomic replacement does not follow an artifact-file symlink.

The matrix contains only IDs, evidence kinds, categorical statuses/reasons and counts. It does not contain
SQL, query results, binding values, credentials or exception messages. Its fields are:

| Field | Meaning |
| --- | --- |
| `identity` | Verify `started_at`, a fresh `observation_id`, SHA256 of the configuration bytes (null if unavailable), and SHA256 of the canonical in-memory model JSON. These identify the observation; they add no authority or shared snapshot. |
| `selected_items` | Ordered baseline-passing replay IDs and `kind`: `authored`, `generated`, or `unclassified`. |
| `binding_id` | Where available, `gold_id:stratum[index]` in an item descriptor and receipt; V1 names its first binding and V2 its existing selected binding. Indices start at zero. Values remain in memory. |
| `excluded_items` | Planned items failing their unmutated baseline, with baseline status; these never become detectors. |
| `counts` | Selected mutant/item denominator, their product `planned_cells`, cells with attempts (`executed_cells`), cells without attempts (`not_run_cells`), and observed `mismatch_cells`. Retries count as one executed cell. |
| `cell_status_counts` | Counts of final `pass`, `fail`, `error`, and `not_run` statuses. An attempted `not_run` is executed but remains unjudged, so this status count can exceed `counts.not_run_cells`. |
| `mutants` | Selected instances, existing first-result outcomes, skip reasons and coverage receipts, plus every `observed_detectors` mismatch with item ID and evidence kind. |
| `detector_sets` | Per detecting item, the observed mutant IDs; `unique_mutants` have exactly one observed detector, while `redundant_mutants` also have another observed detector. |
| `complete` | A nonempty selected matrix whose every cell was attempted. Errors and attempted not-run cells can still be present. This never means all passed or all relevant semantics were checked. |

Pair replay IDs preserve chunk and NULL-group suffixes. Selected bindings and key partitions are the same
as the passing baseline; all mode does not sample new values. `mutant_bindings`, pair-key budgets, selected
operators, closure gates and `--only`/`--gold` scope still apply. Selected items in the denominator are the
expanded baseline-passing suite, not every possible query, binding or business case.

Only an executed final `fail` is an observed detector. Persistent error, timeout, startup failure, equivalent
data, not-applicable/gated instances, and missing execution never count as detections. Errors keep only
allowlisted Cube categories or SQLSTATE. A recovered error's category does not annotate the final pass.
Unexecuted cells have categorical reasons such as `item_budget`, `data_equivalent`, `startup`,
`not_applicable`, or `closure_not_passed`; attempted unjudged cells use `keys_not_answered` or `item_not_run`.
No passing baseline produces an empty selected-item denominator with the excluded baseline IDs retained.

Unique/redundant sets describe **observed mismatches on this bounded run**. Missing observations cannot
imply pass, uncovered semantics, equivalence, redundant test removal, or correctness on future data. Even
a complete matrix can contain errors and excludes cases outside its selected scope. Existing
[mutation coverage](mutation-coverage.md) and authoritative first-detector evidence remain the basis of
V4 verdicts.

## Validation

Unit regressions cover continued mismatches, first-result parity, errors/retries/timeouts, budgets,
baseline exclusions, equivalent/gated/startup cases, safe binding metadata, chunk/NULL replay, cleanup,
and diagnostic separation from authored facet detection. The disposable PostgreSQL 17/Cube regression
serves one depth-shift mutant with two selected authored detectors and verifies the following default
run removes the matrix. It requires the existing `SXC_TEST_ADMIN_DSN` and Cube adapter prerequisites;
a skipped local integration test is not runtime verification.

## Design references

The observation option is conceptually informed by
[StrykerJS v10.1.1 `disableBail`](https://github.com/stryker-mutator/stryker-js/blob/959127763e5bbe2eb65f5341a8161780d6d8a476/packages/api/schema/stryker-core.json#L499-L503)
and [Mutation Testing Elements' separate execution and detection fields](https://github.com/stryker-mutator/mutation-testing-elements/blob/4b5b62cc6bf81f23dd9521447131abeb51f895db/packages/report-schema/src/mutation-testing-report-schema.json#L43-L100).
These are pinned design references; no upstream code, schema, engine or runtime dependency is incorporated.
Both projects are Apache-2.0 licensed. Observed execution and observed detection remain different facts.

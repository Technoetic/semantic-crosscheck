# Mutation replay coverage

V4 serves each observable mutant with the existing Cube adapter. A passing bounded V3 pair baseline now
contributes one replay item for every key chunk (`v3:<test>:chunk[0]`, starting at zero) and one for the NULL
group (`v3:<test>:null`). The same bounded baseline key partition stays in memory; keys and row values are
never serialized in receipts. V4-only verification obtains its baseline before expanding these items.
Tests at or below the pair cap keep their original unfiltered item. Above `verify.pair_key_budget`, the
deterministic sample and `sample_blind_possible` disclosure remain.

```toml
[verify]
pair_key_budget = 10000
mutant_max_items = 10000
```

The key budget bounds key reads and partition storage; the item budget is a separate positive integer
(default 10000) limiting logical suite items **per mutant**. A pair chunk or NULL group is one item,
regardless of its number of Cube requests. An error still gets one retry within the same item. Budgets
and receipts reset before every mutant, and an early mismatch still kills that mutant. The first item
left out by the item budget produces `not_run` with `reason: suite_budget`; it cannot establish survival.

`verify/mutation-coverage.json` is an atomic diagnostic artifact, outside report authority. It describes
the selected mutant instances and selected passing suite only. Each instance records an outcome:
`killed`, `survived`, `equivalent`, `not_applicable`, `not_run` or `error`. Equivalent means the existing
witness found no observable change **on current data**, not general SQL equivalence. An equivalent,
inapplicable, gated or failed-to-start instance has no executed items. Executed mutant result details
also contain their `coverage` receipt.

Each receipt lists safe item ids, `status`, `attempts`, `planned_items`, `executed_items`, `max_items` and
`complete`. Unexecuted items are `not_run` with a categorical reason, such as `early_stop`, `item_budget`,
`data_equivalent`, `not_applicable` or `closure_not_passed`. `complete` means every selected item was
attempted, not that every item passed, that all possible tests were selected, or that a business result
was proven. `killed_by` identifies the first failing item, including its exact chunk/NULL suffix.

Baseline failures are excluded from mutation replay and disclosed in `not_proven` and the artifact's
`excluded_items` (safe ids and baseline statuses). If no baseline passes, every selected mutant instance
still appears as `not_run` without witness SQL or Cube startup. A survivor when
baseline failures, closure gates, planning failures or `--gold` exclusions left items out is `not_run`
(`suite_incomplete`). A confirmed kill by an executed item remains valid. The existing `--only`/`--gold`
scope checks and closure gates remain; excluding work cannot establish a full pipeline pass.

This does not give Cube/SQL a shared MVCC snapshot, prove correctness on future data, or certify SQL
equivalence. More chunks require more requests. Source changes during the separate requests remain a
limitation, disclosed by the V3 chunk notes and existing runtime evidence.

The implementation extends the project's existing mutation and metamorphic-testing design. Related
prior art: [SQLancer](https://github.com/sqlancer/sqlancer). No SQLancer dependency or comparator is
bundled; the existing type-aware, bag-aware comparisons remain authoritative.

---
description: "Compare Cube with the gold SQL: V1 strict compare, V2 strata sweep, V3 query shapes and V4 mutation tests"
argument-hint: "[--config crosscheck.toml] [--only v1,v2,v3,v4] [--gold ID] [--out DIR] [--quiet]"
---

Run the `verify` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not shell
code: pass each one as its own argument and never splice them into a shell string.

## When to use

- After [/xc-cube](xc-cube.md) passes, with a successful [/xc-build](xc-build.md) for every hierarchy
  the gold queries use.
- While fixing one gold query, narrow the run with `--gold` and `--only`. A narrowed run is never a
  result: finish with a full run before reporting.

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" verify --config crosscheck.toml
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" verify --config crosscheck.toml --only v1,v2,v3
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" verify --config crosscheck.toml --only v1,v2 --gold q01_explosion
```

- V1 runs the first binding of each gold query; V2 runs every sampled binding per stratum (`top`,
  `mid`, `leaf`, `leaf_parent`, `deep`, `shared`, `empty`, explicit `values`, `values_of` or
  `param_sets`). Rows are matched on the key and every mapped column must be identical in the form of
  its Cube member type: `string` columns as exact text (`'1.10'` is not `'1.1'`), `number` columns as
  exact decimals (`2.50` is `2.5`), `time` columns as UTC timestamps (a date is its midnight) and
  `boolean` columns as booleans. Duplicate keys fail and `zero_if_null` is the only declared equivalence.
- V3 checks query shapes derived from the model: totals against SQL, additivity over the ontology class
  column of every cube joined to the measure through many-to-one or one-to-one steps only (the node cube,
  and the closure's ancestor and descendant copies for `qty` and `pairs`), with the NULL group compared
  against independent SQL over the measure source (an anti-join when Cube starts from the measure's cube;
  only the matched rows without a class when it starts from the node cube, which drops measure rows
  without a node row), and measures queried together against the same measures
  queried alone (join fan-out). A join path with a one-to-many step gets no additivity check and is listed
  in not proven as not applicable. A pair
  test over more than 2,000 keys compares a deterministic sample of 2,000 keys (ordered with
  `verify.seed`, counted and drawn in one read-only snapshot) and says so in not proven, with the
  sample's detection bound (detail `d95_keys`, `d95_rate`): a mismatch affecting at least that many
  keys is detected with 95% probability if the mismatching keys are independent of the sample order;
  fewer can be missed. A sample never compares the NULL key group (keys that are NULL), which its equals
  filter leaves out; since 0.9.0, not proven says so. Since 0.10.0, sampled V3 and V4 replay
  require every requested key in the together and every alone answer. Without a confirmed mismatch or duplicate,
  incomplete answers are `not_run` (`keys_not_answered`); no d95 bound is reported and requested keys are not
  described as compared. Since 0.9.0, with
  `verify.pair_key_budget` (default 2000) raised to at least the key count, the pair test compares every
  key instead, in chunks of 2,000 keys from one read-only snapshot plus the NULL key group (detail
  `mode`, `chunks`, `compared_keys`, `answered_keys`, `total_keys`, `null_key_group`, `elapsed_ms`; no
  `d95_keys`); a key Cube leaves out of a chunk's together or (since 0.10.0) alone answer is not fully compared, so such a test is
  `not_run` (`keys_not_answered`) unless it fails. Not proven says that each Cube query reads its own
  snapshot, shared with no other query and not with SQL. Above the budget the sample stays and not
  proven names the budget. V4 still replays the 2,000-key sample, the first chunk. Since 0.11.0,
  `verify.existence_witness = true` adds `v3w:pair_witness:<member>+<member>` to every
  sampled or chunked pair test: each asked key the together answer holds but an alone answer leaves out
  is read in PostgreSQL with the measure's `where_sql` (in the key-read snapshot and again after the Cube
  queries). The pair test's own status and V4 are unchanged, also when the witness fails; V4 replays run
  no witness. Pair tests of at most 2,000 keys compare every key unfiltered and get no witness check.
- V4 patches copies of the model and serves each mutant in a fresh Cube process. Only suite items that
  pass against the unmutated model are replayed (V1, the first `verify.mutant_bindings` bindings per
  stratum, V3), and the suite stops at the first item that does not pass. Only a failing item kills a
  mutant; an item that still errors after one retry leaves the mutant unjudged (`error`,
  `reason=suite_error`). A witness query first counts the rows the mutation would change on the current
  data; a witness of 0 means the mutant is equivalent here and goes to not proven. V4 is the slowest
  part.
- `--only` and `--gold` leave a `not_run` check for every part (`scope:v1` ... `scope:v4`) and every gold
  query (`scope:gold:<id>`) they skip, so a narrowed run exits 1.
- `verify.mutation_operators` is checked before anything runs: an unknown operator is exit 2, also with
  `--only v1`.
- Gold SQL runs with bound parameters in a read-only transaction; it is trusted code from the
  configuration and is labelled as such in reports. Results go to `.sxc/verify/result.json`, the Cube log
  to `.sxc/verify/server.log` and each mutant to `.sxc/verify/mutants/<operator>__<target>/`; `--out DIR`
  changes the directory and `--quiet` prints only the summary line.
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## Success

When V3 is selected, an independent database-only closure oracle also compares raw BOM edges with
the stored closure, in one read-only REPEATABLE READ snapshot. It checks exact quantities, logical
path counts and minimum/maximum depth without reusing the generated SQL. Parallel BOM lines are
summed into one logical edge, as in build. This oracle does not query Cube and is not a V4 suite item.
With `on_cycle = "report"` it removes the edges inside each strongly connected component itself (an
iterative Tarjan search) before comparing, and a pass lists the removed edge count under NOT PROVEN.
Unsupported equality semantics and exceeded resource bounds are explicitly not run; an empty graph is
disclosed as having no nonempty coverage.

Exit code 0: every check passed (`v1:<gold>`, `v2:<gold>:<stratum>`, `v3:total:<member>`,
`v3:additive:<member>:<member>`, `v3:pair:<member>+<member>`, `v4:<operator>:<target>`) and none was left
out. A pass means only what these checks prove on this data. Always show the NOT PROVEN lines (empty
strata, equivalent mutants, sampled or chunked pair tests, join paths additivity does not apply to) next to the
verdict.

## Failures

Decide which side is wrong before changing anything: the hand-written gold SQL or the configuration
that generated the model. Fix `crosscheck.toml` or the gold SQL file, never the generated model.

- `v1:<gold>` or `v2:<gold>:<stratum>` fails: the detail counts missing keys, extra keys, differing
  columns and duplicate keys (counts only, no values). Common causes: a measure `filter` or
  `filter_normalized` that differs from the gold SQL, a wrong `closure_join` end, a `map` entry
  pointing at the wrong member, NULL versus 0 without `zero_if_null`, or a text value whose spelling
  differs (`'1.10'` and `'1.1'` are different text).
- `v3:total:<member>` fails: Cube's total differs from SQL computed from the configuration (filters,
  normalisation, rows whose grain column is NULL).
- `v3:additive:<member>:<member>` fails: the groups, NULL group included, do not add up to the total
  (`equal` false: a relationship declared many-to-one that is one-to-many in the data, such as a repeated
  node key, or measure keys missing from the node table), or Cube's NULL group differs from the rows
  without a classified dimension row (`null_group_equal` false; `orphan_rows` counts the rows with no
  dimension row at all).
- `v3:pair:<member>+<member>` fails: measures queried together differ from the same measures queried
  alone, which is join fan-out (wrong grain or relationship). A chunked test (since 0.9.0)
  adds up its counts over every chunk and the NULL key group (`null_key_group` holds the latter's own);
  when it is `error`, `chunks_compared` says how many chunks finished, and when it is `not_run`
  (`keys_not_answered`), `answered_keys` is below `compared_keys`: Cube's together or alone answers left keys
  out, and those keys were not compared.
- `v3w:pair_witness:<member>+<member>` (since 0.11.0, with `verify.existence_witness`)
  fails: an alone answer left out a key the measure has rows for under `crosscheck.toml`'s filters in
  both reads (`absent_with_facts`; for the closure `qty`, a key the closure table still has), or a key
  without such rows had a non-empty together value (`witness_value_mismatch`). Compare the generated
  measure cube with the configuration and rerun `cube`. `not_run` (`witness_incomplete`): `undecided_keys`
  keys stayed undecided, missing from the together answer (`together_absent`, counted per key) or with an
  absence (counted per key and measure an alone answer left out) that a failed cast (`witness_type`),
  another failed statement or a lost connection (`witness_error`) or a change between the two reads
  (`witness_changed`) left open. `not_run` (`pair_error`): the witnessed pair test itself is an `error`.
  A pass means only that every key Cube left out of an alone answer has no qualifying row under the same
  filters, not that the data is complete; keys an alone answer holds are not witnessed.
- `v4:<operator>:<target>` fails: a mutant that changes results on this data survived, so the gold suite
  cannot see that kind of error. Add a gold query, a mapped column or a stratum that would observe it,
  then rerun. Do not drop the operator from `verify.mutation_operators` to hide a survivor. When the
  detail says `sample_blind_possible`, the suite's sampled pair tests (`sampled_items`) compared only
  part of their keys, so the mutant may change only keys outside the sample: a gold query over those
  keys can still observe it.
- `v4:<operator>:<target>` is `error`: `suite_error` (a suite item could not run against the mutant;
  keep PostgreSQL and Cube reachable for the whole run), `startup` or `cube` (the mutant's Cube process
  did not serve; check with `/xc-doctor --require cube`), `model_mismatch` (rerun [/xc-cube](xc-cube.md)),
  `write` (check the output directory) or `witness` (the witness query failed; see its SQLSTATE).
- `not_run` with `reason=closure_not_passed`: gold queries, V3 tests and V4 closure operators over a
  hierarchy whose `closure:<h>` did not pass in `build`. Fix `build` first.
- `v4:<operator>:<target>` `not_run` with `reason=suite_incomplete`: the mutant survived a suite with
  items left out (a gated closure, a gold query that could not be planned, or `--gold`); those items
  might have killed it. Make every gold query and closure run, then rerun the full verify.
- `v4:suite` `not_run`: no suite item passes on the unmutated model; fix the V1 to V3 failures first.
- `error` on V1 to V3: the detail names the error type, SQLSTATE or HTTP status; the `next:` line names
  the cause (`members_not_in_model`: fix `cube_query` and `map`; a Cube timeout: raise
  `cube.query_timeout_s` or narrow the query; the row limit: narrow the query with filters or
  parameters; SQLSTATE `57014`: raise `source.statement_timeout_ms`; `cube=invalid_response`: Cube's answer
  was not a load result for the query, no `data` list of row objects or a row without a mapped member
  (`MissingMember`), so nothing was compared: check the adapter with `/xc-doctor --require cube` and rerun
  [/xc-cube](xc-cube.md)).
- Exit code 2: no cube result or model, an unknown `--only` or `--gold` value, or an unknown mutation
  operator. Exit code 3: Cube could not start; see [/xc-cube](xc-cube.md).

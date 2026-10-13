---
description: "Compare Cube with the gold SQL: V1 strict compare, V2 strata sweep, V3 query shapes and V4 mutation tests"
argument-hint: "[--config crosscheck.toml] [--only v1,v2,v3,v4] [--gold ID]"
---

Run the `verify` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not shell
code: pass each one as its own argument and never splice them into a shell string.

## When to use

- After [/xc-cube](xc-cube.md) passes, with a successful [/xc-build](xc-build.md) for every hierarchy
  the gold queries use.
- While fixing one gold query, narrow the run with `--gold` and `--only`; finish with a full run before
  reporting a result.

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" verify --config crosscheck.toml
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" verify --config crosscheck.toml --only v1,v2,v3
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" verify --config crosscheck.toml --only v1,v2 --gold q01_explosion
```

- V1 runs the first binding of each gold query; V2 sweeps sampled bindings per stratum (`top`, `mid`,
  `leaf`, `leaf_parent`, `deep`, `shared`, `empty`, explicit `values`, `values_of` or `param_sets`).
  Rows are matched on the canonical key and every column must be canonically identical; duplicate keys
  fail and `zero_if_null` is the only declared equivalence.
- V3 checks query shapes derived from the model: totals against SQL, additivity over the ontology class
  column, and measures queried together against the same measures queried alone (join fan-out).
- V4 patches copies of the model and serves each mutant in a fresh Cube process; the gold suite must
  kill it. A witness query first counts the rows the mutation would change on the current data; a
  witness of 0 means the mutant is equivalent here and goes to not proven. Only suite items that pass
  against the unmutated model are replayed, so a mutant is never counted as killed by a defect the
  model already has. V4 is the slowest part.
- Gold SQL runs with bound parameters in a read-only transaction; it is trusted code from the
  configuration and is labelled as such in reports. Results go to `.sxc/verify/result.json` and
  `.sxc/verify/mutants/*/`.
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## Success

Exit code 0: every check that ran passed (`v1:<gold>`, `v2:<gold>:<stratum>`, `v3:total:<member>`,
`v3:additive:<measure>:<dimension>`, `v3:pair:<m1>+<m2>`, `v4:<operator>:<target>`). A pass means only
what these checks prove on this data. Always show the NOT PROVEN lines (empty strata, equivalent
mutants) next to the verdict.

## Failures

Decide which side is wrong before changing anything: the hand-written gold SQL or the configuration
that generated the model. Fix `crosscheck.toml` or the gold SQL file, never the generated model.

- `v1:<gold>` or `v2:<gold>:<stratum>` fails: the detail counts missing keys, extra keys, differing
  columns and duplicate keys (counts only, no values). Common causes: a measure `filter` or
  `filter_normalized` that differs from the gold SQL, a wrong `closure_join` end, a `map` entry
  pointing at the wrong member, NULL versus 0 without `zero_if_null`.
- `v3:total:<member>` fails: Cube's total differs from SQL computed from the configuration (filters,
  normalisation, rows whose grain column is NULL).
- `v3:additive:<measure>:<dimension>` fails: the groups do not add up to the total (NULL class values or
  fan-out).
- `v3:pair:<m1>+<m2>` fails: measures queried together differ from the same measures queried alone,
  which is join fan-out (wrong grain or relationship).
- `v4:<operator>:<target>` fails: a mutant that changes results on this data survived, so the gold suite
  cannot see that kind of error. Add a gold query, a column or strata that would observe it. Do not
  drop the operator from `verify.mutation_operators` to hide a survivor.
- `not_run`: gold queries over a hierarchy whose `closure:<h>` did not pass in `build`. Fix `build`
  first.
- Exit code 3: Cube could not start; see [/xc-cube](xc-cube.md).

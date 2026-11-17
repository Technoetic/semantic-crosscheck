---
description: "Independently recompute a bounded single-table SUM/COUNT before comparing one Cube measure"
argument-hint: "--spec FILE [--config FILE] [--out DIR] [--quiet]"
---

Run only when explicitly requested. Pass each user argument separately without shell interpolation:

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" measure-oracle --config FILE --spec FILE --out DIR --quiet
```

Read [the contract](../docs/measure-oracle.md). Use the read-only role, current Cube receipt/model and independently authored raw predicate/query. Unsupported types and exhausted limits cannot pass; disclose empty coverage and the separate raw/Cube snapshots. Do not import generators into the expected calculation, expose values or claim business correctness. The result stays outside default `report` authority.

`--config FILE` selects the configuration; without it, the command requires
`./crosscheck.toml`. `--spec FILE` is required. `--out DIR` selects the output
directory (default: `.sxc` next to the configuration), which must contain the
current Cube receipt/model. `--quiet` prints only the summary line. The command
writes `<out>/measure_oracle/result.json` with counts, hashes and fixed reasons.

Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
[references/codex.md](../skills/semantic-crosscheck/references/codex.md).

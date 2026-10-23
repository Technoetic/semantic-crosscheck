---
description: "Replay a synthetic named mutation through actual original and mutant Cube models and independent gold SQL in an owned scratch schema"
argument-hint: "--mutation-operator measure_agg_to_max --allow-scratch-schema [--seed 7] [--dsn-env SXC_CUBE_REPLAY_DSN] [--statement-timeout-ms 10000] [--replay fixture.json] [--out DIR] [--quiet]"
---

Run the optional standalone `cube-replay` stage. Pass user arguments as separate argv entries.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" cube-replay --mutation-operator measure_agg_to_max --allow-scratch-schema
```

- `--mutation-operator` is required and selects one of the existing nine named mutation templates.
- `--allow-scratch-schema` explicitly authorizes creating and removing only this invocation's private synthetic schema. Without it the stage refuses to write.
- `--dsn-env` defaults to `SXC_CUBE_REPLAY_DSN`; set that variable to a dedicated throw-away PostgreSQL database. Never select an operational source or loader connection.
- `--seed` defaults to 7. `--statement-timeout-ms` defaults to 10000 and bounds SQL execution.
- `--replay FILE` accepts only the generated, strictly bound synthetic replay artifact.
- `--out DIR` defaults to `.sxc` in the working directory. `--quiet` prints only the summary.
- `--config` is rejected; a working-directory configuration is never loaded.

Install the existing pinned Cube adapter with `cube-install` first. The stage compiles actual original/mutant models and compares actual Cube answers against independently authored gold SQL on frozen synthetic data. Missing engines, startup failures, malformed or unstable answers, and cleanup failures cannot pass. Normal results contain only counts, identifiers and hashes; generated input rows belong only in the labelled local fixture artifact.

This fixed synthetic coverage is separate from real-data V4. It does not demonstrate that a production configuration, a supplied business query, or an arbitrary mutation has been verified. See [the replay contract](../docs/cube-replay.md).

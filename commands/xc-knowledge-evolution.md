---
description: "Replay five fixed offline synthetic source-contract checkpoints without engines"
argument-hint: "[--out DIR] [--quiet]"
---

Run the optional unreleased `knowledge-evolution` diagnostic. Pass user arguments
`$ARGUMENTS` as separate argv elements; never interpolate them into shell code.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" knowledge-evolution --out .sxc-evolution --quiet
```

`--out` selects the diagnostic result directory and `--quiet` prints a summary.
`--config` is rejected and cwd configuration is ignored. There is no external
fixture input. Output is `<out>/knowledge_evolution/result.json`; it preserves
fixture and engine hashes within the explicit knowledge_evolution namespace.

The fixed T0-T4 normal/change/delete/restore/retire sequence retains all 26
contracts in its denominator. Restoration leaves the retained case `not_run`;
retirement calls actual empty-case expect and stales original synthetic receipts.
PASS checks synthetic harness behavior only. No DB, network, model, subprocess,
engine execution or operational result generation occurs; semantic coverage is
not measured. Required report gates stay unchanged. See [scope and matrix](../docs/knowledge-evolution.md).

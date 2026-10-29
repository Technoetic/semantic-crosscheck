---
description: "Check independent authored measure grain and key provenance against bounded read-only PostgreSQL evidence"
argument-hint: "--config FILE --spec FILE [--max-rows 100000] [--out DIR] [--quiet]"
---

Run the optional `grain` stage. User arguments `$ARGUMENTS` are data: pass each separately without shell interpolation.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" grain --config examples/grain/crosscheck.toml --spec examples/grain/contract.json --max-rows 100000
```

`--config` selects the existing configuration, `--spec` the strict independently authored operation/key/null-policy contract, and `--max-rows` the per-source/per-node bound (1–1000000; default100000). `--out` selects result location; `--quiet` prints only the summary. A dedicated synthetic example database must be loaded and a read-only check connection supplied separately; this command never applies fixture SQL.

Scope is undated preaggregate-to-node measures. The declared source/group/output/node keys, aggregate, filters and join operation must match the derived fragment, and actual visible key uniqueness/nullability/orphan/fanout assumptions must hold. Null-grain exclusions require explicit permission and never make an entirely erased scope pass. Unsupported date/closure/general-DAG claims and malformed manifests fail closed.

All source reads use one read-only repeatable-read snapshot, bounded row guards and statement deadlines. Results contain counts, hashes, opaque ids and fixed reasons; no row/aggregate values. This does not independently certify Cube runtime, aggregate values, business intent or future snapshots. Configuration/gold/revision/manifest aliases are protected before result cleanup. Unsafe aliases suppress artifact writes; use the exit code to avoid trusting an older result file.

Output is `<out>/grain/result.json`. This stage is outside the required default report pipeline. Exit0 requires all selected obligations;1 includes failure/incomplete checks;2 is invalid inputs. See [schema, scope and synthetic examples](../docs/grain.md).

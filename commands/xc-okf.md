---
description: "Export selected metadata-only OKF contracts or preview a local bundle without applying it"
argument-hint: "--config FILE (--export DIR --select GOLD_ID | --import-preview DIR) [--out DIR] [--quiet]"
---

Run the optional `okf` diagnostic. Pass user arguments `$ARGUMENTS`
as separate argv elements; never interpolate them into shell code.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" okf --config crosscheck.toml --export contract-bundle --select q01_explosion
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" okf --config crosscheck.toml --import-preview contract-bundle
```

Both modes require explicit `--config`. Export requires 1–64 unique existing
Gold IDs through repeatable `--select`; it never defaults to all. Preview rejects
`--select` and never applies imported definitions. `--out` is the separate
diagnostic directory; output is `<out>/okf/result.json`. Existing destinations,
even empty directories, and input/output aliases are rejected without overwrite.
Protected preflight can preserve an old result; always use the exit code.

This closed metadata profile omits SQL, source text, rows, parameter values, local
paths, DSN names and runtime receipts. Source paths/labels appear only as hashes.
No DB, network, model, executor, SQL import or config mutation occurs. PASS means
scoped export/preview completion. Runtime execution, cross-layer correctness,
bundle authenticity and import application remain `not_proven`. This diagnostic
does not change required operational report stages. Unsupported profile fields
do not establish invalidity of general OKF. See [profile and filesystem limits](../docs/okf.md).

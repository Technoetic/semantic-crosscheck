---
description: "Compare a bounded synthetic PostgreSQL query under observed different scan plans"
argument-hint: "--spec FILE [--dsn-env NAME] [--statement-timeout-ms 1000] [--out DIR] [--quiet]"
---

Run only when explicitly requested. Pass each argument separately without shell interpolation:

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" plan-compare --spec FILE --dsn-env SXC_TEST_ADMIN_DSN --statement-timeout-ms 1000 --out DIR --quiet
```

Read [the contract](../docs/plan-compare.md). Choose a dedicated disposable PostgreSQL test database, never an operational DSN. The declarative spec accepts synthetic rows/typed predicates, not arbitrary SQL; only owned session temporary objects are created. Reject `--config` and ignore operational default configuration.

Require actual EXPLAIN plan diversity and complete typed row bags. Different planner settings alone are insufficient. Mismatch means inconsistency, not which answer is correct; matching two plans is not equivalence. Same-plan, empty, partial, error and budget cases cannot pass. No reducer/minimality is supplied. The diagnostic stays outside default report authority.

`--spec FILE` is required. `--dsn-env NAME` selects the environment variable
holding the synthetic connection (default: `SXC_TEST_ADMIN_DSN`).
`--statement-timeout-ms N` sets a per-statement deadline (default: 1000 ms).
`--out DIR` selects the local output directory (default: `./.sxc`), and
`--quiet` prints only the summary line. The command writes
`<out>/plan_compare/result.json` with counts, hashes and fixed reasons.

Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
[references/codex.md](../skills/semantic-crosscheck/references/codex.md).

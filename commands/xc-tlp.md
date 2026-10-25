---
description: "Check trusted nonrecursive SQL COUNT or SUM using true, false and NULL predicate partitions on bounded synthetic fixtures"
argument-hint: "--spec FILE [--oracle tlp|norec] [--dsn-env SXC_TLP_DSN] [--seed 7] [--max-examples 64] [--statement-timeout-ms 10000] [--replay fixture.json] [--out DIR] [--quiet]"
---

Run the standalone `tlp` stage with reviewed trusted SQL. Pass arguments separately, never interpolate SQL or DSNs into shell code.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" tlp --spec "${CLAUDE_PLUGIN_ROOT}/examples/tlp/sum-null.json"
```

- `--spec FILE` is a required strict version 1 JSON file. It declares the synthetic scenario, relational SELECT, predicate, and COUNT/SUM aggregate. See [the complete spec](../docs/tlp.md).
- After 0.9.0 (unreleased), `--oracle` defaults to `tlp`. `--oracle norec` requires exactly `count(*)`: independently compare the WHERE count with a CASE/SUM count over the bounded materialized relation. It does not reuse a frozen predicate between the oracle arms. Check IDs and replay identity name the oracle; a TLP replay cannot be used as NoREC evidence or vice versa.
- `--dsn-env` defaults to `SXC_TLP_DSN`, naming an environment variable with a dedicated synthetic test connection; never place a DSN in arguments.
- `--seed` defaults to 7; `--max-examples` defaults to 64 and is bounded at 1000.
- `--statement-timeout-ms` defaults to 10000 and bounds each PostgreSQL statement.
- `--replay FILE` runs one generated fixture bound to the exact spec bytes and generator implementation.
- `--out DIR` defaults to `.sxc` in the working directory; `--quiet` prints only the summary.
- `--config` is rejected; no operational default configuration is loaded.

Each comparison freezes a relation and its predicate, checks the multiset of rows in the true/false/NULL partitions, and compares exact SQL aggregates. COUNT(*) differs from COUNT(nullable-column); an empty or all-NULL SUM remains NULL. The stage rechecks observable stability. Errors, timeouts, unsupported types, malformed engine output and modified input files cannot pass.

The preceding partition description applies to the default TLP oracle. NoREC materializes only the relation, counts TRUE predicates (FALSE/NULL contribute zero), and uses separate full-row/predicate diagnostics plus a second execution to detect observable instability. Its bounded relation limits optimizer/index coverage; it compares predicate counts, not complete query results. Both modes remain outside operational V1-V5.

Trusted SQL is not a sandbox and may explicitly reference other relations or functions. Use only reviewed SQL with a dedicated test database. A pass covers the tested partition identity, not business-query correctness, general SQL equivalence, or operational V1-V5 coverage. Only labelled fixture artifacts contain synthetic input rows; normal output is count-only.

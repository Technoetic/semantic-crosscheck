---
description: "Compare a bounded synthetic PostgreSQL query under genuinely different scan plans"
argument-hint: "--spec FILE [--dsn-env NAME] [--statement-timeout-ms 1000] [--out DIR] [--quiet]"
---

Run only when explicitly requested. Pass each argument separately without shell interpolation:

```text
python "<plugin root>/skills/semantic-crosscheck/scripts/sxc.py" plan-compare --spec FILE --dsn-env SXC_TEST_ADMIN_DSN --out DIR
```

Read [the contract](../docs/plan-compare.md). Choose a dedicated disposable PostgreSQL test database, never an operational DSN. The declarative spec accepts synthetic rows/typed predicates, not arbitrary SQL; only owned session temporary objects are created. Reject `--config` and ignore operational default configuration.

Require actual EXPLAIN plan diversity and complete typed row bags. Different planner settings alone are insufficient. Mismatch means inconsistency, not which answer is correct; matching two plans is not equivalence. Same-plan, empty, partial, error and budget cases cannot pass. No reducer/minimality is supplied. The diagnostic stays outside default report authority.

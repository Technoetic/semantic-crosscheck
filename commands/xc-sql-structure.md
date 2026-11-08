---
description: Compare explicit PostgreSQL 17 SQL syntax against authored structural expectations offline
---

Run the opt-in offline structural diagnostic only when the user explicitly selects
a SQL file and a separately authored version 1 expectation JSON file:

```sh
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" sql-structure --sql FILE --expect FILE --out DIR --quiet
```

The optional pinned `requirements/sql-structure.txt` dependency must be installed
separately. Do not automatically install it or rewrite/generate SQL. No database,
network, model or operational configuration is used by this command. A missing or
incompatible parser is `not_run` (exit 3). Supported mismatches fail; unsupported
SQL or missing expectation fields remain `not_run` and require clarification.

Use [docs/sql-structure.md](../docs/sql-structure.md) for the exact narrow subset
and schema. The four checks compare relation, ordered selection, grouping and
conjunctive predicates, including inclusive BETWEEN versus half-open boundaries.
Passing means only that admitted syntax matches the authored expectation; business
meaning, catalog binding, aggregate resolution and runtime results remain unproven.
This diagnostic grants no report or operational verification authority. Never
infer its expectation from business prose or a semantic-preflight plan.

Report only status, fixed IDs, counts and hashes. Do not echo SQL, local paths,
identifiers, literals or parser exceptions into commentary or external services.

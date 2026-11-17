---
description: "Independently recompute a bounded single-table SUM/COUNT before comparing one Cube measure"
argument-hint: "--config FILE --spec FILE [--out DIR] [--quiet]"
---

Run only when explicitly requested. Pass each user argument separately without shell interpolation:

```text
python "<plugin root>/skills/semantic-crosscheck/scripts/sxc.py" measure-oracle --config FILE --spec FILE --out DIR
```

Read [the contract](../docs/measure-oracle.md). Use the read-only role, current Cube receipt/model and independently authored raw predicate/query. Unsupported types and exhausted limits cannot pass; disclose empty coverage and the separate raw/Cube snapshots. Do not import generators into the expected calculation, expose values or claim business correctness. The result stays outside default `report` authority.

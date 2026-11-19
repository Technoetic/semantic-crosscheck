# Does an authored row check catch errors missed by row agreement?

The business rule is to sum each recorded unit quantity exactly once for its SKU. A SKU's tags have no bearing on its total. For example, the two quantities 2 and 5 in `dev-shared-fanout.json` total 7. Joining both facts to two tags before summing gives 14.

## Reproduce the comparison

From the repository root, run the following commands with Python 3.11 or later.

```sh
python tools/run_paired_example.py
python tools/paired_release.py --plan examples/paired-release/plan.json --baseline examples/paired-release/baseline.json --candidate examples/paired-release/candidate.json --claims examples/paired-release/claims.json
```

The first command verifies every fixture's raw SHA-256, executes both arms in fresh in-memory SQLite databases and compares the generated receipts byte for byte with the checked-in copies. To save fresh receipts elsewhere, supply `--write --baseline PATH --candidate PATH`. The second command pairs observations with the frozen labels and validates the numeric claims. An added detection is an error case that changed from clear to flagged. A new false positive is the same change on a clean case.

## What ran

Each fixture supplies fact rows, tag rows, the two query modes and a literal expected result written from the rule. The gold query groups the fact rows directly. The model query starts with totals at SKU grain. In `tag_join` mode, the gold query joins facts to tags before grouping, and the model query multiplies the per-SKU total by its tag count using an inner join. The two queries can consequently agree on a wrong answer.

| Arm | Checks executed for each case |
| --- | --- |
| Baseline | The production V1 row comparison, `semantic_crosscheck.compare.compare_rows`, checks gold and model outputs against each other. |
| Candidate | The same V1 comparison plus `semantic_crosscheck.expect.judge`, the production expected-row judge, which also compares both outputs to the authored rows. |

A failed check flags the case. A judge error makes the observation inconclusive. Both arms use the same ordered cases, inputs, types, query code and budget of ten case attempts. The candidate performs more comparisons per attempt. The scorer reads the plan's labels after the receipts are generated. Each label describes the output on the fixture, so a query containing a redundant join can yield a clean case when every applicable SKU has exactly one tag.

## Origin and scope

The five `dev-*` cases were authored by the runner implementer while designing the comparison. A second agent authored and sealed the five `sealed-*` cases after receiving the rule, fixture schema and query-mode definitions. It supplied case IDs, preassigned clean/error labels and SHA-256 hashes for the plan. The implementer first ran the chosen checks after the runner, fixtures, labels and claim names were committed. The held-out designation describes this first evaluation. All ten fixtures are public regression examples for subsequent changes. [Preregistration commit](https://github.com/Technoetic/semantic-crosscheck/commit/370e46fac813494055e74ee960dceaa6e1ccc09c)

The measurements cover this synthetic SKU rule, these fixture rows, local SQL query implementations and the production comparison functions. SQLite stands in for the two data providers. A PostgreSQL/Cube integration run, operational error incidence, and latency or compute comparisons require separate measurements.

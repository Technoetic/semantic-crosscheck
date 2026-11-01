# Offline knowledge evolution (unreleased source)

The standalone `knowledge-evolution` diagnostic exercises source binding and
existing report staleness through a fixed reviewed synthetic fixture. Package
and release metadata remain **0.14.0**. This is a harness check, with no measured
semantic coverage, business truth, engine verification or production data evidence.

```text
python skills/semantic-crosscheck/scripts/sxc.py knowledge-evolution --out .sxc-evolution --quiet
```

The only options are `--out`, `--quiet` and `--help`. `--config` is rejected;
configuration in the working directory is ignored. There is no external fixture
path, SQL, DSN, engine selector or model input. No dependency installation occurs.
Output is `<out>/knowledge_evolution/result.json`, a diagnostic result containing
fixed IDs, closed states, actual contract counts and fixture/engine hashes.

| Checkpoint | Fixed operation | Source | Retained expectation case | Required operational counted status |
|---|---|---|---|---|
| T0 | normal | current | pass | pass |
| T1 | change_spec | changed | not_run | pass |
| T2 | delete_spec | missing | not_run | pass |
| T3 | restore_spec | current | not_run | pass |
| T4 | retire_case | retired | not_run | not_run |

Each checkpoint keeps five contracts: source state, retained case status, and
the actual counted status of build, cube and verify. T4 also checks that actual
`expect.run` returns `not_run` with `no_cases` before database access. The
denominator stays **5 checkpoints and 26 contracts** even when observations fail.
An unavailable observation is an error, a disagreement fails; neither disappears
from the counts. PASS means only that all these fixed synthetic contracts agreed.
An unreadable engine binding also keeps every contract as an error, with
`engine_state` unavailable and an empty engine hash.

The bundled resource is `semantic_crosscheck/resources/knowledge-evolution-v1.json`,
schema `sxc/knowledge-evolution-fixture/v1`, bounded to 64 KiB. It accepts exactly
the five IDs and operations above. No expressions, arbitrary fields or code are
evaluated. The independent literal oracle is separate from the source classifier.

Replay creates one private temporary project and calls the actual configuration
loader, `inputs_fingerprint`, source capture/finish, report staleness and counted
status APIs. Each source checkpoint takes a fresh capture while retaining the
same in-memory expectation case. Exact restoration can legitimately make a source
current, but never promotes its retained `not_run` case or creates a receipt.
Original synthetic operational receipts and execution times stay unchanged.
At T4, removing the authored case changes the real configuration fingerprint,
so existing whole-input guards reject the old synthetic operational passes.
Source-only changes do not change operational fingerprints: `expect` remains
outside the required report pipeline. There is no tombstone or epoch policy.

All case and operational execution evidence is synthetic and in memory with fixed
times. No operational build/cube/verify/expect result files are created, no
`report.run` or live probes are invoked, and SQL is only hashed. Engines executed,
database queries and model calls are zero; `semantic_coverage_measured` is false.
Temporary files are removed on completion. This is a bounded regression scenario,
not an open benchmark or an effectiveness estimate.

The related opt-in [authored source bindings](knowledge-sources.md) bind opaque
bytes to authored expectations only. `current` establishes equality to a pin,
not semantic validity, approval or ongoing filesystem protection. The separate
[OKF metadata profile](okf.md) exports selected metadata or previews it without
applying definitions; integrity does not establish authenticity. None of these
three capabilities changes operational required stages or supplies engine PASS.

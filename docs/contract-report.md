# Optional business contract evidence in operational reports

The explicit evidence boundary is informed by the previously observed
[Data Contract CLI](https://github.com/datacontract/datacontract-cli/tree/09e779dfeaec63ee60edc102e7a3d6970e34e16b)
and [Open Data Contract Standard](https://github.com/bitol-io/open-data-contract-standard/tree/448f7b93329ece7827897d7dd51e827a3e279460).
The earlier revisit condition was actual authored contracts; v0.20.0 supplied
those. This v0.21.0 application binds existing local declarations and statuses
to report freshness. It is not a full ODCS adapter, external test runner or new
data-contract runtime, and no source from those projects is copied.

Add this table to `crosscheck.toml` to require current business-definition and
structured-plan evidence in `report`:

```toml
[semantic_preflight]
contract = "contract.json"
plan = "plan.json"
```

Both keys are required nonblank relative file paths, resolved from the
configuration's directory. Absolute paths, URI schemes, traversal, empty path
segments, unknown options and wrong types are configuration errors. The section
is optional. Without it, report behavior and the operational pipeline order stay
the same, and a standalone semantic-preflight result is ignored.

The section makes `semantic_preflight` an additional required report stage; it
does not run the diagnostic automatically or waive build/reason/cube/verify.
The separate SQL structure diagnostic remains outside the operational report.

## Run the synthetic example

From the repository root:

```text
python skills/semantic-crosscheck/scripts/sxc.py semantic-preflight --config examples/contract-report/crosscheck.toml --contract examples/contract-report/contract.json --plan examples/contract-report/plan.json --out .sxc-contract-report
python skills/semantic-crosscheck/scripts/sxc.py report --config examples/contract-report/crosscheck.toml --out .sxc-contract-report
```

The first command passes without a database, network or credentials. The second
command reports that `semantic_preflight` is required and current, but exits 1
with `not_run` until the ordinary operational stages have actually run. The
example never manufactures operational evidence. Its [configuration](../examples/contract-report/crosscheck.toml),
[contract](../examples/contract-report/contract.json), [plan](../examples/contract-report/plan.json)
and [Gold reference](../examples/contract-report/gold/on_hand.sql) use synthetic
part/stock declarations. Review the existing [document schemas](semantic-preflight.md)
before adapting the example.

`--config`, `--contract` and `--plan` remain explicit, required diagnostic
arguments. To satisfy an opted-in report, pass the exact files selected by that
configuration. A diagnostic for different paths cannot satisfy it, even when
those files contain identical bytes. Relative command-line paths still resolve
from the current working directory; the TOML sidecar paths resolve from the
configuration's directory.

## What a current result requires

The diagnostic records SHA-256 hashes for the captured configuration, contract
and plan bytes, a generator hash and a selected-file binding hash. The latter
binds the normalized resolved identities of all three selected paths without
retaining plaintext paths. The generator covers the parser, evaluator, path/byte
guards, naming, Result contract and evidence code. This is independent of the
existing generic operational engine fingerprint, which still invalidates old
operational stage evidence after an engine upgrade.

With the section enabled, report captures the configured files and the saved
`<out>/semantic_preflight/result.json` under the existing input guards. It
requires matching content, selected-file and generator hashes, the expected
stage/schema and readable chronological run times. It recomputes the declaration
checks from the captured inputs and requires the complete saved check list to
match. Status is recomputed from checks; a recorded status can only worsen it.
Saved arbitrary explanations are replaced by explanations derived from the
current declarations.

| Evidence condition | Report effect |
|---|---|
| No saved diagnostic | Required stage is `not_run`, reason `missing` |
| Different or incomplete binding hashes | Required evidence is stale and cannot pass |
| Current diagnostic failure or incomplete plan | `fail` or `not_run` remains decisive |
| Malformed/truncated/oversized/unreadable Result or input | Error; no cached pass is accepted |
| Saved passing subset or forged check statuses | Error when recomputed checks disagree |
| Files/result/generator change during report | Capture is invalidated permanently; report becomes an error |

A stale receipt retains its own worse saved check status and counts. Its
arbitrary identifiers/explanations are replaced by fixed positional check labels;
staleness never downgrades a saved failure or error to `not_run`.

Files are rechecked before report assembly and around publication. A changed
capture is never replaced with a newly captured passing result in the same run.
Correct the inputs, explicitly rerun semantic-preflight for the configured
files, and rerun report. Changed configuration also requires rerunning the
ordinary stages whose existing configuration hashes are now stale.

The 256 KiB file limit, strict JSON duplicate-key/number/UTF-8/depth/node limits,
regular single-link file requirement, linked ancestor/reparse rejection and
existing standard macOS alias exception remain in force. The same bounded
capture applies to the configured saved diagnostic. These are local observations,
not a lock against hostile concurrent ancestor replacement.

Before CLI cleanup or direct report writes, the selected configuration, contract
and plan are protected against `report.json`, `report.md`, `report/result.json`,
the legacy `report/result.json.part` target and `.gitignore`, including aliases
and hardlinks. An unsafe selection returns a configuration error without output
mutation. New random Result temporary files use exclusive creation.

The diagnostic and report's contract-evidence assessment open no Gold/revision
SQL bodies, databases or network connections. The operational report retains its
normal SQL fingerprints and any explicitly configured data-revision/runtime
probes; opting into a contract does not disable those checks.

## Trust limits

Local hashes provide change detection, not authenticity. A writer able to rewrite
the inputs and matching evidence is not authenticated by these files. A
`reviewed` value is an authored assertion, not a signed approval or independent
review. Equal declarations may share the same mistake.

Matching unit, grain, population, aggregation, denominator, period, time basis or
timezone declarations does not prove SQL implements them. It does not validate
SQL meaning, bind plan periods to SQL parameters, prove business truth or certify
query results. It does not solve the separate cross-engine snapshot problem.
These limitations remain in the report's `not_proven` disclosures, including
when every required stage passes.

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

## Evidence kinds in the report

Since 0.23.0, when a `verify` result is among the results report reads, report also says what kind of evidence
each check is ([design, section 8, "Evidence kinds"](design.md#evidence-kinds)). This is disclosure only: no check,
status, exit code or required stage of report changes, `report/result.json` gains no key, and a report without a
verify result (the compatibility goldens) is unchanged apart from the engine fingerprint.

`report.json` gains `evidence` as its last key:

| Key | Meaning |
|---|---|
| `schema_version`, `rule_version` | 1 and the registry version |
| `included_stages` | the stages whose checks are counted: a result that counts towards the verdict and is not stale, in report's stage order |
| `excluded_stages` | `{"stage", "why"}` with `why` = `missing` (a required stage without a result), `stale`, or `not_counted`; an optional stage without a result is not listed |
| `observation_recorded` | whether the verify result holds a readable `evidence` object |
| `mode` | verify's `gold_observation` mode, or null when nothing is recorded |
| `kinds` | per kind (`authored`, `recomputed`, `generated`, `structural`, `mutation`, and `unclassified` only when above 0): `pass`, `fail`, `error`, `not_run`, the check ids per stage under `ids`, and under `authored` also `zero_row` (the authored checks of both zero-row lists) |
| `overridden` | gold entries whose recorded kind was above what the rule allows; they count as generated |
| `mutation`, `mutation_reason` | verify's mutant split (operators selected and not selected, eight buckets, `generated_only` ids) with reason `""`; or `{"not_judged": <n>}` with `not_recorded` (no readable evidence), `not_included` (verify is stale) or `v4_not_run` (`--only` left V4 out; then `<n>` is 0) |
| `facets`, `facets_reason` | `total`, `compared`, `detected`, `accepted` (facet ids), `gaps` (`facet`, `reason`) with reason `""`; or null with `narrowed`, `not_recorded` or `not_included` |
| `zero_row_checks`, `empty_stratum_zero_row_checks`, `gold_no_rows` | verify's lists (empty when its evidence is not read) |
| `gold_generated_text` | the gold queries that count as generated (generated text, or a recorded kind other than authored) |

Every check of an included stage counts once under the kind of the registry. The only recorded kind report reads is
the kind of a gold query in verify's evidence, which can lower `v1:<gold>` and `v2:<gold>:<stratum>` from authored to
generated and never raise anything; without a gold entry they count as unclassified, never authored. What verify
recorded about mutants, facets, zero rows and gold text is shown only when verify is included and its evidence is
readable. Readable means: an object with `schema_version` 1, the current `rule_version`, `mode` disclose or require,
a `gold` list, the three id lists as lists of strings, a `mutation` object with non-negative integer counts and string
lists, and either `scope` full with facet rows and `facets_reason` "" or `scope` narrowed with `facets` null and
`facets_reason` "narrowed"; a verify result that fails the result contract counts as having none. The values are
checked too:

- every facet id, gold id (of an entry with a string id) and entry of the three id lists is a plain id, made only of
  `[A-Za-z0-9_.:*+[]-]` (no space, no line break);
- a gap reason is one of the seven facet reasons, and a row that is not a gap has the reason "";
- `operators_not_selected` holds distinct operators of the catalogue and adds up with `operators_selected` to 9, and
  every `generated_only` entry is a `v4:<operator>:<target>` id;
- the mutant split follows from the V4 checks of the same verify result: `evidence.v4_violations` finds nothing, no V4
  mutant check id occurs twice, `killed_authored` equals the judged checks (status not `not_run` or `error`) with a
  killer and detection `authored`, `survived` the judged `fail` checks without a killer, `killed_generated_only`,
  `unattributed` and `accepted` add up to the other judged checks, `not_judged` is at least the number of `not_run` or
  `error` mutant checks, and `generated_only` names exactly `killed_generated_only` distinct judged checks whose
  detection is `generated_only`.

An evidence object that fails any of this counts as not recorded and is not printed at all, so no line break and no
row value reaches the console. Only these keys are copied, so a hand-edited key never reaches a report output.

`report.md` gains a `## Evidence` section before `## Not proven`: a fixed introduction (only authored and recomputed
checks compare generated layers with something the generators did not produce; "authored" says who wrote one side,
not that it is right; compared with rows is not detection; paraphrased copies of generated SQL are not detected;
`expect` rows are not read), a table of the kinds with what the two sides are and the four status counts, and five
bullets: the mutants, the facets, the authored checks that compared no row (strata named `empty` apart) with the gold
queries that compared no row, the gold SQL with generated text, and the stages not included. A narrowed verify run
gives "Facets: not judged (narrowed verify run).", a verify result without evidence "Mutants: not judged (the verify
result has no evidence section)." and the same for facets, a stale verify "not judged (verify is not included:
stale)", and a run without V4 "Mutants: not judged (V4 was left out of this narrowed verify run)."; none of them shows
"0 of 0".

The console prints, after the NOT PROVEN lines and before `next:`, not with `--quiet`:

```text
  EVIDENCE  checks by kind (pass of all): authored {p}/{n} ({z}+{ze} compared no row), recomputed {p}/{n}, generated {p}/{n}, structural {p}/{n}, mutation {p}/{n}[, unclassified {p}/{n}]
  EVIDENCE  mutants ({k} of 9 operators selected[; not selected: {ops}]): {a} killed with authored detection, {g} killed by generated checks only[ ({ids})], {u} unattributed, {acc} accepted, {s} survived, {j} not judged, {e} equivalent, {na} not applicable
  EVIDENCE  facets: {c} of {t} compared with rows holding values by authored gold SQL, {d} more detected by an authored mutant kill; no authored evidence: {none|facet (reason)[, ...]}[; accepted: {facets}]
  EVIDENCE  zero rows: {z} authored check(s) compared no row[ ({ids})][, {ze} more in strata named empty]; gold queries that compared no row: {none|ids}; gold SQL with generated text: {none|ids}
  EVIDENCE  not included: {stage} ({why})[, ...]
  EVIDENCE  observation not recorded: the verify result has no readable evidence section
```

The last two lines appear only when a stage is left out or no evidence is recorded; a mutants or facets line that is
not judged reads `mutants: not judged (...)` or `facets: not judged (...)` with the reasons above. Lists are cut at 10
ids with `, and <n> more`. A tool that parses the console should ignore lines that start with `  EVIDENCE  `.

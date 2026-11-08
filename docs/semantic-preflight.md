# Offline business contracts and structured-plan preflight

`semantic-preflight` checks whether a host-authored business-definition contract
agrees with configured measure/Gold/Cube declarations and whether a host-authored
structured plan agrees with that contract. It is an optional offline diagnostic.
It generates or executes no SQL, makes no DB, network or model call, and adds no
runtime dependency or default required operational stage. Existing defaults
remain unchanged. Since v0.21.0 explicitly configured paired sidecars can require
current preflight evidence in [report](contract-report.md).

**`review: reviewed` is an unauthenticated caller assertion, not reviewer approval.
Unit, population, denominator, time basis and timezone are authored assertions;
their agreement does not prove the SQL implements them. A plan period does not
bind SQL parameters or verify SQL date filtering.** A matching pair of documents
may share the same mistake.

## Run the synthetic example

From the repository root:

```text
python skills/semantic-crosscheck/scripts/sxc.py semantic-preflight --config examples/semantic-preflight/crosscheck.toml --contract examples/semantic-preflight/contract.json --plan examples/semantic-preflight/plan.json --out .sxc-semantic-preflight
```

The self-contained [configuration](../examples/semantic-preflight/crosscheck.toml),
[contract](../examples/semantic-preflight/contract.json) and
[plan](../examples/semantic-preflight/plan.json) describe synthetic `part` and
`stock` declarations. The metric is `on_hand`, grouped by `part_id`, with generated
Cube member `m_on_hand.value` and Gold entry `q01_stock`. Its context explicitly
uses `not_applicable` for denominator and time fields. Its `reviewed` value is
only an example assertion. No fixture database or credentials are needed.

The configured [Gold file](../examples/semantic-preflight/gold/on_hand.sql) exists
so normal configuration metadata validation succeeds. Its content is neither read
nor executed by this diagnostic; the example does not certify that SQL.

`--config`, `--contract` and `--plan` must each be supplied exactly once. There is
no implicit configuration fallback. Optional `--out DIR` selects the result root,
and `--quiet` prints only the summary. Pass each argument as a separate argv
element. Argument abbreviation, unknown flags, duplicate flags and malformed
arguments are rejected without output writes, including flags before the command.

## Contract version 1

The top-level object has exactly `version: 1` and `metrics`. `metrics` contains
1 to 64 objects with exactly the following fields:

| Field | Required value |
|---|---|
| `id` | Unique public identifier matching `[a-z][a-z0-9_]{0,63}` |
| `label` | Nonblank human text, at most 160 characters |
| `aliases` | Array of at most 16 nonblank strings, each at most 160 characters; an empty array is allowed |
| `definition` | Nonblank human text, at most 2048 characters |
| `review` | `candidate` or `reviewed`; a caller assertion |
| `binding` | Exactly `measure`, `gold`, `cube_member` |
| `context` | Exactly the seven fields below |
| `period_kind` | `range`, `as_of` or `none` |

The measure and Gold entry must exist in `crosscheck.toml`. The member must be
`m_<measure>.value` under the existing naming API and occur in both the selected
Gold entry's `cube_query.measures` and `map` values. All contract bindings are
checked, including metrics the plan does not select. Definition text is not
parsed as executable instructions or SQL. No synonyms are inferred.

Every context field is a nonempty public identifier with the same grammar as
`id`; use `not_applicable` explicitly where appropriate.

| Context field | Comparison and limitation |
|---|---|
| `unit` | Authored unit identifier; no conversion or SQL unit inference |
| `grain` | Must equal the configured measure's `by` column |
| `population` | Authored population identifier; filters are not derived from its text |
| `aggregation` | Must equal the configured measure's `agg` |
| `denominator` | Authored denominator identifier; ratio or denominator SQL is not proved |
| `time_basis` | Authored time identifier; no association with configured date/SQL semantics is inferred |
| `timezone` | Authored timezone identifier; no timezone conversion or runtime check |

Label and alias lookup applies Unicode NFC normalization, surrounding-whitespace
trimming and casefolding. Different metrics may share a normalized term; that
term then requires an explicit metric ID in a plan. An explicit ID is matched
exactly, without label normalization. Candidate metrics may be stored, but cannot
pass when selected.

## Plan version 1

The top-level object has exactly `version: 1` and `selections`, with 1 to 64
selection objects. A selection accepts only `metric_id`, `term`, `binding`,
`context` and `period`. These fields may be absent to represent an incomplete
plan; present fields still require valid bounded types. Empty selections do not
create vacuous success.

Provide an explicit `metric_id` or a uniquely resolving declared label/alias in
`term`. When both are supplied, the term must belong to that same metric. An
unknown ID, or an unknown/ambiguous term used without an ID, requires
clarification. A term that conflicts with an explicit known ID fails; it cannot
override that ID.

A complete selection repeats all three binding values and all seven context
values exactly. Partial binding/context objects require clarification; unknown
fields are invalid. A selected candidate also requires clarification. The
period must be one of these exact shapes, with real `YYYY-MM-DD` calendar dates:

```json
{"kind": "range", "start": "2024-02-29", "end": "2024-03-01"}
```

A range is start-inclusive and end-exclusive, with `start < end`.

```json
{"kind": "as_of", "date": "2024-02-29"}
```

```json
{"kind": "none"}
```

The kind must equal the selected metric's `period_kind`. Invalid dates,
nonincreasing ranges or extra/missing fields in a supplied period are errors.
A valid period is only a declaration; it is not linked to Gold parameters or
Cube `timeDimensions`, and no SQL date filter is examined.

For clarification behavior, remove the example selection's `period`, or change
its metric to `candidate` in the contract. For a declaration conflict, change
only the plan's `context.unit` or `context.denominator`. These changes cannot
produce a passing plan. Correct the authored inputs after reviewing the intended
meaning; do not treat changing `review` to `reviewed` as independent evidence.

## Status, evidence and input boundary

| Outcome | Status / exit |
|---|---|
| Valid contract bindings and complete, matching reviewed selections | `pass` / 0 |
| Missing information, unresolved/ambiguous metric selection or selected candidate | `not_run`, reason `needs_clarification` / 1 |
| Configured binding/grain/aggregation disagreement or a complete conflicting plan declaration | `fail` / 1 |
| Malformed JSON/schema, unsafe inputs/outputs or changed inputs | Sanitized error; malformed input/arguments exit 2; no passing result |

A failed contract cannot authorize a passing plan. Errors, failures and incomplete
checks retain the existing result precedence; `not_run` never becomes `pass`.
Result evidence is written to `<out>/semantic_preflight/result.json`. It contains
positional check IDs, counts, fixed reason enums and SHA-256 bindings for captured
config/contract/plan bytes plus the generator. It omits supplied metric IDs,
labels, terms, definitions, selected values, SQL, credentials and local paths.
Read the exit code as well as the result; an older artifact is not current proof.

Each of the three files is limited to 256 KiB. JSON rejects duplicate keys,
NaN/Infinity, invalid UTF-8, nesting beyond 16 and more than 10000 nodes. Boolean
`true` is not version integer `1`. Unknown fields, invalid types and empty
metrics/selections are errors. Configuration is parsed from the captured bytes
with the existing validation rules; Gold existence checks remain metadata-only,
and no Gold or revision SQL body is read.

Input capture checks descriptor/path identity and rejects links, reparse points,
hardlinks, nonregular files and linked ancestors. Only the validated standard
macOS `/tmp` and `/var` aliases receive their existing platform exception. Input
paths and descendants are protected from output paths and `.gitignore`,
including stale output aliases. Output links/hardlinks are rejected. Guards
repeat before cleanup and publication, and atomic result writing follows those
checks. Unsafe aliases or input drift suppress publication rather than writing
evidence for different bytes. These are observed local TOCTOU checks, not a lock
against hostile concurrent ancestor replacement.

## Operational limits and upgrade

This checks agreement among authored declarations and configured metadata. It
does not authenticate review, certify a business definition, parse SQL meaning,
prove units/populations/denominators, verify generated Cube behavior, demonstrate
query correctness or establish freshness of real source data. It adds no LLM,
GPU, external toolkit or service dependency.

Without a paired `[semantic_preflight]` configuration this diagnostic remains
outside operational report authority. With that explicit opt-in, report requires
current matching successful evidence in addition to the ordinary required
`build`/`reason`/`cube`/`verify` stages. It cannot replace them or override a
non-pass. See [binding and freshness](contract-report.md). Its explicit
`not_proven` disclosures remain material.
Adding the new Python engine code changes the existing engine fingerprint, so
rerun operational stages after upgrading; the unchanged default stage order
does not preserve old execution evidence as current.

## Existing references reused for this application

NVIDIA's [October 21, 2026 Agent Toolkit article](https://blogs.nvidia.com/blog/enterprise-platforms-agent-toolkit/)
is enterprise-platform context. [NVIDIA Auto Ontology at `c4495670947eb7b4616f0dd9900e5ced2a48ca1e`](https://github.com/NVIDIA/auto-ontology/tree/c4495670947eb7b4616f0dd9900e5ced2a48ca1e)
provides a conceptual reference for explicit definitions and completeness review,
including its [query-validation guidance](https://github.com/NVIDIA/auto-ontology/blob/c4495670947eb7b4616f0dd9900e5ced2a48ca1e/skills/nvidia-ontology-query/references/query-validation.md)
and [interactive completeness implementation](https://github.com/NVIDIA/auto-ontology/blob/c4495670947eb7b4616f0dd9900e5ced2a48ca1e/auto_ontology/retrieval/interactive/completeness.py).

Both are existing [Reference Atlas](https://technoetic.github.io/reference-atlas/)
references, previously applied to agentic-vault definitions contracts at the
older Auto Ontology pin `2a24f5d34ef6b6e168a7f22d308504d3ab36a198`. This application
checks measure/Gold/Cube declarations and structured selections in
semantic-crosscheck. The newer upstream pin records packaging/version changes;
it is not evidence of a new semantic capability. No NVIDIA source code is copied,
no NVIDIA runtime is incorporated, and no upstream benchmark or performance
result is reproduced or claimed.

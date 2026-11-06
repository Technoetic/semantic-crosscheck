# Business contract and structured-plan preflight

User authorization: implement and publicly release the NVIDIA-inspired C1+C2 proposal on 2026-11-06. NVIDIA is an existing reference, reused for a new semantic-crosscheck application; no NVIDIA code or runtime is incorporated.

## Scope and authority

Add one optional offline diagnostic, `semantic-preflight`. It checks a host-authored business-definition contract against existing measure/Gold/Cube declarations and checks a host-authored structured plan against that contract. It does not generate or execute SQL, contact a database/model/network, certify a reviewer, prove a definition true, or become a required operational report stage. Preserve existing report pipeline, statuses and all default commands. C3 operational report binding is deferred. New Python engine files naturally invalidate existing engine fingerprints after upgrade.

## Contract version 1

Strict JSON object with exactly `version: 1` and nonempty `metrics` (maximum 64). Each metric has exactly:

- `id`: unique public ID `[a-z][a-z0-9_]{0,63}`.
- `label`: nonblank human text, maximum 160 characters.
- `aliases`: at most 16 nonblank human strings of maximum 160 characters. Label/aliases resolve by NFC, trim and casefold. Cross-metric collisions are allowed but require an explicit metric ID in a plan. Do not infer synonyms.
- `definition`: nonblank human text, maximum 2048 characters.
- `review`: `candidate` or `reviewed`; this is a caller assertion, not authenticated approval.
- `binding`: exactly `measure`, `gold`, `cube_member`. Measure and Gold must exist; the member must equal `naming.member(naming.cube_measure(measure.name), naming.MEASURE_VALUE)` and be present in the selected Gold's `cube_query.measures` and `map.values()`.
- `context`: exactly `unit`, `grain`, `population`, `aggregation`, `denominator`, `time_basis`, `timezone`; each a nonempty public ID (same grammar). `aggregation` must equal the configured measure's agg and `grain` its by column. `not_applicable` explicitly represents absence where appropriate. Population, unit, time and denominator are authored assertions, not derived SQL facts.
- `period_kind`: `range`, `as_of` or `none`. Range uses an explicit start-inclusive, end-exclusive calendar-date interval; as_of is one calendar date; none is explicit inapplicability. These values do not bind SQL parameters or verify SQL filtering.

Contract malformed shape/type/unknown fields/duplicate IDs are errors. Missing configured measure/Gold, wrong generated member, wrong Gold membership or aggregation/grain disagreement are deterministic failed contract checks. Candidate metrics may be stored but may not pass a selected plan.

## Plan version 1

Strict JSON object with exactly `version: 1` and nonempty `selections` (maximum 64). Each selection accepts only `metric_id`, `term`, `binding`, `context`, `period`; all may be missing to represent an incomplete plan. Present identifiers/text must be correctly typed and bounded. Binding/context accept only the contract field names; partial fields are allowed and require clarification.

Resolve an explicit ID exactly; if term is also given it must match that metric's declared label/alias. Without ID resolve term against all metrics: zero/multiple matches require clarification. A selected candidate, absent selection/binding/context/period, unknown metric/term or ambiguity yields existing `not_run` with fixed reason `needs_clarification`. A complete but conflicting binding/context/period-kind/term yields `fail` with fixed reason. Invalid period syntax/order or unknown JSON fields are errors. Period syntax: `{kind: range, start: YYYY-MM-DD, end: YYYY-MM-DD}`, `{kind: as_of, date: YYYY-MM-DD}`, or `{kind: none}`; type bool is never accepted as integer version. Validate real calendar dates and start < end. All seven context values and all three binding values must exactly match the selected metric. No vacuous success for empty collections.

## Core API

`business_contracts.py` exposes `evaluate(cfg, contract_document, plan_document) -> Result` with stage `semantic_preflight`. It is pure apart from constructing Result timestamps: no input reads, file writes, SQL, DB or model imports. It adds bounded index-based checks and counts/fixed enums only. No raw label, term, definition, selected value, user ID, path, SQL or credential appears in Result, exceptions or summary. Contract validation failure prevents plan passing; total malformed input raises `StageError` with a fixed safe message and config exit 2. Fixed deterministic binding mismatches produce fail checks. Add explicit `not_proven` limitations.

`config.loads(raw: bytes, path: str|Path) -> Config` parses captured UTF-8/BOM bytes with identical validation to `config.load`, without rereading the TOML. Preserve current load behavior and all existing validations; Gold SQL existence metadata checks remain, but no Gold/revision SQL bytes are read.

## CLI and IO boundary

`sxc.py semantic-preflight --config crosscheck.toml --contract FILE --plan FILE [--out DIR] [--quiet]`. No argument abbreviation. A dedicated early CLI route bypasses generic engine/Gold fingerprint reads. Malformed argv, including prefixed global flags, duplicates and unknown flags, returns sanitized error with no output writes. The CLI accepts each config/contract/plan/out only once. Result is written to `OUT/semantic_preflight/result.json`; inputs contain only captured SHA-256 digests and generator digest, plus sizes/counts as appropriate.

Capture all three inputs under a 256 KiB per-file limit using descriptor/lstat identity checks. Reject links, reparse points, nonregular files, hardlinks and linked ancestors; support only existing validated standard macOS /tmp and /var directory aliases. Strict JSON rejects duplicate keys, NaN/Infinity, invalid UTF-8, excessive nesting (16) or node count (10000). Protect input paths and input descendants against every output path and `.gitignore`, including existing stale result aliases. Reject output file hardlinks and linked output directories. Before cleanup and before every result/error publication, recheck input content/identity and path guards. If any input changes, return fixed safe error and do not publish a result for different bytes. This is observed local TOCTOU protection, not a lock against hostile concurrent ancestor replacement. Error handling must not echo TOML parser content or raw file data. Avoid predictable temporary file writes; use Result.write atomically after guards. Do not alter source inputs.

## Evidence and release

Meaningful tests cover incomplete/ambiguous/candidate selections, misleading aliases, denominator/unit/time/population/grain/aggregation mismatches, wrong Gold/Cube bindings, Unicode normalization, period boundaries, zero selections, strict parser budgets, hostile output aliases, changed inputs, repeated/malformed argv, output redaction and proof that no DB/model/network/SQL-content reads occur. Keep existing default pipeline/report tests passing. Add synthetic part/bom_line/sales_order/stock examples only. Release v0.20.0 after local checks and all eight exact-main and exact-tag CI jobs, independently verify native plugin/archive/draft/public assets and immutable release. Update existing two NVIDIA sources in Reference Atlas with pinned new SXC uses, preserve existing images and independently validate before Pages publication. No Sites hosting or company database access.

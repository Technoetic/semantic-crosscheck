# What a pass means: evidence kinds, mutant attribution and zero-row disclosure (design plan)

**Goal:** A reader of a report can tell what kind of evidence every check is, a passing V4 check can no longer be
read as "the gold SQL detects this change", every element without authored evidence is named, and a gold query
that compared no row is disclosed.
**Architecture:** Disclosure by default, verdicts on opt-in. One evidence-kind registry for every check id; verify
records what only it can know (per gold query: generated text, compared members, comparisons with rows, rows with
a non-empty value per compared member; per mutant: who detected it; per facet of a measure, hierarchy or class
dimension: authored evidence or the reason there is none);
report summarises it in `report.json`, a `report.md` section and `  EVIDENCE  ` console lines, only when a verify
result exists. `verify.gold_observation = "require"` turns the disclosures into check statuses.
**Tech stack:** Python 3.11+, existing PostgreSQL and Cube adapters. No new dependency, no extra query, no extra
Cube start.
**Released in:** 0.23.0, with the choices of section 12: disclosure by default, verdicts only in the opt-in require
mode, and the require mode in the same release.

Conventions. This is the design plan as implemented; where the implementation differs, see "Implementation notes"
below. `E/` = `skills/semantic-crosscheck/scripts/semantic_crosscheck/`. Every statement about the code before this
change (v0.22.1, commit `3e76f093`) cites file and line. Claims about demo runs that need PostgreSQL and Cube were
expectations when the plan was written; they now give the values measured on the clean demo in one CI integration
run of the implementation branch, marked **(measured)**. One static probe was run for this plan, on a copy of the
engine and the demo configuration (facts in 3.3).

## Global constraints

- Status set stays closed: `pass`, `fail`, `error`, `not_run` (`E/result_contract.py:13`, `E/result.py:29-31`).
  No `not_proven` status. not_proven lines never change a status (`docs/design.md:106-109`, `SKILL.md:162`).
- Fail-closed contract and exit codes unchanged (`E/result.py:51-66`). A check that did not run is never a pass.
  No rewrite in this design makes a status less severe: require mode only turns `pass` into `fail` or `not_run`.
- Results, console lines and report outputs hold counts, check ids, configuration ids, Cube member names and small
  enums only. Gold SQL text is read for matching and never stored.
- Build-only runs (the compatibility goldens `tests/fixtures/compat_v0_21_1/`) stay byte-identical: every report
  addition needs a verify result; build, reason and cube outputs do not change.
- The optional `data-rules` stage, `expect` and standalone diagnostics stay outside report authority
  (`E/report.py:71-72`, `E/expect.py:40-43`).
- Clean-demo numbers and README pins are set from one integration run, never from lower bounds.

## 1. The problem, with code evidence

### F1 (high). A passing V4 check does not mean a gold query detected the mutant

| Fact | Code |
|---|---|
| The suite is every gold item (`v1:<g>` first binding, `v2:<g>:<s>[<j>]` first `verify.mutant_bindings` per stratum), then every V3 test | `E/stage_verify.py:238-244`, `:255-256`, `:280-281` |
| Any failing suite item kills the mutant: `fail` -> check `pass`, `killed_by` = item | `E/mutate.py:426-433`; suite stops at the first non-pass item, `E/mutation_coverage.py:65-69` |
| Every configured measure gets a `v3:total` test whether or not a gold query reads it | `E/shape_q.py:103-118` |
| The V3 total's SQL side keeps the configured WHERE; filter mutants rewrite only the Cube SQL | `E/shape_q.py:303-307`; `E/mutate.py:237-249` |
| `measure_agg_to_max` changes only the Cube measure type, which `v3:total` sees | `E/mutate.py:294-298` |
| Gold items cannot see `sum -> max` on a cube pre-aggregated per key; the clean demo still passes all 12 V4 checks, so V3 items kill at least 3 of them (measured: exactly the 3 `measure_agg_to_max` kills) | `tests/test_mutate.py:654-656`; `README.md:366` |
| Nothing checks the prefix of `killed_by`; only survivors are rewritten | `E/stage_verify.py:448-461` |
| No output lists measures or members no gold query reads. `_members` is used only for gating and `members_not_in_model` | `E/stage_verify.py:100-115`, `:220-231` |
| Passing checks, and so `killed_by`, never reach `report.md` | `E/report.py:311-313` |
| Documents say a V4 pass is a gold detection | `README.md:158`, `:650`; `SKILL.md:165-166` |

Unit reproduction (demo without `q03_stock`, operator `measure_normalization_drop`, witness
stubbed to 2): `v4:measure_normalization_drop:on_hand` passes with `killed_by=v3:total:m_on_hand.value`, no
not_proven line, and no gold query reads `m_on_hand`.

### F2 (medium). Agreement among generated layers is not separated from independent evidence

| Fact | Code |
|---|---|
| `cube_gen.where_sql(m)` builds the WHERE of the Cube measure (undated and dated) | `E/cube_gen.py:68-75`, `:133`, `:142` |
| The same call builds the V3 total SQL, the V3 additive NULL-group SQL and the pair existence witness | `E/shape_q.py:307`, `:344`, `:543` (its note admits it, `:687-694`) |
| The docstring calls the total SQL "independent of the Cube model" | `E/shape_q.py:304` |
| On the demo, each measure's `where_sql` occurs verbatim on both sides of `v3:total` | static probe on the demo configuration |
| report shows counts per stage, failing checks and not_proven lines only; gold SQL is named as trusted code, nothing more | `E/report.py:337-397`, `:68-70`, `:243-247` |
| Gold SQL independence is a README recommendation, not a check | `README.md:92` |

### F3 (medium). Both sides empty passes V1 and V2 without a not-proven mark

| Fact | Code |
|---|---|
| `compare_rows` with zero rows on both sides: pass, `empty: True` | `E/compare.py:180-193` |
| V1 records the status and detail, adds nothing to not_proven | `E/stage_verify.py:364-366` |
| V2 counts `empty_bindings`, `rows_compared` (gold rows); only a stratum with no binding gets a line | `E/stage_verify.py:161-170`, `:376-378` |
| Intended and documented; the demo's `empty` stratum is empty by construction (8 bindings) | `docs/design.md:562-563`; `E/sweep.py:34`; `tests/test_verify.py:283` |
| The fix scope is "no binding of one gold query compared any row", not per binding or stratum | per binding or stratum, the demo's `empty` stratum would always be named (above) |

## 2. Goals and non-goals

Goals:

- G1. Every check has one evidence kind a reader can see: authored, recomputed, generated (or structural, mutation).
- G2. A passing V4 can no longer be read as "the gold SQL observes this element". Each mutant's detection is
  attributed to the checks that detected it, and every metric or element that no independent evidence observes is
  named as not proven. Two limits, stated here and in "What this design does not solve": attribution names the
  first detecting item and its kind only, because the suite stops at the first non-pass item
  (`E/mutation_coverage.py:65-69`) and later items do not run (full attribution is rejected, see "Alternatives rejected");
  "element" means the facets of 3.3 (measures, hierarchies, the measure joins of the model and the class
  dimension), not every dimension of every entity cube.
- G3. A gold query whose comparisons all had zero rows on both sides is disclosed as not proven.
- Related, nearly free: gold SQL that contains generated text (output schema, the bare name of a generated
  relation, a generated filter clause) is detected and counted as generated.

Non-goals: accepting existing Cube models, ontologies or SHACL files as input; proving gold SQL correct or
independent beyond verbatim generated text; reading `expect`, `data-rules` or diagnostics in report; replaying the
closure oracle against mutants; other silent empty passes (`shacl:<shape>` with no keys, `v5:closure:<h>` with no
pairs, an unfiltered `v3:pair` over zero keys); entity dimensions other than the class column; unrelated fixes;
repository settings.

## 3. Chosen design

Three candidate designs were compared: disclosure without a default verdict change, separate evidence classes,
and fail-closed verdicts by default. The chosen design is disclosure by default with verdicts on opt-in. From the
others it takes a separate `mutation` kind, every status counted per kind, per-gold comparison counts, facet
reason codes, `  EVIDENCE  ` console lines, `observation_recorded`/`excluded_stages`, downgrade-only kinds,
`cube_gen.where_clauses`, facets including joins and node cubes, output-schema detection, the opt-in verdict
mode, the V3 guard and import-closure tests, and remedies in the line texts.

### 3.1 Evidence kinds (closed set)

| Kind | One side is | Example |
|---|---|---|
| `authored` | rows of a gold SQL file a person wrote, with no generated text in it | `v1:<g>`, `v2:<g>:<s>` |
| `recomputed` | recomputed from source rows by engine code that imports no generator and reads no generated artifact on that side | `v3:closure_oracle:<h>` |
| `generated` | both sides come from `crosscheck.toml` through generators, or one generated layer is compared with itself | `v3:*`, `shacl:<shape>`, `v5:closure:<h>`, `closure_apply:<h>`, gold with generated text |
| `structural` | there is one side only: a rule on data, configuration, environment, served model, scope, bookkeeping | `shape:<id>`, `cube:meta`, `scope:*` |
| `mutation` | a V4 mutant check; its strength is in `detection` | `v4:<op>:<target>` |
| `unclassified` | no registry rule matched; never counted as authored or recomputed | (a new id family) |

"Authored" says who wrote one side. It does not say the gold SQL is right or independent of the configuration.
"Recomputed" is independent of generator code, not of the configuration entry (the oracle reimplements the
builder's rules, `E/closure_oracle.py:5-12`). A second engine is not enough: `v5:closure:<h>` runs HermiT, but its
OWL side is built by the ontology generator (`E/owl.py:8-16`, imported at `E/stage_reason.py:42`) and its SQL side
reads the generated closure table (`E/owl.py:231-233`, `:301-311`), so it is `generated`.

### 3.2 Gold queries

At plan time verify reads each gold file once (the same file `inputs_fingerprint` hashes, `E/result.py:100-114`):

- `generated_text`: list of matches of rule 5.2. Non-empty -> the gold's kind is `generated`; its V1/V2 checks
  count as generated and its suite items rank after authored gold items (4.3).
- `compares`: `set(g.map.values())`, the members whose values `compare_rows` compares (`E/compare.py:155`,
  `:175-177`). `_members(g)` (`E/stage_verify.py:100-115`) also holds filter, time-dimension and segment members;
  those are `uses` only. `_members` stays as it is for gating.
- `comparisons`: comparisons that ran without error (V1: 1; V2: `bindings - error_bindings` per stratum).
- `nonempty_comparisons`: comparisons with a row on either side (`comparisons - empty`, from `empty`,
  `E/compare.py:191`, and `empty_bindings`, `E/stage_verify.py:164`).
- `value_rows`: per compared member, the matched rows (`E/compare.py:166-175`) whose gold value is not NULL, not
  numerically 0 and not an empty string, summed over the comparisons that ran. `compare_rows` gains this count as a
  new key; `_detail` (`E/stage_verify.py:131-132`) and `_Sweep.detail` leave it out, so V1 and V2 check details do
  not change; `_Sweep` sums it internally. It is a count, so no value leaves the comparison. Reason: `compare_rows`
  turns a Cube NULL into 0 for `zero_if_null` columns (`E/compare.py:156-163`) and gold SQL usually writes
  `coalesce(..., 0)` (`demo/gold/q02_where_used.sql`), so rows can be compared while no value of a measure was seen.

Gold kinds are stored in a `_Verify` attribute set at the top of `plan_gold`, before its early returns
(`E/stage_verify.py:224-231`), so gated and unplanned golds have a kind too. A gold id with no stored kind (for
example items a unit test sets directly, `tests/test_compare_unit.py:591-617`) has item kind `unclassified`.

### 3.3 Facets

A facet is a part of a measure, hierarchy or class dimension that a gold query can compare. Built from the
configuration, and for the class dimension from the entity cubes of the model verify reads (`E/cube_gen.py:122-176`,
`:193-201`, `E/naming.py:10-14`, `E/shape_q.py:221-223`). The generated model holds no other
joins: entity cubes are created with no joins (`E/cube_gen.py:111`), the only joins are node -> measure
(`:196-201`) and closure -> ancestor, descendant and joined measures (`:162-167`). A facet whose cube is missing
from the model (closure cubes are skipped when the closure table is missing at cube time, `E/cube_gen.py:204-207`)
is kept with the gap reason `not_in_model`.

| Element | Facet id | Compared by gold g when `compares(g)` holds | Mutated by |
|---|---|---|---|
| `measures.<m>` | `m_<m>.value` | it | `measure_filter_drop`, `measure_normalization_drop`, `measure_agg_to_max` |
| `measures.<m>` with `date_column` | `m_<m>.ts` | it | `dated_shift_one_day` |
| `measures.<m>` with `closure_join` | `<h>_closure+m_<m>` | a member of `<h>_closure` and `m_<m>.value` | `closure_join_swap` |
| `measures.<m>` with a node, no `date_column`, single-column node key | `<node>+m_<m>` | a member of the node entity cube `<node>` and `m_<m>.value` | none |
| `ontology.class_column` (when set) | `<e>.<class_column>` for every entity cube `<e>` whose dimensions hold the class column | it | none |
| `hierarchies.<h>` | `<h>_closure.qty` | it | `closure_qty_single_path` |
| | `<h>_closure.path_count` | it | `closure_path_count_one` |
| | `<h>_closure.min_depth` | it | `closure_depth_shift` |
| | `<h>_closure.max_depth`, `<h>_closure.pairs` | it | none |
| | `<h>_ancestor.*` | any member of that cube | none |
| | `<h>_descendant.*` | any member of that cube | `closure_end_swap` |

Facet status, first that applies:

1. `compared`: an authored gold compares it, has `nonempty_comparisons > 0` and `value_rows > 0` for a member of
   the facet (for a join facet: for `m_<m>.value`). This means "compared with rows that hold a value", not "a wrong
   value would be seen".
2. `detected`: a V4 check on this facet has `detection = authored`. Only this shows that a change was seen.
3. `accepted`: the facet or its element is listed in `verify.accepted_gaps`.
4. `gap` with a reason, first that applies:
   `not_in_model` (its cube is not in the model), `no_values` (authored golds compared rows, but every gold value
   of the facet was NULL, 0 or empty), `no_rows_compared` (authored golds compare it, ran, all comparisons empty),
   `gold_not_run` (authored golds compare it, none ran: gated, plan problem, no binding, every comparison errored),
   `generated_text` (only golds with generated text compare it), `filter_only` (authored golds use it only in
   filters, time dimensions or segments), `no_gold_reads` (no gold uses it).

Facets are judged only on full-scope runs (`--only` all parts, no `--gold`). A narrowed run never passes
(`E/stage_verify.py:330-338`) and existing exact not_proven lists of narrowed unit tests stay valid
(`tests/test_compare_unit.py:542`, `:563`, `:573`). On a narrowed run the facet outputs say so explicitly
(`"facets": null`, `"facets_reason": "narrowed"`, line "facets: not judged (narrowed verify run)"); they never
show "0 of 0".

Demo facts (static probe on the demo configuration, real `config.load` and `stage_verify._members`, extended by
reading for the join and class facets): 17 facets. 15 are mapped by an authored gold (the probe listed 11;
`has_component_ancestor.*` and the closure join facet follow from the map of `q02_where_used`; `part+m_on_hand`,
`part+m_consigned` and `part.part_class` from the map of `q03_stock`, `demo/crosscheck.toml:166-178`).
`part+m_firm_open` is a gap `no_gold_reads` by reading: the node cube `part` joins `m_firm_open` one_to_one
(`E/cube_gen.py:196-201`; `firm_open` gets node `part` by default, `E/config.py:633-634`), and no demo gold maps a
`part` member together with `m_firm_open.value` (`q02_where_used` reaches it through the closure join,
`demo/crosscheck.toml:146-161`). **(measured)** On the clean demo 15 facets are `compared` with rows holding
values and `part+m_firm_open` is the one gap (`no_gold_reads`).
`m_firm_open_dated.ts` is used only in the `timeDimensions` `dateRange` of `q05_firm_window`, so it is `filter_only` unless `v4:dated_shift_one_day:firm_open_dated` is killed
by a gold item. With hand-picked bindings it is killed by a `v1:` item (`tests/test_mutate.py:644-650`).
**(measured)** With the verify sweep bindings it is killed by the authored item `v2:q05_firm_window:param_sets[1]`, so
the facet is `detected`. The filter-only members of q01 and q02
(`has_component_closure.ancestor`, `.descendant`) are compared by the other query, so they leave no gap.

### 3.4 Mutant attribution

- Item kind: `v1:<g>`, `v2:<g>:<s>[<j>]` -> the gold's stored kind (`authored` or `generated`; `unclassified` when
  no kind is stored, 3.2); `v3:...` including `:chunk[<i>]` and `:null` suffixes (`E/shape_q.py:996-997`) ->
  `generated`; anything else -> `unclassified`.
- Suite order is enforced by a stable sort before `Suite` is built: rank 0 authored gold items and gold items of
  unknown kind, rank 1 generated gold items, rank 2 everything else. Without a generated gold this is today's
  order (`E/stage_verify.py:280-281`), so the suite runs the same items in the same order and every V4 status and
  `killed_by` stays as today; `tests/test_compare_unit.py:591-617` (golds `a`, `b` unknown to the plan) keeps its
  order. With a generated gold the order changes, so `killed_by` can change, and V4 statuses can change too: an
  item that errors twice ends the suite as `error` (`E/mutation_coverage.py:47-69`, `E/mutate.py:436`), and the
  item budget ends it as `not_run` `suite_budget` (`E/mutation_coverage.py:39-43`, `E/mutate.py:434-435`), so a
  different first item can reach either before the item that failed first today. This is the one default status
  change of the design; it needs a gold that contains generated text (5.2). Keeping today's order instead would
  leave `detection` unknowable for items after a generated gold item (they do not run).
- `detection` of a V4 check with a `killed_by` key: `killed_by` non-empty and its item kind `authored` ->
  `authored`; `generated` -> `generated_only`; `unclassified` -> `unattributed`; `killed_by` empty -> `none`.
  Because authored items run first and the suite stops at the first non-pass item
  (`E/mutation_coverage.py:65-69`), `generated_only` means every replayed authored item ran and passed against
  the mutant. It is exact for the replayed bindings only (V1 first binding, first `mutant_bindings` per stratum,
  `E/stage_verify.py:238-244`); the texts say "replayed".
- `facet` (from the table in 3.3) and `gold_comparing` (authored golds that compare that facet, static).
- Mutant split, each selected mutant in exactly one bucket, first that applies, counted from the coverage ledger
  rows (`record` calls, `E/mutate.py:520-553`) and the V4 checks: `equivalent` (witness 0, no check,
  `E/mutate.py:549-553`), `not_applicable` (no check, `:530-535`), `not_judged` (V4 check with status `not_run`
  or `error`, including require-mode `suite_incomplete`), `survived` (`killed_by` empty), `accepted` (a
  `generated_only` or `unattributed` kill listed in `verify.accepted_gaps`), `killed_authored`,
  `killed_generated_only`, `unattributed`. Also recorded: `operators_selected` (k of the 9 in `E/mutate.py:69-79`)
  and `operators_not_selected` (names). `verify.mutation_operators = []` is valid today (`E/config.py:547-549`,
  `E/mutate.py:359-371`); the split then shows "0 of 9 operators selected", never a clean-looking line.
- Set in `_Verify.v4` in the loop at `E/stage_verify.py:449-461`, not in `mutate.verdict`, so `mutate.run_mutants`
  callers and their exact-dict tests stay (`tests/test_mutate.py:423`, `:454-455`). A detail without a `killed_by`
  key gets no new key (`tests/test_compare_unit.py:614-617` stays exact).

### 3.5 Modes

| | `verify.gold_observation = "disclose"` (default) | `"require"` (opt-in) |
|---|---|---|
| V4 `generated_only`, not in `verify.accepted_gaps` | `pass`, not_proven line N5 | `fail`, `reason=generated_only`; when items were left out (`self.excluded`, the condition at `E/stage_verify.py:449`): `not_run`, `reason=suite_incomplete`, with its own line N5b and next action (not the survivor text of `:459-460`, the mutant was killed) |
| V4 `unattributed` | `pass`, line N6 | `fail`, `reason=unattributed`; items left out: as above |
| V4 kill listed in `verify.accepted_gaps` | `pass`, line N5a instead of N5/N6 | `pass`, line N5a |
| facet gaps | one line N4 per element | one check `gold_evidence:<element>` per measure, hierarchy and `ontology.class_column`, full-scope runs only |
| gold with zero rows | line N3 | line N3, and a `v1:<g>` whose status is `pass` becomes `not_run`, `reason=no_rows_compared`; any other `v1:<g>` status stays (an `error` is never rewritten to `not_run`) |
| gold with generated text | line N1/N2 | same (kinds change; V1/V2 statuses do not; the V4 order changes in both modes, 3.4) |
| operators not selected | shown in the mutant split | same, plus line N7 |

`killed_by` keeps its meaning in both modes: the first failing suite item. In require mode a `fail` V4 check can
therefore carry a non-empty `killed_by`; read `detection`.

## 4. Output contract

### 4.1 Configuration (both keys are part of `config_sha256`, so changing them makes results stale)

| Key | Type, default | Validation (exit 2, like `E/config.py:545-546`) |
|---|---|---|
| `[verify] gold_observation` | `"disclose"` or `"require"`, default `"disclose"` | other values |
| `[verify] accepted_gaps` | list of strings, default `[]` | each is `measures.<m>`, `hierarchies.<h>` or `ontology.class_column` of this configuration, a facet id of 3.3, or a V4 check id `v4:<operator>:<target>` of a known operator and target |

### 4.2 `verify/result.json`

New top-level key `evidence`, only in verify results. It is not a dataclass field: `Result.to_dict`
(`E/result.py:68-72`) adds it only when set, so no other stage's file changes. `validate_result` keeps unknown
top-level keys (`E/result_contract.py:1-8`, `:24-58`).

```json
"evidence": {
 "schema_version": 1,
 "rule_version": 1,
 "mode": "disclose",
 "scope": "full",
 "gold": [
  {"id": "q05_firm_window", "kind": "authored", "generated_text": [],
   "compares": ["m_firm_open_dated.key", "m_firm_open_dated.value"], "uses_only": ["m_firm_open_dated.ts"],
   "comparisons": 7, "nonempty_comparisons": 6,
   "value_rows": {"m_firm_open_dated.key": 759, "m_firm_open_dated.value": 743}}
 ],
 "facets": [
  {"element": "measures.firm_open_dated", "facet": "m_firm_open_dated.ts", "status": "detected", "reason": "",
   "compared_by": [], "uses_only_by": ["q05_firm_window"],
   "detected_by": ["v4:dated_shift_one_day:firm_open_dated"]},
  {"element": "measures.firm_open", "facet": "part+m_firm_open", "status": "gap", "reason": "no_gold_reads",
   "compared_by": [], "uses_only_by": [], "detected_by": []}
 ],
 "facets_reason": "",
 "zero_row_checks": ["v2:q02_where_used:top"],
 "empty_stratum_zero_row_checks": ["v2:q01_explosion:empty"],
 "gold_no_rows": [],
 "mutation": {"operators_selected": 9, "operators_not_selected": [],
              "killed_authored": 9, "killed_generated_only": 3, "unattributed": 0, "accepted": 0,
              "survived": 0, "not_judged": 0, "equivalent": 2, "not_applicable": 0,
              "generated_only": ["v4:measure_agg_to_max:firm_open", "v4:measure_agg_to_max:on_hand",
                                 "v4:measure_agg_to_max:consigned"]}
}
```

**(measured)** The values are those of the clean demo, including the zero-row lists (as read from the code before the
run, `v2:q02_where_used:top` is empty on both sides: the `top` stratum binds parts with no parent, `E/sweep.py:28`;
the gold starts from edges whose child is `:part`, `demo/gold/q02_where_used.sql:7`; the closure has no depth-0 pair,
`E/closure.py:106-107`, and Cube filters on `descendant = :part`, `demo/crosscheck.toml:153`). `scope` is
`"narrowed"` on narrowed runs; `facets` is then `null` and `facets_reason` is `"narrowed"`.
The only per-check kind verify records is `gold[].kind`; it applies to `v1:<g>` and `v2:<g>:*` (4.3, 5.1).
`generated_text` items: `output_schema`, `relation:<name>` or `filter:measures.<m>`. Zero-row V1/V2 checks (V1
with `empty: true`; V2 with `bindings > 0` and `empty_bindings == bindings`; V2 with `empty_stratum`) go to
`empty_stratum_zero_row_checks` when the stratum is named `empty` (`E/sweep.py:34`: a node with no children, which
many gold queries answer with no row by design), otherwise to `zero_row_checks`. `gold_no_rows`: gold ids with
`comparisons >= 1` and `nonempty_comparisons == 0`. Lists hold ids only; lists in lines are cut at 10 ids with
`, and <n> more`.

V4 check detail, new keys (scalars appear on the console for non-pass checks, `sxc.py:285-287`):

```json
{"id": "v4:measure_agg_to_max:on_hand", "status": "pass", "config_ref": "measures.on_hand",
 "detail": {"witness": 12, "killed_by": "v3:total:m_on_hand.value",
            "detection": "generated_only", "facet": "m_on_hand.value", "gold_comparing": ["q03_stock"],
            "coverage": {"...": "unchanged; each receipt item gains kind"}, "summary": {"...": "unchanged keys"}}}
```

Require mode adds `"reason": "generated_only"` and the status is `fail`. `summary` keeps the `OUTCOMES` keys
(`E/mutate.py:82`); a require-mode `generated_only` counts as `survived`.

New check (require mode only, full scope only, one per measure, hierarchy and `ontology.class_column` when set,
after the V4 checks):

```json
{"id": "gold_evidence:measures.firm_open_dated", "status": "fail", "config_ref": "measures.firm_open_dated",
 "detail": {"reason": "filter_only", "facets": 2, "with_evidence": 1, "accepted": 0, "gaps": 1,
            "operators_not_selected": []}}
```

Status: `pass` when no facet is a gap; `not_run` when every gap is `gold_not_run` or `not_in_model` (`reason` =
the first of them); otherwise `fail` with `reason` = the reason of the first other gap in facet order. Require mode
also writes line N7 when `operators_not_selected` is not empty; the detail of every `gold_evidence:*` check carries
`operators_not_selected`, so a passing check shows which operators were dropped.

`verify/mutation-coverage.json` (`schema_version` stays 1, additive keys, `E/mutation_coverage.py:82`): each mutant
row gains `detection`; each receipt item gains `kind`. It stays outside report authority; report never reads it
(`docs/mutation-coverage.md`; no reader in `E/report.py`), so these item kinds are never used to set a check's kind.

### 4.3 `report.json`

New top-level key `evidence`, only when `verify` is among the results report read (`E/report.py:217-231`).
`stage:*` checks and their detail keys do not change.

```json
"evidence": {
 "schema_version": 1,
 "rule_version": 1,
 "included_stages": ["doctor", "demo", "build", "reason", "cube", "verify"],
 "excluded_stages": [],
 "observation_recorded": true,
 "mode": "disclose",
 "kinds": {
  "authored":   {"pass": 17, "fail": 0, "error": 0, "not_run": 0, "zero_row": 2,
                 "ids": {"verify": ["v1:q01_explosion", "..."]}},
  "recomputed": {"pass": 1,  "fail": 0, "error": 0, "not_run": 0,
                 "ids": {"verify": ["v3:closure_oracle:has_component"]}},
  "generated":  {"pass": 26, "...": "..."},
  "structural": {"pass": 40, "...": "..."},
  "mutation":   {"pass": 12, "...": "..."}
 },
 "overridden": 0,
 "mutation": {"operators_selected": 9, "operators_not_selected": [],
              "killed_authored": 9, "killed_generated_only": 3, "unattributed": 0, "accepted": 0, "survived": 0,
              "not_judged": 0, "equivalent": 2, "not_applicable": 0,
              "generated_only": ["v4:measure_agg_to_max:firm_open", "..."]},
 "facets": {"total": 17, "compared": 15, "detected": 1, "accepted": [],
            "gaps": [{"facet": "part+m_firm_open", "reason": "no_gold_reads"}]},
 "facets_reason": "",
 "zero_row_checks": ["v2:q02_where_used:top"],
 "empty_stratum_zero_row_checks": ["v2:q01_explosion:empty"],
 "gold_no_rows": [],
 "gold_generated_text": []
}
```

`included_stages`: stages in `results` whose `counted_as` is not null and that are not stale
(`E/report.py:234-240`, `:442-444`). `excluded_stages` items: `{"stage": "verify", "why": "stale"}`, `"missing"`
or `"not_counted"`. `zero_row` under `authored` counts the authored checks in both zero-row lists.

Kinds of `v1:<g>` and `v2:<g>:*` (the only checks with a recorded kind, `gold[].kind` of 4.2): `generated` when
the recorded kind is `generated` or the entry's `generated_text` is not empty; `authored` only when the recorded
kind is `authored` and `generated_text` is empty; `unclassified` when the verify result has no gold entry for
`<g>`. `overridden` = the number of gold entries whose recorded kind is above what this rule allows (recorded
`authored` with a non-empty `generated_text`, or a value other than `authored` and `generated`); such an entry
counts as `generated`. No other check takes a recorded kind; every other kind comes from the registry (5.1).

`observation_recorded` is false when verify has no readable `evidence` object (missing, or malformed); then V1/V2
count as `unclassified` (never `authored`), `mutation` holds only `not_judged` (all V4 checks), `facets` is `null`
with `facets_reason` `"not_recorded"`. Unclassified ids add a key `unclassified`.

### 4.4 `report.md`

New section `## Evidence`, placed before `## Not proven` (`E/report.py:374`), only with a verify result. Exact text
(the numbers, lists and facet results are those of the clean demo, **(measured)** in report.json `evidence` and the
EVIDENCE console lines of the CI run; the run's report.md file itself was not captured):

```markdown
## Evidence

Every check of an included stage counts once, under the kind of evidence it is (docs/design.md, section 8,
"Evidence kinds"). Only authored and recomputed checks compare generated layers with something the generators did
not produce. "Authored" says who wrote one side, not that it is right. Compared with rows is not detection: only an
authored mutant kill shows that a change of a facet is seen. Paraphrased copies of generated SQL are not detected.
Authored expected rows (`expect`) are not read by report.

| Kind | What the two sides are | Pass | Fail | Error | Not run |
|---|---|---:|---:|---:|---:|
| authored | gold SQL a person wrote (no verbatim generated text found) against the served model; 2 of them compared no row; v1 repeats the first binding of v2 | 17 | 0 | 0 | 0 |
| recomputed | a recomputation from source rows by code that imports no generator | 1 | 0 | 0 | 0 |
| generated | two layers generated from crosscheck.toml (also by a second engine), or one generated layer with itself | 26 | 0 | 0 | 0 |
| structural | one side only: a rule on the data, the configuration, the environment, the served model | 40 | 0 | 0 | 0 |
| mutation | broken copies of the model; pass = killed: 9 with an authored detection, 3 by generated checks only | 12 | 0 | 0 | 0 |

- Mutants (9 of 9 operators selected): 9 killed with an authored detection; 3 killed by generated checks only (v4:measure_agg_to_max:firm_open, v4:measure_agg_to_max:on_hand, v4:measure_agg_to_max:consigned); 0 unattributed; 0 accepted; 0 survived; 0 not judged; 2 equivalent on this data; 0 not applicable.
- Facets: 15 of 17 compared with rows holding values by authored gold SQL; 1 more detected by an authored mutant kill; no authored evidence: part+m_firm_open (no_gold_reads); accepted: none.
- Authored checks that compared no row: 1 (v2:q02_where_used:top); in strata named `empty`: 1 (v2:q01_explosion:empty). Gold queries that compared no row: none.
- Gold SQL with generated text: none.
- Stages not included: none.
```

A row `unclassified` appears only when its count is above 0. On a narrowed run the facets line reads "Facets: not
judged (narrowed verify run)."; without a recorded observation it reads "Facets: not judged (the verify result has
no evidence section)." and the mutant line reads "Mutants: not judged (the verify result has no evidence
section)."

### 4.5 Console

verify prints the new not_proven lines as `  NOT PROVEN  <text>` (`sxc.py:306-307`). report prints new lines after
its NOT PROVEN lines and before `next:`, not with `--quiet`, only with a verify result. They are carried as a
dynamic attribute, as `_contract_evidence` is (`E/report.py:494-497`), and printed by `sxc._summary`
(`sxc.py:290-311`).

```text
  EVIDENCE  checks by kind (pass of all): authored {p}/{n} ({z}+{ze} compared no row), recomputed {p}/{n}, generated {p}/{n}, structural {p}/{n}, mutation {p}/{n}[, unclassified {p}/{n}]
  EVIDENCE  mutants ({k} of 9 operators selected[; not selected: {ops}]): {a} killed with authored detection, {g} killed by generated checks only[ ({ids})], {u} unattributed, {acc} accepted, {s} survived, {j} not judged, {e} equivalent, {na} not applicable
  EVIDENCE  facets: {c} of {t} compared with rows holding values by authored gold SQL, {d} more detected by an authored mutant kill; no authored evidence: {none|facet (reason)[, ...]}[; accepted: {facets}]
  EVIDENCE  zero rows: {z} authored check(s) compared no row[ ({ids})][, {ze} more in strata named empty]; gold queries that compared no row: {none|ids}; gold SQL with generated text: {none|ids}
  EVIDENCE  not included: {stage} ({why})[, ...]                                         (only when a stage is left out)
  EVIDENCE  observation not recorded: the verify result has no readable evidence section   (only then)
```

On a narrowed run the facets line is `  EVIDENCE  facets: not judged (narrowed verify run)`. Without a recorded
observation the mutants and facets lines read `not judged (the verify result has no evidence section)`.

### 4.6 New not_proven line texts (verify; exact)

| Id | Condition | Text |
|---|---|---|
| N1 | gold text names the output schema | `gold:{g}: the gold SQL names the output schema {schema}; v1:{g} and v2:{g}:* count as generated evidence, not authored` |
| N2 | gold text contains a generated filter clause | `gold:{g}: the gold SQL contains the generated filter of {refs} verbatim; v1:{g} and v2:{g}:* count as generated evidence, not authored` |
| N3 | gold `comparisons >= 1`, `nonempty_comparisons == 0` | `gold:{g}: every comparison that ran ({n}) had zero rows on both sides; {ids} pass without comparing any row; add a binding, stratum or parameter set with rows` |
| N4 | full scope, element with a facet gap (disclose mode) | `{element}: no authored evidence for {facet} ({reason}[: {golds}])[, ...]; only checks between generated layers cover these facets; add a gold query, stratum or parameter set that compares them, or list them in verify.accepted_gaps` |
| N4a | full scope, accepted gaps (both modes) | `{element}: no authored evidence for {facets}; accepted by verify.accepted_gaps, so this is not proven` |
| N5 | V4 `generated_only` (disclose mode), not accepted | `{cid}: killed by {item}, a check between generated layers; no replayed gold item detected this change to {config_ref} ({readers}); {remedy}` |
| N5a | V4 `generated_only` or `unattributed`, listed in `verify.accepted_gaps` (both modes) | `{cid}: killed only by {item}, a check between generated layers or of no known kind; accepted by verify.accepted_gaps, so this is not proven` |
| N5b | require mode, V4 `generated_only` or `unattributed` with items left out | `{cid}: killed only by {item}, a check between generated layers; gold items were left out ({excluded}), which might have detected it; not judged` |
| N6 | V4 `unattributed` (disclose mode), not accepted | `{cid}: killed by {item}, which no evidence rule classifies; no replayed gold item is shown to have detected this change to {config_ref}` |
| N7 | require mode, `operators_not_selected` not empty | `verify.mutation_operators leaves out {ops}; require mode judges only the selected operators, so changes of those kinds are not proven to be detected` |

`{readers}` = `gold queries that compare {facet}: {ids}` or `no authored gold query compares {facet}`. `{ids}` in
N3 = `v1:{g}`, `v2:{g}:*` or both joined by ` and `, for the parts that ran.

`{remedy}` per operator, so the line says what would help:

| Operator | `{remedy}` |
|---|---|
| `measure_agg_to_max` | `a gold query that returns one row per key of {m} cannot see this change, because the measure cube holds one pre-aggregated row per key; add a gold query that aggregates {m} over more than one key` (`E/cube_gen.py:142-148`, `tests/test_mutate.py:654-656`) |
| `measure_filter_drop`, `measure_normalization_drop` | `add a gold query, stratum or parameter set whose keys have source rows that the filter of {m} excludes` |
| `dated_shift_one_day` | `add a parameter set whose date range starts or ends on a date with rows of {m}` |
| any other | `add a gold query, stratum or parameter set that would detect it` |

Order inside verify's list: N1-N3 after the V3 lines, before V4; N5, N5a, N5b, N6 after V4's own lines
(`E/stage_verify.py:468`), before the sample line (`:469-472`); N7, then N4/N4a last. N5b replaces, for this case,
the survivor line of `E/stage_verify.py:459-460`. The unbound lines stay after them (appended later, `E/runtime_provenance.py:431`,
`E/provenance.py:163`).

### 4.7 Reason codes and next actions

| Where | Codes |
|---|---|
| V4 `detection` | `authored`, `generated_only`, `unattributed`, `none` |
| V4 `reason` (require) | `generated_only`, `unattributed` (new); `suite_incomplete` (existing code, new line N5b and next action when the mutant was killed) |
| facet `status` | `compared`, `detected`, `accepted`, `gap` |
| facet `reason`, `gold_evidence:*` `reason` | `not_in_model`, `no_values`, `no_rows_compared`, `gold_not_run`, `generated_text`, `filter_only`, `no_gold_reads` |
| mutant split buckets | `equivalent`, `not_applicable`, `not_judged`, `survived`, `accepted`, `killed_authored`, `killed_generated_only`, `unattributed` |
| `v1:<g>` `reason` (require) | `no_rows_compared` (new) |

New next-action texts in `_next_action` (`E/stage_verify.py:549-579`), require mode only:

- `{cid}: only {item}, a check between generated layers, detected the mutant; no replayed gold item did ({readers}); {remedy}, or list {cid} in verify.accepted_gaps, then rerun verify (do not drop the operator)`
- `{cid}: killed only by {item}, a check between generated layers, while gold items were left out ({excluded}): make every gold query and closure run (full verify, passing builds), then rerun verify` (require mode, `suite_incomplete` after a kill; the existing survivor text at `E/stage_verify.py:522-524` stays for survivors)
- `{cid}: no authored evidence for {facets}; add a gold query that maps them and compares rows, or list them in verify.accepted_gaps, then rerun verify`
- `{cid}: the gold queries that compare {facets} did not run, or the model lacks their cube; fix their errors or gates (rerun build and cube for a missing closure cube), then rerun verify`
- `{cid}: no authored evidence for {facets}: compared rows held no value of them (NULL, 0 or empty); add a binding or parameter set whose rows hold values, then rerun verify` (reason `no_values`)
- `v1:{g}: every comparison of this gold query had zero rows on both sides; check its parameters, strata and the data, then rerun verify`

The survivor text at `E/stage_verify.py:563` says "no gold query observes this kind of error"; it becomes "no
replayed suite item detects this kind of error".

## 5. How a check's evidence kind is decided

### 5.1 Rule

Name the code behind each side, then, first that applies: one side only -> `structural`; a V4 mutant ->
`mutation`; one side is a gold SQL file (or authored rows) without generated text -> `authored`; one side reads only
source rows through code whose module-level import closure has no generator module (`cube_gen`, `closure`,
`shapes`, `shape_q`, `mutate`, `owl`) -> `recomputed`; otherwise `generated`. Sharing the configuration does not make
a side generated; sharing generator code or a generated artifact does. A second engine (HermiT) does not make a side
recomputed when its input is built by a generator. The import closure counts module-level imports only: lazy
imports inside functions (`E/result.py:105` -> `provenance` -> `runtime_provenance` -> `owl`,
`E/provenance.py:143`, `:150`, `E/runtime_provenance.py:19`) serve provenance metadata, not the expected side.

Registry `E/evidence.py` `RULES`, ordered, first match wins, no default row (no match -> `unclassified`). In code
the keys are tuples of id prefixes and stage names, not id-shaped strings, so the catalogue test that reads id
templates from engine source (`tests/test_docs_catalogue.py:230-233`) does not see them; the table below is
rendered from `RULES`, and it writes families the way the catalogue does (`v3:*`, `scope:v1`), because a
documented token such as `v3:<test>` or `scope:<part>` fails `test_documents_name_only_catalogued_checks`
(`tests/test_docs_catalogue.py:244-248`; checked by re-implementing the test's matching: these three tokens fail,
the other 25 pass):

| # | Stage | Id pattern | Kind | Code |
|---|---|---|---|---|
| 1 | any | `stage`, `data_revision:stable`, `data_revision:current`, `runtime_evidence:stable`, `runtime_evidence:current` | structural | `E/provenance.py:171-176`; `E/report.py:408-419` |
| 2 | doctor, demo, cube, semantic_preflight, report | every id | structural | one side |
| 3 | build | `shape:<id>`, `closure:<h>`, `rule_view:<id>`, `rule_subjects:view` | structural | `E/stage_build.py:170-186`; `E/closure.py:176-200` |
| 4 | build | `closure_apply:<h>` | generated | same builder, same transaction, `E/closure.py:143-169` |
| 5 | reason | `shacl:meta:generated`, `owl:consistency`, `owl:explanation`, `scope:shacl`, `scope:owl`, `v5:witness:<name>` | structural | `E/stage_reason.py:112-121`, `:370-401`; synthetic graph, `E/owl_witness.py:1-8` |
| 6 | reason | `shacl:pair:<shape>`, `shacl:<shape>`, `v5:defined:<name>`, `v5:closure:<h>` | generated | one shape entry or `kind` template, `E/stage_reason.py:254-258`, `:410-421`; `v5:closure`: OWL side from the ontology generator (`E/owl.py:8-16`), SQL side the closure table (`E/owl.py:231-233`), `E/stage_reason.py:403-409` |
| 7 | verify | `v1:<gold>`, `v2:<gold>:<stratum>` | authored; generated or unclassified by the recorded gold kind (4.3) | `E/compare.py:98-124` |
| 8 | verify | `v3:closure_oracle:<h>` | recomputed | `E/closure_oracle.py:1-16`, `:25`, `:339-340` |
| 9 | verify | `v3:*`, `v3w:*` (after row 8) | generated | `E/shape_q.py:303-349`, `:543`, `:843-875` |
| 10 | verify | `v4:suite`, `scope:v1`, `scope:v2`, `scope:v3`, `scope:v4`, `scope:gold:<gold>`, `gold_evidence:<element>` | structural | `E/stage_verify.py:330-338`, `:421-428` |
| 11 | verify | `v4:<operator>:<target>` | mutation | `E/mutate.py:426-436` |

The table is also a new subsection "Evidence kinds" of `docs/design.md` section 8 (Report). It is not placed in
section 6: the catalogue test reads every table row of section 6 as a check row
(`tests/test_docs_catalogue.py:43-53`).

Downgrade only: report takes kinds from the registry. The only recorded kind is `gold[].kind` of the verify
`evidence` object, applied to `v1:<g>` and `v2:<g>:*` (rule in 4.3). It can lower `authored` to `generated`, never
raise anything; a recorded kind above what the rule allows is ignored, the gold counts as `generated`, and the
entry is counted in `overridden`. Without a gold entry the checks count as `unclassified`, never `authored`.

Every `recomputed` row must name a module whose module-level import closure lies inside a pinned set. Today that
holds for row 8 only: the closure of `closure_oracle` is `canon`, `compare`, `constraints`, `cube_client`, `db`,
`ident`, `naming`, `result`, `result_contract` (top-level imports only; with function-level imports it reaches
`owl`).

### 5.2 Generated text in gold SQL

Gold text: comments (`--` to end of line, `/* */`) removed outside single-quoted literals, whitespace runs
collapsed to one space. No other normalisation.

1. `output_schema`: regex `(?<![A-Za-z0-9_"])"?<schema>"?\s*\.`, case-insensitive. The schema is a plain
   identifier (`E/config.py:505-506`). Reading generated tables or views is never independent.
2. `relation:<name>`: the bare name of a generated relation as an identifier, regex
   `(?<![A-Za-z0-9_."])"?<name>"?(?![A-Za-z0-9_])`, case-insensitive. Names: the table part of
   `naming.closure_table` and `naming.cycles_table` per hierarchy (`<h>_closure`, `<h>_cycles`,
   `E/naming.py:22-29`), `naming.shape_view` per shape (`v_shape_<id>`, `:32-34`), and the rule views
   `v_rule_<id>` and `v_rules_subjects` (`E/stage_build.py:20-22`). Reason: gold SQL runs with the connection's
   own `search_path` (`E/compare.py:98-107`; the engine sets `search_path` only in `E/cube_replay.py:257`), so a
   gold can read a generated table without naming the schema. A source table or CTE with the same name is also
   flagged; that error lowers a kind, never raises one.
3. `filter:measures.<m>`: a clause of `cube_gen.where_clauses(m)[1:]` (new helper; `where_sql` becomes
   `" AND ".join(where_clauses(m))`, `E/cube_gen.py:68-75`), whitespace-collapsed, is a substring. The first
   clause `t."<by>" IS NOT NULL` is skipped as too common.

A hand-written filter such as `upper(btrim(s.location_id)) IN ('MAIN', 'SUB')` (`demo/gold/q03_stock.sql`) does
not match the generated `upper(btrim(t."location_id")) IN ('MAIN', 'SUB')`. No demo gold matches rules 1 and 3
(static probe) or rule 2 (`grep -i '_closure\|_cycles\|v_shape_\|v_rule\|sxc_out'` over
`demo/gold/*.sql` finds nothing). Paraphrased copies are not detected. A gold file that cannot be read is not flagged; its
comparisons error as today.

### 5.3 Hard cases

| Case | Kind | Why |
|---|---|---|
| Gold SQL copied from generated SQL (verbatim filter clause or output schema) | `generated`, whole gold; its facets and kills give no authored evidence | 5.2 |
| Gold SQL copied but paraphrased | `authored` | not detectable; every authored count is described as "a person wrote one side" |
| `v3:total:m_<m>.value` | `generated` | SQL side calls `cube_gen.where_sql` (`E/shape_q.py:307`); docstring at `:304` is corrected |
| `v3:total:<h>_closure.*` | `generated` | both sides read the build table (`E/shape_q.py:310-312`) |
| `v3:additive`, `v3:pair`, `v3w:pair_witness` | `generated` | Cube vs Cube, or `where_sql` (`E/shape_q.py:344`, `:543`, `:843-875`) |
| Expected rows (`expect`) | not counted | report never reads expect (`E/expect.py:40-43`); kind would be `authored` only for `source = authored` (`E/expect.py:193`, `:208`) |
| `v3:closure_oracle:<h>` | `recomputed`; not facet evidence | it never reads Cube and is not replayed against mutants (`E/stage_verify.py:12-13`) |
| `v5:closure:<h>` | `generated` | second engine, but its OWL side comes from the ontology generator and its SQL side is the generated closure table: a swapped `parent`/`child` in the configuration reaches both the same way |
| `closure_apply:<h>` | `generated` | same builder, same transaction |
| `v5:witness:*` | `structural` | engine-authored synthetic graph: tests templates, not this data |
| V4 kill by an authored item that reads the change only through a join | `mutation`, `detection = authored` | the gold rows differed from Cube's |
| V2 stratum all empty, V1 binding empty | `authored`, listed in `zero_row_checks` (or `empty_stratum_zero_row_checks` for a stratum named `empty`) | the kind says who wrote the side; the zero-row list says nothing was compared |
| `v1:<g>` and `v2:<g>:all` of a gold without parameters | both `authored`; the same binding is compared twice (`comparisons` counts 2) | V1 replays the first binding, V2 replays it again (`E/stage_verify.py:238-244`, `:368-387`; one binding `("all", {})`, `E/sweep.py:69-70`); the report.md table says so |
| Optional diagnostics, `data-rules`, `expect` | not counted | outside `PIPELINE` (`E/report.py:43`) |

## 6. Algorithm per stage, with cost

| Piece | Where | Data | Extra queries | Extra Cube starts |
|---|---|---|---|---|
| gold text match, `compares`, `uses`, stored gold kind | top of `_Verify.plan_gold` (`E/stage_verify.py:220-245`), before its early returns | gold file, `cube_gen.where_clauses`, `cfg.source.output_schema`, `naming` relation names | 0 (one file read per gold) | 0 |
| `value_rows` count | `compare.compare_rows` (`E/compare.py:166-193`), new key; left out of check details by `_detail` and `_Sweep.detail` | matched rows | 0 | 0 |
| per-gold comparisons | `_Verify.v1` after `:364`; `_Verify.v2` from `agg` after `:386` | comparison dicts | 0 | 0 |
| N1-N3, `zero_row_checks`, require `v1` rewrite | new `_Verify.gold_evidence()` in `run()` after the serving block, before `v4` (`:289-305`) | the above | 0 | 0 |
| suite order | `_Verify.v4` before `Suite(...)` (`:441-444`); stable sort by item kind | `self.items` | 0 | 0 |
| detection, facet, readers, N5-N6, mutant split, ledger and receipt annotation, require rewrite | `_Verify.v4` loop (`:449-461`) | `killed_by`, gold kinds, facet table, ledger | 0 | 0 |
| facets, N4/N4a, N7, `gold_evidence:*` | new `_Verify.facets()` after `v4` in `run()`; full scope only | plans, V1/V2 counts, `value_rows`, V4 detections, model cubes | 0 | 0 |
| report evidence | `report.run.assemble` (`E/report.py:437-462`) after `_add_stage_checks`; `render_md` (`:337-397`); console lines | stage results it already reads | 0 | 0 |

Compute is O(checks + golds x facets). Cube starts dominate V4 time (one per observable mutant, `E/mutate.py:449`)
and do not change.

## 7. Existing configurations

### 7.1 What changes (engine 0.23.0, no new key set: disclose mode)

| Output | Change | When |
|---|---|---|
| check ids, check counts, exit-code rules | none | never |
| statuses | none, except V4 statuses and `killed_by` (3.4: a different first item can error twice or reach the item budget first; a different item can be the first to fail) | only with a gold that has generated text (5.2) |
| doctor, demo, build, reason, cube outputs | none | never |
| `verify/result.json` | top-level `evidence`; V4 keys `detection`, `facet`, `gold_comparing` | always with V4 |
| `verify/result.json` `not_proven`, verify console | N1-N6, N4a, N5a lines | when their condition holds (clean demo, **(measured)**: 3 N5 lines and 1 N4 line for `measures.firm_open`) |
| V4 suite order | generated golds after authored golds | only with a gold that has generated text |
| `verify/mutation-coverage.json` | `detection` per row, `kind` per receipt item | always with V4 |
| `report.json` | `evidence` key | a verify result exists |
| `report.md` | `## Evidence`; more lines under `## Not proven`; larger verify "Not proven" cell (`E/report.py:353`) | a verify result exists |
| report console | `  EVIDENCE  ` lines | a verify result exists, not `--quiet` |
| `report/result.json` | no new key; `stage:verify` detail `not_proven` value grows with verify's lines (`E/report.py:267`) | a verify result exists |

Default is disclosure because: no verdict changes (apart from the suite-order case above, which needs a gold that
copies generated text), so the fail-closed contract and exit codes hold; an opt-in
disclosure would leave every existing report as misleading as today; the precedent for a default disclosure without
a verdict change is the `unbound` revision line of 0.2.0; default verdict changes were made only to close a
fail-open path in the code contract (0.14.0, `CHANGELOG.md:361-370`). Here the code contract is accurate
(`docs/design.md:392`: "a suite item failed against it"); the wrong reading is in `README.md:158`, `:650` and
`SKILL.md:165-166`, and it is fixed there.

### 7.2 Opt-in require mode

`verify.gold_observation = "require"` on the clean demo **(measured)**: verify `FAIL` with 49 `pass` and 4 `fail`:
`v4:measure_agg_to_max:{firm_open,on_hand,consigned}` (`generated_only`, `killed_by` kept) and, of the 6
`gold_evidence:*` checks (4 measures, 1 hierarchy, `ontology.class_column`), `gold_evidence:measures.firm_open`
(`no_gold_reads` for `part+m_firm_open`); the other five pass. The N4 and N5 lines are not written: in that run (the
stage called by the integration test, without the runtime and revision lines a command-line run adds) the only
not_proven lines are those of the two equivalent mutants. The demo configuration keeps the default.

### 7.3 Compatibility goldens

They hold build and report runs without verify (`tests/fixtures/compat_v0_21_1/capture.py:1-22`). Build code and
the demo configuration are not changed, `capture.py:43` keeps reading the live demo, every report addition needs a
verify result, and `Result` gains no dataclass field. So every golden stays byte-identical apart from the normalised
`engine_sha256`. A new test asserts the absence on the golden setup.

### 7.4 Pinned NOT PROVEN counts

The clean-demo counts in this section were expectations when it was written; the **(measured)** values follow each.

- README "`NOT PROVEN` 14줄" and "나머지 5줄은 reason의 3줄과 verify의 2줄" (`README.md:375`, test
  `tests/test_docs_readme.py:63-75`): become `14 + v` and `5 + v` / `2 + v`, with `v` = new verify lines of the
  clean demo (G N5 lines, G >= 3; 1 N4 line for `measures.firm_open`; plus 1 N4 line for
  `measures.firm_open_dated` if `m_firm_open_dated.ts` is not detected or a facet is `no_values`). **(measured)**
  `v` = 4 (G = 3; `m_firm_open_dated.ts` is detected and no facet is `no_values`), so 18 lines, 9 of them
  reason's 3 and verify's 6. The zero-row
  stratum `v2:q02_where_used:top` adds no verify line (its gold has rows in other strata, so no N3); it shows only
  on the EVIDENCE zero-row line. The sentence pattern and
  the `sum(other) == reason + verify` assertion stay: EVIDENCE lines are not NOT PROVEN lines, and report's console
  prints no new NOT PROVEN line of its own (report.md "## Not proven" repeats verify's new lines).
- README block (`README.md:351-372`): verify gains the `v` lines; report gains the EVIDENCE lines.
  `expected_output()` (`tests/test_docs_quickstart.py:88-99`) reads only `[sxc` and `NOT PROVEN` lines; it is
  extended to read report's `EVIDENCE` lines so the integration test pins them too.
- `tests/test_verify.py:279` (`--only v1,v2,v3`, `not_proven == []`): unchanged (narrowed: no N4; no V4; no
  gold with zero rows or generated text).
- `tests/test_verify.py:370` (`--only v4`, `not_proven == []`): breaks if `closure_join_swap` is killed by a V3
  item. Replace with: every not_proven line is an N5 line of a check whose `detection` is `generated_only`.
- `tests/test_compare_unit.py:542`, `:563`, `:573`, `:591-617`; `tests/test_soundness_unit.py:420-439`;
  `tests/test_pair_chunks.py:221-238`; `tests/test_mutate.py:435`, `:590`: unchanged by reading (narrowed runs, no
  comparisons, details without `killed_by`, or golds of unknown kind that keep rank 0, 3.4); rerun to confirm in
  phase 2-3. The `compare_rows` tests of `tests/test_compare_unit.py` (from `:158`) read single keys; rerun them
  after `value_rows` is added.
- `tests/test_docs_catalogue.py`: `test_documents_name_only_catalogued_checks` (`:244-248`) and
  `test_every_id_template_with_a_catalogued_prefix_is_catalogued` (`:230-233`) run over the new design.md table and
  `E/evidence.py`. They pass only if the table uses catalogue spellings (`v3:*`, `scope:v1`) and `RULES` keys are
  not id-shaped strings (5.1). `gold_evidence:<element>` gets a row in design.md section 6 so `names_a_check`
  accepts it.
- `tests/test_doctor_demo_report.py:299`, `:328`; `tests/test_contract_evidence.py:330`: no verify result, unchanged.

### 7.5 Engine fingerprint

Any engine `.py` change changes `engine_sha256` (`E/provenance.py:55-85`). Every earlier build, reason, cube and
verify result is stale after the upgrade (`why=inputs_changed`); no 0.22 verify result is read by 0.23 report code
as current. A hand-copied old verify result has no `evidence`: report says "observation not recorded".

### 7.6 CHANGELOG 0.23.0 (user-visible, to write)

- Rerun the whole pipeline after upgrading (engine fingerprint).
- A passing `v4:` check no longer reads as "a gold query detects this". Read `detection`: only `authored` means a
  gold item failed against the mutant.
- New keys: verify `evidence`; V4 `detection`, `facet`, `gold_comparing`; coverage `detection`, item `kind`;
  report.json `evidence`; config `verify.gold_observation`, `verify.accepted_gaps`; check `gold_evidence:<element>`
  (require mode).
- New lines: N1-N6, N4a, N5a in verify (N5b, N7 in require mode); `  EVIDENCE  ` lines on the report console.
  Scripts that count NOT PROVEN lines will see more in verify. Console parsers should ignore lines starting with
  `  EVIDENCE  `.
- Statuses and exit codes are unchanged unless `verify.gold_observation = "require"`, with one exception: a gold
  query containing generated text (output schema, a generated relation name, a generated filter clause) now counts
  as generated evidence and runs after authored gold items in V4, so for such a configuration `killed_by` and V4
  statuses can change (an item error or the item budget can now be met first).
- A facet "compared" now needs rows holding values; the clean demo shows one facet with no authored evidence
  (`part+m_firm_open`, **(measured)**).

## 8. Test plan

### 8.1 Unit, no database (one or more per rule)

| Test | Asserts |
|---|---|
| `test_evidence.py::every_rule_row` | one id per row of 5.1 gives its kind; stage-wide rows win over id shape (`reason:java` in doctor) |
| `::no_default_row` | an unknown id is `unclassified`; summarise never adds it to authored or recomputed |
| `::catalogue_complete` | every row of `docs/design.md` section 6 for a pipeline stage matches a rule other than `unclassified` (reuse `catalogue()`, `tests/test_docs_catalogue.py:43-53`) |
| `::docs_equal_registry` | the "Evidence kinds" table of design.md section 8 equals the table rendered from `evidence.RULES`; every backticked token in it passes `names_a_check` (`tests/test_docs_catalogue.py`) |
| `::downgrade_only` | gold entry recorded `authored` with `generated_text: ["output_schema"]` -> `v1:g`, `v2:g:*` count as `generated`, `overridden == 1`; recorded `generated` -> `generated`, `overridden == 0`; recorded `recomputed` -> `generated`, `overridden == 1`; no gold entry for `g` -> `unclassified`; a recorded kind never changes a `v3:` or `v5:` check |
| `::v3_total_is_generated` | `shape_q.measure_total_sql(cfg, m)` contains `cube_gen.where_sql(m)` for every demo measure, and its kind is `generated` |
| `::v5_closure_is_generated` | `v5:closure:h` in stage reason has kind `generated` |
| `::recomputed_rows_outside_generators` | for every `recomputed` row: the module-level AST import closure of its module (imports at module top level only, `from . import x` included) equals the pinned set of 5.1 and contains no `cube_gen`, `closure`, `shapes`, `shape_q`, `mutate`, `owl` |
| `test_gold_text.py::matches` | verbatim filter clause flagged; whitespace variant flagged; text only in a comment not flagged; `t."part_id" IS NOT NULL` alone not flagged; `sxc_out.x` and `"sxc_out"."x"` flagged; `sxc_outer.x` not; `FROM has_component_closure` and `"has_component_closure"` flagged as `relation:has_component_closure`; `has_component_closure_old` and `x.has_component_closure` not flagged by rule 2; five demo golds not flagged |
| `::value_rows` | `compare_rows` counts per gold column the matched rows with a non-NULL, non-zero, non-empty gold value; a Cube NULL turned into 0 by `zero_if_null` with gold 0 counts 0; V1 and V2 check details have no `value_rows` key |
| `::where_clauses_join` | `" AND ".join(where_clauses(m)) == where_sql(m)` for every demo measure and a literal containing ` AND ` |
| `test_verify_evidence.py::compares_vs_uses` | `compares` = map values; a gold whose only use of `m_on_hand.value` is a filter leaves facet `m_on_hand.value` a gap `filter_only` |
| `::facet_reasons` | each reason in order: cube not in model; no reader; filter only; generated text; gated reader; reader with zero rows; reader with rows but no values; compared; detected; accepted by element and by facet |
| `::facet_set` | demo configuration and a model without closure cubes: the node join facets `part+m_on_hand`, `part+m_consigned`, `part+m_firm_open`, the class facet `part.part_class`; `part+m_firm_open` is `no_gold_reads`; without closure cubes the closure facets stay, reason `not_in_model`; no facet for a dated measure's node join |
| `::facets_full_scope_only` | `--only v1` or `--gold q01` -> no N4 line, `scope = "narrowed"`, `facets` null, `facets_reason = "narrowed"`; exact lists at `tests/test_compare_unit.py:542`, `:563`, `:573` unchanged |
| `::gold_zero_rows` | `run_binding` stubbed to `compare_rows(g, [], [])` for every binding of one gold -> one N3 line with `{n}` and `{ids}`; one non-empty binding -> none; an errored binding is not a comparison; gold without bindings -> only the existing no_binding line |
| `::zero_row_checks` | V1 empty, V2 all empty, V2 `empty_stratum` listed; a stratum named `empty` goes to `empty_stratum_zero_row_checks`; a mixed stratum not listed |
| `::suite_order_contract` | `_Verify.run` with patched `db.connect`, `sweep.bindings`, `cube_run.serve`, `compare.run_binding`, `shape_q.run_test`, capturing `suite.items` in `mutate.run_mutants`: every authored gold item precedes every generated gold item precedes every `v3:` item; without a generated gold the order equals today's; a gold kind is stored for gated and unplanned golds; items of golds unknown to the plan keep rank 0 and get kind `unclassified` |
| `::v4_pass_is_not_gold_detection` (F1 guard) | items `v1:a` (passes on the mutant) and `v3:total:x` (fails): check `pass`, `killed_by == "v3:total:x"`, `detection == "generated_only"`, the N5 line present; killer `v1:a` -> `authored`, no line; killer `w9:x` -> `unattributed`, N6; require mode -> `fail`, `reason = generated_only`, summary tally and ledger row consistent. Plus `evidence.v4_violations(result)`: every V4 check with `detection = authored` has a killer of kind authored, and no V4 check whose killer is a `v3:` item has `detection = authored` |
| `::f1_trace` | the trace setup (demo without q03, `measure_normalization_drop`, SQLite source, fake Cube, real `shape_q`, `mutate.run_mutants`, `Suite`) through `_Verify.v4`: `detection = generated_only`, `gold_comparing == []`, N5 and the N4 line for `measures.on_hand` |
| `::require_mode` | `gold_evidence:*` statuses and reasons (`not_in_model` and `gold_not_run` -> `not_run`); `v1` `pass` -> `not_run` `no_rows_compared`, `v1` `error` stays `error` and `v1` `fail` stays `fail` while N3 is written; `suite_incomplete` precedence; a killed `generated_only` mutant with items left out gets N5b and the kill next action, never the survivor text; N7 and `operators_not_selected` when an operator is not selected; an accepted `v4:` id stays `pass` with N5a |
| `::mutant_split` | buckets sum to the selected mutants; `equivalent` and `not_applicable` counted from ledger rows that have no check; `mutation_operators = []` -> "0 of 9 operators selected"; `not_judged` = V4 checks with status `not_run` or `error` |
| `::n5_remedy` | `measure_agg_to_max` N5 carries the "aggregates over more than one key" remedy; filter and dated operators their own; others the generic text |
| `test_config.py` | bad `gold_observation` and `accepted_gaps` values (unknown element, facet, operator or target) -> exit 2; `ontology.class_column` and `v4:<op>:<target>` accepted |
| `test_report_evidence.py::golden_absence` | report on the compat golden setup (`tests/test_data_rules_compat.py:39-60`): no `evidence` key, no section, no EVIDENCE line |
| `::section_and_lines` | report with written results (`write_result`, `tests/test_doctor_demo_report.py:259`): exact section text and console lines of 4.4-4.5 |
| `::excluded_and_unrecorded` | stale verify -> `excluded_stages`, "not included" line; verify without `evidence` -> `observation_recorded: false`, its line, V1/V2 counted `unclassified`, facets and mutants "not judged"; malformed `evidence` treated as absent; a narrowed verify -> "facets: not judged (narrowed verify run)", never "0 of 0" |
| `::privacy` | `ROW_ID`-style check (as `tests/test_verify.py:267`) over every new key and line |
| `test_docs_evidence.py` | the old sentences are gone from README, SKILL.md, design.md, commands: `README.md:158` "기준 질의가 잡아내는가" in the V4 row, `:650` "gold suite의 실패 항목이 변형 모델을 검출", `SKILL.md:165-166` "so the gold suite cannot see that kind of error" applied to passes; the new `detection` sentence is present |

The test that fails if a V4 pass can again be read as gold detection: `v4_pass_is_not_gold_detection`, together with
`evidence.v4_violations` called in every integration test that runs V4, and `test_docs_evidence.py`.

### 8.2 Integration (PostgreSQL, Java, Node, Cube)

- Clean demo full run: pins A, G, the facet counts and gaps (expected by reading: `part+m_firm_open`
  `no_gold_reads`), the N5 set, both zero-row lists (expected by reading: at least `v2:q01_explosion:empty` and
  `v2:q02_where_used:top`; `tests/test_verify.py:283` pins only the first) and the README block (summary, NOT
  PROVEN and EVIDENCE lines); `v4_violations == []`; `assert_catalogued` with the new ids. **(measured)** The
  zero-row list is exactly `v2:q02_where_used:top` and the empty-stratum list exactly `v2:q01_explosion:empty`;
  the pins are in `tests/test_evidence_pg.py` (`assert_clean_demo` and the require-mode scenario).
- Bare generated name: an extra gold that reads `has_component_closure` without a schema, run by a role whose
  `search_path` includes the output schema -> `relation:has_component_closure`, its checks counted generated.
- F1: demo without `q03_stock`, dirty data, `mutation_operators = ["measure_normalization_drop"]` -> N5 and N4 for
  `measures.on_hand`.
- F2: `q03_stock` with a copied clause `upper(btrim(t."location_id")) IN ('MAIN', 'SUB')` -> N2, its checks counted
  generated, facets `m_on_hand.value`, `m_consigned.value` gaps `generated_text`.
- F3: `q05_firm_window.param_sets` = only the 2030 window, `mutation_operators = ["closure_qty_single_path"]` -> N3
  and N4 (`no_rows_compared`, `filter_only`); statuses unchanged.
- Require mode on the clean demo -> the statuses of 7.2.
- Dirty demo pin `v4:measure_normalization_drop:on_hand` = `pass` (`tests/test_docs_quickstart.py:290`) still holds
  in disclose mode.

## 9. Documentation changes

| File | Change |
|---|---|
| `docs/design.md` | section 8 subsection "Evidence kinds" (5.1-5.3, legend; catalogue spellings only, 5.1); V4 row `:392` and V4 section `:746-760`: order contract, `detection`, mutant split, modes; compare `:562-563`: the zero-row line and `value_rows`; result contract `:100-110`: verify `evidence`; catalogue row `gold_evidence:<element>` in section 6; config keys |
| `skills/semantic-crosscheck/SKILL.md:165-166` | a passing V4 check means a suite item failed; only `detection = authored` means a gold item did; read the EVIDENCE lines |
| `README.md` | V4 row `:158`, catalogue `:650`; `:92` points to the Evidence section; clean block and prose `:351-375` from the integration run; reading results `:544-571`: kinds, EVIDENCE lines, modes; limits section `:742`: what generated agreement cannot catch, paraphrased copies |
| `docs/mutation-coverage.md` | `detection`, item `kind`; still outside report authority |
| `docs/contract-report.md` | report.json `evidence`, report.md section, console lines; disclosure, no verdict effect in report |
| `skills/semantic-crosscheck/references/config.md` | the two keys; zero-row line next to the empty-stratum text |
| `commands/xc-report.md:56-63`, `commands/xc-verify.md` | report the EVIDENCE lines; never call a `generated_only` kill a gold detection |
| `SECURITY.md:51` | "an agent overstates a result": evidence kinds and `detection` |
| docstrings | `E/shape_q.py:304`, `E/mutate.py:3-9`, `E/stage_verify.py:31-39`, `:563` |
| `CHANGELOG.md`, release notes, this plan under `docs/plans/` | 7.6 |

## 10. Implementation phases

The work was done in six phases, each with its tests written first. P1-P4 and P6 reach G1-G3 without P5; the
require mode (P5) is the last engine phase, so it could have been left out without changing the others.

| Phase | Files | What was implemented |
|---|---|---|
| P1 registry | new `E/evidence.py`; design.md section 8 table; `tests/test_evidence.py` | rules with prefix-tuple keys, `kind_of`, `item_kind`, the order helper and the table renderer; tests for every rule row, no default row, catalogue completeness, docs equality (catalogue spellings), downgrade only, the V3 guard, `v5:closure` generated and recomputed rows outside generators; the `E/shape_q.py:304` docstring corrected |
| P2 verify gold and facets | `E/stage_verify.py`, `E/cube_gen.py` (`where_clauses`), `E/compare.py` (`value_rows`), `E/result.py` (`to_dict`), `E/config.py` (`accepted_gaps`, used by N4a and N5a in both modes); `tests/test_gold_text.py`, `tests/test_verify_evidence.py` | plan-time matching and stored gold kinds, `value_rows`, per-gold counts, N1-N4a, `evidence` in verify results only; tests for gold text matching (bare relation names included), `where_clauses`, `value_rows`, compares vs uses, zero-row lists, facet set and reasons, full-scope gating, narrowed text, `accepted_gaps` validation and privacy |
| P3 V4 attribution | `E/stage_verify.py` (`v4`, mutant split, remedies), facet table in `E/evidence.py` | stable sort, `detection`, `facet`, `gold_comparing`, mutant split, N5, N5a, N6, coverage keys; tests for the suite order contract (unknown golds), the F1 guard and trace, the mutant split, N5 remedies, ledger and receipt annotation, unchanged exact dicts |
| P4 report | `E/evidence.py` summarise and render, `E/report.py`, `sxc.py`; `tests/test_report_evidence.py` | `evidence.summarise`, the report.json key, the report.md section and the EVIDENCE console lines; tests for golden absence, section and lines, excluded and unrecorded results, unclassified checks and privacy |
| P5 require mode | `E/config.py` (`gold_observation`), `E/stage_verify.py`; catalogue row; `tests/test_require_mode.py` | the key and its validation, `gold_evidence:*`, the V4 rewrites (with N5b) and the pass-only V1 rewrite with summary and ledger, N7, next actions |
| P6 documents, pins, integration | section 9 files; `tests/test_docs_*`; `tests/test_verify.py:370`; `tests/test_evidence_pg.py` | the documents and `test_docs_evidence.py`, CHANGELOG 0.23.0 with 7.6, the release notes and this plan; the integration scenarios of 8.2; the README block, its prose and the clean-demo pins from one CI integration run |

## Invariants to keep
- No new line, key or check appears in a build-only run (goldens byte-identical).
- `detection` is exact only because of the enforced order; a change to suite planning must keep the order test green.
- A recorded kind can never raise a check above the registry kind; only `gold[].kind` is ever read.
- No `recomputed` row whose code reaches a generator module at module level; `v5:closure` stays `generated`.
- Facets use compared members only; filter, time-dimension and segment members never count as compared; `compared`
  needs `value_rows > 0`.
- No require-mode rewrite makes a status less severe (`error` is never rewritten).
- Design.md tokens use catalogue spellings; `tests/test_docs_catalogue.py` stays green.
- No gold SQL text, row value or parameter value reaches results, lines or report outputs (`value_rows` is a count).

## Verification outcome
What has run: the full unit suite (`python -m unittest discover -s tests`, no database), after every phase, ending
with exit code 0 on one Windows machine; every rule of 8.1 has a unit test that needs no database. And one CI run of
the implementation branch: its unit jobs passed, and its integration job (PostgreSQL 17, HermiT, Cube, Jena) passed
every test, the new scenarios of 8.2 included, except the comparison of the README quick-start block, which then
still showed the output of v0.22.1. The README block, its prose and the pins of the clean demo were then set from that
job's log; that change has passed the unit suite, and its integration run is the next CI run. Every value marked
**(measured)** in this plan comes from that log. Before implementation, the static facet probe of 3.3 and two probes
of 5.1 (the catalogue token matching and the module-level import closure) were run on copies of the engine.

## 11. Risks

| Risk | Mitigation |
|---|---|
| A, G and the facet result of the clean demo are unknown without a database | pinned from one CI integration run (P6): 9 authored kills, the 3 `agg_to_max` kills by generated checks only, 15 of 17 facets compared, 1 detected, 1 gap |
| More not_proven lines in verify for real configurations (one N4 per element with a gap, one N5 per generated-only kill) | one line per element, not per facet; ids capped at 10; `accepted_gaps` for known gaps |
| `filter_only` on the demo's dated facet looks like noise | the facet is cleared by an authored kill of `dated_shift_one_day`; the reason code says what was done |
| Generated-text false positive (a hand-written clause that equals the generated text, including `t."col"` quoting; a source table or CTE named like a generated relation) | unlikely; it only lowers a kind; N1/N2 name the match so the user can rename or rewrite |
| Lines readers learn to skip (the `empty` stratum on every explosion gold, fixed sentences, N5 for `measure_agg_to_max` on per-key golds) | `empty` strata on their own list; fixed sentences once in the section header; N5 remedy per operator; `accepted_gaps` takes `v4:` ids (N5a) |
| Require mode passed by dropping the operators that fail | operator count and names on the EVIDENCE line and in `gold_evidence:*`; N7 in require mode; the next action says "do not drop the operator" |
| V4 status change in disclose mode for a configuration whose gold copies generated text | stated in 3.4, 7.1 and the CHANGELOG; only after a gold is newly flagged generated |
| Whole-gold downgrade under-credits authored joins and keys of a gold that copied one filter | stated; the per-measure rule is the alternative of choice D3 |
| Readers ignore not_proven lines | EVIDENCE lines on the report console list the mutation split, facet gaps and zero rows |
| Console parsers see a new line type | only with verify present; documented in CHANGELOG |
| `killed_by` non-empty on a require-mode `fail` confuses a reader | documented; `detection` and `reason` are on the same line |
| Registry drifts from code | catalogue completeness and docs-equality tests |
| Suite order changes later | order enforced by sort and pinned by a test |

## 12. Design choices

| # | Question | Chosen | Alternative |
|---|---|---|---|
| D1 | Default behaviour in 0.23.0 | disclosure; verdicts opt-in | verdicts by default: unchanged configurations with V3-only kills, zero-row golds or facet gaps exit 1; demo rewrite and frozen golden inputs |
| D2 | Require mode (P5) in 0.23.0 | yes, as the last phase | a later release, once an integration run shows how many kills are `generated_only` |
| D3 | Gold with a verbatim generated filter clause | whole gold `generated` | per measure (a fourth sort rank, loses exactness for later items), or disclose only |
| D4 | Facet set | all of 3.3 | only facets an operator mutates |
| D5 | Filter, time-dimension and segment use | does not count as compared (`filter_only`); an authored kill can still clear the facet | count a `timeDimensions` dateRange as compared for `ts` |
| D6 | Report console form | `  EVIDENCE  ` lines | one report NOT PROVEN line |
| D7 | `killed_by` on a require-mode `fail` | keep the first failing item | empty it and add `detected_by` |
| D8 | `expect` in report | no | read expect results as non-voting evidence |
| D9 | Release | 0.23.0 minor; README pins from one integration run | none |
| D10 | Demo zero-row stratum and the `part+m_firm_open` gap | keep the demo as is | change `demo/crosscheck.toml` (new golden capture, new pins) |
| D11 | `verify.accepted_gaps` takes `v4:<op>:<target>` ids | yes (N5a) | facets and elements only |
| D12 | Facet `compared` needs rows holding values | yes (`value_rows`, a count) | rows only |

## Implementation notes

Where the implementation (0.23.0) differs from this plan, or settles what it left open:

- Registry table (5.1): each rule carries a short plain-English reason instead of file and line citations, which would
  drift; the table writes the catalogue spellings `shacl:<id>` and `shacl:pair:<id>`, and the stage
  `semantic-preflight` with a hyphen (`_` is read as `-`). "any" means any stage the registry names; every id of
  another stage is `unclassified`.
- Generated text (5.2): a match of rule 2 also gets its own line in the N1 family, `gold:{g}: the gold SQL names the
  generated relation {names}; ...`. Rule views are matched by the prefix `v_rule_` (verify does not read the rule
  file), and any match is the one fixed item `relation:v_rule_*` (after the configured relation names, before
  `relation:v_rules_subjects`), never the text that matched, which may be a literal. Case folding in rules 1 and 2 is
  ASCII only, as PostgreSQL folds unquoted identifiers. Comments are not removed inside double-quoted identifiers
  either, and nested block comments are removed as PostgreSQL nests them.
- `value_rows` (3.2): every boolean gold value counts as a value.
- Facets (3.3): without a model (configuration validation) the class facets are listed for every entity. With a
  model in which no entity cube holds the class column, one facet `*.<class_column>` (cube `*`, reason
  `not_in_model`) stands for it, so a set class column is never left out. Reason
  `generated_text` also covers a facet that only generated golds use in filters. A gap line names the golds behind
  its reason. Full scope means every part and every configured gold query planned; a `--gold` naming all of them is
  full scope.
- Coverage (4.2): a coverage row gains `detection` only when it has `killed_by`. `gold_comparing` names the authored
  golds of this run. `{m}` in the remedies is `measures.<m>`. A V4 pass without a `killed_by` key counts as
  `unattributed`; a coverage row without a check that is neither equivalent nor not applicable counts as
  `not_judged`; a run without V4 records an all-zero split, which report shows as not judged.
- Report (4.3-4.5): `excluded_stages` lists a required stage without a result as `missing` and an optional stage
  without a result not at all. A stale verify gets the not-judged texts with reason `not_included`. report.json has
  `mutation_reason` (`not_recorded`, `not_included`, `v4_not_run`). "Malformed evidence" is defined field by field,
  values included (closed reason and operator sets, plain ids without spaces or line breaks), the mutant split must
  follow from the V4 checks of the same result, and a verify result that fails the result contract counts as having
  none. The zero-row texts list authored checks
  only; `gold_generated_text` names the golds counted as generated.
- Require mode (3.5, 4.6, 4.7): for an `unattributed` kill the N5b line and the next actions call the killer "an item
  which no evidence rule classifies". The detail of a passing `gold_evidence:<element>` has no `reason`; an element
  without any facet in the model is `not_run` (`not_in_model`). A rewritten kill's summary moves from `killed` to
  `survived` or `not_run`, and its coverage row gets that outcome. N7 is written whenever operators are left out, also
  on narrowed runs. A rewritten `v1:<gold>` stays a V4 suite item. The survivor next action of 4.7 changed after
  the require-mode phase, after the require-mode tests had pinned the default outputs.
- Tests: the 8.1 row `test_verify_evidence.py::require_mode` is `tests/test_require_mode.py`; it also pins the
  default-mode outputs of eight scenarios by digest.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| Verdict changes by default | a default verdict change fails the unchanged demo (`tests/test_mutate.py:654-656` implies 3 `generated_only` checks) and every existing configuration with a V3-only kill, forces a demo rewrite and frozen golden inputs (`capture.py:43`), while the code contract (`docs/design.md:392`) is accurate and the misreading is in documents; it also changes `suite_incomplete` semantics outside the goals and counts `v5:witness` as authored |
| Disclosure with a static check only | static "names a member" check (a key or filter member suppresses it), no rows tie, no copy detection, V4 kills folded into authored, gaps not on the report console |
| Evidence classes with full attribution | six console lines, one line per member, attribution modes beyond the goals, `observe:` lines on narrowed runs that break `tests/test_compare_unit.py:563`, `:573`, copied-filter gold kept as authored observer |
| Attribution beyond the first detecting item | `detection` is exact in the first-failing mode at no cost; running the items after the first failure multiplies Cube work |
| One report NOT PROVEN line instead of `  EVIDENCE  ` lines | it cannot carry the gaps, and it would change the README prose pin (`tests/test_docs_readme.py:72-75`) |
| Kind `independent` for the recomputation | authored evidence is also independent of the generators; "independent" would blur that |
| A `not_proven` status | breaks the closed status set (`E/result_contract.py:13`, `:50-52`), `Check.__post_init__` (`E/result.py:29-31`), `sxc._summary` counts (`sxc.py:296-298`) and every reader following SKILL.md |
| Per-binding or per-stratum zero-row lines | the `empty` stratum is empty by design (`E/sweep.py:34`; 8 bindings, `tests/test_verify.py:283`), so such a line would always be written |
| Counting `_members` (filters, segments) as compared | a filter on a member does not compare its values (`E/compare.py:175-177` compares `gold.map` columns only) |
| Closure oracle as facet evidence | it never reads Cube and is not replayed against mutants (`E/stage_verify.py:12-13`) |
| Model diff (`mutate.changed`) to find mutated members | the static operator-to-facet table gives the same facets at no cost |
| Reading `expect` results in report | report never reads expect (`E/expect.py:40-43`); kept outside report authority |
| Kind stamped on every check of every stage | stamps build checks, breaks the goldens, duplicates a static fact |

## What this design does not solve

- Gold SQL wrong in the same way as `crosscheck.toml` (both from one misread specification): authored is not correct.
- Paraphrased copies of generated SQL, generated SQL reached through a view the user made, or dynamic SQL. Rule
  5.2 is verbatim text and names only.
- G2, attribution: only the first detecting item and its kind are known. Later items do not run (early stop,
  `E/mutation_coverage.py:65-69`), so the design cannot say which other checks would also have detected a mutant.
- G2, elements: facets cover measures, hierarchies, every join of the generated model and the class dimension.
  Other entity dimensions (attributes passed through unchanged, `E/cube_gen.py:86-111`) have no facet and are not
  named.
- "Compared with rows holding values" is not "a wrong value would be seen": a wrong value can equal the right one on
  the sampled rows. Only facets with an operator can be `detected`.
- Bindings not replayed against mutants (`E/stage_verify.py:241-242`): a facet a gold compares can still have a
  `generated_only` kill; the line names it, the design does not add bindings.
- Facets without an operator (`max_depth`, `pairs`, ancestor cube, node joins, class dimension) can only be
  `compared`, never `detected`.
- Require mode cannot stop a user from deselecting operators; it discloses them (N7, `operators_not_selected`).
- Disclose mode keeps statuses except one case: a configuration whose gold copies generated text gets a new V4
  suite order, so `killed_by` and V4 statuses can change (3.4).
- Other silent empty passes (`shacl:<shape>` with no keys, `v5:closure:<h>` with no pairs, an unfiltered `v3:pair`
  over zero keys, an `expect` case with all sides empty).
- `expect`, `data-rules` and diagnostics stay outside report; the closure oracle is not replayed against mutants.
- Independent recomputation exists only for the closure (`v3:closure_oracle`). Measures have no recomputed
  evidence; their only non-generated evidence is gold SQL.

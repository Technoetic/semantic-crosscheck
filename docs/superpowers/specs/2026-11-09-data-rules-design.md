# Data rules: typed rules evaluated twice, with database lookup views

## Intent

Structural data rules over a bill of materials are easy to state and tedious to check by hand: a kit used by
another assembly needs components of its own, an active assembly should not consist of retired parts only, a
stored level should equal the longest chain below a part. The plugin should find the keys that break such rules,
derive every finding twice by independent means, and let a person find the keys in the database without putting
row values into results, consoles or reports.

## Approach

A new opt-in, read-only stage `sxc.py data-rules` (`/xc-data-rules`) reads a typed rule file named by a new
`[data_rules]` table of crosscheck.toml. Every rule is evaluated on one READ ONLY REPEATABLE READ snapshot twice:
as compiled PostgreSQL SQL with bound parameters, and by a standard-library Python evaluator over the rows exported
in the same transaction. A rule gets a verdict only when both outputs are equal as canonical multisets; a
disagreement leaves it `not_run`. With `views = true` (the default) build creates one lookup view per rule and a
subjects view, and data-rules proves that they return exactly the agreed rows in canonical order.

Alternatives considered and not taken in this release: a trusted SQL rule with a Python twin (it would open the
closed rule language), a SHACL engine as a third evaluation (slow at realistic sizes and it needs a deadline
worker), folding data-rule findings into the report verdict (kept separate on purpose), and a local file of
problem keys (it changes the row-value policy, so it waits for an explicit decision and would ship later).

## Configuration and rule file

- `[data_rules]`: `file` (required, relative to the configuration with the `[semantic_preflight]` path rules, never
  inside the output directory), `views` (true), `require_examples` (true), `max_export_rows` and `max_rule_rows`
  (200000 each, 1..2000000). Without the table nothing changes.
- The rule file is TOML version 1, UTF-8 (a byte order mark is tolerated), at most 256 KiB: named `[sets]` of one
  TOML type, `[refs.<name>]` reference sets with optional constant filters, and 1..128 `[[rule]]` tables. Six
  kinds: `row` (an AND-list of atoms), `duplicate`, `mode_outlier`, `same_signature`, `stored_metric` (height) and
  `text_quantity` (a number written in a text, read with a restricted pattern). Severity is explicit; a `subject`
  names an output column holding node keys and feeds the subjects view.
- Atoms: column comparisons, set membership, prefixes, reference sets, NULL tests, endpoint existence, endpoint
  attributes, structure tests, `all`/`any`/`none` over children or parents (depth 1) and disjunctions of 2..8
  branches. At most 32 atoms per conjunction. Atoms are placed by entity role: endpoint atoms only on the hierarchy
  edge entity, structure and quantified atoms only on its node entity.
- Patterns use a subset that both engines read alike: no alternation, backreferences, lookaround, inline flags or
  lazy quantifiers, ASCII-only `\d` and `\s`, at most 16 quantifiers, and no repetition over a group that itself
  contains a quantifier.
- `[[rule.example]]` holds authored input rows per entity and the rows the rule must output, written from the
  business rule. Values must fit their declared column types exactly. Examples are required by default.
- Validation is closed at every level. Every problem is a configuration error (exit 2) whose message names a
  position and never a constant, a cell or the pattern. Types are checked against the catalog before any row is
  read; a column of an unsupported type or a nondeterministic collation makes the rules that read it `not_run`.

## Semantic contract (both engines)

- Text compares exactly; `bpchar` values are read without pad spaces; numbers compare by value; booleans as
  booleans. A comparison with a NULL operand is false (including `ne`, `not_in`, prefixes and reference sets);
  NULL is a violation only through explicit `is_null`.
- An edge is a hierarchy row with both ends non-NULL. Endpoint attributes need an existing endpoint row. `all`
  needs at least one neighbour; `none` holds without neighbours.
- Output rows carry `n`, the number of identical rows, so line rules need no unique line key. `same_signature`
  groups nodes with equal children, summed quantities and attributes; heights that cannot be bounded by
  `max_depth` make the rule `not_run`.
- Arithmetic is exact: PostgreSQL numeric, Python Decimal with Inexact trapped.

## Checks and boundaries

- data-rules checks: `data_rules:snapshot`, `data_rules:key:<entity>`, and per rule `rule:<id>`,
  `rule_xcheck:<id>`, `rule_example:<id>` (fails with `common_mode` when both engines agree with each other but not
  with the authored rows, and the rule is then `not_run`) and `rule_lookup:<id>`; then `rule_subjects:lookup`,
  `scope:rules` for a narrowed run and `data_rules:inputs`. build adds `rule_view:<id>` and `rule_subjects:view`.
- Statuses stay fail-closed (pass, fail, not_run, error); budgets, gates and missing evidence never pass.
- Results hold counts, ids and small enums only; `rules.sql` holds identifiers and placeholders only. Row values
  live only in the output-schema views, like the shape views. Rule constants appear as literals in view
  definitions and in build.sql, whose output directory is ignored by git.
- data-rules writes nothing to PostgreSQL, and report never reads its result: report only adds one not-proven line
  when `[data_rules]` is configured.
- Without `[data_rules]` every generated file, check list, result and report stays byte-identical to v0.21.1,
  enforced by goldens captured before any data-rules code existed. Only the engine fingerprint changes.
- The evaluator imports only the standard library, `canon`, `ident` and `data_rules`, so it can be reused without
  the stage modules; the pinned shared modules stay unchanged.

## Verification

Unit tests cover the rule-file model (closed keys, limits, placement, types, examples, the pattern subset and
position-only messages), the evaluator against the hand-written outputs of the clean-room example, the compiled
SQL (golden statements, no configured constant in parameterized SQL, literal escaping, parse checks), the stage
with a fake connection (every status and reason, budgets, result-boundary scans) and the compatibility goldens.
PostgreSQL 17 integration tests run in the strict CI job with zero skips, including SQL and Python mutants that
must make the cross-check fail. Independent verification and leak scans precede any release.

## Limitations

Agreement of two evaluations does not show that a rule or its constants are the right business rule; authored
examples pin only the cases they contain. The views are live and equal the certified rows only at check time.
Regex capture differences between the engines fail closed. The node key must be a single column. There is no
file of problem keys and no integration into other applications in this release.

# Data rules (`data-rules`)

Since v0.22.0. The optional `data-rules` stage checks structural data rules that are easy to state and tedious to
check by hand: a kit used by another assembly needs components of its own, an active assembly should not consist of
retired parts only, a stored level should equal the longest chain below a part, a quantity should not repeat a weight
written in the part name. The rules live in a typed rule file named by the `[data_rules]` table of `crosscheck.toml`.
Every selected rule is evaluated twice on one read-only snapshot:

1. **Path 1:** the rule compiled to PostgreSQL SQL and run with bound parameters;
2. **Path 2:** an independent standard-library Python evaluation of the rows the stage exported in the same
   transaction. It never sees SQL and does not import the compiler.

A rule gets a verdict only when both outputs are equal as canonical multisets and its authored examples hold. With
`views = true` (the default) `build` creates one lookup view per rule and one subjects view in the output schema, and
`data-rules` proves that they return exactly the agreed rows in canonical order. The keys a rule found are therefore
read in the database, with the user; results, console output and `rules.sql` hold counts, ids, small enums and
placeholders only.

The stage is optional and outside the required report pipeline. `report` adds one `not_proven` line when
`[data_rules]` is configured and never reads `data_rules/result.json`, so data-rule findings do not change the report
verdict. A clean-room synthetic example is in [examples/data-rules](../examples/data-rules/).

## Data rules or `[[shapes]]`

- **`[[shapes]]`** stay the place for class and bound rules on one column. build creates `v_shape_<id>` and reason
  compares it with the SHACL focus nodes of the generated shapes (`shacl:<id>`); an `sql_violation` predicate is
  trusted SQL that is checked in SQL only.
- **Data rules** are for the keys behind richer structural rules: endpoint existence and attributes, quantified
  conditions over children or parents, reference sets with constant filters, duplicates, mode outliers, identical
  assemblies, stored heights and quantities written in text. The rule language is closed (no SQL text), and every
  rule is checked by two independent evaluations instead of being trusted.

## Running it

```text
python skills/semantic-crosscheck/scripts/sxc.py build --config crosscheck.toml
python skills/semantic-crosscheck/scripts/sxc.py data-rules --config crosscheck.toml
python skills/semantic-crosscheck/scripts/sxc.py data-rules --config crosscheck.toml --rule kit_without_bom
```

- Run `build` first when `views = true`, and again after every change to the rule file or to `crosscheck.toml`. A
  lookup is `view_stale` when `build/build.sql` lacks the statement data-rules generates for its view now. This is
  decided per statement: after a change to one rule's constants only that rule's lookup is stale (and
  `rule_subjects:lookup` when that rule names a subject), while the other lookups still pass.
- `--rule ID` (repeatable) checks only the named rules. The others are left out with `scope:rules` `not_run`, so a
  narrowed run never passes, and `rule_subjects:lookup` is `not_run` (`excluded_by_option`) when a left-out rule names
  a subject. An unknown id is exit 2.
- Files under the output directory: `data_rules/result.json` and `data_rules/rules.sql`, the statements the run
  executed, in order (the catalog query, the encoding checks, the gates, rules, lookups, exports and examples), with
  `:pN` placeholders and identifiers only. A budget stop leaves no rule or export statement in it.
- Exit codes: 0 when every check passed; 1 otherwise; 2 for a missing or invalid `[data_rules]` table or rule file,
  an unknown `--rule`, an unknown entity or column, or a type-incompatible atom or example value; 3 for the DSN, the
  connection, PostgreSQL older than 17, an unreadable table or missing pg8000.
- The stage writes nothing to PostgreSQL. The check role needs SELECT on the tables the rules read and, for the
  lookups, USAGE on the output schema. With `views = false` it also runs with a role that may create nothing.

## `[data_rules]` in crosscheck.toml

```toml
[data_rules]
file = "data-rules.toml"     # required: relative to crosscheck.toml
views = true                 # build creates v_rule_<id> and v_rules_subjects; data-rules certifies them
require_examples = true      # a rule without [[rule.example]] leaves rule_example:<id> not_run (no_example)
max_export_rows = 200000     # 1..2000000: rows path 2 may export, all entities together
max_rule_rows = 200000       # 1..2000000: output rows one compiled rule may return
```

The key table with types and defaults is in [references/config.md](../skills/semantic-crosscheck/references/config.md).
`file` follows the `[semantic_preflight]` path rules (no absolute path, `..`, empty segment, `:` or control character)
and must not resolve into the output directory. Without the table nothing changes: no stage reads a rule file, and
build output, check lists, results and reports are byte-identical to v0.21.1 apart from the engine fingerprint.
`expect` builds its fixture without rule views either way.

## The rule file

The rule file is TOML (UTF-8, a byte order mark is tolerated, at most 256 KiB). Validation is closed at every level:
unknown keys, wrong types, TOML floats, NUL characters, atoms placed on the wrong entity, unused sets or refs and
duplicate output columns are configuration errors (exit 2). Messages name positions only, counted from 0 as in
`crosscheck.toml` (for example `data-rules.toml rule[2] kit_without_bom where[1]`), never a constant, a cell or the
pattern.

### Top level

| Key | Required | Meaning |
|---|---|---|
| `version` | yes | Must be `1`. |
| `hierarchy` | when crosscheck.toml has more than one hierarchy | Name of a `[hierarchies.<name>]` entry; with exactly one hierarchy that one is used. Rules that use edges (structure, endpoint and quantified atoms, `same_signature`, `stored_metric`, `text_quantity`, endpoint key parts) need one. |
| `[sets]` | no | `name = [..]`: 1..1000 members of one TOML type (string, integer or boolean). TOML floats are refused; on a numeric column a string member must be a plain decimal numeral (`-?[0-9]+(\.[0-9]+)?`). Every set must be used by a rule. |
| `[refs.<name>]` | no | `entity`, `column` and an optional non-empty `where = { other_column = constant, ... }`: the non-NULL values of that column on the rows whose `where` columns equal the constants (a composite-key reference set). Every ref must be used by a rule. |
| `[[rule]]` | 1..128 | See below. |

Set and ref names match `^[a-z_][a-z0-9_]{0,62}$`. Rule ids match `^[a-z_][a-z0-9_]{0,55}$`, so `v_rule_<id>` stays a
valid 63-character identifier, are unique, and must not give a view name equal to a relation build already generates
in the output schema (shape views, closure and cycle tables).

### Common rule keys

- `id` and `kind` (`row`, `duplicate`, `mode_outlier`, `same_signature`, `stored_metric` or `text_quantity`): required.
- `severity`: required, `violation` or `warning`. There is no default, so the choice is explicit. A `warning` rule
  passes and still reports its count.
- `subject`: optional. It names an output column holding node keys: the node key column (rules on the node entity,
  and `stored_metric`), the hierarchy parent or child column (rules on the edge entity), or `member` or `group_id`
  (`same_signature`). Its column must have the type class of the node key. Only rules with a subject feed
  `v_rules_subjects`.
- `[[rule.example]]`: authored examples, see [Authored examples](#authored-examples).

### Kinds

| Kind | Keys (defaults) | Output columns |
|---|---|---|
| `row` | `entity` (any entity); `where`: a non-empty AND-list of atoms; `report` (`"key"`) | the report form |
| `duplicate` | `entity`; `key`: a list of own column names or `{ end = "parent"\|"child", column = C }` endpoint parts (edge entity only); `count = "rows"` or `{ distinct = C }` (`"rows"`); `min`, an integer >= 2 (2); `report = "group"` or `{ distinct = C }` with C an own-column key part (`"group"`) | `"group"`: the key parts (own name, or `<end>_<column>`) plus `n` = the group size; `{ distinct = C }`: C |
| `mode_outlier` | `entity`; `group_by` (own column); `value` (own numeric column); `min_mode_share`, a decimal string in (1/2, 1]; `ratio`, a decimal string > 1; optional `min_distinct = { column = C, n = 1.. }`; `report` (`"key"`) | the report form; `columns` adds `mode` |
| `same_signature` | `attrs`: a non-empty list of node columns; `min_members`, an integer >= 2 (2) | `group_id`, `member` |
| `stored_metric` | `metric = "height"`; `stored`: a numeric node column | `<node key column>`, `kind`, `stored`, `height` |
| `text_quantity` | `entity`: the hierarchy edge entity; `end`; `text` (node text column); optional `where_end` (column atoms on the endpoint's node row); `pattern`; `quantity` (own numeric column); `rel_tol`, a decimal string >= 0; optional `max_text_length` (4096, 1..1000000); `report` (`"key"`) | the report form; `columns` adds `numerator`, `divisor` |

Decimal settings (`min_mode_share`, `ratio`, `rel_tol`) are strings such as `"0.75"`; TOML floats are refused.

**Report forms** (`row`, `mode_outlier`, `text_quantity`):

- `"key"`: the entity's key columns. The keys must be unique and non-NULL, which a gate checks.
- `{ distinct = C }`: the non-NULL values of one own column.
- `{ columns = [C, ...] }`: the listed own columns, then the kind's derived columns, then `n`, the number of rows
  with identical output content (so rules over lines need no unique line key).

Two output columns with the same name are exit 2. With `views = true` an output column named `ord` is exit 2 as well,
because every lookup view adds an `ord` column.

### Atoms

`where` is an AND-list of at most 32 atoms. The same limit applies to each `any` branch, each quantified sub-where and
`where_end`.

| Atom | Allowed on | Form |
|---|---|---|
| column comparison | any entity (own column) | `{ column = C, op = "eq"\|"ne"\|"lt"\|"le"\|"gt"\|"ge", value = V }` |
| set membership | any entity | `{ column = C, op = "in"\|"not_in", set = S }` |
| prefix | any entity, text columns | `{ column = C, op = "starts_with", set = S }`: any member is a prefix |
| reference | any entity | `{ column = C, op = "not_in_ref", ref = R }` |
| null test | any entity | `{ column = C, op = "is_null"\|"is_not_null" }` |
| endpoint existence | hierarchy edge entity | `{ end = "parent"\|"child", exists = true\|false }` |
| endpoint attribute | hierarchy edge entity | `{ end = E, column = C, op = ..., value / set / ref }`: a comparison on the endpoint's node row |
| endpoint structure | hierarchy edge entity | `{ end = E, has_children\|has_parents\|in_hierarchy = true\|false }` |
| structure | hierarchy node entity | `{ has_children\|has_parents\|in_hierarchy = true\|false }` |
| quantified | hierarchy node entity | `{ children\|parents = "all"\|"any"\|"none", where = [...] }`: depth 1; the sub-where holds column and structure atoms on the neighbour's node row |
| disjunction | any entity | `{ any = [[atoms], [atoms], ...] }`: 2..8 branches, directly in a rule's `where`, never nested or inside a sub-where |

**Typing** is checked against the catalog after connecting and before any row is read; a mismatch is exit 2:

- numeric column (`int2`, `int4`, `int8`, `numeric`): `value` is a decimal-numeral string or an integer;
- text, varchar or bpchar column: a string; `lt`, `le`, `gt` and `ge` are refused, and `starts_with` needs such a
  column;
- boolean column: a TOML boolean, with `eq`, `ne` or a null test only;
- `not_in_ref`: the column and the ref column have the same type class; the ref's `where` constants suit their columns;
- the hierarchy parent and child columns, and every subject column, have the type class of the node key;
- `mode_outlier` `value`, `stored_metric` `stored`, `text_quantity` `quantity` and the hierarchy quantity of
  `same_signature` are numeric; `text_quantity` `text` is text.

A rule that reads a column of another type (including domains, floating-point and date types) or text with a
nondeterministic collation is not run (`column_type`, `collation`); cast the column in a database view and point the
entity at it.

Text columns that a rule compares with each other (the node key and the hierarchy parent and child columns, or a
`not_in_ref` column and its ref column) must share one collation. The type check does not compare collations. With two
different deterministic collations PostgreSQL cannot choose one: a rule that compares them fails with SQLSTATE 42P22
and is an `error` without a verdict, and with a `stored_metric` rule, parent and child columns of different
collations already fail the height gate with 42P21, which ends the snapshot (`data_rules:snapshot` `error`, no rule
decided). `next_safe_action` names the mismatch. Cast every compared column to the same collation (for example
`COLLATE "C"`) in database views and point the entities at them; casting only one side is what produces the mismatch.

### Pattern subset (`text_quantity`)

Python `re` and PostgreSQL regular expressions differ, so a pattern must stay inside a subset both read alike:

- **Allowed:** literal characters (non-ASCII too), a backslash plus ASCII punctuation, `.`, bracket classes with the
  ASCII ranges `0-9`, `a-z` and `A-Z`, `\d` and `\s` (expanded to `[0-9]` and `[ \t\n\r\f\v]`), greedy quantifiers
  `*`, `+`, `?`, `{m}`, `{m,}` and `{m,n}` with m <= n <= 255, capturing groups that are never quantified directly,
  `(?:...)`, a leading `^` and a trailing `$`.
- **Refused:** alternation, backreferences, lookaround, named groups, inline flags, lazy or possessive quantifiers,
  `\w`, `\W`, `\S`, `\D`, `\b`, `\B` and every other letter escape, POSIX classes, more than 256 characters, more than
  16 quantifiers, and a repetition (`*`, `+`, `{m,}` or an upper bound above 1) over a group that contains a
  quantifier. `?` and `{0,1}` over such a group stay allowed.
- **Backtracking.** These rules exclude the nested repetition that makes Python's backtracking exponential, but they
  do not bound it. Neighbouring quantified items that can match the same characters (`[0-9]+[0-9]*`, or
  `[0-9]*.[0-9]*`, since `.` matches a digit too) still backtrack polynomially on a long text that does not match,
  and the Python evaluation has no time limit (`statement_timeout_ms` bounds only the SQL side). Keep neighbouring
  quantified items disjoint, as `\s*([0-9.]+)\s*g` does, and `max_text_length` no larger than the data needs.
- The pattern needs 1 or 2 capturing groups: the amount, and optionally the divisor. Refused examples:
  `(?:[0-9]+)+x`, `(?:ab*){2,3}`.

### Authored examples

```toml
[[rule.example]]
spec = "where in the business rules these rows come from"   # required, non-empty
rows.part = [ { part_id = "K1", issue = "KIT" }, ... ]      # one list per entity the rule reads; [] allowed
rows.bom_line = [ ... ]
expect = [ { part_id = "K1" } ]                              # every output column in every row; [] = no output
```

An example holds small typed input rows and the rows the rule must output, written from the business rule rather
than from either evaluation. Both paths evaluate it without reading a table: path 1 compiles the rule over CTEs of
the bound example rows in a second read-only transaction, path 2 evaluates the same rows. `rule_example:<id>` passes
when the SQL output, the Python output and `expect` are equal for every example.

- Cells: numbers as decimal-numeral strings or integers, text as strings, booleans as booleans, NULL as
  `{ null = true }` (the convention of `[[gold.expect]]`).
- `rows.<entity>` is given for exactly the entities the rule reads. Every column the rule reads must be in every row;
  extra columns are allowed and type-checked.
- Values must fit their declared column types exactly (type, precision, scale, length), so no rounding happens on
  either path; a value that does not fit is exit 2 at catalog time.
- Node keys, and the keys of an entity reported by `"key"`, are unique and non-NULL. Duplicate `expect` rows are
  refused, and so are derived values no output can hold: NULL, an `n` or `divisor` that is not an integer >= 1, a
  `height` that is not an integer >= 0, a `numerator` <= 0 or an unknown `kind`. An authoring slip is then an exit 2,
  not a `common_mode` failure.
- Limits: 8 examples per rule; 200 input rows, 4000 input cells and 200 expected rows per example.

With `require_examples = true` (the default) a rule without examples leaves `rule_example:<id>` `not_run`
(`no_example`), so the stage cannot pass. With `require_examples = false` such a rule gets no `rule_example` check,
only a `not_proven` line.

## Semantic contract

Both paths implement this contract separately.

1. **Values.** Text compares exactly: no trimming, case folding or Unicode normalization. bpchar values are read
   without pad spaces (every compiled reference and the export cast them to text). Numbers compare by value (`2.0`
   equals `2.000000`); booleans as booleans. Supported base types: `int2`, `int4`, `int8`, `numeric`, `text`,
   `varchar`, `bpchar`, `bool`.
2. **NULL.** A comparison atom with a NULL operand is false, including `ne`, `not_in`, `starts_with` and
   `not_in_ref`. `is_null` and `is_not_null` test NULL explicitly; otherwise NULL is never a violation.
3. **Edges.** An edge is a row of the hierarchy edge entity with parent and child both non-NULL. `has_children(x)`:
   an edge with parent x; `has_parents(x)`: an edge with child x; `in_hierarchy`: either. Structure atoms do not need a
   node row.
4. **Endpoints.** `exists = false`: the endpoint is non-NULL and has no node row; `exists = true`: it is non-NULL and
   a node row exists. An endpoint attribute atom holds only when the endpoint is non-NULL, its node row exists and the
   comparison holds. Endpoint structure is false for a NULL endpoint.
5. **Quantifiers.** The neighbours of x are the distinct child (or parent) values over its edges. `all`: at least one
   neighbour, and every neighbour has a node row that satisfies the sub-where. `any`: some neighbour has a node row
   that satisfies it. `none`: no neighbour has such a row, which is true without neighbours.
6. **duplicate.** Rows form groups only when every key part is non-NULL; an endpoint part needs an existing endpoint
   row with a non-NULL attribute. The size is the number of rows, or of distinct non-NULL values of the `count`
   column. A group qualifies when its size is at least `min`.
7. **mode_outlier.** Groups are the non-NULL `group_by` values; N is the number of rows in the group, NULL values
   included. The mode is the most frequent non-NULL value. A group is eligible when the distinct non-NULL count of
   `min_distinct.column` is at least `n` (when given), count(mode) / N >= `min_mode_share` (above 1/2, so the mode is
   unique) and the mode is above 0. A value v is flagged when v >= ratio * mode or ratio * v <= mode.
8. **same_signature.** Members are node rows with at least one child edge. The signature is the set of (child, sum of
   the quantities of that parent's lines to the child); attributes compare exactly, with NULL equal to NULL. A class of
   at least `min_members` members qualifies; `group_id` is its smallest member (code-point order for text keys,
   numeric order for number keys). A NULL edge quantity makes the rule not run (`null_values`).
9. **stored_metric.** height(x) is 0 without child edges, else 1 plus the largest height of its children; a dangling
   child is a leaf. Every node row is compared with its stored value; NULL and an equal value are not flagged.
   Otherwise `kind` is `has_children_stored_zero`, `stored_below` (children, stored below the height and not 0),
   `stored_above` (children, stored above the height), `leaf_stored_nonzero` (no children, has parents) or
   `outside_stored_nonzero` (no edges). A height that `max_depth` cannot bound (a cycle or a deeper chain) makes the
   rule not run (`cycle_or_depth_limit`).
10. **text_quantity.** The endpoint row must exist and satisfy `where_end`. The pattern is searched leftmost in the
    endpoint's text. Capture 1 must be a plain decimal numeral X > 0; an optional capture 2 must be digits N > 0, and
    N = 1 when that group did not take part; anything else is a defined no-match. A row with a non-NULL quantity q is
    flagged when abs(q * N - X) <= rel_tol * X; the output adds `numerator` = X and `divisor` = N. A text longer than
    `max_text_length` makes the rule not run (`value_too_long`).
11. **Exact arithmetic.** Only addition, subtraction, multiplication and comparison are used: numeric in PostgreSQL,
    Decimal with precision 60 and Inexact trapped in Python. An inexact step is an evaluator error, never a rounded
    answer.

**Canonical cells and order.** Numbers in exact canonical form (trailing zeros removed), text exactly, booleans,
NULL. Rows sort by type and then value: NULL < false < true < numbers by value < text by code point. Code-point order
equals UTF-8 byte order and `COLLATE "C"`, so the `ord` column of the views and the Python order agree, and
`rule_lookup:<id>` checks that they do. Outputs are compared as multisets: duplicates count.

## Gates and budgets

Inside the one READ ONLY REPEATABLE READ snapshot, before any rule runs:

- the server encoding must be UTF8 (`server_encoding`);
- the rows of every entity the rules read are counted, each no further than `max_export_rows + 1`; above
  `max_export_rows` in total every rule is `not_run` (`resource_limit`) and no rule, lookup or export statement runs;
- node keys, and the keys of an entity a rule reports by `"key"`, must be non-NULL and unique
  (`data_rules:key:<entity>`; dependent rules `key_null`, `key_not_unique`);
- numeric columns a rule reads must hold no NaN or infinity (`non_finite_values`);
- `same_signature` needs non-NULL edge quantities (`null_values`), `stored_metric` bounded heights
  (`cycle_or_depth_limit`), `text_quantity` texts within `max_text_length` (`value_too_long`).

Each compiled rule runs under its own savepoint and may return at most `max_rule_rows` rows (above that: that rule
`resource_limit`); an SQL error in one rule leaves the others running. The export reads the needed columns, and its
row count must equal the budget count (`export_incomplete` otherwise). Path 2 applies the same gates to the exported
rows; when only a Python gate trips, the rule is `not_run` with that reason and `rule_xcheck:<id>` records
`not_evaluated=python`. A gate never becomes a pass.

## Lookup views

With `views = true`, build creates in the output schema:

| View | Columns |
|---|---|
| `v_rule_<id>` | the rule's output columns, then `ord` (bigint): the row number in canonical order |
| `v_rules_subjects` | `subject` (node key type), `severity` (`violation` or `warning`, the worst over its rules), `severity_rank` (0 or 1), `rule_count`, `rules` (text[] of rule ids in configuration order), `ord` (by severity rank, then subject) |

Find the rows in the database, with the user (`sxc_out` stands for `source.output_schema`):

```sql
SELECT subject, severity, rule_count, rules FROM sxc_out.v_rules_subjects ORDER BY ord;
SELECT * FROM sxc_out.v_rule_kit_without_bom ORDER BY ord;
```

- **The views are live.** They return the certified rows only at the time `data-rules` checked them; rerun data-rules
  after the data changes. The counts in its result let a reader see drift.
- build creates the rule views in one write transaction of its own, after the shape views and before the closures,
  under the output schema's advisory lock. It drops `v_rules_subjects`, creates or replaces each `v_rule_<id>` under a
  savepoint (`rule_view:<id>`; a view whose columns changed is dropped and created again), drops the marked rule views
  the rule file no longer generates, and creates `v_rules_subjects` over the subject rules' views
  (`rule_subjects:view`). Each view carries this fixed comment:

  ```text
  semantic-crosscheck data rule view: rows are cross-checked only by sxc.py data-rules
  ```

  The views of the current rule file are created or replaced by name, as the shape views are, and
  `v_rules_subjects` is dropped and created again. Any other view whose name starts with `v_rule_` is dropped only
  when it carries that exact comment, and never with CASCADE, so a user view with the same prefix and no marker
  survives. Keep the output schema for generated objects.
- build does not apply the data-dependent gates: `data-rules` decides whether a view's rows can be certified.
  `rule_lookup:<id>` is `not_run` while the rule is not agreed (`rule_not_agreed`), and when build has not run
  (`build_missing`), did not create the view (`view_not_built`), created it from another rule file or configuration
  (`view_stale`: `build/build.sql` lacks the statement data-rules generates now) or the view is gone (`view_missing`).
- **A lookup certifies rows, not the rule's verdict.** `rule_lookup:<id>` compares the view with the rows both
  evaluations agreed on. It can pass while `rule:<id>` is `not_run` because an authored example failed
  (`example_mismatch`), and that rule's subjects then stay in a passing `v_rules_subjects`. Check `rule:<id>` before
  acting on a rule's rows.
- **Constants are visible in view definitions.** The rule's constants appear as literals in the view definitions
  (`pg_views`) and in `build/build.sql`, as `sql_violation` predicates do; the output directory has a `.gitignore` of
  `*`.
- With `views = false` build creates no rule view and data-rules certifies none; a `not_proven` line says so. build
  does not drop the rule views an earlier build created: they stay live in the output schema and still return source
  rows to anyone who can read it. Drop them by hand when they must go (`v_rules_subjects` first, then each
  `v_rule_<id>` that carries the comment above).

## Checks

| Stage | Check id | Passes when | Other outcomes |
|---|---|---|---|
| data-rules | `data_rules:snapshot` | the budget was counted, the gates read and the export complete (`rows_<entity>`, `rows_total`, `limit`) | `not_run` (`resource_limit`, `server_encoding`); `error` (SQLSTATE outside the rule savepoints, `export_incomplete`) |
| data-rules | `data_rules:key:<entity>` | no NULL and no duplicate key | `fail` (`null_keys`, `duplicate_keys`) |
| data-rules | `rule:<id>` | both paths agree, no example failed, and the rule found nothing or is a `warning` | `fail` (agreed, a `violation` rule found rows); `not_run` (`paths_disagree`, `example_mismatch`, a gate or budget reason, `snapshot_not_read`); `error` (SQLSTATE, `evaluator`) |
| data-rules | `rule_xcheck:<id>` | the canonical output multisets are equal (`sql_outputs`, `py_outputs`, `sql_ms`, `py_ms`) | `fail` (`sql_only`, `py_only`); `not_run` (a gate reason, `path_error`) |
| data-rules | `rule_example:<id>` | SQL = Python = `expect` for every example | `fail` (`common_mode`, `sql_mismatch`, `py_mismatch`, `both_mismatch` counts); `not_run` (`no_example`, `example_not_evaluable`); `error` |
| data-rules | `rule_lookup:<id>` | `v_rule_<id>` returns exactly the agreed rows in canonical `ord` order, whatever the rule's verdict | `fail` (`lookup_only`, `agreed_only`, `order_differs`); `not_run` (see above); `error` |
| data-rules | `rule_subjects:lookup` | `v_rules_subjects` equals the Python roll-up of the agreed outputs, in order | `fail`; `not_run` (`incomplete`, `excluded_by_option`, the view reasons); `error` |
| data-rules | `scope:rules` | never passes | `not_run` (`excluded_by_option`): `--rule` left rules out |
| data-rules | `data_rules:inputs` | error check only | `error` (`inputs_changed`): crosscheck.toml or the rule file changed during the run |
| build | `rule_view:<id>`, `rule_subjects:view` | the view was created from the current rule file | `error` (SQLSTATE; the view is dropped); `not_run` (`column_type`, `collation`, `rule_view_missing`) |

The full wording is in the [check catalogue](design.md#6-check-catalogue). `config_ref` of a rule check is
`data_rules.rule[<i>] <id>`. `violations` counts keys, values or rows (the sum of `n`) for `row`, `text_quantity` and
`mode_outlier`, groups or values for `duplicate`, members (plus `groups`) for `same_signature`, and nodes (plus one
count per kind) for `stored_metric`; `outputs` counts distinct output rows.

**Reading a failure.** Follow `next_safe_action`, which takes the first match of: an error with its SQLSTATE hint; a
disagreement (check the rule's column types, NULLs and pattern against this page, report a defect if the rule is
plain); `common_mode` (check the example's expected rows against the business rule first, then the rule); a gate or
budget (raise a budget, cast a column in a view, fix duplicate keys, see build's `closure:<h>` for cycles); a lookup
(rerun build, then data-rules, or set `views = false`); violations found (query the views with the user, fix the data
or the rule file, rerun build and data-rules).

## Limits

- **Common mode.** Agreement of the two evaluations does not show that a rule or its constants are the right business
  rule: both paths read the same rule file. Authored examples pin only the cases they contain, and `common_mode`
  appears only when an example covers the misunderstanding. Write examples from the business rule, never from an
  output. Every run with an agreed rule carries a `not_proven` line saying this.
- **Live views.** The lookup views equal the certified rows only at check time.
- **Constants in view definitions.** Rule constants are readable in the view definitions and in `build.sql`; row
  values are only in the views, never in results.
- **Regex engines.** The pattern subset keeps Python and PostgreSQL matches equal for the supported forms; any
  remaining difference in how captures are assigned shows up as a disagreement (`paths_disagree`), never as a pass.
- **Pattern backtracking.** The subset excludes nested repetition but does not bound Python's backtracking over
  neighbouring quantified items that can match the same characters, and the Python evaluation has no time limit. Keep
  such items disjoint (see [Pattern subset](#pattern-subset-text_quantity)).
- **Collations of compared columns.** Text columns a rule compares with each other must share one collation. Two
  different deterministic collations make the rules comparing them errors (SQLSTATE 42P22), or end the snapshot
  (42P21 from the height gate of a `stored_metric` rule): never a verdict. Cast them to one collation in database
  views (see [Typing](#atoms)).
- **Views after opting out.** `views = false` stops build from creating rule views but does not drop the ones an
  earlier build created; they stay live until dropped by hand.
- **Single-column node key.** The hierarchy node entity needs a one-column key.
- **Report separation.** `report` never reads the data-rules result; run data-rules and read its result separately.
- **No file of keys.** The keys a rule found are only in the database views, never written to a file.
- **Upgrading.** New engine modules change the engine fingerprint, so rerun the operational stages after upgrading.

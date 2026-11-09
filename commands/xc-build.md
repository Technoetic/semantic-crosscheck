---
description: "Build the shape violation views, the hierarchy closures and, when configured, the data rule views in the output schema and write build.sql, shapes.ttl and fk_review.sql for review"
argument-hint: "[--config crosscheck.toml] [--hierarchy NAME] [--out DIR] [--quiet]"
---

Run the `build` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not shell
code: pass each one as its own argument and never splice them into a shell string.

## When to use

- After [/xc-doctor](xc-doctor.md) passes with `--config` (for the demo: after [/xc-demo](xc-demo.md)
  and doctor).
- Again after every change to `entities`, `hierarchies` or `shapes` in `crosscheck.toml`, and after the
  source data changes. `reason`, `cube` and `verify` read what `build` produced, and `report` counts
  them as stale until they run again after it.
- With `[data_rules]` and `views = true`: again after every change to the rule file, before
  [/xc-data-rules](xc-data-rules.md), which certifies the rule views only when build created them from the
  rule file as it is now.

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" build --config crosscheck.toml
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" build --config crosscheck.toml --hierarchy has_component
```

- `--hierarchy NAME` rebuilds only that hierarchy's closure. Every other hierarchy then gets
  `closure:<other>` `not_run` (`excluded_by_option`), so the run exits 1 even when NAME passes, and
  `reason` and `verify` treat those closures as not passed. Use it while fixing one hierarchy, then
  rerun `build` without it.
- Everything derived from the configuration alone is generated first, so a bad entry fails with exit
  code 2 before the database is touched. Then every table and column the shapes and hierarchies name
  must exist (exit 2) and be readable by the check role (exit 3).
- The stage connects with the check role and writes only to `source.output_schema` and to temporary
  tables: one `v_shape_<id>` view per shape, one `<h>_closure` table per hierarchy (applied with
  `MERGE`) and `<h>_cycles` with example cycle paths when cycles exist. It reads the source tables inside
  these write transactions, so it relies on the check role's privileges, which doctor proves. The output
  schema is created when missing and the check role may create it; otherwise the stage stops with exit
  code 3 and prints the statement an administrator should run.
- With `[data_rules]` and `views = true` (only then) build also loads the rule file before connecting (exit 2
  when it is invalid), checks the columns its rules read (exit 2 or 3) and renders every rule view from their
  catalog types (exit 2 on a type-incompatible rule) before the output schema is touched. After the shape
  views, one more write transaction creates or replaces `v_rule_<id>` for every rule (its output rows plus
  `ord`) and `v_rules_subjects` (one row per node key the subject rules found, with the worst severity and
  the rule ids), each marked with a fixed comment. These names are created or replaced as the shape views
  are; of the other views whose names start with `v_rule_`, build drops only marked ones the rule file no
  longer generates, never with CASCADE. The views hold row values in the output schema,
  like `v_shape_<id>`; the rule's constants appear as literals in their definitions and in `build.sql`.
  Without `[data_rules]` none of this runs. See [docs/data-rules.md](../docs/data-rules.md).
- Every write transaction first takes an advisory lock on the output schema, so two builds of the same
  output schema run one after the other; the wait counts against `source.statement_timeout_ms`.
- Files next to the configuration: `.sxc/build/result.json`, `.sxc/build/build.sql` (every statement,
  for review), `.sxc/build/shapes.ttl` (SHACL shapes generated from the configuration) and, while a shape
  has `fk_ddl = "review_only"`, `.sxc/build/fk_review.sql` (foreign keys written for a person to review,
  never executed). `--out DIR` changes the directory; `--quiet` prints only the summary line.
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## Success

Exit code 0 with:

- `shape:<id>`: no violations. A shape with `severity = "warning"` passes and reports its count.
- `closure:<h>`: the hierarchy is acyclic (or `on_cycle = "report"`) and fits within `max_depth`. The
  detail has edge, pair, path and depth counts.
- `closure_apply:<h>`: after the `MERGE`, the table equals the recomputation in both directions
  (inserted, updated and deleted counts are reported).
- `rule_view:<id>` (with `[data_rules]` and `views = true`): `v_rule_<id>` was created or replaced from the
  current rule file (detail `kind`, `view`). `rule_subjects:view`: `v_rules_subjects` was created over the
  views of every rule that names a subject. A pass says the view exists, not that its rows are right:
  `data-rules` checks them.

## Failures

Fix `crosscheck.toml` or the source data, never `build.sql`, `shapes.ttl` or the generated tables and
views: they are rewritten on every run.

- `shape:<id>` fails: the data breaks the rule; the detail has the violation count and the view that
  lists the violating keys (`v_shape_<id>` in the output schema). Results hold counts only. Decide with
  the user whether the data or the rule is wrong, and look at the violating rows in the database, not in
  a result or a message. Do not loosen a rule only to get a pass.
- `shape:<id>` is `error`: the view could not be built or counted (a broken `sql_violation`
  predicate, a timeout). The detail has the SQLSTATE; the view is dropped and the other views are still
  built. Fix the predicate in `crosscheck.toml` or raise `source.statement_timeout_ms`.
- `closure:<h>` fails with `reason = cycle`: the detail has `cycle_nodes` and `cycle_examples`; the
  example paths are in `<h>_cycles` for review. Fix the edges at their source, or set
  `on_cycle = "report"` when cycles are legitimate (edges inside each cycle are then left out of the
  closure and listed as not proven). The closure table is left untouched, and every gold query, V3 test
  and V4 operator over this hierarchy is `not_run` in `verify` until the check passes.
- `closure:<h>` fails with `reason = depth_limit`: raise `max_depth` (at most 64) only when the
  hierarchy really is that deep; otherwise look for a runaway chain in the edges.
- `closure:<h>` is `error`: the closure could not be computed; the `next:` line explains the SQLSTATE
  (`42501`: a missing privilege such as `TEMPORARY` on the database; `57014`: the statement timeout;
  `23505`, `40001`, `40P01`, `42P06` or `42P07`: another run wrote the same output schema, rerun build).
- `closure_apply:<h>` fails: the table differs from the recomputation, typically because something
  else writes to it. Rerun `build`; never edit the closure table by hand.
- A note about `null_quantity_edges`: edges with a NULL quantity give a NULL `qty` for the affected
  pairs. Fix the data or accept it explicitly in the report.
- `rule_view:<id>` is `error`: the view could not be created (the detail has the SQLSTATE); it is dropped
  and the other rule views are still built. Fix the rule in the rule file, then rerun build. `rule_view:<id>`
  is `not_run` (`column_type`, `collation`): the rule reads a column of an unsupported type or a
  nondeterministic collation, so it gets no view; cast the column in a database view, change the rule, or
  set `data_rules.views = false`. `rule_subjects:view` is `not_run` (`rule_view_missing`) while a subject
  rule's view is missing.
- Exit code 2: unknown entity, column or identifier in the configuration, or a table or column that does
  not exist in the database; with `[data_rules]` and `views = true` also an invalid rule file or a rule
  whose constants do not suit its column types. Exit code 3: no connection, PostgreSQL older than 17, a
  source table the check role cannot read, or an output schema it cannot create or use.

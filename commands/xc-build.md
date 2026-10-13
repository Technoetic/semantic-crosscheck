---
description: "Build the shape violation views and hierarchy closures in the output schema and write build.sql, shapes.ttl and fk_review.sql for review"
argument-hint: "[--config crosscheck.toml] [--hierarchy NAME]"
---

Run the `build` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not shell
code: pass each one as its own argument and never splice them into a shell string.

## When to use

- After [/xc-doctor](xc-doctor.md) passes (and after [/xc-demo](xc-demo.md) when using the demo).
- Again after every change to `entities`, `hierarchies` or `shapes` in `crosscheck.toml`, and after the
  source data changes. `reason`, `cube` and `verify` read what `build` produced.

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" build --config crosscheck.toml
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" build --config crosscheck.toml --hierarchy has_component
```

- `--hierarchy NAME` builds only that hierarchy.
- Everything derived from the configuration alone is generated first, so a bad entry fails with exit
  code 2 before the database is touched.
- The stage connects with the check role, only reads the source tables and writes only to
  `source.output_schema`: one `v_shape_<id>` view per shape, one `<h>_closure` table per hierarchy
  (applied with `MERGE`) and `<h>_cycles` with example cycle paths when cycles exist. The output schema
  is created when missing and the check role may create it; otherwise the stage stops with exit code 3
  and prints the statement an administrator should run.
- Files next to the configuration: `.sxc/build/result.json`, `.sxc/build/build.sql` (every statement,
  for review), `.sxc/build/shapes.ttl` (SHACL shapes generated from the configuration) and
  `.sxc/build/fk_review.sql` (foreign keys for shapes with `fk_ddl = "review_only"`; written for a
  human to review, never executed).
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## Success

Exit code 0 with:

- `shape:<id>`: no violations. A shape with `severity = "warning"` passes and reports its count.
- `closure:<h>`: the hierarchy is acyclic (or `on_cycle = "report"`) and fits within `max_depth`. The
  detail has edge, pair, path and depth counts.
- `closure_apply:<h>`: after the `MERGE`, the table equals the recomputation in both directions
  (inserted, updated and deleted counts are reported).

## Failures

Fix `crosscheck.toml` or the source data, never `build.sql`, `shapes.ttl` or the generated tables and
views: they are rewritten on every run.

- `shape:<id>` fails: the data breaks the rule; the detail has the violation count. Decide with the
  user whether the data or the rule is wrong. Do not loosen a rule only to get a pass. Results hold
  counts only; `output.show_rows` (default 0) is the only way to write example rows, into a local
  samples file that must not be shared.
- `shape:<id>` is `error`: the view could not be built or counted (a broken `sql_violation`
  predicate, a timeout). The detail has the SQLSTATE; the other views are still built. Fix the
  predicate in `crosscheck.toml` or raise `source.statement_timeout_ms`.
- `closure:<h>` fails with `reason = cycle`: the detail has `cycle_nodes` and `cycle_examples`; the
  example paths are in `<h>_cycles` for review. Fix the edges at their source, or set
  `on_cycle = "report"` when cycles are legitimate (edges inside each cycle are then left out of the
  closure). Gold queries over this hierarchy are `not_run` in `verify` until the check passes.
- `closure:<h>` fails with `reason = depth_limit`: raise `max_depth` (at most 64) only when the
  hierarchy really is that deep; otherwise look for a runaway chain in the edges.
- `closure_apply:<h>` fails: the table differs from the recomputation, typically because something
  else writes to it. Rerun `build`; never edit the closure table by hand.
- A note about `null_quantity_edges`: edges with a NULL quantity give a NULL `qty` for the affected
  pairs. Fix the data or accept it explicitly in the report.
- Exit code 2: unknown entity, column or identifier in the configuration. Exit code 3: connection.

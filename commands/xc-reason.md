---
description: "Cross-check the SHACL shapes (pySHACL) against the SQL violation views and the OWL ontology (HermiT) against the SQL closure and class definitions"
argument-hint: "[--config crosscheck.toml] [--only shacl,owl] [--synthetic-witnesses] [--out DIR] [--quiet]"
---

Run the `reason` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not shell
code: pass each one as its own argument and never splice them into a shell string.

## When to use

- After [/xc-build](xc-build.md), and again after changing `shapes` or `ontology` in
  `crosscheck.toml` (and after every build: `report` counts an older reason result as stale).
- It needs `requirements/reason.txt` (owlready2, pySHACL, rdflib) and, for the OWL part, Java 11 or
  later (`SXC_JAVA`, or `java` on `PATH`).

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" reason --config crosscheck.toml
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" reason --config crosscheck.toml --only shacl
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" reason --config crosscheck.toml --synthetic-witnesses
```

- SHACL: the needed rows are exported as RDF in memory (target rows with the shape paths, referenced
  entities with their types) in one read-only snapshot and checked by pySHACL without inference against
  `build/shapes.ttl`. Per shape, the set of focus-node keys must equal the set of keys in `v_shape_<id>`.
- OWL: an in-memory ontology (node class, one pairwise disjoint subclass per `ontology.classes`
  value, the edge property `<h>` and its transitive super-property `<h>_plus`, `no_children` axioms
  and the `ontology.defined` classes) is checked by HermiT in a private temporary directory. The
  Pellet reasoner bundled with owlready2 is never called.
- `--only shacl` (or `--only owl`) runs one part and adds the check `scope:owl` (or `scope:shacl`) as
  `not_run`, so a narrowed run exits 1 and is never a result. Without Java, `--only shacl` still
  compares the shapes; report the OWL checks as not run, not as passed.
- Database reads are read-only transactions. Results go to `.sxc/reason/result.json` next to the
  configuration (or under `--out DIR`); `--quiet` prints only the summary line.
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## Success

Exit code 0 with `shacl:<id>`, `owl:consistency`, `v5:closure:<h>` and `v5:defined:<name>` passing.
A passing `shacl:<id>` means SHACL and SQL agree on which rows violate the shape, not that there are
no violations (that is `shape:<id>` in `build`). One kind of shape is not compared with SHACL and is
listed as not proven, "checked only in SQL":

- `sql_violation`: a shape with an `sql_violation` predicate, also combined with `class` or bounds (no
  SHACL form).

A shape that combines `class` with bounds is compared: its node shape has two property shapes, `sh:class`
on the column and the bounds on the same value written as a number on `<entity>.<column>/value`, and its
SHACL keys are the rows either one flags (releases up to 0.5.0 listed it as `class_and_bounds`, checked
only in SQL; after upgrading, rerun [/xc-build](xc-build.md) before reason). Its target and class-entity rows
now count against `verify.shacl_max_rows`, and next to a bound-only shape on its column it opens an extra value
group, so an unchanged configuration can exceed the limit: check the row count after upgrading or raise it.

Shapes on the same target column that need different kinds of RDF value (a `class` shape and a bound
shape, or `class` shapes naming different entities; a shape with `class` and bounds counts as a `class` shape
here) are all compared: each shape goes to the first value
group in which its column carries no other kind of value (`main`; a colliding shape opens `extra:1`, `extra:2`,
...), each group is validated on its own data graph, and each `shacl:<id>` names its `value_group`.
Without a colliding shape there is one group, `main`. `verify.shacl_max_rows` bounds the rows of all
groups together. If validating one group fails (pySHACL returns no results graph or raises), only that
group's shapes are `error` (`validation_failure`, with the exception type only).

A defined class without members in OWL or in SQL passes vacuously and is listed as not proven.

`--synthetic-witnesses` adds separate checks of configured definitions on small synthetic graphs,
including positive examples and nonmember controls with independently specified expected memberships.
It executes HermiT and the SQL definition on those fixtures; source tables are not modified. A missing
inferred membership is not an explicit OWL negation. Impossible positive examples under the configured
axioms are reported explicitly, never made valid by dropping an axiom. Synthetic success does not
remove the real-data vacuity disclosure. The flag cannot be combined with `--only shacl`.

## Failures

Fix `crosscheck.toml` (or the data), never `shapes.ttl` or the generated views.

- `shacl:<id>` fails: the two formulations disagree; the detail counts keys found only by SQL and
  only by SHACL. Typical causes: NULL handling (SHACL ignores missing values), a referenced row without
  a type, or a bound compared on a different type. See
  [references/pitfalls.md](../skills/semantic-crosscheck/references/pitfalls.md).
- `shacl:<id>` is `not_run`: `shape:<id>` did not pass or fail in build, or its view does not exist;
  rerun [/xc-build](xc-build.md).
- `owl:consistency` fails: the data contradicts an axiom, for example an item whose class is listed in
  `ontology.no_children` has a child. Fix the data or the `ontology` entry. While the ontology is
  inconsistent the V5 checks cannot prove anything and are `not_run` (`inconsistent`).
- `v5:closure:<h>` fails: the OWL transitive closure and the SQL closure table differ. Rerun
  [/xc-build](xc-build.md) and then this stage; if it persists, report it as a defect with the counts.
- `v5:closure:<h>` or a `contains_class` definition is `not_run`: build's `closure:<h>` did not pass
  (`closure_not_passed`), it left the edges inside cycles out (`cycle_edges_dropped`), or the closure
  table is missing (`closure_table_missing`). Fix the build first.
- `v5:defined:<name>` fails: the members of a defined class differ from its SQL definition. Check
  `ontology.class_column`, `ontology.classes` and the `kind` and `class` of the definition.
- `scope:shacl` or `scope:owl` is `not_run`: the run was narrowed with `--only`. Rerun without it before
  reporting a result.
- Exit code 2: no build result, or `build/shapes.ttl` no longer matches the shapes in `crosscheck.toml`
  (rerun build first). Exit code 3: a missing package or Java for a part that runs (the message names it
  and offers `--only` for the other part), or no connection. Size limits come from
  `verify.shacl_max_rows` and `verify.owl_max_individuals`: a part above its limit leaves explicit
  `not_run` checks and an explanation in `not_proven`, so neither reason nor report can pass.

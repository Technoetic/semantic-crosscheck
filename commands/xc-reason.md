---
description: "Cross-check the SHACL shapes (pySHACL) against the SQL violation views and the OWL ontology (HermiT) against the SQL closure and class definitions"
argument-hint: "[--config crosscheck.toml] [--only shacl,owl]"
---

Run the `reason` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not shell
code: pass each one as its own argument and never splice them into a shell string.

## When to use

- After [/xc-build](xc-build.md), and again after changing `shapes` or `ontology` in
  `crosscheck.toml`.
- It needs `requirements/reason.txt` (owlready2, pySHACL, rdflib) and, for the OWL part, Java 11 or
  later (`SXC_JAVA`, or `java` on `PATH`). Without Java, run `--only shacl` and report the OWL checks
  as not proven.

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" reason --config crosscheck.toml
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" reason --config crosscheck.toml --only shacl
```

- SHACL: the needed rows are exported as RDF in memory (target rows with the shape paths, referenced
  entities with their types) and checked by pySHACL without inference against `build/shapes.ttl`.
  Per shape, the set of focus-node keys must equal the set of keys in `v_shape_<id>`.
- OWL: an in-memory ontology (node class, one pairwise disjoint subclass per `ontology.classes`
  value, the edge property `<h>` and its transitive super-property `<h>_plus`, `no_children` axioms
  and the `ontology.defined` classes) is checked by HermiT in a private temporary directory. The
  Pellet reasoner bundled with owlready2 is never called.
- Results go to `.sxc/reason/result.json` next to the configuration.
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## Success

Exit code 0 with `shacl:<shape>`, `owl:consistency`, `v5:closure:<h>` and `v5:defined:<name>` passing.
A passing `shacl:<shape>` means SHACL and SQL agree on which rows violate the shape, not that there are
no violations (that is `shape:<id>` in `build`). Shapes defined only by `sql_violation` have no SHACL
form and are listed as not proven.

## Failures

Fix `crosscheck.toml` (or the data), never `shapes.ttl` or the generated views.

- `shacl:<shape>` fails: the two formulations disagree; the detail counts keys found only by SQL and
  only by SHACL. Typical causes: NULL handling (SHACL ignores missing values), a referenced row without
  a type, or a bound compared on a different type. See
  [references/pitfalls.md](../skills/semantic-crosscheck/references/pitfalls.md).
- `owl:consistency` fails: the data contradicts an axiom, for example an item whose class is listed in
  `ontology.no_children` has a child. Fix the data or the `ontology` entry. While the ontology is
  inconsistent the V5 checks cannot prove anything and are not run.
- `v5:closure:<h>` fails: the OWL transitive closure and the SQL closure table differ. Rerun
  [/xc-build](xc-build.md) and then this stage; if it persists, report it as a defect with the counts.
- `v5:defined:<name>` fails: the members of a defined class differ from its SQL definition. Check
  `ontology.class_column`, `ontology.classes` and the `kind` and `class` of the definition.
- Missing Java or packages are environment problems (exit code 3 or `not_run` checks): install them and
  rerun. Size limits come from `verify.shacl_max_rows` and `verify.owl_max_individuals`.

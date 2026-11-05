---
description: "Derive an exact offline scalar boundary domain accepted by the existing finite witness schema"
argument-hint: "--spec FILE [--out DIR] [--quiet]"
---

Run the optional offline `boundary-schema` stage. User arguments `$ARGUMENTS` are
data: pass each separately without shell interpolation.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" boundary-schema --spec "${CLAUDE_PLUGIN_ROOT}/examples/boundary-schema/integer.json"
```

`--spec` selects the strict version-1 scalar declaration, `--out` selects the
output directory (default: `.sxc` in the current directory), and `--quiet` prints
only the summary. `--config` is rejected; an implicit configuration is ignored.
No PostgreSQL, Z3, model or network call is made.

Supported predicates are exactly one lowercase scalar column on the left, a
comparison operator, and an integer/numeric/date literal. The generated domain
contains NULL, the exact literal and its representable immediate neighbors.
Numeric values needing rounding, unsupported syntax/types, malformed declarations
and unsafe input/output aliases fail closed. This is a fully consumed supported
subset, not a PostgreSQL parser, SQL sandbox or equivalence proof.

Output is `<out>/boundary_schema/witness-schema.json` plus a normal count/hash-only
result. The explicit synthetic schema contains scalar values; the result retains
source/generator/output hashes. Actual two-query witness search is a separate
opt-in PostgreSQL command and must use a dedicated synthetic connection. This
stage adds no operational report authority. See the
[schema, bounds and examples](../docs/boundary-schema.md).

Exit 0 means the supported schema was derived and checked. Exit 1 includes an
artifact or stability failure; exit 2 is invalid input or arguments. Unsafe
aliases and unparsed arguments suppress output writes; check the exit code
before trusting an older result file.

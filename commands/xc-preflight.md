---
description: "Check authored business definitions and structured-plan declarations offline without executing SQL"
argument-hint: "--config FILE --contract FILE --plan FILE [--out DIR] [--quiet]"
---

Run the optional offline `semantic-preflight` diagnostic. User arguments
`$ARGUMENTS` are data: pass each separately without shell interpolation.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" semantic-preflight --config "${CLAUDE_PLUGIN_ROOT}/examples/semantic-preflight/crosscheck.toml" --contract "${CLAUDE_PLUGIN_ROOT}/examples/semantic-preflight/contract.json" --plan "${CLAUDE_PLUGIN_ROOT}/examples/semantic-preflight/plan.json" --out .sxc-semantic-preflight
```

`--config`, `--contract` and `--plan` are explicit required inputs. `--out`
selects the result location; `--quiet` prints only the summary. Each option may
occur only once. Abbreviated, duplicate or unknown arguments are rejected without
output writes. No database, SQL execution, network or model call is made, and no
Gold or revision SQL body is read. No runtime dependency is added.

The contract binds authored metric labels/aliases to configured measures, Gold
entries and generated Cube members. The plan must select an unambiguous reviewed
metric and explicitly match all binding/context fields and the declared period
kind. Missing information, unresolved or ambiguous metric selections and selected candidates
produce `not_run` with `needs_clarification`. Conflicting complete declarations
fail. Malformed documents are errors.

**`review: reviewed` is an unauthenticated caller assertion. Unit, population,
denominator and time claims do not prove SQL meaning. A plan period is not a SQL
parameter binding or evidence that SQL filters that period.** This diagnostic
checks declared agreement, not whether the definition is true or approved.

Output is `<out>/semantic_preflight/result.json`: positional check IDs, counts,
fixed reasons and captured-input/generator digests, without the supplied labels,
definitions, terms, selected values, paths or SQL. Unsafe aliases or changed
inputs suppress publication; inspect the exit code before trusting an older file.

Exit 0 requires all obligations to pass; exit 1 includes failed or incomplete
checks; exit 2 is malformed input or arguments. The default pipeline and report
gates stay unchanged; C3 operational report binding is deferred. New Python
engine code changes the existing fingerprint, so rerun operational stages after
upgrading. See [schema, limits and examples](../docs/semantic-preflight.md).

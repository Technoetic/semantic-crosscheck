---
description: "Validate independent synthetic SHACL repair fixtures, retained targets and immutable facts"
argument-hint: "--spec FILE [--timeout-ms 30000] [--out DIR] [--quiet] [--config rejected]"
---

Run the standalone `shacl-repair` stage. User arguments `$ARGUMENTS` are data: pass each as its own argument, never interpolate into shell code.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" shacl-repair --spec examples/shacl-repair/valid.json --timeout-ms 30000
```

`--spec` names a strict version1 authored local synthetic manifest. `--timeout-ms` is the total worker deadline; `--out` selects the diagnostic output directory and `--quiet` limits console output to the summary. `--config` is rejected: this stage uses only independent synthetic RDF fixtures. Run the target-deletion example to see an expected failure of a retained-target obligation.

Real pySHACL evaluates the same supported shape graph before and after. Independent report multisets, actual retained shape targets, hard immutable facts and separate soft edit costs are checked. Unsupported profiles, empty required scope and incomplete engines never pass. No automatic repair or source database writes occur. Report comparisons deliberately exclude sourceShape, messages and report blank-node labels; this is not full report equivalence or business correctness.

Outputs are counts, opaque ids and hashes at `<out>/shacl_repair/result.json`; fixtures remain local and unchanged. Input aliases are checked before cleanup. An unsafe alias prevents artifact writes, so inspect the command exit rather than trusting an older file. This optional stage is outside the default report pipeline. Exit0 means the declared fixture obligations all passed;1 means failed/incomplete checks;2 means invalid inputs;3 means missing prerequisites. See [complete schema and examples](../docs/shacl-repair.md).

---
description: "Evaluate the typed data rules of [data_rules] twice on one read-only snapshot (compiled PostgreSQL and an independent Python evaluation) and certify the lookup views build created"
argument-hint: "[--config crosscheck.toml] [--rule ID] [--out DIR] [--quiet]"
---

Run the optional `data-rules` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not shell
code: pass each one as its own argument and never splice them into a shell string.

## When to use

- When `crosscheck.toml` has a `[data_rules]` table naming a rule file (typed rules of six kinds: row, duplicate,
  mode_outlier, same_signature, stored_metric, text_quantity), to find the keys that break those rules. See
  [references/config.md](../skills/semantic-crosscheck/references/config.md) for the table.
- With `views = true` (the default), after [/xc-build](xc-build.md): build creates the lookup views `v_rule_<id>` and
  `v_rules_subjects` in the output schema, and this stage proves they return exactly the rows both evaluations agreed
  on. Rerun build after every change to the rule file, then rerun data-rules.
- The stage is outside the required report pipeline: report adds one not-proven line and never reads its result.

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" data-rules --config crosscheck.toml
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" data-rules --config crosscheck.toml --rule qty_over_limit
```

- `--config` names the configuration (required unless `./crosscheck.toml` exists); `--out DIR` changes the output
  directory (default `.sxc` next to the configuration); `--quiet` prints only the summary line.
- `--rule ID` (repeatable) checks only those rules. Every other rule is left out: `scope:rules` is `not_run`, so a
  narrowed run never passes, and `rule_subjects:lookup` is `not_run` when a rule naming a subject is left out.
- The stage reads only. It checks every table and column the rules read (exit 2 or 3) and type-checks every rule
  against the catalog (exit 2) before any row is read, then evaluates each rule twice in one READ ONLY REPEATABLE
  READ transaction and runs the authored examples in a second read-only transaction.
- Files: `.sxc/data_rules/result.json` (counts, ids and reasons only) and `.sxc/data_rules/rules.sql` (every
  statement of the run with placeholders only, for review).
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## Results

- `rule:<id>` passes when both evaluations agree, no authored example failed, and the rule found nothing or has
  `severity = "warning"` (its count is still reported); it fails when they agree and a `violation` rule found rows.
- `rule_xcheck:<id>` compares the two canonical output multisets; a difference leaves `rule:<id>` `not_run`
  (`paths_disagree`).
- `rule_example:<id>` compares both evaluations with the authored `[[rule.example]]` rows. `common_mode` means both
  agree but differ from the authored rows; `rule:<id>` is then `not_run` (`example_mismatch`).
- `rule_lookup:<id>` and `rule_subjects:lookup` certify the lookup views (`view_stale`: build ran with another rule
  file or configuration).
- `data_rules:snapshot` and `data_rules:key:<entity>` report the row budget, the gates and the key checks. A gate that
  trips (NULL or duplicate keys, NaN, NULL edge quantities, cycles, long text, unsupported types) or a budget leaves
  the rules it concerns `not_run`, never passed.
- Exit code 0 only when every check passed; 1 otherwise; 2 for a missing or invalid `[data_rules]` table or rule
  file, an unknown `--rule`, an unknown column or a type-incompatible rule; 3 for the connection or the
  environment.

## Next steps

- Follow `next_safe_action`. To look at the rows a rule found, query `v_rules_subjects` and `v_rule_<id>` (ORDER BY
  ord) in the database with the user; results hold counts only. Agreement of the two evaluations does not show that
  a rule is the right business rule: write the examples from the business rule.

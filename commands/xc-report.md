---
description: "Summarise every stage result into report.md and report.json, with failed checks, not_proven items and the overall exit code"
argument-hint: "[--config crosscheck.toml]"
---

Run the `report` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not shell
code: pass each one as its own argument and never splice them into a shell string.

## When to use

- At the end of a run, and whenever results are summarised for the user or for a review.
- It reads the `result.json` of every stage in the output directory and runs nothing else.

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" report --config crosscheck.toml
```

- Pass the same `--config` (and `--out`, if one was used) as the other stages, so the report reads the
  same output directory.
- The stage writes `report/result.json`, plus `report.md` and `report.json` at the top of the output
  directory (`.sxc/report.md` next to the configuration by default).
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## Success

The `stage:<name>` checks mirror the status of each stage that ran (recomputed from its checks, so a
hand-edited result cannot look better), and the exit code is the overall verdict: 0 only when every
stage that ran passed. Stages that never ran are listed as not proven, and results older than a stage
they read from (for example a `cube` run before the latest `build`) are flagged as stale. A passing
report therefore says nothing about the stages it lists as not run or stale.

## How to report to the user

1. Lead with the overall status and the exit code.
2. List each failed or errored check with its `config_ref` (the `crosscheck.toml` entry to fix) and the
   `next` action from the stage result.
3. List every not proven item as written (empty strata, equivalent mutants, shapes without a SHACL
   form, components that are not installed). Do not call those parts verified.
4. Name the gold SQL files and `sql_violation` predicates as trusted SQL: they are the reference the
   harness compares against, not something it verified.
5. Use counts and ids only. Never paste row values (keys, names, quantities) into the report, a
   message, an issue or a commit.

## Failures

- `stage:<name>` fails or is `not_run`: run or fix that stage (see its command) and then rerun
  `report`. Do not edit `result.json` files or the report by hand.
- Stale results: rerun the stale stages in pipeline order, then `report` (the report's `next` line names
  the first one).
- No stage results at all (the report is `not_run`, exit code 1): check that `--config` and `--out`
  point at the directory the other stages wrote to.

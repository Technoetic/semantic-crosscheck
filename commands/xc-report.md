---
description: "Summarise every stage result into report.md and report.json, with failed checks, missing or stale stages, not_proven items and the overall exit code"
argument-hint: "[--config crosscheck.toml] [--out DIR] [--quiet]"
---

Run the `report` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not shell
code: pass each one as its own argument and never splice them into a shell string.

## When to use

- At the end of a run, and whenever results are summarised for the user or for a review.
- It reads the `result.json` of `doctor`, `demo`, `build`, `reason`, `cube` and `verify` in the output
  directory. Without a revision probe it never touches the database. With
  `source.data_revision_sql_file`, it reads the current revision in a fresh read-only transaction and
  rejects missing or different revision bindings; it never writes to the database.

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" report --config crosscheck.toml
```

- Pass the same `--config` (and `--out`, if one was used) as the other stages, so the report reads the
  same output directory. `--quiet` prints only the summary line.
- The stage writes `report/result.json`, plus `report.md` and `report.json` at the top of the output
  directory (`.sxc/report.md` next to the configuration by default).
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## How the verdict is formed

- **Required stages** are `build`, `cube` and `verify`, plus `reason` when the configuration has an
  `[ontology]` or any `[[shapes]]`. Each gets a `stage:<name>` check with its status recomputed from its
  checks, so a hand-edited result cannot look better.
- **Missing**: a required stage without a result is `stage:<name>` `not_run` (`reason=missing`).
- **Stale**: a stage that started before a stage it depends on finished its latest run counts as at least
  `not_run` (`reason=stale`, with `after` and `why`). Dependencies: `build` after `demo`; `reason` and
  `cube` after `build`; `verify` after `build` and `cube`. A stage that depends on a stale one is stale
  too.
- **Narrowed**: a stage whose own status is `not_run` (for example a verify narrowed with `--only` or
  `--gold`, whose `scope:*` checks are `not_run`) is `stage:<name>` `not_run`.
- **Optional** stages (`doctor`, `demo`): a present result that passed, failed or errored is mirrored, so a
  failing doctor or demo still fails the report; one that is `not_run` or stale is only listed as not
  proven. Without a doctor result the report notes that the check role's privileges were not verified.
- The exit code is the overall verdict: 0 only when every required stage ran, is current and passed.

## Success

Exit code 0 and `[sxc report] PASS`. The `next:` line confirms that every required stage ran after the
stages it depends on and passed.

## How to report to the user

1. Lead with the overall status and the exit code.
2. List each failed or errored check with its `config_ref` (the `crosscheck.toml` entry to fix) and the
   `next` action from the stage result.
3. List every missing or stale stage, and every not proven item as written (empty strata, equivalent
   mutants, shapes compared only in SQL, sampled pair tests, components that are not installed). Do not
   call those parts verified.
4. Name the gold SQL files and `sql_violation` predicates as trusted SQL: they are the reference the
   harness compares against, not something it verified.
5. Use counts and ids only. Never paste row values (keys, names, quantities) into the report, a
   message, an issue or a commit.

## Failures

- `stage:<name>` fails or errors: fix that stage (see its command) and rerun it, then the stages after
  it, then `report`. Do not edit `result.json` files or the report by hand.
- `stage:<name>` `not_run` with `reason=missing` or `reason=stale`: run or rerun the stages the `next:`
  line names, in the order it gives, then `report`.
- No stage results at all: check that `--config` and `--out` point at the directory the other stages
  wrote to.
- Editing `crosscheck.toml` or a gold SQL file after a run makes the stages that read it stale
  (`why=inputs_changed`): every result records a fingerprint of those files, taken before the stage ran.
- Engine/probe changes also invalidate dependent evidence. Without a configured data-revision probe,
  source-data freshness remains explicitly unproven; file timestamps alone cannot establish it.

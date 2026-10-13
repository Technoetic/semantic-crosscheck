---
description: "Load the synthetic parts, BOM, orders and stock demo into a throw-away database with the loader role (clean or dirty variant)"
argument-hint: "[--config sxc-demo/crosscheck.toml] [--variant clean|dirty] [--seed N] [--scale N] [--out DIR] [--quiet]"
---

Run the `demo` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not shell
code: pass each one as its own argument and never splice them into a shell string.

## When to use

- To try the harness end to end before writing a configuration for real data, or to see how planted
  defects surface in the later stages. For the demo the stage order is `demo`, then
  [/xc-doctor](xc-doctor.md) with `--config`, [/xc-build](xc-build.md), [/xc-reason](xc-reason.md),
  [/xc-cube](xc-cube.md), [/xc-verify](xc-verify.md) and [/xc-report](xc-report.md).
- Only against a throw-away database. The stage loads the demo tables (`sxc_demo.part`,
  `sxc_demo.bom_line`, `sxc_demo.sales_order`, `sxc_demo.stock`) with the loader role
  (`SXC_PG_OWNER_DSN`, or the variable named by `source.owner_dsn_env`). Never point the loader DSN at a
  database that holds real data.
- Use the demo configuration shipped with the plugin. Copy the plugin's `demo/` directory into the
  working directory first (for example to `sxc-demo/`), so results are written next to the copy and
  not into the plugin installation.
- It can be rerun at any time, also after `build`, for example to switch between the clean and the
  dirty variant.

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" demo --config sxc-demo/crosscheck.toml --variant clean
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" demo --config sxc-demo/crosscheck.toml --variant dirty
```

- `--seed` (default 20261013) and `--scale` (default 1, about 2,000 parts and 700 orders per unit)
  change the generated data; the same seed always produces the same rows on every platform.
- The tables are reloaded in place: the schema and each table are created only when missing, all four
  are emptied with `TRUNCATE` and filled with `COPY`. Nothing is dropped, so the views `build` created on
  them stay valid. Concurrent demo runs wait for each other.
- The loader role must own the demo tables; while they are missing it also needs to own `sxc_demo` (or
  be allowed to create it). The check role needs `USAGE` on the schema and `SELECT` through default
  privileges. [docs/setup-roles.sql](../docs/setup-roles.sql) sets this up.
- Results go to `.sxc/demo/result.json` and `.sxc/demo/expected.json` next to the configuration (or
  under `--out DIR`); `--quiet` prints only the summary line.
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## Success

Exit code 0 with `demo:rows:<table>` passing for every table (the loaded row counts equal the
generator) and `demo:copy_on_error` passing (PostgreSQL 17 `COPY ... ON_ERROR ignore` skipped exactly
the malformed rows: none for `clean`, two for `dirty`). Then run [/xc-doctor](xc-doctor.md) with
`--config` (the check role's privileges can only be proven now that the tables exist) and continue with
[/xc-build](xc-build.md).

`expected.json` holds the reference numbers of an independent Python implementation, grouped by what they
are compared with: `rows` and `graph` (both variants), `closure` (clean variant only: the dirty data has a
cycle, so build refuses that closure and the section says `compared: false`) and `planted_defects`. The
dirty variant plants these defects; the later stages show them as follows:

| Planted defect | Check | Outcome |
|---|---|---|
| a two-node cycle | `closure:has_component` (build) | fail, `reason=cycle`; verify reports every check over the closure as `not_run` |
| two orphan child references | `shape:child_exists` (build) | fail with 2 violations; `shacl:child_exists` agrees |
| a zero-quantity BOM line | `shape:qty_positive` (build) | fail with 1 violation; `shacl:qty_positive` agrees |
| two negative stock rows | `shape:stock_nonnegative` (build) | pass (severity warning) with 2 violations; `shacl:stock_nonnegative` agrees |
| three orders with `qty_open` above `qty_total` | `shape:open_le_total` (build) | fail with 3 violations (`sql_violation`: SQL only) |
| a raw material with a child | `owl:consistency` (reason) | fail; the V5 checks are `not_run` |
| two malformed CSV rows | `demo:copy_on_error` (demo) | pass with `skipped=2` |
| three location codes with whitespace or case variants | none (exercises `v1:q03_stock`, `v2:q03_stock:values`, `v4:measure_normalization_drop:on_hand`) | not reported: Cube and the gold SQL both normalise them, and they make the normalisation mutant observable, which q03 kills |
| two firm orders with out-of-range due dates | none (exercises `v1:q05_firm_window`, `v2:q05_firm_window:param_sets`) | not reported: the q05 windows include them (1990 to 2100) or leave them out (2026) on both sides |

After the dirty variant, `build`, `reason`, `verify` and `report` exit 1 on purpose. In a database where
the clean variant was built before, `cube` still passes (the closure table of the clean build is left
untouched when build refuses the cyclic closure, and verify does not trust it); in a fresh database
`cube:gold_members` fails for the gold queries over the closure, and the `next:` line points at the cycle.

## Failures

- Exit code 3: `SXC_PG_OWNER_DSN` is not set, holds a malformed DSN, or the server is unreachable. Set
  the variable in the environment; never in `crosscheck.toml`.
- Exit code 3 also when the server is older than PostgreSQL 17 (`PostgreSQL 17 or later is required`).
- Exit code 2: the configuration does not list `sxc_demo` in `source.schemas` (run the demo with the demo
  configuration, not with one written for real data), `--scale` is below 1, or `sxc_demo` already holds
  tables with other columns than the demo defines. The message names each difference (for example
  `sxc_demo.stock: missing column qty`); nothing was written. Those tables are not the demo's and demo
  never drops a table: use another database, or drop them yourself.
- `demo:load` is `error`: the load was rolled back and the detail has the SQLSTATE. The `next:` line
  names the cause: `42501` the loader role lacks a privilege (it must own the demo tables, and while they
  are missing own or be allowed to create `sxc_demo`; fix the roles with `docs/setup-roles.sql` instead
  of using a superuser), `0A000` a foreign key from another table blocks `TRUNCATE`, `23xxx` a constraint
  added to the demo tables (such as one from `fk_review.sql`) rejects the dirty rows, `40P01`, `55P03` or
  `57014` another session was using the tables; anything else, check the server log.
- `demo:copy_on_error` fails: `ON_ERROR ignore` did not skip exactly the malformed rows; run
  [/xc-doctor](xc-doctor.md) and check the server version.
- `demo:rows:<table>` fails: something else changed the demo tables during the load. Rerun on a
  database nobody else uses.

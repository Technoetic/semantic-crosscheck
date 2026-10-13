---
description: "Load the synthetic parts, BOM, orders and stock demo into a throw-away database with the loader role (clean or dirty variant)"
argument-hint: "[--variant clean|dirty] [--seed N] [--scale N]"
---

Run the `demo` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not shell
code: pass each one as its own argument and never splice them into a shell string.

## When to use

- To try the harness end to end before writing a configuration for real data, or to see how planted
  defects surface in the later stages.
- Only against a throw-away database. The stage drops and recreates the demo tables
  (`sxc_demo.part`, `sxc_demo.bom_line`, `sxc_demo.sales_order`, `sxc_demo.stock`) with the loader
  role (`SXC_PG_OWNER_DSN`, or the variable named by `source.owner_dsn_env`). Never point the loader
  DSN at a database that holds real data.
- Use the demo configuration shipped with the plugin. Copy the plugin's `demo/` directory into the
  working directory first (for example to `sxc-demo/`), so results are written next to the copy and
  not into the plugin installation.

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" demo --config sxc-demo/crosscheck.toml --variant clean
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" demo --config sxc-demo/crosscheck.toml --variant dirty
```

- `--seed` (default 20261013) and `--scale` (default 1, about 2,000 parts and 700 orders per unit)
  change the generated data; the same seed always produces the same rows on every platform.
- The loader role must own the `sxc_demo` schema (or be allowed to create it); the check role needs
  `USAGE` on it and `SELECT` through default privileges. [docs/setup-roles.sql](../docs/setup-roles.sql)
  sets this up.
- Results go to `.sxc/demo/result.json` and `.sxc/demo/expected.json` next to the configuration.
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## Success

Exit code 0 with `demo:rows:<table>` passing for every table (the loaded row counts equal the
generator) and `demo:copy_on_error` passing (PostgreSQL 17 `COPY ... ON_ERROR ignore` skipped exactly
the malformed rows: none for `clean`, two for `dirty`). `expected.json` holds reference numbers from an
independent Python implementation: closure pairs, path counts, the depth histogram and the planted
defects. Then run [/xc-doctor](xc-doctor.md) with `--config` (the check role's privileges can only be
proven now that the tables exist) and continue with [/xc-build](xc-build.md).

The `dirty` variant plants, and counts in `expected.json`: a two-node cycle, two orphan child
references, a zero-quantity BOM line, two negative stock rows, three orders with `qty_open` above
`qty_total`, a raw material that has a child, three location codes with whitespace or case variants,
two firm orders with out-of-range due dates and two malformed CSV rows. The later stages are expected
to report these (shape and closure checks in `build`, OWL consistency in `reason`, gold queries over
the cyclic hierarchy as `not_run` in `verify`). Compare what they report with `expected.json`.

## Failures

- Exit code 3: `SXC_PG_OWNER_DSN` is not set or the server is unreachable. Set the variable in the
  environment; never in `crosscheck.toml`.
- Exit code 3 also when the server is older than PostgreSQL 17 (`COPY ... ON_ERROR` is required).
- Exit code 2: the configuration does not list `sxc_demo` in `source.schemas`; run the demo with the
  demo configuration, not with one written for real data.
- `demo:load` is `error` (the detail has the SQLSTATE): the load was rolled back, usually because the
  loader role does not own `sxc_demo` (or may not create it). Fix the roles with
  `docs/setup-roles.sql` instead of using a superuser DSN.
- `demo:copy_on_error` fails: `ON_ERROR ignore` did not skip exactly the malformed rows; run
  [/xc-doctor](xc-doctor.md) and check the server version.
- `demo:rows:<table>` fails: something else changed the demo tables during the load. Rerun on a
  database nobody else uses.

---
description: "Generate the Cube data model from crosscheck.toml, start Cube on 127.0.0.1 with read-only database sessions and check its metadata"
argument-hint: "[--config crosscheck.toml]"
---

Run the `cube` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not shell
code: pass each one as its own argument and never splice them into a shell string.

## When to use

- After [/xc-build](xc-build.md) (closure cubes are generated only when the closure table exists) and
  after the adapter is installed with [/xc-install](xc-install.md).
- Again after any change to `entities`, `measures`, `hierarchies`, `gold` or `[cube]`.

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" cube --config crosscheck.toml
```

- The model is generated from the configuration and the live catalog into `.sxc/cube/model/*.yml`
  (one file per cube) and `.sxc/cube/model.json`.
- The pinned Cube adapter runs as a child process on 127.0.0.1 (port from `cube.port`, 0 picks a free
  one) with a random per-run API secret, development mode and telemetry off, and
  `PGOPTIONS=-c default_transaction_read_only=on`, so every Cube database session is read-only. It
  connects with the check role, or with the variable named by `cube.dsn_env`.
- Adapter errors go to `.sxc/cube/server.log`; request logs are dropped because they contain filter
  values. Cube is stopped when the stage ends.
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## Success

Exit code 0 with `cube:model` (the model was generated; generator notes such as skipped columns or a
closure table that is not built yet are listed as not proven), `cube:meta` (Cube's metadata lists
exactly the public members of the model) and `cube:gold_members:<gold>` (every member each gold query
names exists and is public). Then continue with [/xc-verify](xc-verify.md).

## Failures

Fix `crosscheck.toml`, never the generated YAML or `model.json`: they are rewritten on every run and
the mutation tests patch copies of `model.json`.

- `cube:model` fails or exit code 2: an entity or measure names a column that does not exist, or a
  table is not readable by the check role. Fix the entry named in the message, or the grants.
- `cube:meta` fails: Cube did not load the model. Read the `SXC_CUBE_ERROR` lines in `server.log`,
  find the configuration entry the failing cube came from (`<entity>`, `m_<measure>`, `<h>_closure`,
  `<h>_ancestor`, `<h>_descendant`) and fix that entry.
- `cube:gold_members:<gold>` fails: the gold query's `cube_query` or `map` names a member the model
  does not have. Members are written `cube.member`; closure cubes exist only after a successful
  `build`; the synthetic primary key of a composite-key cube is hidden and cannot be queried.
- Exit code 3: the adapter is not installed ([/xc-install](xc-install.md)), `node` is not on `PATH`,
  the Cube DSN variable is not set, or Cube did not become ready within `cube.startup_timeout_s`.

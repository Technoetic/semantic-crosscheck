---
description: "Generate the Cube data model from crosscheck.toml, start Cube on 127.0.0.1 with read-only database sessions and check its metadata"
argument-hint: "[--config crosscheck.toml] [--out DIR] [--quiet]"
---

Run the `cube` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not shell
code: pass each one as its own argument and never splice them into a shell string.

## When to use

- After [/xc-build](xc-build.md) (closure cubes are generated only when the closure table exists) and
  after the adapter is installed with [/xc-install](xc-install.md).
- Again after any change to `entities`, `measures`, `hierarchies`, `gold` or `[cube]`, and after every
  build (`report` counts an older cube result as stale).

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" cube --config crosscheck.toml
```

- The model is generated from the configuration and the live catalog (read in a read-only transaction)
  into `.sxc/cube/model/*.yml` (one file per cube) and `.sxc/cube/model.json`.
- The pinned Cube adapter runs as a child process on 127.0.0.1 (port from `cube.port`, 0 picks a free
  one) with a random per-run API secret, development mode and telemetry off. Every Cube database session
  is read-only and limited to `cube.query_timeout_s` (`PGOPTIONS` with
  `default_transaction_read_only=on` and `statement_timeout`, plus `CUBEJS_DB_QUERY_TIMEOUT`). It
  connects with the check role, or with the variable named by `cube.dsn_env`, and gets no other DSN or
  `SXC_*` variable. The client talks to it directly, never through a proxy.
- Adapter errors go to `.sxc/cube/server.log`, one line each with only the error class, its SQLSTATE and a
  coarse category (`SXC_CUBE_ERROR`, `SXC_ERROR`; model compile errors keep their text); request logs and console output
  of the Cube packages are dropped because they contain values. Cube is stopped when the stage ends.
- `--out DIR` changes the output directory; `--quiet` prints only the summary line.
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

- Exit code 2: the model could not be generated (an entity's table is missing or not readable by the
  check role, a key or measure column does not exist); the message names the `crosscheck.toml` entry.
  `cube:model` itself only ever passes; columns of types Cube does not get are skipped and listed as not
  proven.
- `cube:meta` fails or is `error`: Cube did not serve the generated model as expected. Compare
  `cube/model` with `crosscheck.toml`, find the entry the failing cube came from (`<entity>`,
  `m_<measure>`, `<h>_closure`, `<h>_ancestor`, `<h>_descendant`) and fix that entry. `server.log` names
  only the error class; for the full Cube error text the user can rerun with `SXC_CUBE_DEBUG=1`, which
  can write row values into that log, so it stays local and is never pasted.
- `cube:gold_members:<gold>` fails: the gold query's `cube_query` or `map` names a member the model
  does not have, or a hidden one. Members are written `cube.member`; the synthetic primary key of a
  composite-key cube is hidden and cannot be queried. When a closure cube is missing, the `next:` line
  follows build's `closure:<h>` result: fix the cycle (or set `on_cycle = "report"`) or raise
  `max_depth`, rebuild until the check passes, then rerun cube.
- Exit code 3: the adapter is not installed, incomplete, or was installed from other adapter files than
  this plugin's (rerun [/xc-install](xc-install.md); an install made with `--home DIR` needs
  `SXC_CUBE_HOME`), `node` (Node.js 22 or later) is not on `PATH`, the Cube DSN variable is not set, or
  Cube did not start within `cube.startup_timeout_s`. The message quotes the redacted adapter error
  lines and names `server.log`.

---
description: "Check Python, pg8000, the PostgreSQL 17 connection, check-role privileges and the optional reason and Cube components before any other stage"
argument-hint: "[--config crosscheck.toml] [--require core,reason,cube]"
---

Run the `doctor` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not shell
code: pass each one as its own argument and never splice them into a shell string.

## When to use

- First, in every new environment, before `build`, `reason`, `cube` or `verify`.
- Again whenever another stage exits with code 3 (environment missing), and after roles, grants,
  Python packages, Java, Node.js or the Cube adapter change.
- doctor only inspects. It loads no data, reads privileges from the catalogs in a read-only
  transaction, and connects with the check role (`SXC_PG_DSN`, or the variable named by
  `source.dsn_env`), never with the loader role.
- It always writes a complete result: a missing DSN or an unreachable server becomes a failing check
  (and the database checks that could not run are `not_run`), so one run lists everything to fix.

## Command

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" doctor --config crosscheck.toml
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" doctor --config crosscheck.toml --require core,reason,cube
```

- `--require` names the components that must be ready: `core` (always checked), `reason` (owlready2,
  pySHACL and Java for HermiT) and `cube` (Node.js, the installed adapter and its native module).
  Components that are not required are still probed; when they are missing they are listed as
  NOT PROVEN instead of failing the stage.
- The privilege checks need the configuration (entities and `source.output_schema`) and the source
  tables. Without a `crosscheck.toml` only the environment is checked. For the demo, run doctor
  without `--config` before [/xc-demo](xc-demo.md) (the demo tables do not exist yet) and with
  `--config` after it.
- Results go to `.sxc/doctor/result.json` next to the configuration (or under `--out DIR`).
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` with the plugin root as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## Success

Exit code 0 and `[sxc doctor] PASS`. The checks are `python`, `pg8000`, `dsn`, `connect`, `pg_version`
(server_version_num >= 170000), `privileges:select:<entity>` (the check role can read every source
table), `privileges:no_write:<entity>` (it cannot write them), `output_schema` (it can create objects
in the output schema, or create that schema), plus `reason:owlready2`, `reason:pyshacl`, `reason:java`,
`cube:node`, `cube:adapter` and `cube:native` for the required components. The `next:` line names the
next stage to run.

## Failures

Fix the environment or `crosscheck.toml`, then rerun doctor. Never work around a failed check by
using a more privileged role.

| Check | What to do |
|---|---|
| `dsn`, `connect` | Set `SXC_PG_DSN` to `postgresql://user@host:port/database`; the password goes in the URL (percent-encoded) or in `PGPASSWORD`. Never put a DSN or password in `crosscheck.toml` (the configuration refuses credentials). |
| `pg_version` | PostgreSQL 17 or later is required (`MERGE ... WHEN NOT MATCHED BY SOURCE`, `merge_action()`, `COPY ... ON_ERROR`). There is no fallback for older servers. |
| `privileges:select:<entity>` | Grant `USAGE` on the source schema and `SELECT` on the table to the check role ([docs/setup-roles.sql](../docs/setup-roles.sql)). |
| `privileges:no_write:<entity>` | The check role can modify a source table. Revoke `INSERT`, `UPDATE`, `DELETE` and `TRUNCATE`, and remove its membership in the table owner's role (a member could `SET ROLE` to the owner), before running any other stage. |
| `output_schema` | The check role must own (or have `CREATE` and `USAGE` on) `source.output_schema`, or be allowed to create it; the schema must differ from the source schemas. |
| `reason:*` | `pip install -r requirements/reason.txt`; install Java 11 or later and set `SXC_JAVA` to the `java` executable when it is not on `PATH`. |
| `cube:*` | Install Node.js 20 or later, then run [/xc-install](xc-install.md). |

- A failed or `not_run` check gives exit code 1; the `next:` line names the first thing to fix.
- Exit code 2 is a configuration error (or an unknown `--require` component): fix the
  `crosscheck.toml` entry named in the message.
- Read every NOT PROVEN line to the user. A component listed there is not verified, even when the
  stage passed.

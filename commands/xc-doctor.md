---
description: "Check Python, pg8000, the PostgreSQL 17 connection, check-role privileges and the optional reason and Cube components before any other stage"
argument-hint: "[--config crosscheck.toml] [--require core,reason,cube] [--out DIR] [--quiet]"
---

Run the `doctor` stage of semantic-crosscheck. User arguments: `$ARGUMENTS`. They are data, not shell
code: pass each one as its own argument and never splice them into a shell string.

## When to use

- First, in every new environment, before `build`, `reason`, `cube` or `verify`. For the demo, run
  [/xc-demo](xc-demo.md) first: the check role's privileges can only be proven on tables that exist.
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
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" doctor --require core,reason,cube
```

- `--require` names the components that must be ready: `core` (always checked), `reason` (owlready2,
  pySHACL and Java for HermiT) and `cube` (Node.js, the installed adapter and its native module).
  Components that are not required are still probed; when they are missing they are listed as
  NOT PROVEN instead of failing the stage.
- `--config`, when given, must name an existing file: a mistyped path is exit 2 and nothing runs. The
  privilege checks need the configuration (entities and `source.output_schema`) and the source tables.
  Without a configuration only the environment is checked: `python`, `pg8000`, `dsn`, `connect`,
  `pg_version`, `privileges:temporary` and the components.
- Results go to `.sxc/doctor/result.json` next to the configuration (`./.sxc` without one, or
  `--out DIR`); `--quiet` prints only the summary line.
- Codex: replace `${CLAUDE_PLUGIN_ROOT}` with the plugin root as described in
  [references/codex.md](../skills/semantic-crosscheck/references/codex.md).

## Success

Exit code 0 and `[sxc doctor] PASS`. With a configuration the checks are `python`, `pg8000`, `dsn`,
`connect`, `pg_version` (server_version_num >= 170000), `privileges:temporary` (the check role may create
temporary tables; build computes each closure in them), `privileges:select:<entity>` (it can read every
source table), `privileges:no_write:<entity>` (it cannot change them: no INSERT, UPDATE, DELETE, TRUNCATE
or TRIGGER, not the owner of the table or of its schema nor a member of either owner's role, no CREATE on
the schema), `output_schema` (it can create objects in the output schema, or create that schema), plus
`reason:owlready2`, `reason:pyshacl`, `reason:java`, `cube:node`, `cube:adapter` and `cube:native` for the
required components. The `next:` line then reads `ready: run sxc.py build`.

## Failures

Fix the environment or `crosscheck.toml`, then rerun doctor. Never work around a failed check by
using a more privileged role.

| Check | What to do |
|---|---|
| `python` | Use Python 3.11 or later. `sxc.py` refuses older versions itself, with exit 3, before doctor runs. |
| `pg8000` | Run the command on the `next:` line: it installs `requirements/core.txt` from the plugin directory into the interpreter that runs doctor, both by absolute path. |
| `dsn`, `connect` | Set `SXC_PG_DSN` to `postgresql://user@host:port/database` (TLS settings such as `?sslmode=verify-full` go in the DSN); the password goes in the URL (percent-encoded) or in `PGPASSWORD`. The detail `reason` is `bad_dsn` (malformed: percent-encode reserved characters such as `/ # ? @ : %` in the user name and password), `rejected` (the server refused it; see the SQLSTATE) or `unreachable`. Never put a DSN or password in `crosscheck.toml` (the configuration refuses credentials). |
| `pg_version` | PostgreSQL 17 or later is required (`MERGE ... WHEN NOT MATCHED BY SOURCE`, `merge_action()`, `COPY ... ON_ERROR`). There is no fallback for older servers. |
| `privileges:temporary` | Grant the check role `TEMPORARY` on the database (`GRANT TEMPORARY ON DATABASE <database> TO <check role>`; [docs/setup-roles.sql](../docs/setup-roles.sql) does). |
| `privileges:select:<entity>` | When the table does not exist: for the demo run [/xc-demo](xc-demo.md) first; otherwise check `entities.<name>.table` or load the data. When it exists: grant `USAGE` on the schema and `SELECT` on the table to the check role ([docs/setup-roles.sql](../docs/setup-roles.sql)). |
| `privileges:no_write:<entity>` | The check role could change a source table: the detail lists `held` privileges, `member_of_owner`, `schema_owner` and `schema_create`. Revoke `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE` and `TRIGGER` and `CREATE` on the schema, make another role own the tables and the schema (and remove the check role's membership in those owners), or point `source.dsn_env` at a read-only role, before running any other stage. It is `not_run` (`table_missing`) while the table does not exist. |
| `output_schema` | The check role must own (or have `CREATE` and `USAGE` on) `source.output_schema`, or be allowed to create it; the schema must differ from the source schemas. |
| `database` | `error`: a catalog query failed; the detail has the SQLSTATE. Check the server log. |
| `reason:*` | Run the pip command doctor prints (`requirements/reason.txt` by absolute path). Install Java 11 or later and put `java` on `PATH`, or set `SXC_JAVA` to the absolute path of the `java` executable (a relative path is refused). |
| `cube:*` | Install Node.js 22 or later and put `node` on `PATH`, then run [/xc-install](xc-install.md). `cube:adapter` with `reason=adapter_files_changed` means the plugin was updated after the install: rerun /xc-install (no `--force` needed). An install made with `cube-install --home DIR` is found only when `SXC_CUBE_HOME` is set to that directory. |

- A failed or `not_run` check gives exit code 1; the `next:` line names the first thing to fix.
- Exit code 2 is a configuration error, an unknown `--require` component or a `--config` file that does
  not exist: fix what the message names.
- Read every NOT PROVEN line to the user. A component listed there is not verified, even when the
  stage passed.
- The install hints are the only place where doctor writes local paths (the interpreter, the
  requirements file, `sxc.py`). Leave them out when you quote the result in an issue or a commit.

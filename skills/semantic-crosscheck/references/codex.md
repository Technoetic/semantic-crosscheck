# Using semantic-crosscheck from Codex

Codex runs the same `sxc.py` stages as Claude Code. The command documents in the plugin's `commands/`
directory are shared: Claude Code exposes them as `/xc-*` slash commands, and Codex reads them through
this skill. Codex may also discover the command files itself; the skill entry point below is the
default.

## Invocation

- The plugin is installed with `codex plugin marketplace add Technoetic/semantic-crosscheck --ref <tag>`
  and `codex plugin add semantic-crosscheck@semantic-crosscheck-local` (see the README). The skill is
  `semantic-crosscheck:semantic-crosscheck`. Ask for a stage with
  `$semantic-crosscheck:semantic-crosscheck <stage> [arguments]`, for example
  `$semantic-crosscheck:semantic-crosscheck verify --only v1,v2`.
- A request in plain language ("check my semantic layer against the gold queries") maps to the stage
  order in `SKILL.md`: `doctor`, `build`, `reason`, `cube`, `verify`, `report`; for the demo `demo` first,
  then `doctor` with `--config` and the rest.

## Paths

- The skill directory is the directory of the installed `SKILL.md`; `sxc.py` is
  `<skill directory>/scripts/sxc.py`. The plugin root is `Path(skill_file).resolve().parents[2]`
  (from a file in `references/`, `parents[3]`).
- `${CLAUDE_PLUGIN_ROOT}` in the command documents is a placeholder for that plugin root. Codex does
  not set this variable; substitute the resolved path instead of requiring or exporting it.
- The Python requirements are installed from the plugin root by absolute path:
  `python -m pip install -r "<plugin root>/requirements/reason.txt"`. When a package is missing, `doctor`
  prints this command with the absolute paths filled in.
- Do not copy the scripts into the user's project. If a file the stage needs is missing, report an
  incomplete installation and stop.

## Stage map

| Codex request | Procedure | Command |
|---|---|---|
| `doctor` | [xc-doctor.md](../../../commands/xc-doctor.md) | `sxc.py doctor --config crosscheck.toml --require core,reason,cube` |
| `demo` | [xc-demo.md](../../../commands/xc-demo.md) | `sxc.py demo --config sxc-demo/crosscheck.toml --variant clean` |
| `build` | [xc-build.md](../../../commands/xc-build.md) | `sxc.py build --config crosscheck.toml` |
| `reason` | [xc-reason.md](../../../commands/xc-reason.md) | `sxc.py reason --config crosscheck.toml` |
| `cube` | [xc-cube.md](../../../commands/xc-cube.md) | `sxc.py cube --config crosscheck.toml` |
| `verify` | [xc-verify.md](../../../commands/xc-verify.md) | `sxc.py verify --config crosscheck.toml` |
| `report` | [xc-report.md](../../../commands/xc-report.md) | `sxc.py report --config crosscheck.toml` |
| `install` or `cube-install` | [xc-install.md](../../../commands/xc-install.md) | `sxc.py cube-install` |
| `counterexample` | [xc-counterexample.md](../../../commands/xc-counterexample.md) | `sxc.py counterexample --reference-sql reference.sql --candidate-sql candidate.sql` |
| `cube-replay` | [xc-cube-replay.md](../../../commands/xc-cube-replay.md) | `sxc.py cube-replay --mutation-operator measure_agg_to_max --allow-scratch-schema` |
| `tlp` | [xc-tlp.md](../../../commands/xc-tlp.md) | `sxc.py tlp --spec spec.json` |
| `shacl-corpus` | [xc-shacl-corpus.md](../../../commands/xc-shacl-corpus.md) | `sxc.py shacl-corpus --jena-home /opt/apache-jena --timeout-ms 30000` |
| `expect` | [xc-expect.md](../../../commands/xc-expect.md) | `sxc.py expect --config crosscheck.toml --allow-fixture-database` |

`cube-replay`, `tlp` and `shacl-corpus` are optional standalone synthetic checks. They reject `--config`, ignore any
working-directory configuration and preserve independent Result stages; their success does not change
operational V1–V5 or global report coverage. `cube-replay` requires a dedicated throw-away connection
and explicit `--allow-scratch-schema` for its owned synthetic schema lifecycle. TLP accepts trusted SQL
in a strict versioned spec; read-only execution is not a SQL sandbox. The SHACL corpus checks both
actual validators against independent expected reports.

`expect` (after 0.8.0, not released yet) is different: it reads the operational `--config` for its
`[[gold.expect]]` cases, but connects only to the dedicated fixture database named by `--dsn-env`
(`SXC_EXPECT_DSN`), never to an operational DSN, and needs explicit `--allow-fixture-database`. Its result is
also kept out of `report` and of operational V1–V5 coverage.

## Arguments and execution

- Run a Python 3.11 or later interpreter with the script path and every argument as separate argv
  elements, for example `subprocess.run([python, str(sxc), "verify", "--config", str(config)],
  shell=False)`. The user's arguments are data; never interpolate them into shell code.
- `$ARGUMENTS` in the command documents means the text after the stage name in the Codex request.
- Read the exit code and the `result.json` the stage wrote; the console summary is a convenience. A run
  narrowed with `--only`, `--gold` or `--hierarchy` exits 1 and is not a result.

## Sandbox and environment

- The stages connect to PostgreSQL (and `cube` and `verify` start a local Cube process listening on
  127.0.0.1). When the Codex sandbox blocks network access, ask the user to allow it for these
  commands instead of working around the sandbox.
- `cube-install` downloads from the npm registry and from the Cube GitHub release; it needs network
  access once.
- `SXC_PG_DSN`, `SXC_PG_OWNER_DSN` (demo only), `PGPASSWORD`, `SXC_JAVA` and `SXC_CUBE_HOME` must be
  visible to the commands Codex runs. If a stage reports a missing variable, ask the user to provide it
  through the environment; never ask for a password in the chat or write one into a file.
- Auxiliary tests use `SXC_CUBE_REPLAY_DSN` and `SXC_TLP_DSN` for dedicated synthetic test connections, and
  `expect` uses `SXC_EXPECT_DSN` for the user's dedicated fixture database.
  They never infer authorization to select operational DSNs. `SXC_JENA_HOME` selects an external Jena
  distribution for `shacl-corpus`; the tested Jena 6.2.0 distribution needs a Java 21-compatible runtime.
  This optional engine does not change HermiT's Java 11 minimum. `SXC_W3C_SHACL_SUITE` names a W3C
  data-shapes checkout for `shacl-corpus --w3c-suite` (after 0.8.0, unreleased); it is read only when that
  option is given, and an unpinned suite runs no engine.
- `SXC_CUBE_DEBUG=1` and `SXC_DEBUG=1` are for the user's local debugging only: their output can hold row
  values, so do not set them on your own and never paste what they print.

## Same rules as Claude Code

Edit `crosscheck.toml`, never the generated SQL, TTL or YAML. Report not proven items with every
verdict. Source data is read-only, and results, messages and commits carry counts and ids only, never
row values. See `SKILL.md` for the full list.

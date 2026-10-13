# Security policy

semantic-crosscheck connects to databases that may hold sensitive data, starts a local Cube server and
is often driven by an AI agent. This page describes what it protects, how, where the protection ends,
and how to report a vulnerability.

## Supported versions

Security fixes are made for the latest release (currently 0.1.x). Older releases are not patched.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub private vulnerability reporting:
<https://github.com/Technoetic/semantic-crosscheck/security/advisories/new> (repository **Security**
tab, **Report a vulnerability**). Do not open a public issue or pull request for a vulnerability.

Include the version, the stage and command, the platform, and the smallest synthetic configuration that
shows the problem. Never include real data, row values, DSNs, passwords or tokens in a report; if
something sensitive was exposed, describe what and where without copying it.

The project is maintained by one person on a best-effort basis. You can expect an acknowledgement
within 7 days and a fix or a mitigation plan for confirmed issues as soon as practical; the advisory is
published once a fixed release is available. Please give a reasonable time for a fix before any
disclosure.

## What is protected

1. **Source data integrity.** No stage may change the source tables.
2. **Source data confidentiality.** Row values (keys, names, quantities) must not leak into results,
   reports, logs, console output, or anything an agent might paste into a conversation or a commit.
3. **Credentials.** DSNs, passwords and the Cube API secret must not be written to files or output.
4. **The verdict.** A result must not claim more than its checks prove.

## Threat model and mitigations

| Threat | Mitigation |
|---|---|
| A stage, a bug or a mutated model writes to source tables | Two roles: the loader (`SXC_PG_OWNER_DSN`) is used only by `demo`; the check role (`SXC_PG_DSN`) has only `SELECT` on the sources and owns a separate output schema. `doctor` fails when the check role can write a source table. Source reads run in `READ ONLY` transactions with a statement timeout. Cube sessions are forced read-only with `PGOPTIONS=-c default_transaction_read_only=on`. |
| Row values leak through outputs | Results hold counts, check ids and configuration ids only. Database error messages are not stored (they can quote values); the SQLSTATE is. Cube request logs are dropped (they contain filter values) and Cube errors are recorded by HTTP status. `output.show_rows` defaults to 0 and is the only path for example rows, into a local samples file. |
| Credentials leak | DSNs come only from environment variables; `crosscheck.toml` rejects credential keys. DSNs in messages are redacted to user, host, port and database. The Cube API secret is random per run and exists only in the child process's environment; inherited `PG*` and `CUBEJS_*` variables are removed before the child starts. |
| SQL injection through the configuration | Every identifier is validated against `^[a-z_][a-z0-9_]{0,62}$` and quoted; values are bound parameters, or literals from the validated configuration. `sql_violation` predicates and gold SQL files are trusted code by design and are labelled as such in reports. |
| The Cube server is reachable by others | The adapter binds to 127.0.0.1 only, requires a JWT signed with the per-run secret, runs with development mode and telemetry off, and is stopped when the stage ends. |
| Supply chain of the Cube adapter | `npm ci --ignore-scripts` from a committed lockfile (npm verifies every tarball's integrity; no package install script runs). The native module is downloaded separately and accepted only when its size and SHA-256 match `native-sha256.json`; only its single expected file is extracted. |
| Supply chain of the Python and CI dependencies | Direct Python dependencies are pinned to exact versions in `requirements/`. GitHub Actions are pinned by full commit SHA and the workflow token is read-only (`contents: read`). |
| Reasoner side effects | HermiT runs in a private temporary directory that is removed afterwards. The Pellet reasoner bundled with owlready2 (AGPL-3.0) is never invoked. |
| An agent misreads or overstates a result | Stage status is fail-closed (any error, failure or check not run prevents a pass); `not_proven` lists what a run could not prove; the report recomputes statuses from the checks and flags stale results. Data read from the database and from results is data, never instructions. |

## Out of scope and known limits

- **The configuration and gold SQL are trusted code.** Anyone who can edit `crosscheck.toml` or a gold
  SQL file can run SQL as the check role. Review them like code.
- **TLS for the Python stages.** The Python stages connect through pg8000 with its default behaviour:
  TLS is used when the server offers it, without certificate or host name verification, and DSN query
  parameters such as `sslmode` are not applied. Cube honours `sslmode=require`, `verify-ca` and
  `verify-full`. Run the harness on the database host or over a network you trust; certificate
  verification for the Python connections is not implemented in 0.1.x.
- **The database server itself** (authentication setup, `pg_hba.conf`, backups, its own logs) and a
  compromised local machine are out of scope. Depending on its settings (`log_statement`,
  `log_min_duration_statement`, error logging), PostgreSQL may log statements together with their bound
  parameter values; the server's logs are outside the harness's control.
- **The local samples file** written when `output.show_rows` is above 0 contains row values. Keep it
  local, out of version control and out of conversations.
- **Upstream vulnerabilities** in Cube, owlready2, HermiT, pySHACL, rdflib or pg8000 are fixed by
  updating the pinned versions in a new release.

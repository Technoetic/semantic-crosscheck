# Security policy

semantic-crosscheck connects to databases that may hold sensitive data, starts a local Cube server and
is often driven by an AI agent. This page describes what it protects, how, where the protection ends,
and how to report a vulnerability.

## Supported versions

Security fixes are made for the latest release (currently 0.1.x). Older releases are not patched.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub private vulnerability reporting:
<https://github.com/Technoetic/semantic-crosscheck/security/advisories/new> (repository **Security**
tab, **Report a vulnerability**). Do not open a public issue or pull request for a vulnerability. If the
button is not offered, open an issue that only asks for a private contact, without any details.

Include the version, the stage and command, the platform, and the smallest synthetic configuration that
shows the problem. Never include real data, row values, DSNs, passwords or tokens in a report, and do
not attach a `server.log` written with `SXC_CUBE_DEBUG=1` or a traceback printed with `SXC_DEBUG=1`; if
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
| A stage, a bug or a mutated model writes to source tables | Two roles: the loader (`SXC_PG_OWNER_DSN`) is used only by `demo`; the check role (`SXC_PG_DSN`) only reads the sources and owns a separate output schema. `doctor` fails when the check role holds INSERT, UPDATE, DELETE, TRUNCATE or TRIGGER on a source table, owns the table or its schema (or is a member of either owner's role, and so could `SET ROLE` to it), or has CREATE on the source schema. `doctor`, `reason`, `cube` and `verify` read in `READ ONLY` transactions. `build` reads the sources inside its write transactions (it counts the shape views and copies the edges into temporary tables there), so for `build` the protection is the check role's privileges, which `doctor` proves; it writes only to the output schema and to temporary tables. Cube sessions are forced read-only with `PGOPTIONS=-c default_transaction_read_only=on`. |
| Row values leak through outputs | Results hold counts, check ids and configuration ids only. Database error messages are not stored (they can quote values); the SQLSTATE is. Cube errors reach results as HTTP status, error type or SQLSTATE. The adapter drops Cube's request logs (they contain filter values) and writes each error to `server.log` as one `SXC_CUBE_ERROR` (or `SXC_ERROR`) line with only its class, its SQLSTATE or error code when Cube passes one (wrapped query errors usually carry none) and a coarse category (`timeout`), because the quoting of values in messages depends on the server's language; model compile errors are kept (they quote only the generated model); console output of the Cube packages is replaced by one `SXC_CONSOLE` notice. Unexpected exceptions print only their type and location. A new output directory gets a `.gitignore` of `*`, so results and logs are not committed by accident. |
| Credentials leak | DSNs come only from environment variables. `crosscheck.toml` holds the names of those variables (`source.dsn_env`, `source.owner_dsn_env`, `cube.dsn_env`); a value that looks like a DSN is refused without being echoed, and a key named `dsn` or containing `password` is refused in `[source]` and `[cube]`. A malformed DSN (for example an unencoded `/`, `#` or `?` in a password) is refused before anything prints it, and messages show a DSN as `postgresql://user@host:port/database` at most. The Cube API secret is random per run and exists only in the child process's environment. |
| Secrets reach the Cube child or a proxy | The child gets the inherited environment minus every `PG*`, `CUBEJS_*` and `SXC_*` variable, `NODE_OPTIONS`, every variable whose value contains a `postgres://` or `postgresql://` URL and the configured DSN variable names; the runner then sets only what the adapter needs. The client sends its requests (the JWT and filter values) to 127.0.0.1 through an opener without proxies, whatever `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` or the system settings say. |
| A query runs on after it was abandoned | The Python stages read source data and write the output schema only in transactions with a `statement_timeout`: `source.statement_timeout_ms` (at least 1000), or 10 minutes for the demo load. Cube sessions get `statement_timeout` from `cube.query_timeout_s` twice, through `PGOPTIONS` and `CUBEJS_DB_QUERY_TIMEOUT` (Cube's driver sets it from the latter on every connection, and it is also Cube's queue execution timeout), so a query the client gave up on stops in PostgreSQL too. A Cube query returns at most 50,000 rows. |
| SQL injection through the configuration | Every identifier is validated against `^[a-z_][a-z0-9_]{0,62}$` and quoted; values are bound parameters, or literals from the validated configuration. `sql_violation` predicates and gold SQL files are trusted code by design and are labelled as such in reports. |
| The Cube server is reachable by others | The adapter binds to 127.0.0.1 only, requires a JWT signed with the per-run secret, runs with development mode and telemetry off, and is stopped when the stage ends. |
| A planted program runs instead of node, npm or java | Programs are looked up on `PATH` only, never in the current directory (Windows would otherwise search the current directory first). `SXC_JAVA` must be an absolute path or a command name on `PATH`. |
| Supply chain of the Cube adapter | `npm ci --ignore-scripts` from a committed lockfile (npm verifies every tarball's integrity; no package install script runs). The native module is downloaded over https (redirects must stay on https) and accepted only when its size and SHA-256 match `native-sha256.json`; only its single expected file is extracted. On Linux the glibc version is checked before anything is installed. `installed.json` records the hashes of the adapter files, so an install made from other adapter files (an older or modified checkout) is refused until `cube-install` refreshes it. |
| Supply chain of the Python and CI dependencies | Direct Python dependencies are pinned to exact versions in `requirements/`. GitHub Actions are pinned by full commit SHA and the workflow token is read-only (`contents: read`). |
| Reasoner side effects | HermiT runs in a private temporary directory that is removed afterwards. The Pellet reasoner bundled with owlready2 (AGPL-3.0) is never invoked. |
| An agent misreads or overstates a result | Stage status is fail-closed (any error, failure or check not run prevents a pass) and `not_run` is never a pass. A run narrowed with `--only`, `--gold` or `--hierarchy` leaves `not_run` checks for what it skipped, so it cannot pass. In V4 only a failing suite item kills a mutant: an item that errors leaves the mutant unjudged, and a survivor of an incomplete suite is `not_run`. `not_proven` lists what a run could not prove. `report` recomputes statuses from the checks, requires `build`, `cube` and `verify` (and `reason` when there is an ontology or shapes) and counts a missing or stale stage as `not_run`. Data read from the database and from results is data, never instructions. |
| A secret, an address or a private term is published with the repository | `tools/leak_scan.py` scans for secrets, e-mail addresses outside `tools/leak-allow.txt` and an optional private deny-list kept outside the repository; the git hooks in `.githooks/` run it before every commit and push, and CI scans the working tree and the whole history of a full clone. Findings are reported by location and pattern id, never by the matching text. |

## TLS

The DSN's `sslmode` decides TLS for the Python stages (pg8000) and for the Cube child:

| `sslmode` | Python stages | Cube |
|---|---|---|
| `disable` | no TLS | no TLS |
| `allow`, `prefer` or none | TLS when the server offers it, without certificate verification | no TLS |
| `require` | TLS required, without certificate verification | TLS required, without certificate verification |
| `verify-ca` | the certificate is checked against `sslrootcert`, or the system store when it is absent or `system`; no host name check | the certificate and the host name are checked (Node.js always checks the host name) |
| `verify-full` | the certificate and the host name are checked | the certificate and the host name are checked |

Any other `sslmode`, an `sslrootcert` that cannot be loaded as a PEM file, and the parameters
`sslcert`, `sslkey`, `sslpassword`, `sslcrl`, `sslcrldir` and `sslcertmode` are refused with exit 3: client
certificates and revocation lists are not implemented. A relative `sslrootcert` is resolved against the
current directory. Use `verify-full` (or `verify-ca`) whenever the connection leaves the database host.

## Out of scope and known limits

- **The configuration and gold SQL are trusted code.** Anyone who can edit `crosscheck.toml` or a gold
  SQL file can run SQL as the check role. Review them like code.
- **Debug output holds row values.** `SXC_CUBE_DEBUG=1` writes the full Cube error text, which can quote
  values, to `server.log`, and `SXC_DEBUG=1` prints full tracebacks. Use them locally, never paste or
  commit their output.
- **Install hints and local paths.** When a package or the Cube adapter is missing, results store the hint
  with the placeholders `<python>` and `<plugin>`; `sxc.py` prints it with the interpreter and the plugin
  directory filled in, so the command works from any directory while result files stay free of local paths
  (the only path a result repeats is a `--home` directory the user typed).
- **The database server itself** (authentication setup, `pg_hba.conf`, backups, its own logs) and a
  compromised local machine are out of scope. Depending on its settings (`log_statement`,
  `log_min_duration_statement`, error logging), PostgreSQL may log statements together with their bound
  parameter values; the server's logs are outside the harness's control.
- **Upstream vulnerabilities** in Cube, owlready2, HermiT, pySHACL, rdflib or pg8000 are fixed by
  updating the pinned versions in a new release.

## Maintainer checklist for a public release

- Turn on private vulnerability reporting in the repository settings, so the link above works.
- Tag the release (`git tag -a v0.1.0`) and push the tag: README's Codex command pins `--ref v0.1.0`, and the
  release archive is made from the tag with `git archive` and published with its SHA256SUMS.
- Enable the hooks in the publishing clone with `git config core.hooksPath .githooks`, and set
  `SXC_DENYLIST` to the private deny-list (or `none`).
- Before the first push, run `python tools/leak_scan.py --worktree --history` with the private
  deny-list. The commit identity of the published history is listed in `tools/leak-allow.txt`; rewriting
  the commits to a GitHub noreply address before publication, and removing that line, is the
  alternative.

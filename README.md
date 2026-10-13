# semantic-crosscheck

Cross-layer verification for an ontology plus semantic layer built on PostgreSQL 17.

semantic-crosscheck generates four layers from one configuration file and checks that they agree with
each other and with hand-written "gold" SQL:

| Layer | Technology | Stage |
|---|---|---|
| Hierarchy closure | PostgreSQL 17 (`WITH RECURSIVE`, `CYCLE`, `MERGE ... RETURNING merge_action()`) | `build` |
| Data rules | SQL violation views and generated SHACL shapes checked with pySHACL | `build`, `reason` |
| Ontology | OWL 2 classes and axioms checked with HermiT (through owlready2) | `reason` |
| Semantic layer | Cube Core data model served on 127.0.0.1 | `cube`, `verify` |

It ships as a command-line tool (`sxc.py`), a Claude Code plugin and a Codex plugin.

**What it is:** a harness that turns "the dashboard, the rules and the ontology seem to agree" into
checks with ids, counts and an exit code, and that says explicitly what a run could not prove.

**What it is not:** a modelling tool, a BI front end, a data-quality platform or a reasoner of its own.
It does not design your ontology, it does not repair data, and it never writes to your source tables.

## Why cross-layer verification

A semantic layer, a set of data rules and an ontology usually describe the same facts three times, in
three languages, maintained by different people. Each layer can look right on its own and still
disagree with the others: a measure filter that differs from the report it replaces, a join that fans
out, a referential rule whose SQL and SHACL forms treat NULL differently, a closure that silently
truncates at some depth. Testing each layer in isolation does not catch these, because each test
shares the misconception of the layer it tests.

semantic-crosscheck compares independent formulations of the same question instead:

- the generated closure table against an independent recomputation and against OWL's transitive closure;
- SQL violation views against SHACL validation, row key by row key;
- Cube answers against gold SQL that computes its result on its own (V1, V2), against totals,
  additivity and measure pairs derived from the model (V3);
- and the gold suite itself against mutants of the model (V4): if a deliberately broken model still
  passes, the gold queries cannot see that kind of error.

Disagreement between two layers is a defect in one of them. Each failed check names the
`crosscheck.toml` entry to fix. Everything is generated from that file, so the fix goes there, never
into the generated SQL or YAML.

## Requirements

| Component | Needed for | Version |
|---|---|---|
| Python with pg8000 (`requirements/core.txt`) | every stage | Python 3.11 or later |
| PostgreSQL | every database stage | PostgreSQL 17 or later (`MERGE ... WHEN NOT MATCHED BY SOURCE`, `merge_action()`, `COPY ... ON_ERROR`) |
| psql (the PostgreSQL client) | running `docs/setup-roles.sql` once | any recent version |
| owlready2, pySHACL, rdflib (`requirements/reason.txt`) | `reason` | pinned in the file |
| Java | HermiT in `reason` | Java 11 or later |
| Node.js with npm | `cube-install`, `cube`, `verify` | Node.js 22 or later |
| A platform with a pinned Cube native module | `cube`, `verify` | Windows x64, macOS x64 and arm64, Linux x64 (glibc 2.34 or later) and arm64 (glibc 2.30 or later); musl is not supported |

The pins are listed in `skills/semantic-crosscheck/adapters/cube/native-sha256.json` (Cube 1.8.3). The
CI workflow runs the unit tests on Linux, Windows and macOS with Python 3.11 and 3.13, and the
integration tests and the demo end to end on Linux x64 with PostgreSQL 17, Java 21 and Node.js 22.
The minimums above are the versions `doctor` and the stages check; Java 11 itself has not been run end
to end. The macOS and Linux arm64 Cube pins were checked against the release assets but have not been
run end to end.

## Install

### Claude Code

Type these yourself (`/plugin` commands cannot be run by the agent), then restart Claude Code:

```text
/plugin marketplace add Technoetic/semantic-crosscheck
/plugin install semantic-crosscheck@semantic-crosscheck
```

The marketplace follows the repository's default branch. To use exactly a release, clone its tag and add
the clone instead: `/plugin marketplace add <path to the clone>`.

The plugin adds the commands `/xc-doctor`, `/xc-demo`, `/xc-build`, `/xc-reason`, `/xc-cube`,
`/xc-verify`, `/xc-report` and `/xc-install`, and a skill that tells the agent the stage order and the
rules of the harness.

### Codex

Register the release tag and add the plugin:

```text
codex plugin marketplace add Technoetic/semantic-crosscheck --ref v0.1.0
codex plugin add semantic-crosscheck@semantic-crosscheck-local
```

If Windows PowerShell refuses `codex.ps1` because of the execution policy, use `codex.cmd` instead of
`codex` in both commands. Then ask for a stage with `$semantic-crosscheck:semantic-crosscheck verify` or in
plain language. See
[skills/semantic-crosscheck/references/codex.md](skills/semantic-crosscheck/references/codex.md).

### Plugin setup

The plugin brings the commands and the scripts, not a Python environment or a database. Once per
machine:

1. **Python.** Create a virtual environment with Python 3.11 or later and activate it before you start
   Claude Code or Codex, so that `python` in the agent's shell is that interpreter. Install the
   requirements from the plugin directory by absolute path (`reason.txt` includes `core.txt`):

   ```bash
   python -m pip install -r "<plugin root>/requirements/reason.txt"
   ```

   `<plugin root>` is the installed plugin directory, for example
   `~/.claude/plugins/cache/semantic-crosscheck/semantic-crosscheck/0.1.0` (Claude Code) or
   `~/.codex/plugins/cache/semantic-crosscheck-local/semantic-crosscheck/0.1.0` (Codex); these are the
   locations current releases use. You do not have to find it yourself: when a package is missing,
   `/xc-doctor` prints the exact `pip install -r "..."` command with the interpreter and the
   requirements file as absolute paths.
2. **Database roles.** Run `<plugin root>/docs/setup-roles.sql` with psql as a superuser (see the quick
   start, step 2) and set the environment variables below.
3. **Java** 11 or later for `reason`: put `java` on `PATH` or set `SXC_JAVA`.
4. **Cube adapter**: run `/xc-install` (or `sxc.py cube-install`) once; it needs Node.js 22 or later.
5. Run `/xc-doctor` until it passes, then follow the stage order (`/xc-demo` first for the demo).

### Environment variables

DSNs come only from environment variables; `crosscheck.toml` holds the variable names, never a DSN.

| Variable | Used by | Meaning |
|---|---|---|
| `SXC_PG_DSN` | every database stage, Cube | check-role DSN, `postgresql://user@host:port/database[?sslmode=...]` (name configurable with `source.dsn_env`) |
| `SXC_PG_OWNER_DSN` | `demo` | loader-role DSN (name configurable with `source.owner_dsn_env`) |
| `PGPASSWORD` | database stages, Cube | password for a DSN without one (pg8000 does not read `~/.pgpass`) |
| `SXC_JAVA` | `doctor`, `reason` | absolute path of the `java` executable, or a command name on `PATH` |
| `SXC_CUBE_HOME` | `cube-install`, `doctor`, `cube`, `verify` | install directory of the Cube adapter (default: a per-user cache) |
| `SXC_CUBE_DEBUG` | `cube`, `verify` | `1` writes the full Cube error text to `server.log`; local debugging only, the log can then hold row values |
| `SXC_DEBUG` | every stage | `1` prints the full traceback of an unexpected exception; local debugging only |

On Windows PowerShell set a variable with `$env:SXC_PG_DSN = "..."`.

## Quick start with the demo

The demo loads a synthetic parts / bill-of-materials / orders / stock data set into a throw-away
database and runs every stage on it. Run the commands from the root of a clone of this repository.

1. Install the Python packages (Python 3.11 or later):

   ```bash
   python -m venv .venv
   . .venv/bin/activate              # Windows: .venv\Scripts\activate
   python -m pip install -r requirements/reason.txt
   ```

   `requirements/core.txt` alone is enough for `doctor`, `demo`, `build`, `cube`, `verify` and `report`;
   `reason.txt` adds owlready2, pySHACL and rdflib for the `reason` stage.

2. Create a throw-away PostgreSQL 17 database and the two roles with
   [docs/setup-roles.sql](docs/setup-roles.sql). It needs psql and a superuser; add `-h` and `-p` for
   your server:

   ```bash
   createdb -U postgres sxc_demo_db
   psql -U postgres -d sxc_demo_db -v loader_role=sxc_loader -v check_role=sxc_check -v source_schema=sxc_demo -v output_schema=sxc_out -f docs/setup-roles.sql
   ```

   When the server uses password authentication, set the two passwords interactively, in psql as the
   superuser, with `\password sxc_loader` and `\password sxc_check`: they never reach a file or the
   shell history.

3. Point the environment at the two roles:

   ```bash
   export SXC_PG_OWNER_DSN=postgresql://sxc_loader@localhost:5432/sxc_demo_db   # loader: demo only
   export SXC_PG_DSN=postgresql://sxc_check@localhost:5432/sxc_demo_db          # check role: every other stage
   ```

   A DSN without a password uses `PGPASSWORD`; with two roles see
   [Troubleshooting](#troubleshooting). Put `java` (11 or later) on `PATH` or set `SXC_JAVA`.

4. Install the pinned Cube adapter once (Node.js 22 or later with npm; downloads the npm packages and
   the SHA-256-pinned Cube native module, about 355 MB on disk):

   ```bash
   python skills/semantic-crosscheck/scripts/sxc.py cube-install
   ```

   Optionally check the environment before loading anything (this writes `./.sxc/doctor`; the check
   role's privileges are proven after the demo tables exist):

   ```bash
   python skills/semantic-crosscheck/scripts/sxc.py doctor --require core,reason,cube
   ```

5. Load the demo, then run every stage in this order:

   ```bash
   python skills/semantic-crosscheck/scripts/sxc.py demo --config demo/crosscheck.toml --variant clean
   python skills/semantic-crosscheck/scripts/sxc.py doctor --config demo/crosscheck.toml --require core,reason,cube
   python skills/semantic-crosscheck/scripts/sxc.py build --config demo/crosscheck.toml
   python skills/semantic-crosscheck/scripts/sxc.py reason --config demo/crosscheck.toml
   python skills/semantic-crosscheck/scripts/sxc.py cube --config demo/crosscheck.toml
   python skills/semantic-crosscheck/scripts/sxc.py verify --config demo/crosscheck.toml
   python skills/semantic-crosscheck/scripts/sxc.py report --config demo/crosscheck.toml
   ```

   Every command should exit 0. Results go to `demo/.sxc/`: one `result.json` per stage, and
   `report.md` and `report.json` from the last step. Each stage prints a summary line, then the checks
   that did not pass, the NOT PROVEN items and a `next:` action. The summary lines and NOT PROVEN items
   of the clean demo:

   ```text
   [sxc demo] PASS  checks: 5 pass, 0 fail, 0 error, 0 not_run  -> .../demo/.sxc/demo/result.json
   [sxc doctor] PASS  checks: 21 pass, 0 fail, 0 error, 0 not_run  -> .../demo/.sxc/doctor/result.json
   [sxc build] PASS  checks: 6 pass, 0 fail, 0 error, 0 not_run  -> .../demo/.sxc/build/result.json
   [sxc reason] PASS  checks: 9 pass, 0 fail, 0 error, 0 not_run  -> .../demo/.sxc/reason/result.json
     NOT PROVEN  shacl:open_le_total: no SHACL form (sql_violation); checked only in SQL (shape:open_le_total in build)
     NOT PROVEN  v5:defined:ProductUsedAsPart: the class has no members in OWL or in SQL, so the comparison holds vacuously
   [sxc cube] PASS  checks: 7 pass, 0 fail, 0 error, 0 not_run  -> .../demo/.sxc/cube/result.json
   [sxc verify] PASS  checks: 42 pass, 0 fail, 0 error, 0 not_run  -> .../demo/.sxc/verify/result.json
     NOT PROVEN  v4:measure_normalization_drop:on_hand equivalent on this data
     NOT PROVEN  v4:measure_normalization_drop:consigned equivalent on this data
   [sxc report] PASS  checks: 6 pass, 0 fail, 0 error, 0 not_run  -> .../demo/.sxc/report/result.json
   ```

   The four NOT PROVEN items are expected: `open_le_total` is an `sql_violation` shape, which has no
   SHACL form; no product is used as a part, so that defined class is empty in both layers; and the
   clean data has no location codes with stray whitespace or case, so dropping the normalisation changes
   nothing that could be observed. `verify` is the slow stage (one Cube process per mutant; 43 seconds on
   the machine the numbers above come from).

6. Load the dirty variant into the same database and run the stages from `build` onwards again:

   ```bash
   python skills/semantic-crosscheck/scripts/sxc.py demo --config demo/crosscheck.toml --variant dirty
   python skills/semantic-crosscheck/scripts/sxc.py build --config demo/crosscheck.toml
   python skills/semantic-crosscheck/scripts/sxc.py reason --config demo/crosscheck.toml
   python skills/semantic-crosscheck/scripts/sxc.py cube --config demo/crosscheck.toml
   python skills/semantic-crosscheck/scripts/sxc.py verify --config demo/crosscheck.toml
   python skills/semantic-crosscheck/scripts/sxc.py report --config demo/crosscheck.toml
   ```

   `demo` reloads the tables in place, so it works after `build`. The dirty variant fails on purpose:
   `demo` exits 0, `build` 1, `reason` 1, `cube` 0, `verify` 1 and `report` 1. The planted defects and
   the checks that show them:

   | Planted defect | Check | Outcome on the dirty variant |
   |---|---|---|
   | a two-node cycle | `closure:has_component` (build) | fail, reason `cycle`; `verify` then reports every check over the closure as `not_run` |
   | two orphan child references | `shape:child_exists` (build) | fail, 2 violations; `shacl:child_exists` agrees |
   | a zero-quantity BOM line | `shape:qty_positive` (build) | fail, 1 violation; `shacl:qty_positive` agrees |
   | two negative stock rows | `shape:stock_nonnegative` (build) | pass (severity warning) with 2 violations |
   | three orders with `qty_open` above `qty_total` | `shape:open_le_total` (build) | fail, 3 violations (SQL only) |
   | a raw material with a child | `owl:consistency` (reason) | fail; the V5 checks are `not_run` |
   | two malformed CSV rows | `demo:copy_on_error` (demo) | pass, `skipped=2` |
   | location codes with whitespace or case variants | none | not reported: q03 normalises them like Cube, so `v1:q03_stock` passes; they make `v4:measure_normalization_drop:on_hand` observable, and q03 kills it |
   | two firm orders with out-of-range due dates | none | not reported: the q05 windows include or exclude them on both sides, so `v1:q05_firm_window` passes |

   `cube` passes here because the closure table of the clean build is still there: `build` leaves it
   untouched when it refuses a cyclic closure, and `verify` does not trust it. On a fresh database the
   dirty variant makes `cube` exit 1 instead: `cube:gold_members` fails for the three gold queries over
   the closure, and the `next:` line points at the cycle.
   `demo/.sxc/demo/expected.json` holds the reference numbers of the independent Python implementation
   and names the check to compare each one with.

## Stages

```text
python skills/semantic-crosscheck/scripts/sxc.py <stage> [--config PATH] [--out DIR] [--quiet] [options]
```

| Stage | Role | Options | Writes |
|---|---|---|---|
| `doctor` | Python, pg8000, DSN, PostgreSQL >= 17, check-role privileges, optional components | `--require core,reason,cube` | `doctor/result.json` |
| `demo` | loads the synthetic demo with the loader role | `--variant clean\|dirty`, `--seed N`, `--scale N` | `demo/result.json`, `demo/expected.json` |
| `build` | shape violation views, hierarchy closures | `--hierarchy NAME` | `build/result.json`, `build/build.sql`, `build/shapes.ttl`, `build/fk_review.sql` |
| `reason` | SHACL against SQL, OWL consistency, OWL against SQL | `--only shacl,owl` | `reason/result.json` |
| `cube` | generates the Cube model, starts Cube, checks it | none | `cube/result.json`, `cube/model/*.yml`, `cube/model.json`, `cube/server.log` |
| `verify` | V1 strict compare, V2 strata sweep, V3 query shapes, V4 mutation tests | `--only v1,v2,v3,v4`, `--gold ID` | `verify/result.json`, `verify/server.log`, `verify/mutants/*/` |
| `report` | summary of every stage | none | `report/result.json`, `report.md`, `report.json` |
| `cube-install` | installs the pinned Cube adapter once per user | `--home DIR`, `--native-file PATH`, `--force` | `cube_install/result.json`, `cube_install/npm-ci.log`, `cube_install/smoke.log` |

- `--config PATH`: when given it must exist, otherwise every stage exits 2 without running. Without it
  `./crosscheck.toml` is used; only `doctor` and `cube-install` run without a configuration.
- `--out DIR`: the output directory, by default `.sxc` next to the configuration (or `./.sxc` without
  one). A new output directory gets a `.gitignore` of `*`.
- `--quiet` prints only the summary line; `sxc.py --version` prints the version.

Exit codes: 0 pass; 1 a check failed, errored or did not run (also a run narrowed with `--only`,
`--gold` or `--hierarchy`); 2 configuration or usage error; 3 environment missing (DSN, PostgreSQL,
Python packages, Java, Node.js, the Cube adapter). The configuration format is documented in
[skills/semantic-crosscheck/references/config.md](skills/semantic-crosscheck/references/config.md); the
full contract, including every check id and the verification semantics, is [docs/design.md](docs/design.md).

## Reading results

Every stage writes `<stage>/result.json` with `status`, `checks` (id, status, `config_ref`, `detail`
with counts), `not_proven` and `next_safe_action`.

- **Status is fail-closed:** any `error` > any `fail` > any `not_run` > all `pass`.
- **`not_run` is never a pass.** A check is `not_run` when it could not run or was left out: a gold query
  over a closure that did not build, a part left out by `--only` or `--gold` (the `scope:*` checks), a
  hierarchy left out by `--hierarchy`, a surviving mutant whose suite was incomplete. A narrowed run is
  therefore never a result.
- **`not_proven`** lists what a passing check does not show: an empty stratum, an equivalent mutant, a
  shape compared only in SQL, a vacuous defined class, a pair test that compared a sample of its keys,
  a component that is not installed. It does not change the status; show it with every verdict.
- **`report` is the verdict.** It requires `build`, `cube` and `verify`, plus `reason` when the
  configuration has an `[ontology]` or `[[shapes]]`. A required stage that never ran, or that ran before
  a stage it depends on (`build` after `demo`; `reason` and `cube` after `build`; `verify` after `build`
  and `cube`), is `not_run` there, so `report` exits 0 only when every required stage ran, is current
  and passed. `doctor` and `demo` are optional, but a failing one still fails the report.

## Check catalogue

| Stage | Check id | Passes when |
|---|---|---|
| doctor | `python`, `pg8000`, `dsn`, `connect`, `pg_version`, `privileges:temporary` | the basics are present, the server is PostgreSQL 17 or later and the check role may create temporary tables |
| doctor | `privileges:select:<entity>`, `privileges:no_write:<entity>`, `output_schema` | the check role can read every source table, cannot write, own or create in them, and can create in the output schema |
| doctor | `reason:*`, `cube:*` | required components are ready (others are listed as not proven when missing) |
| demo | `demo:rows:<table>`, `demo:copy_on_error` | loaded rows equal the generator; `COPY ... ON_ERROR ignore` skipped exactly the malformed rows |
| build | `shape:<id>` | no violations (a `warning` shape passes and reports its count) |
| build | `closure:<h>`, `closure_apply:<h>` | acyclic (or `on_cycle = "report"`) within `max_depth`; the table equals the recomputation |
| reason | `shacl:<id>` | pySHACL focus nodes equal the SQL violation keys |
| reason | `owl:consistency`, `v5:closure:<h>`, `v5:defined:<name>` | HermiT finds the ontology consistent; OWL closure and defined classes equal SQL |
| cube | `cube:model`, `cube:meta`, `cube:gold_members:<gold>` | model generated; Cube lists exactly its public members; gold members exist and are public |
| verify | `v1:<gold>`, `v2:<gold>:<stratum>` | gold rows and Cube rows are identical in the form of each column's Cube type |
| verify | `v3:total:<member>`, `v3:additive:<member>:<member>`, `v3:pair:<member>+<member>` | totals equal SQL; groups add up; measures together equal measures alone |
| verify | `v4:<operator>:<target>` | the mutant is killed by a failing gold-suite item |
| report | `stage:<name>` | the stage ran, is current and passed |

`stage` (any stage that stopped early), `database` (doctor), `demo:load` (demo), `v4:suite` (verify),
the `scope:*` checks (reason, verify) and the `cube_install:*` checks complete the list in
[docs/design.md](docs/design.md#6-check-catalogue).

## Security model

- **Two database roles.** The loader (`SXC_PG_OWNER_DSN`) is used only by `demo`. The check role
  (`SXC_PG_DSN`) reads the source tables and owns the output schema, where every generated table and
  view lives. `doctor` proves that the check role can read the sources, cannot write, own or create in
  them, and may create temporary tables.
- **Reads.** `doctor`, `reason`, `cube` and `verify` read in `READ ONLY` transactions with a statement
  timeout. `build` reads the sources inside its write transactions and relies on the check role's
  privileges; it writes only to the output schema and to temporary tables. Cube sessions run with
  `PGOPTIONS=-c default_transaction_read_only=on` and a statement timeout of `cube.query_timeout_s`.
- **No row values in outputs.** Results, console output and reports carry counts and ids only, never
  keys, names or quantities from the database. Database error messages are not stored (they can quote
  values); the SQLSTATE is. Cube's error log keeps only each error's class and first line, with quoted
  text, numbers and SQL replaced; `SXC_CUBE_DEBUG=1` adds the full text, for local debugging.
- **Credentials only in the environment.** DSNs come from environment variables; the configuration
  refuses credentials and accepts only variable names. The Cube adapter listens on 127.0.0.1 only, with
  a random per-run API secret, and the client never sends its requests through a proxy.
- **Pinned supply chain.** The Cube adapter is installed with `npm ci --ignore-scripts` from a committed
  lockfile; the native module is accepted only if its size and SHA-256 match `native-sha256.json`.
  Direct Python dependencies are pinned to exact versions in `requirements/`, and CI actions are pinned
  by commit SHA.
- **Generated SQL is parameterised.** Identifiers are validated and quoted; values are bound
  parameters. `sql_violation` predicates and gold SQL files are trusted code from the configuration and
  are labelled as such in reports.

See [SECURITY.md](SECURITY.md) for the threat model, TLS (`sslmode`) and how to report a vulnerability.

## Troubleshooting

- **Proxies.** The stages talk to the local Cube adapter directly: `HTTP_PROXY`, `HTTPS_PROXY` and the
  system proxy are ignored for it, and no `NO_PROXY` entry is needed. `cube-install` does use the proxy:
  npm needs registry access (its own proxy settings) and the native module download honours
  `HTTPS_PROXY`. Without network access, download the release tarball named in `native-sha256.json`
  elsewhere and pass it with `--native-file`; it is verified the same way.
- **Java.** `reason:java` fails or `reason` exits 3: install Java 11 or later and put `java` on `PATH`,
  or set `SXC_JAVA` to the absolute path of the `java` executable (doctor refuses a relative path).
- **Node.js.** `cube:node` fails or `cube-install` exits 3: install Node.js 22 or later with npm, built
  for the machine's architecture, and put it on `PATH`. Programs are looked up on `PATH` only, never in
  the current directory.
- **glibc.** On Linux, `cube-install` exits 3 when glibc is older than the pinned native module needs
  (2.34 for x64, 2.30 for arm64; `ldd --version` shows yours). Use a newer distribution or container.
  When the smoke test fails, read `.sxc/cube_install/smoke.log`.
- **`sslmode`.** The Python stages verify the server certificate only with `sslmode=verify-ca` or
  `verify-full` (with `sslrootcert`, or the system store); `require` encrypts without verification, and
  `prefer` or no `sslmode` uses TLS only when the server offers it. Client certificates (`sslcert`,
  `sslkey`) are refused with exit 3. See [SECURITY.md](SECURITY.md#tls).
- **Two roles with passwords.** A DSN without a password uses `PGPASSWORD`, and pg8000 does not read
  `~/.pgpass`. Either set `PGPASSWORD` to the loader's password for `demo` only and to the check role's
  password for every other stage, or give each DSN its own percent-encoded password. Read a password
  without echoing it, for example `read -rs PGPASSWORD && export PGPASSWORD` in bash.
- **Stale adapter.** `cube:adapter` fails with `reason=adapter_files_changed`, or `cube` and `verify`
  exit 3 saying the install "was made from other adapter files than this checkout": the plugin was
  updated. Rerun `sxc.py cube-install` (no `--force` needed).
- **`cube-install --home DIR`.** The other stages find that install only with `SXC_CUBE_HOME=DIR`; the
  `next:` line says so.
- **Stale results.** `report` lists a stage as stale when a stage it depends on ran after it: rerun the
  stages it names, in the order it gives, then `report`. Each result also records a fingerprint of
  `crosscheck.toml` and the gold SQL taken before the stage ran, so editing either one makes the stages
  that read it stale (`why=inputs_changed`) until they are rerun.
- **`--config` mistyped.** Every stage exits 2 with `--config <name>: no such file`; nothing was run.
- **Demo tables.** `demo` exits 2 when `sxc_demo` already holds tables with other columns than the
  demo defines; it never drops a table. Use a fresh database. When `demo:load` errors, the `next:` line
  explains the SQLSTATE (privileges, a foreign key, a constraint, another session).
- **Privileges.** Follow the `next:` line of `doctor --config`: `privileges:no_write` means the check
  role could change a source table (revoke the privilege, change the owner, or use another role);
  `privileges:temporary` means the closure's temporary tables are not allowed (`GRANT TEMPORARY`).
- **Cube does not start.** `cube` exits 3 with the redacted adapter error lines and points at
  `cube/server.log`. For the full error text rerun with `SXC_CUBE_DEBUG=1`; that log can quote row
  values, so keep it local.
- **Unexpected exception.** The stage exits 1 and prints only the exception type and location. Rerun
  with `SXC_DEBUG=1` for the traceback, locally.

## Limitations and "not proven"

A passing run means: these checks found no disagreement on this data. It does not mean the model is
correct in general, and the tool says where its evidence ends. Every result has a `not_proven` list
that does not change the status but is shown with every verdict:

- an empty stratum (no node of that kind exists, so V2 had nothing to compare);
- an equivalent mutant (the mutation changes no result on the current data, so V4 cannot tell);
- a shape compared only in SQL: one with `sql_violation`, one that combines `class` with bounds, or
  shapes on the same column that need different kinds of RDF value (`shared_path`: adding a shape can
  remove another shape's SHACL comparison);
- a defined class without members in either layer (the comparison holds vacuously);
- a V3 pair test over more than 2,000 keys, which compares a deterministic sample of 2,000 keys;
- a component that is not installed (Java, Node.js, the Cube adapter), or an optional stage that did
  not run.

Further limits of this release:

- The gold SQL is the reference and is trusted. If the gold SQL and the configuration share a mistake,
  they agree; write gold queries independently of the generated objects.
- OWL is open-world: only definitions whose OWL and SQL meanings coincide are compared.
- One swept parameter per gold query (use `param_sets` for several); `timeDimensions` take a
  `dateRange`, not a `granularity`; `closure_join` needs exactly one hierarchy.
- A Cube query returns at most 50,000 rows; a query that reaches the limit is an error, not a partial
  comparison.
- Identifiers must be plain lower-case SQL names; quoted mixed-case names are not supported.
- Cube 1.8.3 is pinned; PostgreSQL before 17 is not supported; client certificates are not supported.
- V4 starts one Cube process per mutant and is the slowest stage; SHACL and OWL inputs are capped by
  `verify.shacl_max_rows` and `verify.owl_max_individuals`.
- `report` cannot tell that `crosscheck.toml` or a gold SQL file changed after the last run.

## Uninstall

- Claude Code: `/plugin uninstall semantic-crosscheck@semantic-crosscheck`, then
  `/plugin marketplace remove semantic-crosscheck`.
- Codex: `codex plugin remove semantic-crosscheck@semantic-crosscheck-local`, then
  `codex plugin marketplace remove semantic-crosscheck-local`.
- The Cube adapter: delete its install directory, `SXC_CUBE_HOME` if you set it, otherwise
  `%LOCALAPPDATA%\semantic-crosscheck\cube-1.8.3` on Windows and
  `$XDG_CACHE_HOME/semantic-crosscheck/cube-1.8.3` or `~/.cache/semantic-crosscheck/cube-1.8.3`
  elsewhere.
- Results: delete the `.sxc` directories next to your configurations, and `./.sxc` where you ran
  `doctor` or `cube-install` without `--config`.
- The database objects: drop the throw-away database, or `DROP SCHEMA sxc_out CASCADE` for the generated
  objects alone. Roles survive `DROP DATABASE`: `DROP ROLE sxc_loader, sxc_check` once nothing they own
  is left (`DROP OWNED BY` in each database that still has some).
- The Python packages: delete the virtual environment.

## Development

```bash
python -m unittest discover -s tests -v
```

Unit tests need no database. Integration tests run when `SXC_TEST_ADMIN_DSN` names a superuser
connection to a throw-away PostgreSQL 17 server (`SXC_TEST_ROLE_PASSWORD` sets the test roles' password
where the server requires one); OWL tests also need Java (`SXC_JAVA` or `java` on `PATH`) and Cube tests
an adapter installed from this checkout (`SXC_CUBE_HOME` or the default location; refresh it with
`sxc.py cube-install` after changing the adapter files). The documentation tests keep this README, the
command documents and the CI workflow in line with the code.

`tools/leak_scan.py` checks for secrets, e-mail addresses outside `tools/leak-allow.txt` and, optionally,
terms from a private deny-list kept outside the repository (`SXC_DENYLIST`, or `SXC_DENYLIST=none` when
there is deliberately none). Enable it for commits and pushes once per clone with
`git config core.hooksPath .githooks`. CI scans the working tree and the whole history of a full clone
with the generic patterns and the allowlist only. When a contribution brings a public commit address
that is not on the allowlist, add it there or commit with a GitHub noreply address.

## License

MIT, see [LICENSE](LICENSE). Runtime dependencies are not vendored; their licences are listed in
[THIRD_PARTY.md](THIRD_PARTY.md). Changes are recorded in [CHANGELOG.md](CHANGELOG.md); the release notes
are in [docs/releases/v0.1.0.md](docs/releases/v0.1.0.md).

## 한국어 요약

semantic-crosscheck는 PostgreSQL 17 위에 만든 온톨로지와 시맨틱 레이어를 교차 검증하는 도구입니다.
설정 파일 하나(`crosscheck.toml`)에서 계층 클로저(PostgreSQL 17), 데이터 규칙(SQL 뷰와 SHACL),
OWL 온톨로지(HermiT), Cube 데이터 모델을 생성하고, 이 계층들이 서로 그리고 손으로 쓴 gold SQL과
일치하는지 확인합니다. 두 계층이 다르면 둘 중 하나에 결함이 있다는 뜻이며, 실패한 검사는 고쳐야 할
설정 항목을 알려 줍니다. 생성된 SQL이나 YAML은 고치지 않고 설정 파일을 고칩니다.

- 설치: Claude Code는 `/plugin marketplace add Technoetic/semantic-crosscheck` 후
  `/plugin install semantic-crosscheck@semantic-crosscheck`, Codex는 `--ref v0.1.0` 태그로 등록합니다.
  Python 패키지는 플러그인 디렉터리의 `requirements/reason.txt`를 절대 경로로 설치합니다.
- 데모 순서: `demo` → `doctor` → `build` → `reason` → `cube` → `verify` → `report`. Cube 어댑터는
  `cube-install`로 한 번 설치하고, 역할은 `docs/setup-roles.sql`을 psql로 superuser가 실행해 만듭니다.
- 원본 데이터는 읽기만 합니다. 검사 역할은 원본 테이블을 쓰거나 소유할 수 없음을 `doctor`가 확인하며,
  Cube 세션도 읽기 전용으로 강제됩니다. 결과와 보고서에는 건수와 ID만 담기고 행 값은 담기지 않습니다.
- `not_run`은 통과가 아니며 `--only`·`--gold`로 좁힌 실행은 결과가 아닙니다. `report`는 필수 단계가 모두
  최신 상태로 통과했을 때만 통과합니다. 증명하지 못한 부분(빈 층, 동등 돌연변이, SHACL로 비교하지 못한
  규칙, 표본으로 비교한 키, 설치되지 않은 구성 요소)은 `not_proven`으로 함께 보고됩니다.
- 요구 사항: Python 3.11 이상, PostgreSQL 17 이상, `reason`에는 Java 11 이상, Cube에는 Node.js 22
  이상(Linux는 glibc 2.34 이상, arm64는 2.30 이상). 라이선스는 MIT입니다.

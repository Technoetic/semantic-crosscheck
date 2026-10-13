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
- Cube answers against gold SQL that computes its result on its own (V1, V2), against totals and
  additivity derived from the model (V3);
- and the gold suite itself against mutants of the model (V4): if a deliberately broken model still
  passes, the gold queries cannot see that kind of error.

Disagreement between two layers is a defect in one of them. Each failed check names the
`crosscheck.toml` entry to fix. Everything is generated from that file, so the fix goes there, never
into the generated SQL or YAML.

## Quick start with the demo

The demo loads a synthetic parts / bill-of-materials / orders / stock data set into a throw-away
database and runs every stage on it. Run the commands from a clone of this repository.

1. Install the Python packages (Python 3.11 or later):

   ```bash
   python -m venv .venv
   . .venv/bin/activate              # Windows: .venv\Scripts\activate
   pip install -r requirements/reason.txt
   ```

   `requirements/core.txt` alone is enough for `doctor`, `demo`, `build`, `cube`, `verify` and `report`;
   `reason.txt` adds owlready2, pySHACL and rdflib for the `reason` stage.

2. Create a throw-away PostgreSQL 17 database and the two roles with
   [docs/setup-roles.sql](docs/setup-roles.sql) (as a superuser):

   ```bash
   createdb sxc_demo_db
   psql -d sxc_demo_db -v loader_role=sxc_loader -v check_role=sxc_check -v source_schema=sxc_demo -v output_schema=sxc_out -f docs/setup-roles.sql
   ```

   Then set the two passwords interactively in psql with `\password sxc_loader` and
   `\password sxc_check`.

3. Point the environment at the two roles. DSNs come only from environment variables; the
   configuration file refuses credentials.

   ```bash
   export SXC_PG_OWNER_DSN=postgresql://sxc_loader@localhost:5432/sxc_demo_db   # loader: demo only
   export SXC_PG_DSN=postgresql://sxc_check@localhost:5432/sxc_demo_db          # check role: every other stage
   ```

   A password goes in the URL (percent-encoded) or, when the URL has none, in `PGPASSWORD`. On Windows
   PowerShell use `$env:SXC_PG_DSN = "..."`. Optional: set `SXC_JAVA` to the `java` executable when
   Java 11 or later is not on `PATH` (needed for HermiT).

4. Install the pinned Cube adapter once (Node.js 20 or later with npm; downloads the npm packages and
   the SHA-256-pinned Cube native module):

   ```bash
   python skills/semantic-crosscheck/scripts/sxc.py cube-install
   ```

5. Run the stages. The demo tables exist only after `demo`, so the first `doctor` checks the
   environment only (no `--config`) and the second one proves the check role's privileges on the
   loaded tables:

   ```bash
   python skills/semantic-crosscheck/scripts/sxc.py doctor --require core,reason,cube
   python skills/semantic-crosscheck/scripts/sxc.py demo --config demo/crosscheck.toml --variant clean
   python skills/semantic-crosscheck/scripts/sxc.py doctor --config demo/crosscheck.toml --require core,reason,cube
   python skills/semantic-crosscheck/scripts/sxc.py build --config demo/crosscheck.toml
   python skills/semantic-crosscheck/scripts/sxc.py reason --config demo/crosscheck.toml
   python skills/semantic-crosscheck/scripts/sxc.py cube --config demo/crosscheck.toml
   python skills/semantic-crosscheck/scripts/sxc.py verify --config demo/crosscheck.toml
   python skills/semantic-crosscheck/scripts/sxc.py report --config demo/crosscheck.toml
   ```

   Results are written to `demo/.sxc/`: one `result.json` per stage, and `report.md` and
   `report.json` from the last step. Every stage prints a one-line summary followed by the checks that
   did not pass, the NOT PROVEN items and a `next:` action.

6. Load the dirty variant and run the stages from `build` onwards again:

   ```bash
   python skills/semantic-crosscheck/scripts/sxc.py demo --config demo/crosscheck.toml --variant dirty
   ```

   It plants a cycle, orphan references, a zero quantity, negative stock, inconsistent orders, a raw
   material with a child, location codes with whitespace and case variants, out-of-range dates and
   malformed CSV rows. The stages should report them; `demo/.sxc/demo/expected.json` holds the
   reference numbers from an independent Python implementation.

## Stages

```text
python skills/semantic-crosscheck/scripts/sxc.py doctor --config crosscheck.toml --out .sxc
```

| Stage | Role | Writes |
|---|---|---|
| `doctor` | Python, pg8000, DSN, PostgreSQL >= 17, check-role privileges, optional components (`--require core,reason,cube`) | `doctor/result.json` |
| `demo` | loads the synthetic demo with the loader role (`--variant clean\|dirty`) | `demo/result.json`, `demo/expected.json` |
| `build` | shape violation views, hierarchy closures | `build/result.json`, `build/build.sql`, `build/shapes.ttl`, `build/fk_review.sql` |
| `reason` | SHACL against SQL, OWL consistency, OWL against SQL (`--only shacl,owl`) | `reason/result.json` |
| `cube` | generates the Cube model, starts Cube, checks its metadata | `cube/result.json`, `cube/model/*.yml`, `cube/model.json`, `cube/server.log` |
| `verify` | V1 strict compare, V2 strata sweep, V3 query shapes, V4 mutation tests (`--only`, `--gold`) | `verify/result.json`, `verify/mutants/*/` |
| `report` | summary of every stage | `report/result.json`, `report.md`, `report.json` |
| `cube-install` | installs the pinned Cube adapter once per user | `cube_install/result.json` |

`--out` defaults to `.sxc` next to the configuration. Exit codes: 0 pass, 1 a check failed or could
not run, 2 configuration or usage error, 3 environment missing (DSN, PostgreSQL, Java, Node.js,
adapter). The configuration format is documented in
[skills/semantic-crosscheck/references/config.md](skills/semantic-crosscheck/references/config.md); the
full contract, including the verification semantics, is [docs/design.md](docs/design.md).

## Check catalogue

| Stage | Check id | Passes when |
|---|---|---|
| doctor | `python`, `pg8000`, `dsn`, `connect`, `pg_version` | the basics are present and the server is PostgreSQL 17 or later |
| doctor | `privileges:select:<entity>`, `privileges:no_write:<entity>`, `output_schema` | the check role can read, cannot write the sources, can create in the output schema |
| doctor | `reason:*`, `cube:*` | required components are ready (others are listed as not proven when missing) |
| demo | `demo:rows:<table>`, `demo:copy_on_error` | loaded rows equal the generator; `COPY ... ON_ERROR ignore` skipped exactly the malformed rows |
| build | `shape:<id>` | no violations (a `warning` shape passes and reports its count) |
| build | `closure:<h>`, `closure_apply:<h>` | acyclic (or `on_cycle = "report"`) within `max_depth`; the table equals the recomputation |
| reason | `shacl:<shape>` | pySHACL focus nodes equal the SQL violation keys |
| reason | `owl:consistency`, `v5:closure:<h>`, `v5:defined:<name>` | HermiT finds the ontology consistent; OWL closure and defined classes equal SQL |
| cube | `cube:model`, `cube:meta`, `cube:gold_members:<gold>` | model generated; Cube lists exactly its public members; gold members exist |
| verify | `v1:<gold>`, `v2:<gold>:<stratum>` | gold rows and Cube rows are canonically identical |
| verify | `v3:total:<member>`, `v3:additive:<measure>:<dimension>`, `v3:pair:<m1>+<m2>` | totals equal SQL; groups add up; measures together equal measures alone |
| verify | `v4:<operator>:<target>` | the mutant is killed by the gold suite |
| report | `stage:<name>` | mirrors each stage status |

## Using it from Claude Code or Codex

Claude Code (type these yourself; `/plugin` commands cannot be run by the agent), then restart Claude
Code:

```text
/plugin marketplace add Technoetic/semantic-crosscheck
/plugin install semantic-crosscheck@semantic-crosscheck
```

The plugin adds the commands `/xc-doctor`, `/xc-demo`, `/xc-build`, `/xc-reason`, `/xc-cube`,
`/xc-verify`, `/xc-report` and `/xc-install`, and a skill that tells the agent the stage order and the
rules below.

Codex:

```text
codex plugin marketplace add Technoetic/semantic-crosscheck --ref v0.1.0
codex plugin add semantic-crosscheck@semantic-crosscheck-local
```

Then ask for a stage with `$semantic-crosscheck:semantic-crosscheck verify` or in plain language. See
[skills/semantic-crosscheck/references/codex.md](skills/semantic-crosscheck/references/codex.md).

## Security model

- **Two database roles.** The loader (`SXC_PG_OWNER_DSN`) is used only by `demo`. The check role
  (`SXC_PG_DSN`) has `SELECT` on the source tables and owns the output schema, where every generated
  table and view lives. `doctor` verifies that the check role cannot write the source tables.
- **Read-only reads.** Source data is read in `READ ONLY` transactions with a statement timeout. Cube
  connects with the check role and `PGOPTIONS=-c default_transaction_read_only=on`, so every Cube
  session is read-only.
- **No row values in outputs.** Results, console output and reports carry counts and ids only, never
  keys, names or quantities from the database. Database error messages are not stored (they can quote
  values); the SQLSTATE is. `output.show_rows` (default 0) is the only way to write example rows, into a
  local samples file.
- **Credentials only in the environment.** DSNs come from environment variables; the configuration
  refuses credentials. The Cube adapter listens on 127.0.0.1 only, with a random per-run API secret that
  exists only in the child process's environment; its request logs are dropped.
- **Pinned supply chain.** The Cube adapter is installed with `npm ci --ignore-scripts` from a committed
  lockfile; the native module is accepted only if its SHA-256 matches `native-sha256.json`. Direct
  Python dependencies are pinned to exact versions in `requirements/`, and CI actions are pinned by
  commit SHA.
- **Generated SQL is parameterised.** Identifiers are validated and quoted; values are bound
  parameters. `sql_violation` predicates and gold SQL files are trusted code from the configuration and
  are labelled as such in reports.

See [SECURITY.md](SECURITY.md) for the threat model and how to report a vulnerability.

## Requirements

- Python 3.11 or later and pg8000 (`requirements/core.txt`).
- PostgreSQL 17 or later (`MERGE ... WHEN NOT MATCHED BY SOURCE`, `merge_action()`, `COPY ... ON_ERROR`).
- Optional, for `reason`: owlready2, pySHACL and rdflib (`requirements/reason.txt`) and Java 11 or later
  for HermiT.
- Optional, for `cube` and `verify`: Node.js 20 or later with npm, and a platform listed in
  `skills/semantic-crosscheck/adapters/cube/native-sha256.json` (currently Windows x64, Linux x64 and
  arm64 with glibc, macOS x64 and arm64).

## Limitations and "not proven"

A passing run means: these checks found no disagreement on this data. It does not mean the model is
correct in general, and the tool says where its evidence ends. Every result has a `not_proven` list
that does not change the status but is shown with every verdict:

- an empty stratum (no node of that kind exists, so V2 had nothing to compare);
- an equivalent mutant (the mutation changes no result on the current data, so V4 cannot tell);
- a shape without a SHACL form (`sql_violation`, or `class` combined with bounds);
- a component that is not installed (Java, Node.js, the Cube adapter), or a stage that was not run.

Further limits of this release:

- The gold SQL is the reference and is trusted. If the gold SQL and the configuration share a mistake,
  they agree; write gold queries independently of the generated objects.
- OWL is open-world: only definitions whose OWL and SQL meanings coincide are compared.
- One swept parameter per gold query (use `param_sets` for several); `timeDimensions` take a
  `dateRange`, not a `granularity`; `closure_join` needs exactly one hierarchy.
- Identifiers must be plain lower-case SQL names; quoted mixed-case names are not supported.
- Cube 1.8.3 is pinned; PostgreSQL before 17 is not supported.
- The Python stages connect through pg8000 with its default TLS behaviour (see [SECURITY.md](SECURITY.md)).
- V4 starts one Cube process per mutant and is the slowest stage; SHACL and OWL inputs are capped by
  `verify.shacl_max_rows` and `verify.owl_max_individuals`.

## Development

```bash
python -m unittest discover -s tests -v
```

Unit tests need no database. Integration tests run when `SXC_TEST_ADMIN_DSN` names a superuser
connection to a throw-away PostgreSQL 17 server (`SXC_TEST_ROLE_PASSWORD` sets the test roles' password
where the server requires one); OWL tests also need Java (`SXC_JAVA` or `java` on `PATH`) and Cube tests
an installed adapter (`SXC_CUBE_HOME` or the default location). The demo's expected numbers come from an
independent Python implementation and are the reference for the closure, the shapes and the dirty-data
findings.

`tools/leak_scan.py` checks for secrets, personal e-mail addresses and, optionally, terms from a private
deny-list kept outside the repository (`SXC_DENYLIST`). Enable it for commits and pushes once per clone
with `git config core.hooksPath .githooks`. CI runs the same scanner with its generic patterns only.

## License

MIT, see [LICENSE](LICENSE). Runtime dependencies are not vendored; their licences are listed in
[THIRD_PARTY.md](THIRD_PARTY.md). Changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## 한국어 요약

semantic-crosscheck는 PostgreSQL 17 위에 만든 온톨로지와 시맨틱 레이어를 교차 검증하는 도구입니다.
설정 파일 하나(`crosscheck.toml`)에서 계층 클로저(PostgreSQL 17), 데이터 규칙(SQL 뷰와 SHACL),
OWL 온톨로지(HermiT), Cube 데이터 모델을 생성하고, 이 계층들이 서로 그리고 손으로 쓴 gold SQL과
일치하는지 확인합니다. 두 계층이 다르면 둘 중 하나에 결함이 있다는 뜻이며, 실패한 검사는 고쳐야 할
설정 항목을 알려 줍니다. 생성된 SQL이나 YAML은 고치지 않고 설정 파일을 고칩니다.

- 단계: `doctor` → `demo`(선택) → `build` → `reason` → `cube` → `verify` → `report`, Cube 어댑터는
  `cube-install`로 한 번 설치합니다.
- 원본 데이터는 읽기만 합니다. 검사 역할은 원본 테이블에 SELECT만 가지며, Cube 세션도 읽기 전용으로
  강제됩니다. 결과와 보고서에는 건수와 ID만 담기고 행 값은 담기지 않습니다.
- 통과는 검사가 증명한 범위만 뜻합니다. 증명하지 못한 부분(빈 층, 동등 돌연변이, SHACL로 표현할 수
  없는 규칙, 설치되지 않은 구성 요소)은 `not_proven`으로 함께 보고됩니다.
- 요구 사항: Python 3.11 이상, PostgreSQL 17 이상, 선택적으로 Java 11 이상(HermiT)과 Node.js 20
  이상(Cube). 라이선스는 MIT입니다.

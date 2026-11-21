# Third-party software

semantic-crosscheck vendors no third-party code. Its dependencies are installed by the user:

- Python packages from PyPI through `requirements/core.txt`, `requirements/reason.txt`, and the optional
  `requirements/witness.txt` and `requirements/sql-structure.txt` (direct
  dependencies pinned to exact versions; their own dependencies are resolved by pip);
- the Cube adapter's npm packages, installed by `sxc.py cube-install` into a per-user directory with
  `npm ci --ignore-scripts` from the committed `package-lock.json`;
- the Cube native module, downloaded by `cube-install` from the Cube GitHub release and accepted only
  when its size and SHA-256 match `native-sha256.json`;
- optional Apache Jena SHACL, supplied as a separately installed distribution for `shacl-corpus`;
- the optional W3C SHACL test suite for `shacl-corpus --w3c-suite` (since 0.9.0), read at test time
  from a separately downloaded w3c/data-shapes checkout (below) and never copied into this repository;
- the runtimes (Java for HermiT, Node.js for Cube, the PostgreSQL server).

The independent arithmetic oracle and synthetic witnesses are original implementations using the
existing dependencies and Python standard library. Their design references include
[Provenance Semirings](https://repository.upenn.edu/handle/20.500.14332/8764),
[Polygon](https://arxiv.org/abs/2504.06542) and
[RDFGraphGen](https://arxiv.org/abs/2407.17941). No source from these projects is copied or bundled;
NetworkX, Polygon, RDFGraphGen and a second OWL reasoner are not new runtime dependencies.

The TLP partition-checking methodology is informed by
[SQLancer](https://github.com/sqlancer/sqlancer), whose project is MIT-licensed. No SQLancer source is
copied or bundled, and SQLancer is not a runtime dependency. The independent SHACL corpus uses
[sheval](https://arxiv.org/abs/2609.05944v2) as an architectural reference only. Its repository's code
licence was not verified; no sheval source is adopted, copied or bundled. The OWL explanation uniqueness
check reimplements the intersection-of-justifications criterion of
[Union and Intersection of all Justifications](https://arxiv.org/abs/2109.11216); no code from that work is used
or bundled.

Licence names below were read from the installed packages' metadata (Python) and from the licence fields
recorded in `skills/semantic-crosscheck/adapters/cube/package-lock.json` (npm), unless noted otherwise.

The offline scalar boundary-schema experiment is an original implementation
informed by [Data Generation for Testing and Grading SQL Queries (XData)](https://arxiv.org/abs/1411.6704).
No XData, SQLGlot, JSON Schema validator or ProvSQL code is copied or installed;
the experiment uses a small fully consuming stdlib grammar, not a general SQL
parser or query-aware constraint solver. Saved-result and live SHACL boundaries
refer to the [SHACL report contract](https://www.w3.org/TR/shacl/#validation-report)
and explicit local version/type/identity rules without a new validator dependency.

The offline business-definition/structured-plan preflight is an original
standard-library implementation conceptually informed by NVIDIA's
[October 21, 2026 Agent Toolkit article](https://blogs.nvidia.com/blog/enterprise-platforms-agent-toolkit/)
and [Auto Ontology at c4495670947eb7b4616f0dd9900e5ced2a48ca1e](https://github.com/NVIDIA/auto-ontology/tree/c4495670947eb7b4616f0dd9900e5ced2a48ca1e),
including its
[query-validation guidance](https://github.com/NVIDIA/auto-ontology/blob/c4495670947eb7b4616f0dd9900e5ced2a48ca1e/skills/nvidia-ontology-query/references/query-validation.md)
and [interactive completeness implementation](https://github.com/NVIDIA/auto-ontology/blob/c4495670947eb7b4616f0dd9900e5ced2a48ca1e/auto_ontology/retrieval/interactive/completeness.py).
These are existing Reference Atlas sources reused for a new semantic-crosscheck
application: explicit metric/Gold/Cube bindings and structured-plan consistency.
Their prior agentic-vault use pinned Auto Ontology at
`2a24f5d34ef6b6e168a7f22d308504d3ab36a198`; the newer packaging/version pin does
not establish a new semantic capability. No NVIDIA code is copied or bundled,
no NVIDIA runtime, GPU or model dependency is added, and no upstream performance
result is reproduced. Authored review and context declarations are not
certification or SQL correctness proofs. See [scope](docs/semantic-preflight.md).

## v0.21.0 references and optional parser

The opt-in contract-to-report evidence boundary is an original implementation
informed by [Data Contract CLI](https://github.com/datacontract/datacontract-cli)
at `09e779dfeaec63ee60edc102e7a3d6970e34e16b` (MIT) and
[Open Data Contract Standard](https://github.com/bitol-io/open-data-contract-standard)
at `448f7b93329ece7827897d7dd51e827a3e279460` (Apache-2.0).
These previously observed sources are reused now that authored contracts exist;
no external contract engine/code is installed or bundled and no full ODCS
import/export compatibility is claimed.

The separately selected SQL structure diagnostic uses
[libpg_query](https://github.com/pganalyze/libpg_query) through the optional
[pglast](https://github.com/lelit/pglast) Python binding, pinned to 7.22
(PostgreSQL17 parser) in `requirements/sql-structure.txt`. pglast is
GPL-3.0-or-later; libpg_query's own code is BSD-3-Clause with PostgreSQL-licensed
server sources and separately licensed bundled components. These are not all
MIT/BSD code. No parser source is vendored; the user separately installs the
optional package. The core/reason requirements and operational stages do not
acquire this dependency. See [parser scope](docs/sql-structure.md).

Original independent synthetic bag/provenance fixtures are informed by
[ProvSQL: A General System for Keeping Track of the Provenance and Probability of Data](https://arxiv.org/abs/2504.12058v3)
and [provenance-lean](https://github.com/PierreSenellart/provenance-lean)
at `92af5cf2a057dfb46aab0a364d4ade5fde9c5699` (MIT).
They exercise literal small expectations and the documented Dedup/Diff
counterexample's assumptions; no upstream proof, benchmark or universal SQL
correctness result is claimed as reproduced. No Lean/Mathlib or ProvSQL extension
is copied, installed or bundled. Existing ProvSQL background references remain
background. See [fixture scope](docs/provenance-fixtures.md).

## Python packages

| Package | Version | Licence | Used for |
|---|---|---|---|
| pg8000 | 1.31.5 | BSD-3-Clause | PostgreSQL connections (core) |
| owlready2 | 0.51 | LGPL-3.0-or-later | OWL ontology and the HermiT reasoner (`reason`) |
| pySHACL | 0.40.1 | Apache-2.0 | SHACL validation (`reason`) |
| rdflib | 7.6.0 | BSD-3-Clause | RDF graphs for SHACL (`reason`) |
| z3-solver | 4.15.3.0 | MIT | Optional finite-domain integrity-valid synthetic inputs (`--witness-schema`) |
| pglast | 7.22 | GPL-3.0-or-later | Optional offline PostgreSQL 17 SQL AST check (`sql-structure`, imported only by that stage) |

Structured reduction is an original implementation informed by
[Vulcan (2023)](https://doi.org/10.1145/3586049) and [Perses](https://github.com/uw-pluverse/perses).
No Perses GPL-3.0 source is copied or bundled. Typed fixture generation borrows the bounded
constraint-model principle of [Qex](https://www.microsoft.com/en-us/research/publication/qex-symbolic-sql-query-explorer/)
and uses the separately installed [MIT Z3 implementation](https://github.com/Z3Prover/z3).
Qex is a research reference, not a bundled tool. Z3 does not supply PostgreSQL query semantics.

Their dependencies, as resolved by pip in the environment the licences were read from:

| Package | Pulled in by | Licence |
|---|---|---|
| scramp | pg8000 | MIT-0 |
| asn1crypto | scramp | MIT |
| python-dateutil | pg8000 | Dual License: Apache-2.0 and BSD-3-Clause (for different contributions) |
| six | python-dateutil | MIT |
| owlrl | pySHACL | W3C-20150513 |
| packaging | pySHACL | Apache-2.0 OR BSD-2-Clause |
| prettytable | pySHACL | BSD-3-Clause |
| wcwidth | prettytable | MIT |
| html5rdf | rdflib (html extra, required by pySHACL) | MIT |
| pyparsing | rdflib | MIT |

On Python 3.11 pySHACL also requires importlib-metadata (and its dependency zipp); their licences were not
read from local metadata.

### Components bundled inside owlready2

| Component | Licence | Use by semantic-crosscheck |
|---|---|---|
| HermiT OWL 2 reasoner (Java) and the libraries it ships with | LGPL-3.0-or-later (HermiT) | Run by the `reason` stage; needs a Java runtime |
| Pellet 2.3.1 reasoner and the Java libraries shipped for it | AGPL-3.0 (dual-licensed by its authors) | Never invoked |

The owlready2 wheel contains the Pellet files even though semantic-crosscheck never runs them. If you
redistribute an environment with these packages installed (a container image, for example), the
licences of everything in it apply to that distribution.

## Cube adapter (npm)

Direct dependencies pinned in `skills/semantic-crosscheck/adapters/cube/package.json`:

| Package | Version | Licence |
|---|---|---|
| @cubejs-backend/server-core | 1.8.3 | Apache-2.0 |
| @cubejs-backend/postgres-driver | 1.8.3 | Apache-2.0 |
| express | 4.22.3 | MIT |

The lockfile pins the complete transitive tree with each package's licence field. For this release the
licence fields are mostly MIT, Apache-2.0, ISC and BSD-2-Clause or BSD-3-Clause, plus BlueOak-1.0.0,
0BSD, Unlicense, Python-2.0, MIT OR CC0-1.0 and CC-BY-4.0 (the caniuse-lite data package). Three packages
(codesandbox-import-utils, codesandbox-import-util-types and rechoir) record no licence field. The Cube
packages (`@cubejs-backend/*`) are Apache-2.0.

`npm ci --ignore-scripts` skips every install script. Four packages in the lockfile declare one:

| Package | Version | Note |
|---|---|---|
| `@cubejs-backend/native` | 1.8.3 | its script would download the native module without an integrity check; `cube-install` fetches and verifies it instead (below) |
| `@cubejs-backend/cubestore` | 1.8.3 | Cube Store is not used: the adapter runs with the in-memory cache and queue driver |
| `es5-ext` | 0.10.64 | a postinstall script (`_postinstall.js`); nothing in the adapter depends on it |
| `fsevents` | 2.3.3 | an optional dependency installed on macOS only |

Several packages in the locked tree declare `engines.node >= 22`, which is why the adapter's
`package.json` and `cube-install` require Node.js 22 or later.

The v0.27.0 lockfile was refreshed within the declared ranges (`npm update` of the advised packages with
`--package-lock-only --ignore-scripts`). `npm audit --package-lock-only` then reports five unique advisories
instead of eleven: three for basic-ftp and one each for elliptic and graphql-scalars. Updating every package
within its declared range leaves the same five, so clearing them needs a change this release does not make (a
newer Cube pin or package overrides). The adapter listens on 127.0.0.1 only. The two packages the refresh
added, is-in-ssh and powershell-utils, are MIT; the Cube packages stay at 1.8.3.

### Cube native module

| Component | Version | Licence | Source |
|---|---|---|---|
| @cubejs-backend/native (`native/index.node`) | 1.8.3 | Apache-2.0 (package licence field) | `native-<platform>-fallback.tar.gz` assets of the Cube v1.8.3 GitHub release, downloaded at install time and pinned by size and SHA-256 in `native-sha256.json` |

On Linux the pinned modules need glibc 2.34 or later (x64) and 2.30 or later (arm64), the highest
`GLIBC_` symbol versions they reference.

## Runtimes

| Runtime | Needed for | Licence |
|---|---|---|
| PostgreSQL 17 or later | every database stage | PostgreSQL License |
| A Java runtime, 11 or later (CI uses Eclipse Temurin 21) | HermiT in `reason` | GPL-2.0 with the Classpath Exception (OpenJDK builds) |
| Node.js 22 or later | `cube-install`, `cube`, `verify` | MIT, with bundled third-party components |
| Apache Jena SHACL 6.2.0 (optional external distribution) | `shacl-corpus`; selected distribution uses a Java 21-compatible runtime | Apache-2.0; the binary distribution also carries its own dependency notices |

These licence names for the runtimes come from their publishers, not from local metadata.
Apache Jena's licence was also checked in the verified distribution's `LICENSE`; Jena and its Java
runtime are not bundled with this repository. CI verifies the pinned archive's SHA-512 before extraction.

## Test data

| Data | Version | Licence | Used for |
|---|---|---|---|
| [W3C SHACL test suite](https://github.com/w3c/data-shapes) (`data-shapes-test-suite/tests/core`) | commit `1dfcd9f30cab70ac02b049611eeba451fa1308e7` | W3C Software and Document License | Expected reports for `shacl-corpus --w3c-suite` (test time only; since 0.9.0) |

The suite is not redistributed: no file of it is in this repository or its release archives. The unit tests use
original synthetic manifests in the suite's Turtle format, and CI downloads the pinned commit's source tarball,
verifies its SHA-256 and extracts only `data-shapes-test-suite/` and `LICENSE.md`. The licence name comes from the
suite's `LICENSE.md`. Under the licence's 2023 text, copying suite files into a distribution would require the full
licence notice, the existing notices and a note of changes, and such copies would remain under that licence.


## v0.24.0 mutation observation and authored join/aggregate fixtures

The optional all-item mutation observations are an original implementation informed by
[StrykerJS v10.1.1's disableBail option](https://github.com/stryker-mutator/stryker-js/blob/959127763e5bbe2eb65f5341a8161780d6d8a476/packages/api/schema/stryker-core.json#L499-L503)
and [Mutation Testing Elements' separate coveredBy, killedBy and testsCompleted fields](https://github.com/stryker-mutator/mutation-testing-elements/blob/4b5b62cc6bf81f23dd9521447131abeb51f895db/packages/report-schema/src/mutation-testing-report-schema.json#L43-L100).
Both referenced projects are Apache-2.0 licensed; no source, schema or JavaScript engine is copied, bundled or
installed. Execution coverage is not detection, and a bounded partial matrix is not full semantic coverage.

The authored duplicate-join and aggregate corpus is conceptually informed by
[SPES: A Symbolic Approach to Proving Query Equivalence Under Bag Semantics (ICDE 2022), arXiv v2](https://arxiv.org/abs/2004.00481v2)
by Qi Zhou, Joy Arulraj, Shamkant Navathe, William Harris and Jinpeng Wu. The
[public SPES repository](https://github.com/georgia-tech-db/spes/tree/8049f98d8e64a5c2c43d1ed581e7d5b6e0a006e2)
is Apache-2.0 licensed. The fixtures, SQL and bounded Python reference arithmetic are independently authored;
no SPES code, proof engine, Java/Calcite/Z3 stack or paper examples are incorporated. The fixtures do not
establish general PostgreSQL/Cube query equivalence or certify business definitions.

## v0.28.0 exact arithmetic and bounded diagnostics

The exact-sum helper and numerical regressions are original standard-library Python implementations.
The generated/permutation boundary cases extend the property-testing ideas already referenced from
[Hypothesis](https://github.com/HypothesisWorks/hypothesis); Hypothesis is not installed or bundled.

The optional plan diagnostic follows the differential-plan idea in
[Keep It Simple: Testing Databases via Differential Query Plans (2024)](https://doi.org/10.1145/3654991)
and the already referenced [SQLancer project, pinned source](https://github.com/sqlancer/sqlancer/tree/a513c87781ed879e5aae6bc78f8b6c6056bd7a99).
It is independently implemented for a bounded declarative PostgreSQL synthetic subset. No paper examples or
SQLancer/Cloudberry oracle code are copied, installed or claimed as PostgreSQL drop-in support. Identical GUC
settings are not plan evidence; observed physical plan diversity and complete typed bags are required.

The measure oracle's raw-row evaluation is independent of the generators, but not a proof that its authored
specification is the correct business rule. These diagnostics do not extend default report authority.

## v0.30.0 paired evaluation and numerical release claims

Chulmin Pyo's [connectome-cognition-benchmark v1.1.0 software record](https://doi.org/10.5281/zenodo.24361690) inspired the evaluation design.
The preregistration documents [prior exposure](https://github.com/ai3inc/connectome-cognition-benchmark/blob/9c5d863256fdc20c341b94b874e62921e92f22c1/preregistration/PREREG_EXTERNAL_COHORT.md#L8-L22) and the [registered test and subsequent outcome](https://github.com/ai3inc/connectome-cognition-benchmark/blob/9c5d863256fdc20c341b94b874e62921e92f22c1/preregistration/PREREG_EXTERNAL_COHORT.md#L228-L267).
The [numbers index builder](https://github.com/ai3inc/connectome-cognition-benchmark/blob/9c5d863256fdc20c341b94b874e62921e92f22c1/paper/build_numbers.py#L1-L25) records input provenance. The [manuscript claim checker](https://github.com/ai3inc/connectome-cognition-benchmark/blob/9c5d863256fdc20c341b94b874e62921e92f22c1/paper/claims.py#L139-L146) checks claims against values in that index.
We adapted these methods to synthetic inventory totals and added a check of the release note's marked table. The fixtures, checks and release tooling are independently implemented. The upstream project's neuroscience data, metrics and software are outside this distribution.

## v0.31.0 paired-release statistics, CI static checks and repository governance

The offline small-sample statistics in `tools/paired_release.py` are original Python standard-library
implementations (`math.comb`, `statistics.NormalDist`) with zero new runtime dependencies. They follow:

- [Fagerland, Lydersen & Laake (2013), "The McNemar test for binary matched-pairs data: mid-p and asymptotic are better than exact conditional"](https://doi.org/10.1186/1471-2288-13-91) for exact conditional and Mid-P McNemar two-sided p-values;
- [Newcombe (1998), "Improved confidence intervals for the difference between binomial proportions based on paired data"](https://doi.org/10.1002/%28SICI%291097-0258%2819981130%2917:22%3C2635::AID-SIM954%3E3.0.CO;2-C) (Method 10) for paired Wilson-score 95% confidence intervals;
- [Dietterich (1998), "Approximate Statistical Tests for Comparing Supervised Classification Learning Algorithms"](https://doi.org/10.1162/089976698300017197), [Arcuri & Briand (2014), "A Hitchhiker's guide to statistical tests for assessing randomized algorithms in software engineering"](https://doi.org/10.1002/stvr.1486), and [Klees, Ruef, Cooper, Wei & Hicks (CCS 2018), "Evaluating Fuzz Testing"](https://doi.org/10.1145/3243734.3243804) for paired evaluation design and experimental hygiene in `docs/paired-release.md`.

CI-only static quality checks in `.github/workflows/tests.yml` run pinned third-party linters that are
never added to `requirements/*.txt`, never installed by users, and never grant semantic verdict authority:

- [ruff](https://github.com/astral-sh/ruff) `0.17.6` (MIT) for Python syntax and correctness checks;
- [ty](https://github.com/astral-sh/ty) `0.0.107` (MIT) for curated core and tool static type checks;
- [sqruff](https://github.com/quarylabs/sqruff) `0.44.0` (Apache-2.0) for PostgreSQL SQL parsing and semantic anti-pattern checks on `demo/gold/*.sql` (configured in `.sqruff`, advisory style/lint only, not SQL equivalence or verdict authority).

Repository release-tag immutability (`refs/tags/v*`) and branch governance follow [GitHub Docs: About rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets). No third-party source code from any of these tools is copied or bundled.

## v0.31.1 reproducible release archives, Unicode hygiene, PG 17 built-in collation and CI supply-chain checks

The deterministic release archive builder (`tools/build_release_zip.py`) and invisible Unicode smuggling guard (`tools/check_unicode.py`) are original Python standard-library implementations with zero new runtime dependencies. They follow:

- [Reproducible Builds `SOURCE_DATE_EPOCH` specification](https://reproducible-builds.org/specs/source-date-epoch/) for timezone-independent UTC release ZIP timestamps;
- [Boucher & Anderson (USENIX Security 2023), "Trojan Source: Invisible Vulnerabilities"](https://www.usenix.org/conference/usenixsecurity23/presentation/boucher) for bidirectional control character defenses;
- [Unicode Technical Standard #39: Unicode Security Mechanisms](https://www.unicode.org/reports/tr39/) for `Default_Ignorable` and invisible smuggling code-point classification;
- [PostgreSQL 17 Documentation — 23.2. Collation Support](https://www.postgresql.org/docs/17/collation.html) for deterministic `provider = builtin, locale = 'C.UTF-8'` collation guidance in `docs/data-rules.md` and `tests/test_collation_builtin_pg.py`;
- [G. Ann Campbell (IEEE/ACM TechDebt 2018), "Cognitive complexity: an overview and evaluation"](https://doi.org/10.1145/3194164.3194186) for cognitive complexity tracking;
- [ECMA-424: OWASP CycloneDX Software Bill of Materials Standard](https://ecma-international.org/publications-and-standards/standards/ecma-424/) for machine-readable CycloneDX SBOM format checks;
- [npm CLI `npm sbom` documentation](https://docs.npmjs.com/cli/v11/commands/npm-sbom) for built-in Cube adapter CycloneDX SBOM extraction.

CI-only workflow, supply-chain, SBOM and code hygiene checks in `.github/workflows/tests.yml` run pinned third-party tools that are never added to `requirements/*.txt`, never installed by users, and never grant semantic verdict authority:

- [rhysd/actionlint](https://github.com/rhysd/actionlint) `v1.7.12` (MIT, official `linux_amd64` tarball verified by SHA-256) for GitHub Actions workflow syntax and expression linting;
- [zizmorcore/zizmor](https://github.com/zizmorcore/zizmor) `1.32.0` (MIT) for offline GitHub Actions security static analysis;
- [pypa/pip-audit](https://github.com/pypa/pip-audit) `2.10.1` (Apache-2.0) for auditing pinned `requirements/*.txt` against PyPI vulnerability advisories;
- [CycloneDX/cyclonedx-python](https://github.com/CycloneDX/cyclonedx-python) (`cyclonedx-bom==9.0.0`, Apache-2.0) for reproducible CycloneDX JSON SBOM checks on `requirements/*.txt`;
- [rohaquinlop/complexipy](https://github.com/rohaquinlop/complexipy) `9.0.0` (MIT) for cognitive complexity ceilings on `tools/` and top-10 hotspot visibility on package scripts;
- [jendrikseipp/vulture](https://github.com/jendrikseipp/vulture) `2.16` (MIT) for high-confidence (`>= 80%`) dead-code hygiene checks;
- [osprey-oss/deptry](https://github.com/osprey-oss/deptry) `0.25.1` (MIT) for checking Python package imports against `requirements/*.txt`.

No third-party source code from any of these tools is copied or bundled.

# Third-party software

semantic-crosscheck vendors no third-party code. Its dependencies are installed by the user:

- Python packages from PyPI through `requirements/core.txt`, `requirements/reason.txt` and the optional
  `requirements/witness.txt` (direct
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

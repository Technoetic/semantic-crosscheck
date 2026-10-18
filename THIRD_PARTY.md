# Third-party software

semantic-crosscheck vendors no third-party code. Its dependencies are installed by the user:

- Python packages from PyPI through `requirements/core.txt` and `requirements/reason.txt` (direct
  dependencies pinned to exact versions; their own dependencies are resolved by pip);
- the Cube adapter's npm packages, installed by `sxc.py cube-install` into a per-user directory with
  `npm ci --ignore-scripts` from the committed `package-lock.json`;
- the Cube native module, downloaded by `cube-install` from the Cube GitHub release and accepted only
  when its size and SHA-256 match `native-sha256.json`;
- the runtimes (Java for HermiT, Node.js for Cube, the PostgreSQL server).

The independent arithmetic oracle and synthetic witnesses are original implementations using the
existing dependencies and Python standard library. Their design references include
[Provenance Semirings](https://repository.upenn.edu/handle/20.500.14332/8764),
[Polygon](https://arxiv.org/abs/2504.06542) and
[RDFGraphGen](https://arxiv.org/abs/2407.17941). No source from these projects is copied or bundled;
NetworkX, Polygon, RDFGraphGen and a second OWL reasoner are not new runtime dependencies.

Licence names below were read from the installed packages' metadata (Python) and from the licence fields
recorded in `skills/semantic-crosscheck/adapters/cube/package-lock.json` (npm), unless noted otherwise.

## Python packages

| Package | Version | Licence | Used for |
|---|---|---|---|
| pg8000 | 1.31.5 | BSD-3-Clause | PostgreSQL connections (core) |
| owlready2 | 0.51 | LGPL-3.0-or-later | OWL ontology and the HermiT reasoner (`reason`) |
| pySHACL | 0.40.1 | Apache-2.0 | SHACL validation (`reason`) |
| rdflib | 7.6.0 | BSD-3-Clause | RDF graphs for SHACL (`reason`) |

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

These licence names for the runtimes come from their publishers, not from local metadata.

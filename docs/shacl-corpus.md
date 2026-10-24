# Independent SHACL corpus

`shacl-corpus` runs a fixed synthetic, nonrecursive corpus through actual pySHACL
and Apache Jena SHACL. Each engine must match independently written expected
constraint reports. Agreement between the engines alone is insufficient.

```console
python skills/semantic-crosscheck/scripts/sxc.py shacl-corpus --jena-home /opt/apache-jena --timeout-ms 30000
```

Install the Python packages in `requirements/reason.txt`. Set `SXC_JENA_HOME` or
pass `--jena-home` for an extracted Apache Jena distribution. Java is selected
with `SXC_JAVA` or an absolute PATH entry. Choose a Java version supported by the
selected Jena release; Apache Jena 6.2.0 requires Java 21 or later. This optional
command needs no operational config, database, Cube or HermiT.

The stage returns `Result('shacl_corpus')`, with one `pyshacl:<case>` and one
`jena:<case>` check for every case. The CLI writes `shacl_corpus/result.json`.
All sixteen checks must pass. Missing Python packages, Java or the Jena
distribution produce environment exit 3; invalid config/options produce exit 2.
A runtime error, timeout, malformed report, missing report or mismatch cannot
pass. The stage's `not_proven` explicitly limits the conclusion to this corpus;
it does not change operational V1–V5 or global report coverage.

The optional `--w3c-suite` mode (after 0.8.0, unreleased) replaces this fixed
corpus with expected reports written by the W3C Data Shapes working group; see
[W3C core subset](#w3c-core-subset). Without the option the fixed corpus runs
exactly as described here, even when `SXC_W3C_SHACL_SUITE` is set.

## Corpus and independent expectations

The corpus uses only synthetic `part`, `bom_line` and `stock` identities.
`bom_line` has the synthetic single-column key `line_no`. Each expected report
tuple is written independently in `shacl_corpus.corpus()`. Expected IRIs and
numeric values are literals; naming helpers, validators and exporters never
calculate expected violations.

| Case | Independently specified behavior | Expected violations |
| --- | --- | ---: |
| `class` | Missing part 9 violates `sh:class`; existing part 1 and NULL conform. | 1 |
| `null_no_triple` | NULL class reference and NULL quantity emit no value triple. Neither constraint is violated. | 0 |
| `min_exclusive` | Quantities 0 and -0.001 violate a strict lower bound of 0; 0.001 and NULL conform. | 2 |
| `decimal_inclusive` | Bounds [1.1, 1.2] include both endpoints; values smaller/larger by an exact decimal amount violate the corresponding bound with Warning severity. | 2 |
| `combined` | Class and maximum 5 constrain separate predicates carrying the same column as an IRI and a literal. Missing/out-of-range 9 produces two constraints on one focus; missing 4 and existing/out-of-range 7 produce one each. Existing 1 and NULL conform. | 4 |
| `collision_main` | Class-part graph: 9 is missing; 1 exists. | 1 |
| `collision_extra_1` | Literal graph on the same column: 9 exceeds maximum 5; 1 conforms. | 1 |
| `collision_extra_2` | Class-stock graph on the same column: 9 exists; 1 is missing. | 1 |

Every fixture uses the current production `shapes.shapes_ttl` and
`shacl_check.build_graph` implementations. Collision fixtures also use the real
`shacl_check.groups`; their expected groups and violations are independently
specified. A changed exporter or shape translation must still satisfy these
fixed expectations.

## Report comparison

Reports are parsed as Turtle and must contain exactly one typed
`sh:ValidationReport`, exactly one typed boolean `sh:conforms`, and consistent
typed `sh:ValidationResult` members. A conforming report with no results is valid;
empty output or an untyped/malformed report is not. Each result must supply
focus, simple IRI path, constraint component, severity and value.

The canonical multiset contains `(focus, path, component, severity, value)`.
Comparison preserves duplicate semantic results and detects a changed or missing
constraint even when both reports contain the same focus nodes. Exact finite
integer/decimal values normalize without floating point or context rounding.
Result blank-node labels, source-shape labels and diagnostic messages are
ignored. The fixed corpus uses IRI focus/value nodes and literal values; blank
data nodes and complex/recursive paths are outside this profile and are refused.

Both validators run in private child processes. A single per-case, per-engine
deadline covers execution and both output pipes; combined stdout/stderr is
capped at 256 KiB. The process runner uses literal argument arrays, a restricted
environment, Windows job objects or a private POSIX process group, and kills
owned descendants on completion/error/timeout. Raw engine output, fixture
values, paths and exceptions are excluded from Result and ordinary CLI output.
Synthetic Turtle files live in temporary directories and are removed afterward.

Jena is invoked directly as Java with `-cp <jena-home>/lib/* shacl.shacl validate
--shapes <file-uri> --data <file-uri>`. No batch file, shell expansion, downloaded
wrapper or production data is executed. File URIs avoid Jena 6.2's rejection of
Windows backslash paths and safely represent spaces.

<a id="w3c-core-subset"></a>
## W3C core subset (`--w3c-suite`, after 0.8.0, unreleased)

The fixed corpus's expected tuples and sxc's exporter are written by the same
project, so they can share a mistake. This mode takes the expected reports from
the [W3C SHACL test suite](https://w3c.github.io/data-shapes/data-shapes-test-suite/)
instead and runs the core cases that stay inside what sxc emits.

```console
python skills/semantic-crosscheck/scripts/sxc.py shacl-corpus --w3c-suite /path/to/w3c-data-shapes --jena-home /opt/apache-jena
```

`DIR` is a checkout or extracted archive of
[w3c/data-shapes](https://github.com/w3c/data-shapes) that contains
`data-shapes-test-suite/tests/core/manifest.ttl` (optionally with a
`PINNED_COMMIT` file naming its commit). `--w3c-suite` without `DIR` reads the
directory from `SXC_W3C_SHACL_SUITE`; the variable alone never switches the
mode. Engines, Jena location, timeouts, output caps and process cleanup are the
fixed corpus's. A missing directory or core manifest is exit 3. A manifest that
is missing, is not valid Turtle, is not an `mf:Manifest`, has a malformed entry
list or includes a file outside the core Turtle suite is exit 2: its entries
could not be enumerated.

### Manifests and checks

The stage reads `tests/core/manifest.ttl` and follows `mf:include` recursively
(each file once). Every `mf:entries` member becomes one case with two checks,
`w3c:pyshacl:<case>` and `w3c:jena:<case>`, where `<case>` is the suite-relative
test name such as `core/node/class-001`. Only `sht:Validate` entries with
`mf:status sht:approved` count; the pinned suite marks all 98 core entries so.
Other entry types, `sht:proposed`, `sht:rejected` or a missing status are
`not_run`. Files are parsed against a synthetic base IRI, so no local path
reaches a canonical term, and results carry counts, reason ids and SHACL term
names only. Each in-profile case runs on private temporary copies of the exact
bytes that were digested, keeping the suite's relative layout (a case whose data
and shapes graph are one document still uses one file).

### Profile

The profile is what `shapes.shapes_ttl()` emits and the fixed corpus uses:

| Feature | Inside the profile |
| --- | --- |
| Constraints | `sh:class` (IRI), `sh:minExclusive`, `sh:minInclusive`, `sh:maxInclusive` (`xsd:decimal`, `xsd:integer`, `xsd:int` or `xsd:long` value) |
| Severity | `sh:severity` `sh:Violation` or `sh:Warning` |
| Shapes | node shapes with `sh:property`; property shapes with exactly one IRI `sh:path` and no nested `sh:property` |
| Targets | `sh:targetClass`, `sh:targetNode` with IRI values, implicit class targets (a shape that is an `rdfs:Class`) |
| Expected report | IRI focus nodes; IRI or literal values; terms not relative to the test document |

sxc emits constraints on property shapes reached from a `sh:targetClass` node
shape. The suite reaches the same components on IRI focus nodes through
`sh:targetNode`, implicit class targets and node-level constraints, so those are
inside the profile; node-level results have no `sh:resultPath`. `sh:datatype`,
`sh:maxExclusive`, `sh:minCount` and `sh:maxCount` are outside it because sxc
never emits them. SHACL vocabulary is checked across the whole shapes graph: an
unused or ill-formed path node is outside the profile too. A case outside the
profile is `not_run` with one reason, chosen in this order, and the SHACL term
names that caused it in `features`:

| Reason | Meaning |
| --- | --- |
| `suite_not_pinned` | the content digest differs from the pinned suite (below); nothing runs |
| `unsupported_entry_type`, `status_not_approved` | not an approved `sht:Validate` entry |
| `missing_graph`, `outside_suite`, `non_turtle` | the data or shapes graph is absent, outside `tests/core/`, remote or not a `.ttl` file |
| `malformed_graph` | the shapes graph is not valid Turtle |
| `sparql` | SHACL-SPARQL or SHACL-AF vocabulary (`sh:sparql`, `sh:declare`, `sh:rule`, ...) |
| `complex_path` | a non-IRI `sh:path` or any path expression (`sh:oneOrMorePath`, `sh:inversePath`, ...) |
| `closed_shape` | `sh:closed` or `sh:ignoredProperties` |
| `unsupported_target` | `sh:targetSubjectsOf`, `sh:targetObjectsOf`, an `owl:Class` shape without `rdfs:Class` |
| `non_iri_focus` | a literal or blank `sh:targetNode`, or a literal focus node in the expected report |
| `unsupported_component` | any other SHACL term on a shape or in the graph, or `owl:imports` (feature `owl_imports`), which an engine could follow |
| `unsupported_value` | an allowed key with a value outside the profile (non-numeric bound, `sh:Info` or custom severity, literal `sh:class`) |
| `blank_node_term`, `relative_iri` | the expected report names a blank node or a document-relative IRI, which cannot be identified across graphs |
| `malformed_expected` | the expected report is not exactly one consistent `sh:ValidationReport` |

### Expected reports and comparison

The expected report is the entry's `mf:result`. It uses the same canonical
multiset as the fixed corpus, `(focus, path, component, severity, value)`, with
two differences required by the suite: `sh:resultPath` is absent for node-shape
results and `sh:value` may be absent, and both are compared as present or absent.
`sh:sourceShape` is optional in the suite and is not compared, as are result
node typing and messages. Duplicate results are kept. An engine report must
satisfy the fixed corpus's strict parser rules otherwise; a blank node in it is
an opaque term that no expected term equals, so it fails instead of being
dropped. Numbers compare by exact value, as in the fixed corpus.

### Pin, provenance and licence

The suite is pinned at w3c/data-shapes
`1dfcd9f30cab70ac02b049611eeba451fa1308e7` (committed 2026-10-09). The pin is a
SHA-256 over the sorted suite-relative paths and bytes of every core manifest
and graph file the stage reads, each framed by its length: 121 files,
`27d212e662936162ed5707f57ab927e1a6a33e7e487844c71d2e0b704e53725b`. The digest,
not the commit label, decides whether the suite is pinned. Bytes are not
normalized, so a checkout that rewrites line endings is not the pinned content.
`result.inputs` records `w3c_suite_commit` (from `PINNED_COMMIT` when it holds
a 40-hex commit, else empty), `w3c_suite_sha256`, `w3c_suite_files` and
`w3c_suite_pinned`. A different digest makes every case `not_run`
(`suite_not_pinned`) and no engine starts; `--allow-unpinned-suite` runs it
anyway, adding a `not_proven` line that the results are not comparable with the
recorded baseline.

The suite's `LICENSE.md` licenses it under the
[W3C Software and Document License](https://www.w3.org/Consortium/Legal/copyright-software).
semantic-crosscheck reads it at test time and does not redistribute it. No suite
file is copied into this repository: unit tests use original synthetic manifests
in the suite's Turtle format, and CI downloads the pinned tarball. Under the
licence's 2023 text, copying suite files would require the full notice, the
existing notices and a note of changes, and the copies would stay under that
licence rather than MIT.

CI fetches `https://codeload.github.com/w3c/data-shapes/legacy.tar.gz/<commit>`,
checks its SHA-256
(`6213a0a58b9fd812b9659d9c0ceba67448f449fc4ebe22a459f1fe3e778c3646`), extracts
only `data-shapes-test-suite/` and `LICENSE.md` with tarfile's `data` filter,
writes `PINNED_COMMIT`, exports `SXC_W3C_SHACL_SUITE` and checks the content
digest before the tests. GitHub has regenerated archive bytes before; such a
change fails the checksum step closed rather than running an unverified suite.

### Observed results

On 2026-10-24, with pySHACL 0.40.1, RDFLib 7.6.0, Apache Jena 6.2.0 and Temurin
Java 21 on the pinned suite, 6 of the 98 core entries are inside the profile and
both engines match all of their expected reports (12 checks pass, 184 not_run):

| Case | Expected results | pySHACL | Jena |
| --- | ---: | --- | --- |
| `core/node/class-001` | 2 | pass | pass |
| `core/node/class-003` | 5 | pass | pass |
| `core/property/class-001` | 2 | pass | pass |
| `core/property/maxInclusive-001` | 2 | pass | pass |
| `core/property/minExclusive-001` | 2 | pass | pass |
| `core/property/minExclusive-002` | 2 | pass | pass |

The other 92 entries are `not_run`: `unsupported_component` 53,
`non_iri_focus` 18, `complex_path` 14 (including `core/path/path-oneOrMore-001`
and the unused ill-formed paths of `core/path/path-unused-001`),
`unsupported_target` 4, `closed_shape` 2 and `sparql` 1. No pinned core case
puts `sh:minInclusive` inside the profile (its node-level cases target literal
focus nodes), so that bound remains covered by the fixed corpus only. The stage
status on the pinned suite is therefore `not_run` (exit 1), never `pass`; read
the per-case statuses. An engine that fails an in-profile case is a `fail`
check. The gated integration test pins this exact outcome, the expected result
counts and the reason counts, so a change in either direction fails it; it runs
when `SXC_W3C_SHACL_SUITE`, `SXC_JENA_HOME` and `SXC_JAVA` are set:

```console
python -m unittest discover -s tests -p "test_shacl_w3c.py" -v
```

## Runtime verification

Verified on 2026-10-23 with pySHACL 0.40.1, RDFLib 7.6.0, Apache Jena 6.2.0 and
Eclipse Temurin Java 21.0.12.1+1. The official Jena [release page](https://jena.apache.org/download/)
and [SHACL CLI documentation](https://jena.apache.org/documentation/shacl/)
describe this distribution and command. The shipped `bin/shacl` and
`bat/shacl.bat` both use the public main class `shacl.shacl`; actual
`shacl.shacl version` printed `Apache Jena SHACL version 6.2.0`.

The [Apache Jena binary archive](https://dlcdn.apache.org/jena/binaries/apache-jena-6.2.0.zip)
matched its [official SHA-512 checksum](https://downloads.apache.org/jena/binaries/apache-jena-6.2.0.zip.sha512):

```text
fb97634215b4667124bdbe4f42efc25d4dc4e9046b70bfa702d7a71e1a8aed049ec79a38eb469cf0e13b18724cc024adec3cc35113a24e8fa62aa66091d7431e
```

The optional Windows x64 Temurin JRE archive selected by the official
[Adoptium Java 21 assets API](https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture=x64&image_type=jre&os=windows&vendor=eclipse)
matched the API's SHA-256 checksum:

```text
d35f31e712f0fcf6ac5a093edc90204fbff22f720ba3950bd09d331d5e621636
```

Reproduce the actual engine test with `SXC_JENA_HOME` and `SXC_JAVA` set:

```console
python -m unittest discover -s tests -p "test_shacl_corpus*.py" -v
```

Without both explicit runtime variables, the actual Jena integration test skips;
parser, pySHACL, subprocess bounds, descendant cleanup and failure tests still
run. Unit tests substitute only the external Java command boundary when testing
bad output, retaining actual subprocess execution and strict parsing.

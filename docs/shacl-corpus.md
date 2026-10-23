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

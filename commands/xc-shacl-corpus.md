---
description: "Compare pySHACL and Apache Jena reports with independently specified nonrecursive synthetic class and bounds cases, or (since 0.9.0) with W3C SHACL core expected reports"
argument-hint: "[--jena-home DIR] [--timeout-ms 30000] [--w3c-suite [DIR]] [--allow-unpinned-suite] [--out DIR] [--quiet]"
---

Run the optional standalone `shacl-corpus` stage. It uses only a fixed synthetic RDF corpus, or with `--w3c-suite` (since 0.9.0) the W3C SHACL test suite, and never connects to a production database.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" shacl-corpus --jena-home JENA_DIRECTORY
```

- `--jena-home DIR` selects a separately installed Apache Jena distribution. Without it, `SXC_JENA_HOME` supplies the directory.
- `--timeout-ms` defaults to 30000 and bounds an engine process.
- `--w3c-suite [DIR]` (since 0.9.0) replaces the fixed corpus with the W3C SHACL test suite's core cases that stay inside the sxc profile, compared with the suite's own expected reports. `DIR` is a w3c/data-shapes checkout; without `DIR`, `SXC_W3C_SHACL_SUITE` supplies it (exit 3 when neither names one). The variable alone never switches the mode. The engines validate the suite's own graphs, so this mode does not exercise sxc's exporter: run the fixed corpus as well, each with its own `--out`.
- `--allow-unpinned-suite` runs a W3C suite whose content digest differs from the pinned commit, adding a `w3c:suite` check that is `not_run` (`suite_not_pinned`), so the stage cannot pass; without it such a suite runs no engine and every case is `not_run` (`suite_not_pinned`). It is exit 2 without `--w3c-suite`.
- `--out DIR` defaults to `.sxc` in the working directory; `--quiet` prints only the summary.
- `--config` is rejected. The stage never reads a default operational configuration.

Install the existing reason requirements for rdflib and pySHACL. Set `SXC_JAVA` to the chosen Java executable if it is not on PATH. The tested optional distribution is Apache Jena 6.2.0, running on a Java 21 runtime; the main pipeline's HermiT requirements are unchanged. Download tools only from the official distribution and verify its checksum before use.

Class references, bounds, missing triples, decimal boundaries and combined/value-group cases each have independently specified expected reports. Both engines must agree with these expectations. Normalization ignores blank-node labels and message text while preserving constraint-level differences. Valid empty reports are distinguished from absent or malformed output. Missing engines, nonzero exit, timeout, invalid RDF and invalid reports cannot pass.

In the W3C mode each approved `sht:Validate` core entry gives `w3c:pyshacl:<case>` and `w3c:jena:<case>`, `<case>` being the suite-relative test name such as `core/node/class-001`. Only `sh:class` and the `sh:minExclusive`, `sh:minInclusive` and `sh:maxInclusive` numeric bounds (with one Violation or Warning severity), the components sxc's exporter emits, on IRI focus nodes with no path or one IRI path run; anything else, such as `sh:datatype`, counts, complex or recursive paths, SPARQL or `sh:closed`, is `not_run` with a reason. An engine that disagrees with an in-profile expected report fails. On the pinned suite both engines passed all 6 in-profile cases (pySHACL 0.40.1 and Apache Jena 6.2.0, 2026-10-24) and 92 entries are `not_run`, so the stage status is `not_run`; read the per-case statuses. The W3C suite is read at test time under the W3C Software and Document License and is not part of this repository.

This is nonrecursive corpus coverage, not a certification of arbitrary shapes or production RDF export. It does not alter `reason` or V1-V5 verdicts. See [the corpus contract](../docs/shacl-corpus.md).

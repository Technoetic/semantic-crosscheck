---
description: "Compare pySHACL and Apache Jena reports with independently specified nonrecursive synthetic class and bounds cases"
argument-hint: "[--jena-home DIR] [--timeout-ms 30000] [--out DIR] [--quiet]"
---

Run the optional standalone `shacl-corpus` stage. It uses only a fixed synthetic RDF corpus and never connects to a production database.

```text
python "${CLAUDE_PLUGIN_ROOT}/skills/semantic-crosscheck/scripts/sxc.py" shacl-corpus --jena-home JENA_DIRECTORY
```

- `--jena-home DIR` selects a separately installed Apache Jena distribution. Without it, `SXC_JENA_HOME` supplies the directory.
- `--timeout-ms` defaults to 30000 and bounds an engine process.
- `--out DIR` defaults to `.sxc` in the working directory; `--quiet` prints only the summary.
- `--config` is rejected. The stage never reads a default operational configuration.

Install the existing reason requirements for rdflib and pySHACL. Set `SXC_JAVA` to the chosen Java executable if it is not on PATH. The tested optional distribution is Apache Jena 6.2.0, running on a Java 21 runtime; the main pipeline's HermiT requirements are unchanged. Download tools only from the official distribution and verify its checksum before use.

Class references, bounds, missing triples, decimal boundaries and combined/value-group cases each have independently specified expected reports. Both engines must agree with these expectations. Normalization ignores blank-node labels and message text while preserving constraint-level differences. Valid empty reports are distinguished from absent or malformed output. Missing engines, nonzero exit, timeout, invalid RDF and invalid reports cannot pass.

This is nonrecursive corpus coverage, not a certification of arbitrary shapes or production RDF export. It does not alter `reason` or V1-V5 verdicts. See [the corpus contract](../docs/shacl-corpus.md).

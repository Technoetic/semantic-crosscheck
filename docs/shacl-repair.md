# Authored SHACL repair fixtures

`shacl-repair` validates independently supplied synthetic before/after RDF graphs.
It runs real pySHACL twice against the same shapes bytes, compares both results
with literal authored expectations, checks explicit target retention and hard
immutability, and counts edits with optional zero soft costs. It does not find a
repair, mutate data, or change ordinary verification stages.

```console
python skills/semantic-crosscheck/scripts/sxc.py shacl-repair --spec examples/shacl-repair/valid.json --out artifacts/repair-valid
python skills/semantic-crosscheck/scripts/sxc.py shacl-repair --spec examples/shacl-repair/target-deletion.json --out artifacts/repair-evasion
```

The first manifest has successful class, lower-bound and property-pair repairs.
The second removes the target class assertion: its after report is correctly
empty, but retention fails and the command exits 1. The examples contain invented
identities and values. Install `requirements/reason.txt` to provide pySHACL and
RDFLib; no database, Java, operational config or new dependency is required.

The stage API is `run(None, args, out: Path) -> Result('shacl_repair')`, with
`args.spec` and `args.timeout_ms` (default 30000; integer 1 through 600000).
The CLI writes `shacl_repair/result.json`. Configuration/schema/input errors
use exit 2, missing optional Python packages use exit 3, and failed/incomplete
checks use exit 1. Only an all-pass result passes. The pre-discard hook
`prepare_run(args, out)` protects input aliases without writing anything.

## Manifest version 1

All fields shown below are required. Unknown keys, duplicate JSON keys,
nonfinite/JSON decimal numbers, duplicate case IDs/obligations/immutable rules,
and empty cases or retention obligations are rejected. Files are UTF-8 Turtle
named by relative POSIX paths beneath the manifest directory. Absolute paths,
parent traversal, network URLs, escaping symlinks and non-`.ttl` inputs fail.

```json
{
  "schema_version": 1,
  "synthetic": true,
  "cases": [{
    "id": "range-example",
    "before": "numeric-before.ttl",
    "after": "numeric-after.ttl",
    "shapes": "numeric-shapes.ttl",
    "retained_targets": [{
      "shape": "urn:sxc:repair:Shape",
      "node": "urn:sxc:repair:row"
    }],
    "immutable_predicates": ["urn:sxc:repair:serial"],
    "immutable_triples": [],
    "edit_costs": {"add": 1, "remove": 1},
    "expected": {
      "before": {
        "conforms": false,
        "results": [{
          "focus": "urn:sxc:repair:row",
          "path": "urn:sxc:repair:quantity",
          "component": "MinInclusiveConstraintComponent",
          "severity": "Violation",
          "value": {"kind": "number", "value": "-1"}
        }]
      },
      "after": {"conforms": true, "results": []}
    }
  }]
}
```

Before expectations must contain at least one violation and `conforms: false`;
after expectations must have `conforms: true` and no results. A fixture therefore
cannot pass with an unchanged, vacuous pair of conforming graphs. Case IDs use
1–64 ASCII letters, digits, dots, underscores or hyphens and are input metadata;
output uses indexed IDs such as `fixture:0001:retention`.

Each expected result contains exactly `focus`, `path`, `component`, `severity`,
and `value`. Focus and a present path are absolute IRIs; `path: null` denotes
an absent node-shape path. Components are `ClassConstraintComponent`,
`MinExclusiveConstraintComponent`, `MinInclusiveConstraintComponent`,
`MaxInclusiveConstraintComponent`, `LessThanConstraintComponent` or
`LessThanOrEqualsConstraintComponent`; severity is `Violation` or `Warning`.
Terms use exactly one of these representations:

```json
{"kind": "iri", "value": "urn:sxc:repair:part"}
{"kind": "number", "value": "1.100"}
{"kind": "literal", "value": "text", "datatype": "http://www.w3.org/2001/XMLSchema#string", "language": ""}
```

An absent report value uses JSON `null`. Number strings are finite decimal
notation, at most 256 characters, compared exactly without floating point;
`1.100` equals `1.1`. Numeric datatypes must use the number representation in
expected reports. Language-tagged literals require datatype
`http://www.w3.org/1999/02/22-rdf-syntax-ns#langString` and a language tag.
Other term strings are at most 4096 characters.

Expected reports are authored input, never calculated by the validator or the
production exporter. Comparison preserves duplicate facts as a multiset of
focus, optional path, component, severity and optional normalized semantic value.
It ignores source shape identities (which can be local property-shape blank
nodes), messages and result blank-node labels. This is weaker than full report
isomorphism and does not independently certify the single pySHACL engine.

## Retention and hard preservation

Every retained `(shape,node)` must actually be a target of that shape in both
graphs and must remain a subject with at least one data triple. Target membership
is recalculated from the same shapes for each graph; keeping the node while
removing its class assertion cannot pass. Class targets include data-graph
`rdfs:subClassOf` ancestry as required by SHACL, even with inference disabled.
Only declared obligations are preserved; the tool does not infer which business
entities should survive.

For each `immutable_predicates` entry, the entire before/after set of triples
with that predicate must be identical: additions, deletions and changed values
all fail. Each `immutable_triples` entry is a three-element array of term
objects; subject and predicate must be IRIs, and object may be IRI or literal.
Each declared triple must exist in both graphs, so a fact absent from both is
not a satisfied preservation obligation. Literal RDF lexical forms are preserved
for hard comparisons. Example:

```json
[[{"kind":"iri","value":"urn:sxc:repair:row"},
  {"kind":"iri","value":"urn:sxc:repair:serial"},
  {"kind":"literal","value":"fixed","datatype":"http://www.w3.org/2001/XMLSchema#string","language":""}]]
```

Hard rules are separate gates and cannot be paid for by soft costs.
`edit_costs.add` and `.remove` are integer costs from 0 through 1000000. The
`edits` check reports added/removed RDF triples and their weighted sum for the
supplied pair. No optimality, minimum edit distance search, alternative repair
enumeration or undeclared preservation is claimed.

## Supported profile and bounds

The profile reuses the corpus's nonrecursive class/range/property-pair checks:
`sh:class`, `sh:minExclusive`, `sh:minInclusive`, `sh:maxInclusive`, and property
`sh:lessThan`/`sh:lessThanOrEquals`. Bounds use finite `xsd:decimal`, `integer`,
`int` or `long`. Shapes use simple IRI paths and Violation/Warning severity;
node-level class/range constraints are supported too. Targets are IRI
`sh:targetNode`, IRI `sh:targetClass`, or implicit `rdfs:Class` targets.
Every targeted shape must have an IRI identity for explicit retention.

Unsupported shapes produce `not_run`, never pass. SPARQL/AF/JS, complex paths,
unsupported targets such as `sh:targetSubjectsOf`, datatype/count constraints,
closed shapes, relative IRIs, blank data nodes, non-IRI focus targets and
`owl:imports` in any input are refused. Turtle parsing takes bytes against a
synthetic base; it cannot load JSON-LD contexts or XML entities. Imports are
checked before pySHACL and `do_owl_imports`, inference, JS and advanced features
are disabled. No graph is fetched from a network URL.

The manifest limit is 256 KiB, with 1–16 cases, at most 1 MiB per input and
8 MiB of distinct files per manifest. Each graph is capped at 10000 triples;
each facts/rules/retention list is capped at 1024 entries. The subprocess deadline
covers all RDF parsing, profile checks, both validations and preservation checks
for the entire manifest, including engine startup. Combined stdout/stderr is
capped at 256 KiB; Windows jobs or a POSIX process group terminate owned
descendants. Timeouts, malformed graphs/reports and engine errors cannot pass.

Only private temporary copies are validated. Outputs contain fixed check IDs,
counts, reason IDs and a SHA-256 binding the exact manifest/file bytes; they
omit authored case IDs, RDF terms, values, file paths, messages and raw errors.
Input aliases to either own result artifact, including hardlinks and duplicate
JSON declarations, are rejected before cleanup. Stale own `result.json` and
`result.json.part` are cleared before ordinary validation failures. If a complete
bounded UTF-8 JSON parse is impossible (including oversized, unreadable or
malformed manifests), preflight stops without cleanup or output writes: hidden
input aliases cannot be ruled out safely, so existing artifacts are preserved.

# Authored source bindings (unreleased source)

An authored `[[gold.expect]]` case can pin an opaque local definition file. The
existing `spec` citation remains required. This capability is unreleased source
on top of version 0.14.0; it adds no operational report or grain authority.

```toml
[[gold.expect]]
source = "authored"
spec = "Inventory definition, section 2"
rows = []
spec_source = { file = "definitions/inventory.md", sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", object_id = "inventory", revision = "r1" }
```

Replace the illustrative hash with the SHA-256 of the actual file bytes. The
closed `spec_source` table requires `file` and `sha256`; `object_id` and `revision`
are optional strings, each at most 128 characters without controls. They are
caller declarations: SXC does not parse the text, locate an object, or establish
approval. Captured cases reject this table. Cases without it keep legacy behavior,
with no new source reads, checks, namespace or case-count cap.

The canonical configuration directory is the root. `file` is a nonempty relative
path of at most 512 characters; both directory separator spellings are supported.
Absolute, drive, UNC, traversal, dot/empty, hidden, control-character, Windows
device, trailing-dot/space and secret-name components are rejected. Secret names
include env, credential, secret, password and token components and private-key
extensions. Observed symlinks, reparse points/junctions and file hardlinks are
blocked. Canonical root ancestors such as macOS `/tmp` are allowed; links below
the selected root are rejected. Sources cannot live inside output, and output
cannot live below a source. Configuration, gold SQL, revision SQL and sources are
protected from output aliases before cleanup and again before final/error writes.

Only bound cases have these limits: 64 cases, 256 KiB per file, 4 MiB aggregate
content per complete observation batch and 64 KiB public metadata. Repeated
bindings count toward the byte budget. Each read is bounded; a full capture reads
and then rechecks the batch, and completion observes it again. Files are hashed
as bytes, without interpreting frontmatter or natural language.

The states are `current`, `changed`, `missing`, `blocked` and `unavailable`.
`current` means observed bytes match the pin. Only `FileNotFoundError` means
`missing`; other I/O failure is `unavailable`. Unsafe paths or exceeded content
bounds are `blocked`. File identity, size and mtime are compared before/after
reads and across the completed capture batch; `O_NOFOLLOW` is used where available.
These observations are neither a filesystem sandbox nor a continuous lock or an
ABA proof. They reject observed races, but cannot prevent a hostile process from
swapping directory ancestors between checks without kernel-enforced traversal.

Any noncurrent source before execution stops **all** cases before any DB
connection. Case checks are `not_run`. Current bindings proceed through the
existing fixture consent and isolation guards. At completion, affected passing
cases become `not_run`; existing fail/error/not_run cases keep their status.
A noncurrent snapshot is never promoted by a later `finish`. A fresh capture
after exact-byte restoration can legitimately be current; that observation
creates no engine execution or new case pass. Configuration bytes are checked
across capture and completion as well.

Source check IDs are `expect:spec_source:<gold-id>[<case-index>]`. Bound expect
results preserve only the explicit `inputs.spec_sources` namespace alongside
normal input fingerprints. Its public schema is `sxc-spec-sources/v1`, with
`count`, `current` (a count), `digest`, and `items`. Each item contains only
`gold_index`, `case_index`, `state`, `expected_sha256`, `observed_sha256` and
`binding_sha256`. Unobserved hashes are empty strings. The binding hash covers the
declared path, labels and pin. Neither paths, arbitrary labels, bodies, SQL,
rows, parameter values, DSNs nor environment-variable names appear in new source
metadata or bound expect output. Hashes are unsalted and can confirm guesses.

Malformed/protected preflight exits nonzero before writes and can leave an older
result untouched. That old file is **not fresh execution evidence**. Result
publication uses a unique exclusively created temporary file and atomic
replacement; a preexisting fixed `result.json.part` is never truncated.

The Python API is `SourceBinding`, `validate_spec_source(value, ref)`,
`preflight(cfg, out)`, `capture(cfg, cases, out=None)` and
`finish(snapshot, result)`. Cases retain `(gold_index, gold, case_index, case)`
tuples. Snapshots expose `ok` and `public()`; `finish` returns the new observation
and updates only source metadata/checks and affected expectation passes.

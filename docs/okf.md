# Limited OKF metadata exchange (unreleased source)

Version metadata remains 0.14.0. The optional `okf` diagnostic exports selected
local Gold contracts or previews a directory against trusted local inputs. It
never executes SQL, connects to a database, calls a model, fetches a URL, runs an
executor, applies imported definitions, or alters operational report gates.
Operation PASS means this scoped metadata operation completed. Runtime execution,
cross-layer correctness, bundle authenticity and import application are always
`not_proven`. Hash integrity is not authenticated origin or business truth.

```text
python skills/semantic-crosscheck/scripts/sxc.py okf --config crosscheck.toml --export contract-bundle --select q01_explosion --out .sxc
python skills/semantic-crosscheck/scripts/sxc.py okf --config crosscheck.toml --import-preview contract-bundle --out .sxc
```

Both modes require explicit `--config`. Export requires repeatable `--select`
with 1–64 existing unique Gold IDs, never implicit all-selection. Preview gets
its selection from the manifest and rejects `--select`. Modes are mutually
exclusive. No ZIP or remote import is supported. `--out` is the separate
diagnostic directory; `--quiet` limits console output. Output is
`<out>/okf/result.json`; its only stage input namespace is `okf_profile`.
Exit 0 means operation PASS, 1 means incomplete/failed checks, 2 means invalid
arguments or protected preflight. Unsafe or unverified preflight can preserve a
previous result instead of overwriting an input: nonzero exit never makes that
old file fresh evidence. Verified output is cleared before running. Error/final
writes recheck configuration SHA and paths; successful writes reobserve inputs.

## Closed producer profile

Layout is exactly `index.md`, `manifest.json`, and one
`contracts/<gold-id>.md` per selected ID. The root index alone carries
`okf_version: "0.2"`. Concepts have type `SXC Verification Contract`, never
executable Attested Computation. Frontmatter has one JSON literal per line,
parsed with `json.loads`, without YAML inference or evaluation. Concept keys are
exactly `type`, `title`, `resource` (a generated URN), and `x_sxc`. The last is a
closed object containing `profile`, `id`, `sql_sha256`, `configuration_sha256`,
`engine_sha256`, and `source_binding_sha256`. Bodies contain fixed scope text or
generated index links. Preview accepts exactly generated document bytes;
general Markdown/YAML editing is outside this profile. Manifest JSON whitespace
and key order can differ without changing its meaning.

Manifest keys are exactly `schema`, `profile`, `okf_version`, `selected`, `inputs`,
`source_bindings`, `documents`, `losses`. Schema is `sxc-okf-profile/v1`; profile
is `sxc-verification-contract-metadata/v1`; OKF version is the string `0.2`.
`documents` contains each index/concept's `path`, `id`, `bytes`, `sha256`.
The index ID is `index`; concept IDs are selected Gold IDs. Inventory is exact:
unknown, missing, extra, changed, unsafe, linked or case-colliding paths fail.
Unknown keys, verdict/trust additions and unsupported syntax yield
`unsupported_profile`; that does not assert invalidity of general OKF.
Unsupported JSON metadata yields only omitted-field count/hash where available,
never arbitrary values. Input bundles are preserved in place.

`inputs` contains `configuration_sha256`, `engine_sha256` and `contracts`. Each
contract contains selected validated `id`, mapped `members`, `sql_sha256`,
`case_count`, `omitted_count` and opaque `omitted_sha256` of its declaration.
`source_bindings` reuses the closed `sxc-spec-sources/v1` public snapshot: safe
indices, states and hashes, never source paths or declared labels. Bindings must
be current at start/completion. A current pin means exact observed bytes,
without semantic validity or approval. Preview recomputes trusted configuration,
selected SQL and source bindings; changed/missing/blocked/unavailable evidence
has distinct reason codes. Unselected SQL/source and revision contents are never
fingerprinted/read. The config loader still validates declared SQL existence.

No source text, raw SQL/TOML, rows, parameter values, DSN/environment names,
local paths, arbitrary labels, receipts, executors or attesters are copied.
Omission counts/hashes are integrity metadata. Fixed losses are
`source_text_omitted`, `raw_sql_omitted`, `parameter_values_omitted`,
`row_values_omitted`, `runtime_receipts_not_in_bundle`, `trust_not_inferred`.
This profile promises no general lossless OKF round trip.

## Bounds and filesystem boundary

Manifest and each concept/index document are capped at 64 KiB, JSON nesting at
16, total bundle bytes at 4 MiB, directory entries at 256, relative paths at
512 characters and two components. Bounds precede unbounded reads/traversal.
Selected SQL/config observations are also bounded to 4 MiB (selected SQL
collectively); source observations retain their own 64-bound-case,
256-KiB/file, 4-MiB-total and 64-KiB-public-metadata limits.

Only regular files are read. Observed symlinks, Windows reparse points/junctions,
hardlinks, traversal and unsafe components are rejected. Input/output aliases
and ancestors are protected before cleanup and again on write paths. Reads
compare identity, size and mtime before/after; complete batches are rechecked.
These are caller-controlled boundary observations, not a kernel sandbox,
continuous lock, hostile ancestor-race or ABA proof.

Export refuses every existing destination, including an empty directory. Owned
sibling staging is published with Windows no-replace rename, Linux
`renameat2(RENAME_NOREPLACE)` or macOS `renamex_np(RENAME_EXCL)`; unavailable
support fails closed. Only verified invocation-owned staging is cleaned. The
caller's destination is never cleaned, including races. Postpublication input
edits fail the operation; an already published directory may remain and needs
a fresh preview before use.

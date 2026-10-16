# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Optional synthetic SQL counterexample generation, bounded row-deletion shrinking and hash-bound
  replay. NULL, join fan-out, BOM diamond and date-boundary scenarios run in read-only PostgreSQL
  transactions. Bounded agreement is not a proof; query errors are never mismatch witnesses.
- Optional data-revision SQL probes bind stage evidence to hashed source revisions. Reports reject
  missing or changed bindings and changed engine/probe inputs. Legacy configurations explicitly
  report unbound data freshness without a database probe. No common cross-session snapshot is claimed.

## [0.1.1] - 2026-10-14

Release notes: [docs/releases/v0.1.1.md](docs/releases/v0.1.1.md).

### Fixed

- A selected SHACL or OWL check exceeding its resource limit now leaves explicit `not_run` checks, so
  neither `reason` nor the full `report` can pass when only one engine was skipped. The next action
  directs the user to review the limits and rerun reasoning.
- Numeric canonicalisation preserves every decimal digit independently of the active Decimal context;
  distinct high-precision values and numeric keys no longer compare equal through rounding.
- Unknown option keys in entities, ontology definitions and gold parameter specifications stop with
  a configuration error instead of silently dropping a rule. User-defined mapping keys remain supported.

### Changed

- Integration CI fetches the full history and fails if any test is skipped or no tests run.
- Release document checks cover every published version, including this patch release.

## [0.1.0] - 2026-10-14

First release. Release notes: [docs/releases/v0.1.0.md](docs/releases/v0.1.0.md).

### Added

- `sxc.py` with the stages `doctor`, `demo`, `build`, `reason`, `cube`, `verify`, `report` and
  `cube-install`, one `result.json` per stage (counts and ids only, fail-closed status, `not_run` never a
  pass, `not_proven`, `next_safe_action`) and exit codes 0 pass, 1 a check failed or did not run,
  2 configuration or usage error, 3 environment missing. An explicit `--config` must exist; results are
  written atomically and the previous result is removed before a stage runs; new output directories get a
  `.gitignore` of `*`; `SXC_DEBUG=1` prints tracebacks for local debugging.
- `crosscheck.toml` version 1 as the single source of truth: entities, hierarchies, shapes, measures,
  ontology, Cube, verification and gold queries, validated before any stage works (unknown keys,
  credential keys in `[source]` and `[cube]`, DSNs in the `*_dsn_env` keys and out-of-range numbers are
  configuration errors).
- `doctor`: Python, pg8000, the check-role DSN, PostgreSQL 17, `privileges:temporary`,
  `privileges:select:<entity>`, `privileges:no_write:<entity>` (write privileges, TRIGGER, table and
  schema ownership, CREATE on the schema), `output_schema`, and the optional reason and Cube components,
  with install hints that work from any directory.
- PostgreSQL 17 hierarchy closure built level by level (exact path counts and summed path-product
  quantities without enumerating paths), cycle examples with the `CYCLE` clause, and
  `MERGE ... WHEN NOT MATCHED BY SOURCE ... RETURNING merge_action()` followed by an equality proof;
  concurrent builds of one output schema wait for each other.
- Data rules as SQL violation views and generated SHACL shapes; `reason` compares pySHACL with SQL per
  shape (shapes without a separate SHACL form are reported as not proven) and checks an OWL ontology
  with HermiT (consistency, transitive closure, defined classes).
- Cube 1.8.3 adapter installed with `npm ci --ignore-scripts` from a committed lockfile and a size- and
  SHA-256-pinned native module (Node.js 22 or later; glibc checked on Linux); generated Cube model with
  metadata and gold-member checks; read-only, time-limited Cube sessions on 127.0.0.1 with a per-run API
  secret, a scrubbed child environment, proxy-free client requests and redacted adapter logs
  (`SXC_CUBE_DEBUG=1` for full local error text).
- `verify`: V1 strict compare and V2 strata sweep in the form of each column's Cube member type, V3 query
  shapes (totals, additivity, measure pairs, sampled above 2,000 keys) and V4 mutation tests with witness
  queries for equivalent mutants, where only a failing suite item kills a mutant; `--only` and `--gold`
  leave `scope:*` checks so a narrowed run never passes.
- `report` with `report.md` and `report.json`: statuses recomputed from the checks, required stages,
  stale and missing stages counted as not run.
- Synthetic parts / BOM / orders / stock demo with `clean` and `dirty` variants that can be reloaded at
  any time, and `expected.json` reference numbers from an independent Python implementation that name the
  check showing each planted defect.
- TLS for the Python stages and the Cube child following the DSN's `sslmode` (`verify-ca` and
  `verify-full` verify the server certificate).
- Claude Code plugin (`/xc-*` commands and a skill), Codex plugin and marketplace manifests.
- `tools/leak_scan.py` (secrets, e-mail addresses outside `tools/leak-allow.txt`, a private deny-list,
  encoded and compressed content, history, path and ref names) and git hooks in `.githooks/` for commits
  and pushes.
- Documentation: README, design document (`docs/design.md`), configuration reference, pitfalls, Codex
  usage, security policy, third-party licences, release notes and a roles template for psql
  (`docs/setup-roles.sql`).
- CI: unit tests on Linux, Windows and macOS with Python 3.11 and 3.13; integration tests against a
  PostgreSQL 17 service with Java 21 and Node.js 22, after `docs/setup-roles.sql` is run with psql,
  followed by the clean and dirty demo end to end through the command line; whitespace check and a leak
  scan of the working tree and the whole history.

[0.1.1]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.1.1
[0.1.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.1.0

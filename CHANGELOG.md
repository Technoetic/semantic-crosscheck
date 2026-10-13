# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - Unreleased

First release.

### Added

- `sxc.py` with the stages `doctor`, `demo`, `build`, `reason`, `cube`, `verify`, `report` and
  `cube-install`, one `result.json` per stage (counts and ids only, fail-closed status, `not_proven`,
  `next_safe_action`) and exit codes 0 pass, 1 check failed, 2 configuration error, 3 environment
  missing.
- `crosscheck.toml` version 1 as the single source of truth: entities, hierarchies, shapes, measures,
  ontology, Cube, verification, output and gold queries.
- PostgreSQL 17 hierarchy closure built level by level (exact path counts and summed path-product
  quantities without enumerating paths), cycle examples with the `CYCLE` clause, and
  `MERGE ... WHEN NOT MATCHED BY SOURCE ... RETURNING merge_action()` followed by an equality proof.
- Data rules as SQL violation views and generated SHACL shapes; `reason` compares pySHACL with SQL per
  shape and checks an OWL ontology with HermiT (consistency, transitive closure, defined classes).
- Cube 1.8.3 adapter installed with `npm ci --ignore-scripts` from a committed lockfile and a
  SHA-256-pinned native module; generated Cube model with metadata and gold-member checks; read-only Cube
  sessions on 127.0.0.1 with a per-run API secret.
- `verify`: V1 strict compare, V2 strata sweep, V3 query shapes (totals, additivity, measure pairs) and
  V4 mutation tests with witness queries for equivalent mutants.
- `report` with `report.md` and `report.json`, statuses recomputed from the checks and stale results
  flagged.
- Synthetic parts / BOM / orders / stock demo with `clean` and `dirty` variants and reference numbers
  from an independent Python implementation.
- Claude Code plugin (`/xc-*` commands and a skill), Codex plugin and marketplace manifests.
- Documentation: README, configuration reference, pitfalls, Codex usage, security policy, third-party
  licences and a roles template (`docs/setup-roles.sql`).
- CI: unit tests on Linux, Windows and macOS with Python 3.11 and 3.13; integration tests against a
  PostgreSQL 17 service with Java 21 and Node.js 22; whitespace check and a generic leak scan.

[0.1.0]: https://github.com/Technoetic/semantic-crosscheck/releases/tag/v0.1.0

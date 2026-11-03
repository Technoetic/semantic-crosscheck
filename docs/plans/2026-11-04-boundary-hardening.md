# Boundary hardening implementation and release plan

**Goal:** Prevent lossy host decoding from producing false equality and bound Cube response resource use.
**Authorization:** User requested implementation and public GitHub release on 2026-11-04, following the ninth research. Current public release intent is v0.18.1 after the CI infrastructure recovery ruling below; existing authorization continues. Release destination is Technoetic/semantic-crosscheck; company databases and Sites hosting are outside scope.
**Architecture:** Keep the existing exact comparison and authority rules. Validate raw Cube JSON before information is lost; validate PostgreSQL returned metadata before converting tuples to dictionaries. Share monotonic elapsed query accounting and bounded body handling across normal and replay Cube paths. No new dependency or engine change.

## Global constraints
- Never report an operational/decode/schema failure as equality, a surviving mutant, or a confirmed witness.
- Reports retain safe types/categories/counts, never returned row values, error bodies, credentials or DSNs.
- Existing read-only SQL transactions, prepared-statement cleanup, row limits, proxy exclusion and replay provenance remain intact.
- HTTP elapsed acceptance is checked across polling and final success; do not claim hard cancellation of DNS, headers or HTTP framing unless demonstrated. No leaking background transport worker.
- Final review F1 ruling: after strict raw JSON decode, bound aggregate potential fixed-point characters of all decoded Decimal values by the same positive integer as max_response_bytes. Use only sign/digits/exponent, without formatting, normalization or rounding; zero contributes one, other lengths include fractional trailing zeros before stripping. Traverse nested arrays/maps iteratively and reject before returning decoded values. This additional per-response numeric budget applies to normal/replay/meta paths with fixed CubeResponseError; native meta JSON/Decimal parse exceptions remain intact. Preserve admitted values and canonical/RDF/hash representations. No global numeric-string policy or total-heap/CPU guarantee is implied.
- Use TDD for changed behavior; preserve raw RED and GREEN evidence. Work in the isolated boundary-hardening worktree. Publish only after full tests and independent review.

## Task 1: Strict raw Cube JSON
Files: cube_client.py, tests/test_cube_json.py; focused existing Cube envelope tests if contract requires.
- Reject duplicate object keys at every depth and nonfinite JSON constants before overwrite can occur.
- Decode fractional JSON numbers as Decimal, leaving integer, text, boolean, null and numeric strings semantically intact.
- Normalize invalid raw load JSON as CubeResponseError without leaking bodies; preserve meta-stage error behavior where compatible.
- Test duplicate envelope/member/nested keys, NaN/Infinity/-Infinity, invalid JSON/encoding, huge exact decimals and ordinary responses. Demonstrate old false equality is now rejected or a real mismatch.
- Run focused tests plus full local suite with provisioned Python, commit owned files, and self-review. Do not change HTTP transport yet.

## Task 2: Gold SQL returned metadata
Files: compare.py, tests/test_gold_metadata.py; existing comparison tests if necessary.
- Inspect prepared statement column metadata after execution, before any dictionary conversion.
- Reject duplicate or invalid/empty labels, missing gold.map columns even for zero rows, and row-width mismatches without retaining row values.
- Allow extra distinct unmapped columns and ordinary valid empty results; keep reserved parameters and transaction/statement cleanup intact.
- Prove silent duplicate-column overwrite and empty missing-column acceptance no longer pass. Test valid/nonempty/empty/error cleanup and sanitized run_binding reports.
- Run focused tests plus full local suite with provisioned Python, commit owned files, and self-review. Do not touch cube_client.py.

## Task 3: Controlled HTTP experiment and shared bounds
Files: cube_client.py, cube_replay.py, focused transport tests and docs/cube-http.md.
- Experiment decision: use public read1 with checks before/after transport, reads, decoding and final caller acceptance. Default source-body cap is 16 MiB (constructor max_response_bytes override), not a total-heap cap. Socket wait is at most min(30 seconds, remaining operation budget), but repeated header/framing reads may overrun before returning; reject late results once control returns. No worker or private socket access.
- Pass one monotonic deadline through private _req(..., deadline=...) for load and replay. Update test doubles explicitly; do not retry without the deadline on TypeError. Oversize/malformed bodies are CubeResponseError; timeouts retain safe CubeError timeout classification. Validate finite positive numeric nonboolean timeout and positive nonboolean integer byte cap. Health readiness remains a separate boolean probe.
- First record actual local-server slow body, error body and no-length/chunked experiments outside source; specify defaults and precise limitations from the results.
- After Task 1 review, add finite positive elapsed query budgets and a bounded response byte limit, using monotonic time for elapsed accounting and wall time only for JWT claims.
- Bound successful and HTTP-error body reads before JSON decoding and error formatting. Honor the same operation deadline across polls, sleep, transport and final success in normal and replay paths; reject late results.
- Verify exact boundary and cap+1, absent/lying lengths, HTTP errors, slow body/polls, recovery, malformed bodies, replay error authority and timeout/size report sanitization. Use reliable local-server synchronization and generous timing margins.
- Preserve no-proxy transport and existing public call behavior where possible. Document DNS/header/framing limits accurately; avoid private socket internals and background workers unless explicit reviewed evidence requires them.

## Task 4: Documentation, integration and public release
- Document behavior/defaults/compatibility and bump all current release metadata coherently to v0.18.1; retain v0.18.0 source history and tag. No unimplemented research claims.
- Run full local unittest, compile, packaging/docs/leak checks. Optional engines not provisioned locally must remain explicit skips.
- Independent task reviews and whole-branch review; resolve important findings. Run real-engine CI on exact main and tag revisions.
- Create deterministic source release archives and SHA-256 manifest from committed source, verify extracted source and draft assets independently, then publish immutable Latest v0.18.1 and verify public API/asset hashes.
- Record six-field implementation/release evidence in NS, update only owned shared-state deltas, commit locally and synchronize only the authorized local bare mirror. Preserve unrelated dirty files.

## CI infrastructure recovery ruling — v0.18.1

- Observed on v0.18.0 source `fc77ea0c56f727700e968ece48bcbe2f7e08ea33`:
  [main run 41721743823](https://github.com/Technoetic/semantic-crosscheck/actions/runs/41721743823)
  and [tag run 41721743815](https://github.com/Technoetic/semantic-crosscheck/actions/runs/41721743815),
  attempt 1, each completed six non-Mac jobs successfully, including strict
  real-engine integration and hygiene. Across the two runs all four Mac jobs
  were cancelled, had an empty runner name and zero steps. GitHub reported
  hosted-runner acquisition failure and macOS arm64 capacity constraints.
  No Mac test or setup failure was observed, and those jobs supply no Mac
  execution evidence. Raw observations are retained by the release controller.
- Ruling: replace only the Mac unit OS label `macos-latest` with standard
  `macos-15-intel` (macOS 15 x64). Official pinned
  [runner-image labels](https://github.com/actions/runner-images/blob/a2d4955654f519bd5b559bffaea60d9f90156630/README.md#available-images)
  and [public runner policy](https://github.com/github/docs/blob/396218f23a6e3fa24d173ebfa74b3499adde67a3/data/reusables/actions/supported-github-runners.md)
  establish supported standard/free-public status; `macos-latest` maps to
  macOS 26 arm64 and macOS 14 was retired on November 2. File-bound Jev Choice
  advised this patch at confidence 0.85; the host checked the primary facts.
- Preserve Linux/Windows latest, Python 3.11/3.13, six unit jobs plus strict
  integration and hygiene (eight jobs), all workflow steps/action pins and
  no-skip real-engine guards. Add the focused matrix regression through RED
  then GREEN. No product source behavior changes; runtime/dependency pins
  (including Cube/Jena/PostgreSQL) remain unchanged. The CI runner label is
  the explicit infrastructure change.
- Preserve the pushed v0.18.0 tag without retargeting or force. It did not
  receive a formal release. v0.18.1 is the intended first formal bundle of
  the existing boundary fixes plus this CI-only recovery patch.
- Release gate: frozen full local checks and independent review, then exact
  main/new annotated-tag CI with eight successful jobs apiece. Intel capacity
  and test success remain unobserved until those runs complete; do not claim
  Mac arm64 coverage. Record actual future counts, source/tag SHAs, job URLs,
  raw logs and archive hashes in v0.18.1 verification.json after observation.

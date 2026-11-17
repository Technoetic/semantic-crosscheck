# Exact arithmetic and bounded independent diagnostics

The user approved implementing and publishing the recommendations from the v0.27.0 source review. The accepted scope is exact additive arithmetic, a narrowly supported independent SUM/COUNT oracle, and an optional differential-query-plan experiment. This approval includes a GitHub source/release publication and updating Reference Atlas with pinned public evidence. It does not request an active plugin installation or an operational database change.

## Goals and approach

- Fix the observed V3 false verdicts caused by ambient Decimal precision without numerical tolerance or a global context change.
- Independently recompute a global SUM/COUNT over bounded raw rows from one declared table, using an independently authored filter and an explicitly supplied Cube query.
- Compare identical declarative synthetic queries/data under EXPLAIN-confirmed different physical plans in PostgreSQL, preserving full typed row bags.
- Keep both diagnostics optional and outside default operational report authority. A diagnostic pass never proves business correctness or global equivalence.

The alternative of merely increasing precision to 60 is rejected: the required precision depends on inputs. Adding external solvers, Hypothesis as a runtime dependency, arbitrary SQL rewriting, CODDTest, and a Rust runner would enlarge this change without solving the reproduced arithmetic defect. Those remain future research.

## A. Exact additive arithmetic

`exact_numeric.exact_sum(values, *, max_digits=4096, max_terms=100000)` accepts finite Decimal values and NULLs. It returns an exact Decimal, NULL for no present values, or raises `NumericLimit(ValueError)` before an unbounded allocation. It does not use the ambient precision/rounding/trap context. Bound coefficient, exponent, alignment and final size; count NULL entries in the term budget. Canonical zero is positive zero. Invalid nonfinite/type inputs are ValueError. The existing shape_q number parser remains responsible for legacy representation conversion.

Tests must reproduce correct `10^28+1` rejected and incorrect `10^28` accepted at precision 28 before the fix. Exercise context precision/traps, permutations, cancellation, fractions, all-NULL, invalid values and resource bounds. An unsupported/budgeted computation cannot become a pass.

## B. Independent measure oracle

`measure_oracle.run(cfg,args,out)` returns `Result('measure_oracle')`; `sxc.py measure-oracle --config PATH --spec PATH` is an explicit new diagnostic. A strict versioned JSON spec selects one configured entity, a raw column, sum/count, a bounded AND predicate, an independent Cube query containing one global measure, and a row limit (default 10000, maximum 50000). The exact closed schema is documented with the implementation.

Read only needed raw columns in a bounded READ ONLY REPEATABLE READ transaction. Quote identifiers and bind values; do not call/import cube_gen, shape_q or SQL rule compilers in the independent calculation. Admit catalog-validated integer/numeric/text/boolean types and typed predicates only; unsupported types, nonfinite values, unsafe collation, exhausted limits and unsupported forms are not_run. COUNT(column) ignores NULL and returns zero on empty; SUM ignores NULL and returns NULL when none qualify. Exact summation may use A's pure arithmetic primitive.

Serve the existing verified Cube model and compare its single result to the recomputed value. Validate source/config/spec and Cube receipt freshness. Inputs changing during await or export invalidate the result. Report counts, fixed reason codes and hashes only; raw values, predicate constants, SQL, paths and credentials must not reach results/console. Disclose empty coverage and that raw export/Cube sessions share no enforced snapshot. A stable-data/revision contract remains required.

## C. Differential plan diagnostic

`plan_compare.run(None,args,out)` returns `Result('plan_compare')`; `sxc.py plan-compare --spec PATH --dsn-env NAME` does not use operational crosscheck.toml. Default DSN environment name is SXC_TEST_ADMIN_DSN. Only session-owned temporary synthetic tables/indexes are created; no user relation is modified.

The strict declarative spec supplies bounded typed columns/rows, projection, typed AND comparisons, an index column and timezone. It admits no arbitrary SQL, functions, joins or DDL. Cross timestamp/timestamptz comparisons are admitted to reproduce the existing DST sentinel. Input bytes <=256 KiB, rows <=2000, explicit operation/statement budgets.

After fixture setup, execute both sequential/index profiles with SET LOCAL inside one READ ONLY REPEATABLE READ transaction. The SQL and bindings/data are identical. Hash structural EXPLAIN JSON nodes, excluding costs, estimates and literals; different GUC settings alone are insufficient. Compare complete typed row multisets, including duplicate multiplicities. Same plan, empty coverage, truncation, timeout, errors and limits cannot pass. A mismatch proves inconsistency, not which result is correct. No reducer is shipped: no minimality claim; a future reducer must preserve mismatch and plan diversity.

## Integration and boundaries

Both modules export `prepare_run(args,out)` for alias/link protection before cleanup or output creation. CLI sanitizes parser failures, handles --option=PATH spellings, protects spec/config and selected config files, rechecks inputs before publishing, clears only verified old own receipts and preserves module-specific evidence hashes. Existing default command/result contracts remain unchanged except version/fingerprint and the arithmetic correctness fix.

No new runtime dependency or copied upstream code. Existing release checks, six OS/Python CI jobs, strict full-stack PostgreSQL/HermiT/Cube/Jena integration, leak scan and immutable release assets remain gates. Runtime prerequisites may be installed in isolated test locations. Tests use synthetic databases only.

## Review focus

1. A rounded arithmetic result must never certify equality; nonfinite values and pathological exponents fail closed.
2. Raw-row oracle independence is from generators, not from the authored specification or shared arithmetic axioms.
3. A plan signature must witness real physical plan diversity; comparison must preserve duplicate rows and types.
4. Exceptions, CLI usage errors, input/output aliases and input edits cannot leave stale passing evidence or leak input values.
5. Diagnostics cannot silently participate in the default operational report or claim shared Cube/PostgreSQL snapshots.

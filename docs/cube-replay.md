# Synthetic Cube replay

`cube-replay` is an optional standalone check against a **dedicated disposable
test database**. It compiles a fixed synthetic configuration, builds physical
fixture tables and closure tables, applies the selected real model mutation,
and starts separate actual Cube runtimes for the original and mutant models.
An independently authored source SQL query is the gold: closure gold enumerates
paths recursively, whereas the normal closure compiler aggregates by level.

```console
python skills/semantic-crosscheck/scripts/sxc.py cube-replay --mutation-operator closure_path_count_one --allow-scratch-schema --out out
```

Set `SXC_CUBE_REPLAY_DSN` to the dedicated test connection before running.
`--dsn-env NAME` selects another explicitly supplied test variable. There is no
fallback to the operational configuration or its connection. The normal pinned
Cube adapter must already be installed and Node.js available. `--seed` defaults
to 7; `--statement-timeout-ms` defaults to 10000 and accepts 1000–120000.

The explicit `--allow-scratch-schema` flag permits two random private schemas
in that test database. It does not check whether a caller-selected database is
safe. The check never creates roles, resets databases, writes existing schemas,
or executes caller SQL. During comparison, share locks freeze only its own tiny
committed fixture tables while Cube reads them from separate sessions. It checks
physical input, configuration, connection and engine fingerprints around runtime
comparisons. Cleanup checks each successfully created schema's OID, drops known
owned relations, and removes those schemas without `CASCADE`. Existing schema
name collisions are errors and do not authorize cleanup of the colliding schema.
A connection or cleanup failure cannot pass.

All nine built-in operators have fixed distinguishing templates:

| Operator | Fixture and observed query |
| --- | --- |
| `closure_depth_shift` | Unequal weighted diamond; descendant minimum depth |
| `closure_path_count_one` | Diamond; per-descendant path count |
| `closure_qty_single_path` | Diamond; summed path-product quantity |
| `measure_filter_drop` | FIRM and excluded orders; filtered sum |
| `measure_normalization_drop` | Exact, mixed-case and spaced statuses; normalized sum |
| `measure_agg_to_max` | Two positive stock grain keys; total reaggregation |
| `dated_shift_one_day` | Orders straddling January 2026 boundaries; January sum |
| `closure_join_swap` | Asymmetric stock at the two endpoints; joined stock |
| `closure_end_swap` | Different classes at the two endpoints; descendant class |

A pass requires the original Cube answer to equal the independent gold **and**
the mutant answer to differ from that same gold. Results record counts, hashes,
operator ids and runtime enums. They never contain answer rows, SQL, connection
values, temporary paths, or raw engine errors. Missing or malformed runtime data,
timeouts and incomplete execution cannot count as a detected mutant.
Numeric answers must be finite bounded decimal strings, integers or exact
decimals. Floating-point values fail closed because JSON parsing may already
have rounded them.

On success, `out/cube_replay/fixture.json` is an explicitly labelled synthetic
input artifact. It contains generated rows, exact version/operator/seed bindings,
and hashes of the fixture, query/configuration contract and engine. Use
`--replay out/cube_replay/fixture.json` with the same operator and seed to rerun it,
including in the same output directory. Replay accepts only the exact full
generated fixture, rejects duplicate JSON keys and modified rows, and refuses
an engine/template/version mismatch. Failed executions remove stale fixtures.
Replay input bytes must remain unchanged during execution. An in-place replay
is preserved until this check finishes and then atomically replaced on success
or removed on failure.
Generated model files, configuration and runtime working files are temporary.

This bounded check does not establish general equivalence, production integrity
rules, or business-query correctness. It does not modify operational V1–V5,
real-data V4 mutation verdicts, or global report coverage.

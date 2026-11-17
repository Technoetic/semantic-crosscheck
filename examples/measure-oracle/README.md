# Synthetic measure oracle inputs

Load `fixture.sql` into an isolated test database using its owner connection.
Grant the configured check role SELECT access and schema USAGE separately.
Configure the existing `SXC_PG_DSN`/`SXC_PG_OWNER_DSN` environment variables;
no connection strings or credentials are stored here.

Run the normal `cube` stage with `crosscheck.toml`, then run `measure-oracle`
with `sum.json` or `count.json` in the same output directory. The independent
answers are SUM `15.25` and COUNT(column) `2`. NULL measure values do not count;
the row with a NULL grouping key is explicitly excluded by the authored spec.

To demonstrate a shared filter mistake, copy the configuration outside the
example directory, change its configured measure filters from `FIRM` to
`DRAFT`, regenerate Cube, and rerun the unchanged independently authored
specs. The raw oracle still selects the two non-NULL FIRM values and reports
the disagreement. Do not change both the configuration and oracle spec to
the same wrong predicate.

See [the measure oracle contract](../../docs/measure-oracle.md) for its bounds,
supported typed predicates, privacy contract and cross-session limitation.

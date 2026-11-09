# Synthetic data-rules example

A clean-room example for the optional `data-rules` stage ([docs/data-rules.md](../../docs/data-rules.md)): a small
invented bill of materials in the schema `sxc_rules` (tables `part`, `bom_line` and `code_value`) and 15 rules covering
the six kinds and most atom families, each with an authored example. Endpoint structure atoms, `exists = true`, the
`none` quantifier and the operators `ne`, `lt`, `le`, `ge`, `not_in` and `is_not_null` are exercised in
`tests/test_data_rules_pg.py` instead. Every name, value and threshold is invented.

| File | Content |
|---|---|
| `crosscheck.toml` | the entities, the `has_component` hierarchy and the `[data_rules]` table |
| `data-rules.toml` | the sets, the reference set `procure_codes` and the 15 rules with their `[[rule.example]]` rows |
| `fixture.sql` | the three tables with planted defects and near-misses for every rule, then (after the `-- variant:` line) rows with more NULL endpoints and missing references that a correct evaluation mostly ignores; load it yourself, sxc never runs fixture SQL |

## Run it

Use a dedicated, throw-away database. As a superuser, create the two roles and the schemas with the roles template
from the repository root (the source schema is `sxc_rules`, owned by the loader role):

```bash
createdb -U postgres sxc_rules_db
psql -U postgres -d sxc_rules_db -v loader_role=sxc_loader -v check_role=sxc_check -v source_schema=sxc_rules -v output_schema=sxc_out -f docs/setup-roles.sql
```

The template sets no passwords. On a server with password authentication, set both before the next step,
interactively as the superuser (`\password sxc_loader` and `\password sxc_check` in psql), and give each of the
commands below its role's password through `PGPASSWORD`, never in a file.

Load the fixture as the loader role, then run the stages with the check role:

```bash
psql -U sxc_loader -d sxc_rules_db -v ON_ERROR_STOP=1 -f examples/data-rules/fixture.sql
export SXC_PG_DSN=postgresql://sxc_check@localhost:5432/sxc_rules_db
python skills/semantic-crosscheck/scripts/sxc.py doctor --config examples/data-rules/crosscheck.toml
python skills/semantic-crosscheck/scripts/sxc.py build --config examples/data-rules/crosscheck.toml
python skills/semantic-crosscheck/scripts/sxc.py data-rules --config examples/data-rules/crosscheck.toml
```

Results go to `examples/data-rules/.sxc/`.

## What to expect

- `build` creates one lookup view per rule and the subjects view in `sxc_out`: the 15 `rule_view:<id>` checks and
  `rule_subjects:view` pass.
- `data-rules` exits 1. Every `rule_xcheck:<id>`, `rule_example:<id>` and `rule_lookup:<id>` passes, and so does
  `rule_subjects:lookup`: the PostgreSQL and Python evaluations agree, both match the authored examples, and the views
  return exactly the agreed rows. The fixture plants a defect for every rule, so the six `violation` rules fail:
  `rule:qty_over_limit`, `rule:parent_unknown`, `rule:kit_without_bom`, `rule:retired_child_in_active_parent`,
  `rule:all_children_retired` and `rule:retired_assembly_in_use`. The nine `warning` rules pass and report their
  `violations` counts.
- The keys are in the database, not in the result. Look at them there:

  ```sql
  SELECT subject, severity, rule_count, rules FROM sxc_out.v_rules_subjects ORDER BY ord;
  SELECT * FROM sxc_out.v_rule_kit_without_bom ORDER BY ord;
  ```

  `stored_level_drift` names no subject, so it does not feed `v_rules_subjects`; its rows are in
  `v_rule_stored_level_drift`. A passing lookup certifies the rows both evaluations agreed on, not the rule's
  verdict, so read `rule:<id>` before acting on a rule's rows.

## Things to try

- `--rule kit_without_bom` checks one rule: `scope:rules` is `not_run`, and so is `rule_subjects:lookup`
  (`excluded_by_option`), so the run cannot pass.
- Change a constant of one rule in `data-rules.toml` and rerun only `data-rules`: that rule's lookup is `view_stale`
  until `build` runs again, and so is `rule_subjects:lookup` when the rule names a subject; the other lookups still
  pass.
- Change an `expect` row: `rule_example:<id>` fails with `common_mode` (both evaluations agree, the authored rows
  differ) and `rule:<id>` becomes `not_run` (`example_mismatch`), while its lookup still passes. Check the example
  against the business rule first.
- Set `views = false`: build creates no rule views, the lookups are not checked, and a `not_proven` line says so. The
  views an earlier build created stay in `sxc_out` and still return rows: drop `v_rules_subjects` and then the
  `v_rule_*` views by hand when you no longer want them.

Agreement of the two evaluations and of the examples shows that the rules do what their examples say on this data;
it does not show that they are the right business rules for other data.

# Contributing

Thank you for helping. semantic-crosscheck is a verification harness, so a change is only as good as the evidence
that it still catches what it should.

## Development setup

- Python 3.11 or later: `python -m pip install -r requirements/reason.txt` (it includes `requirements/core.txt`).
- Unit tests need nothing else: `python -m unittest discover -s tests -v`.
- Integration tests need a throw-away PostgreSQL 17 server and a superuser DSN in `SXC_TEST_ADMIN_DSN`
  (they create and drop databases and the roles `xc_owner` and `xc_check`). OWL tests need Java 11 or later
  (`SXC_JAVA` or `java` on `PATH`); Cube tests need an adapter installed with `sxc.py cube-install`
  (`SXC_CUBE_HOME` when it is not in the default location). Tests skip what they cannot run.

## Rules for changes

- Results and console output carry counts and ids only: never row values, secrets or local paths.
- Fail closed: a check that could not run is `not_run`, never `pass`; anything a run cannot prove goes to
  `not_proven`.
- Every fix of a false pass comes with a test that fails without the fix.
- Keep `docs/design.md`, `README.md`, `SKILL.md`, `commands/` and `references/` in step with the code;
  `tests/test_packaging.py` and the docs tests check part of that.
- Use synthetic data only (the demo vocabulary: part, bom_line, sales_order, stock).

## Before you push

- Enable the hooks: `git config core.hooksPath .githooks`. pre-commit scans staged files and pre-push scans
  the history with `tools/leak_scan.py` (secrets, e-mail addresses outside `tools/leak-allow.txt`, and an
  optional private deny-list named by `SXC_DENYLIST`).
- Commit with an address listed in `tools/leak-allow.txt` or a GitHub noreply address; CI scans the full
  history.

## Reporting security issues

See [SECURITY.md](SECURITY.md); do not open a public issue for a vulnerability.

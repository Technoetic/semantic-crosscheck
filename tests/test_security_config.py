"""crosscheck.toml validation that protects credentials and turns wrong types into configuration errors.

- The *_dsn_env keys hold the NAME of an environment variable. A DSN pasted there (the natural reaction to "use
  dsn_env") used to be accepted and echoed into results, reports and the console, password included; it is now a
  configuration error that never repeats the value.
- Numbers are type-checked with the entry named, so `statement_timeout_ms = "2 min"` is exit 2 instead of a crash in
  a later stage, and 0 can no longer switch a timeout off.
- The never-implemented [output] table is refused, unknown top-level tables are refused (a misspelt [cube] would
  silently drop its settings), and a UTF-8 BOM is accepted.
No database is needed.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import pgfixture
from semantic_crosscheck import config
from semantic_crosscheck.result import EXIT_CONFIG, StageError

DEMO = pgfixture.DEMO_CONFIG.read_text(encoding="utf-8")
SECRET = "Hunter2TopSecret"
DSN = f"postgresql://xc_check:{SECRET}@db.example.org:5432/warehouse"


def set_key(text: str, section: str, key: str, value: str) -> str:
    """text with `key = value` (a TOML literal) in [section]: an existing line is replaced, otherwise the line is
    added, with the section when it is missing."""
    lines = text.split("\n")
    header = f"[{section}]"
    if header not in lines:
        return f"{text}\n{header}\n{key} = {value}\n"
    start = lines.index(header) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("[")), len(lines))
    for i in range(start, end):
        if lines[i].split("=", 1)[0].strip() == key:
            lines[i] = f"{key} = {value}"
            return "\n".join(lines)
    lines.insert(start, f"{key} = {value}")
    return "\n".join(lines)


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sxc_t_sec_config_"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        shutil.copytree(pgfixture.DEMO_CONFIG.parent / "gold", self.dir / "gold")

    def write(self, text: str, encoding: str = "utf-8") -> Path:
        path = self.dir / "crosscheck.toml"
        path.write_text(text, encoding=encoding)
        return path

    def refused(self, text: str) -> str:
        with self.assertRaises(StageError) as caught:
            config.load(self.write(text))
        self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)
        return str(caught.exception)


class DsnEnvTest(ConfigCase):
    def test_a_dsn_in_a_dsn_env_key_is_refused_without_echo(self):
        for section, key in (("source", "dsn_env"), ("source", "owner_dsn_env"), ("cube", "dsn_env")):
            with self.subTest(entry=f"{section}.{key}"):
                msg = self.refused(set_key(DEMO, section, key, json.dumps(DSN)))
                self.assertIn(f"[{section}.{key}]", msg)
                self.assertIn("NAME of an environment variable", msg)
                self.assertIn("looks like a DSN", msg)
                for part in (SECRET, "xc_check", "db.example.org", "warehouse", "postgresql://"):
                    self.assertNotIn(part, msg)

    def test_names_must_be_plain_variable_names(self):
        for bad in ("SXC PG DSN", "1DSN", "", "user@host", "SXC_PG_DSN\n", "${SXC_PG_DSN}", "%SXC_PG_DSN%"):
            with self.subTest(value=bad):
                msg = self.refused(set_key(DEMO, "source", "dsn_env", json.dumps(bad)))
                self.assertIn("[source.dsn_env]", msg)
                self.assertIn("NAME of an environment variable", msg)
        self.assertIn("NAME of an environment variable", self.refused(set_key(DEMO, "source", "dsn_env", "5")))
        cfg = config.load(self.write(set_key(DEMO, "cube", "dsn_env", '"My_Cube_Dsn2"')))
        self.assertEqual(cfg.cube_dsn_env(), "My_Cube_Dsn2")

    def test_credential_keys_point_at_an_environment_variable(self):
        for section, key, value in (("source", "dsn", DSN), ("source", "password", SECRET), ("cube", "dsn", DSN),
                                    ("cube", "db_password", SECRET)):
            with self.subTest(entry=f"{section}.{key}"):
                msg = self.refused(set_key(DEMO, section, key, json.dumps(value)))
                self.assertIn(f"[{section}.{key}]", msg)
                self.assertIn("put the DSN in an environment variable", msg)
                self.assertIn("NAME of that variable", msg)
                self.assertNotIn(SECRET, msg)


class NumbersTest(ConfigCase):
    def test_wrong_types_and_ranges_name_the_entry(self):
        cases = [
            ("source", "statement_timeout_ms", '"2 min"'), ("source", "statement_timeout_ms", "0"),
            ("source", "statement_timeout_ms", "999"), ("source", "statement_timeout_ms", "60000.0"),
            ("cube", "startup_timeout_s", '"90"'), ("cube", "startup_timeout_s", "0"),
            ("cube", "query_timeout_s", "-1"), ("cube", "query_timeout_s", "true"),
            ("cube", "port", "65536"), ("cube", "port", "-1"), ("cube", "port", '"eighty"'),
            ("verify", "sweep_k", "0"), ("verify", "seed", '"7"'), ("verify", "mutant_bindings", "1.5"),
            ("verify", "shacl_max_rows", "0"), ("verify", "owl_max_individuals", "false"),
        ]
        for section, key, value in cases:
            with self.subTest(entry=f"{section}.{key}", value=value):
                msg = self.refused(set_key(DEMO, section, key, value))
                self.assertIn(f"[{section}.{key}]", msg)
                self.assertIn("must be an integer", msg)

    def test_max_depth_is_an_integer_in_range(self):
        for value in ("0", "65", '"32"', "8.0"):
            with self.subTest(value=value):
                msg = self.refused(set_key(DEMO, "hierarchies.has_component", "max_depth", value))
                self.assertIn("[hierarchies.has_component.max_depth]", msg)
                self.assertIn("must be an integer 1..64", msg)

    def test_boundary_values_load(self):
        text = set_key(DEMO, "source", "statement_timeout_ms", "1000")
        for key, value in (("port", "65535"), ("startup_timeout_s", "1"), ("query_timeout_s", "1")):
            text = set_key(text, "cube", key, value)
        text = set_key(text, "verify", "seed", "-3")
        cfg = config.load(self.write(text))
        self.assertEqual((cfg.source.statement_timeout_ms, cfg.cube.port, cfg.cube.query_timeout_s, cfg.verify.seed),
                         (1000, 65535, 1, -3))

    def test_mutation_operator_list_shape(self):
        for value in ('"all"', "[3]", '"closure_depth_shift"'):
            with self.subTest(value=value):
                msg = self.refused(set_key(DEMO, "verify", "mutation_operators", value))
                self.assertIn("[verify.mutation_operators]", msg)
        cfg = config.load(self.write(set_key(DEMO, "verify", "mutation_operators", "[]")))
        self.assertEqual(cfg.verify.mutation_operators, [])


class TablesAndEncodingTest(ConfigCase):
    def test_output_table_is_refused_as_removed(self):
        msg = self.refused(set_key(DEMO, "output", "show_rows", "0"))
        self.assertIn("[output]", msg)
        self.assertIn("was removed", msg)
        self.assertFalse(hasattr(config.load(self.write(DEMO)), "output"))

    def test_unknown_top_level_keys_are_refused(self):
        msg = self.refused(set_key(DEMO, "cub", "dsn_env", '"OTHER_DSN"'))
        self.assertIn("[cub]", msg)
        self.assertIn("unknown top-level key", msg)

    def test_a_byte_order_mark_is_accepted(self):
        cfg = config.load(self.write(DEMO, encoding="utf-8-sig"))
        self.assertEqual(cfg.cube.version, config.CUBE_VERSION)

    def test_missing_file_message_has_no_directory(self):
        with self.assertRaises(StageError) as caught:
            config.load(self.dir / "nested" / "absent.toml")
        self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)
        self.assertEqual(str(caught.exception), "config not found: absent.toml")


if __name__ == "__main__":
    unittest.main()

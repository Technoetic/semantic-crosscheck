"""Captured configuration bytes retain file-loader validation without content rereads."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pgfixture
from semantic_crosscheck import config
from semantic_crosscheck.result import EXIT_CONFIG, StageError


CONFIG = b'''version = 1
[source]
schemas = ["synthetic"]
data_revision_sql_file = "revision.sql"
[entities.item]
table = "synthetic.item"
key = ["id"]
[measures.stock]
entity = "item"
column = "qty"
agg = "sum"
by = "id"
[[gold]]
id = "q01"
sql = "query.sql"
key = "id"
[gold.cube_query]
dimensions = ["item.id"]
measures = ["m_stock.value"]
[gold.map]
id = "item.id"
qty = "m_stock.value"
'''


class ConfigBytesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sxc-config-bytes-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "crosscheck.toml"
        self.gold = self.root / "query.sql"
        self.gold.write_bytes(b"SQL contents are not configuration inputs.")

    def parse(self, raw, path=None):
        parser = getattr(config, "loads", None)
        self.assertTrue(callable(parser), "captured bytes need config.loads(raw, path)")
        return parser(raw, self.path if path is None else path)

    def test_captured_configuration_does_not_need_a_toml_file(self):
        self.assertFalse(self.path.exists())
        parsed = self.parse(CONFIG, str(self.path))
        self.assertEqual(parsed.path, self.path.resolve())
        self.assertEqual(parsed.measures["stock"].agg, "sum")
        self.assertEqual(parsed.measures["stock"].by, "id")
        self.assertEqual(parsed.gold[0].map, {"id": "item.id", "qty": "m_stock.value"})

    def test_byte_parser_does_not_reopen_configuration_gold_or_revision(self):
        self.path.write_bytes(b"configuration changed after capture")
        (self.root / "revision.sql").write_bytes(b"Revision contents must not be read.")
        with patch.object(Path, "open", side_effect=AssertionError("content reread")):
            parsed = self.parse(CONFIG)
        self.assertEqual(parsed.measures["stock"].agg, "sum")
        self.assertEqual(parsed.source.data_revision_sql_file, "revision.sql")

    def test_bom_bytes_match_the_existing_file_loader(self):
        raw = b"\xef\xbb\xbf" + CONFIG
        self.path.write_bytes(raw)
        parsed = self.parse(raw)
        self.assertEqual(parsed, config.load(self.path))
        self.assertEqual(parsed.source.schemas, ["synthetic"])

    def test_missing_declared_gold_sql_is_still_a_configuration_error(self):
        self.gold.unlink()
        with self.assertRaises(StageError) as caught:
            self.parse(CONFIG)
        self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)
        self.assertIn("gold SQL file not found", str(caught.exception))

    def test_existing_semantic_validation_errors_match_the_file_loader(self):
        cases = [
            CONFIG.replace(b"version = 1", b"version = 2"),
            CONFIG.replace(b'key = ["id"]', b'key = []', 1),
            CONFIG + b"\n[cube]\nport = true\n",
            CONFIG + b"\n[unexpected]\nvalue = 1\n",
            CONFIG.replace(b'schemas = ["synthetic"]', b'schemas = ["synthetic"]\ndsn = "private-value"'),
        ]
        for raw in cases:
            with self.subTest(raw_length=len(raw)):
                self.path.write_bytes(raw)
                with self.assertRaises(StageError) as legacy:
                    config.load(self.path)
                with self.assertRaises(StageError) as captured:
                    self.parse(raw)
                self.assertEqual(captured.exception.exit_code, EXIT_CONFIG)
                self.assertEqual(str(captured.exception), str(legacy.exception))

    def test_malformed_toml_and_invalid_utf8_are_configuration_errors(self):
        for raw in (b"version = [", b"\xff"):
            with self.subTest(raw_length=len(raw)):
                with self.assertRaises(StageError) as caught:
                    self.parse(raw)
                self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)
                self.assertIn("crosscheck.toml", str(caught.exception))

    def test_only_bytes_are_accepted_by_the_captured_input_api(self):
        for raw in (CONFIG.decode("utf-8"), bytearray(CONFIG), None, 1):
            with self.subTest(input_type=type(raw).__name__):
                with self.assertRaises(StageError) as caught:
                    self.parse(raw)
                self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)

    def test_file_loader_still_requires_its_configuration_file(self):
        self.assertFalse(self.path.exists())
        with self.assertRaises(StageError) as caught:
            config.load(self.path)
        self.assertEqual(caught.exception.exit_code, EXIT_CONFIG)
        self.assertEqual(str(caught.exception), "config not found: crosscheck.toml")


if __name__ == "__main__":
    unittest.main()

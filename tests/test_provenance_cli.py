"""A stage verdict cannot bypass its start/end revision guard at CLI dispatch."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pgfixture
import sxc
from semantic_crosscheck import provenance
from semantic_crosscheck.result import Result, StageError


class ProvenanceCLI(unittest.TestCase):
    def test_changed_revision_overrides_a_passing_stage_before_the_result_is_written(self):
        events = []

        def begin(cfg, stage):
            events.append("begin")
            self.assertEqual(stage, "build")
            return {"binding": "revision"}

        def run(cfg, args, out):
            events.append("run")
            res = Result("build")
            res.add("closure", "pass")
            return res

        def finish(cfg, res, start):
            events.append("finish")
            self.assertEqual(start, {"binding": "revision"})
            res.add("data_revision:stable", "fail", reason="changed")
            return res

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(provenance, "begin", side_effect=begin), \
                 patch.object(provenance, "finish", side_effect=finish), \
                 patch.object(sxc.importlib, "import_module", return_value=SimpleNamespace(run=run)), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = sxc.main(["build", "--config", str(pgfixture.DEMO_CONFIG), "--out", tmp])
            self.assertEqual(rc, 1)
            self.assertEqual(events, ["begin", "run", "finish"])
            saved = json.loads((Path(tmp) / "build" / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "fail")

    def test_failed_start_probe_does_not_execute_stage_or_preserve_previous_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Result("build")
            old.add("closure", "pass")
            old.write(tmp)
            run = unittest.mock.Mock(return_value=old)
            with patch.object(provenance, "begin", side_effect=StageError("revision probe unavailable", 3)), \
                 patch.object(sxc.importlib, "import_module", return_value=SimpleNamespace(run=run)), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = sxc.main(["build", "--config", str(pgfixture.DEMO_CONFIG), "--out", tmp])
            self.assertEqual(rc, 3)
            run.assert_not_called()
            saved = json.loads((Path(tmp) / "build" / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "error")


if __name__ == "__main__":
    unittest.main()

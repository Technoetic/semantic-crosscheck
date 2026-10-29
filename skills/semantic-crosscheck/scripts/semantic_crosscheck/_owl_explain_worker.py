"""Private, pipe-gated same-HermiT worker. No source keys, database access or diagnostics leave this process."""

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


def main():
    # Read before importing the reasoner: parent assigns a Windows kill-on-close job before sending this payload.
    # A pipe EOF (failed containment) cannot start HermiT.
    raw = sys.stdin.buffer.read(16 * 1024 * 1024 + 1)
    if not raw or len(raw) > 16 * 1024 * 1024:
        return 1
    model = None
    try:
        payload = json.loads(raw)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            from semantic_crosscheck import owl, owl_explain
            o = SimpleNamespace(**payload["ontology"])
            o.defined = [SimpleNamespace(**item) for item in o.defined]
            h = SimpleNamespace(**payload["hierarchy"])
            cfg = SimpleNamespace(ontology=o, hierarchies={h.name: h})
            graph = owl.Graph({key: set(values) for key, values in payload["nodes"]},
                              [tuple(pair) for pair in payload["edges"]])
            if owl_explain._group_manifest(cfg, graph) != payload["groups"]:
                return 1
            tempfile.tempdir = os.getcwd()
            # Positions belong to the parent-sorted source snapshot. Do not sort encoded rows again: even though
            # ordinals follow the global source-key order, Python-equal numeric aliases can have different raw
            # type sort keys in edge occurrences (e.g. 2 versus 2.0). Preserve their exact group correspondence.
            model = owl.build(cfg, graph, active_axioms=set(payload["active"]), _axiom_ordered=True)
            consistent, _ms = owl.hermit(model.world, payload["java"])
        sys.stdout.buffer.write(b"consistent\n" if consistent else b"inconsistent\n")
        return 0
    except Exception:
        return 1
    finally:
        if model is not None:
            with contextlib.suppress(Exception):
                model.world.close()


if __name__ == "__main__":
    raise SystemExit(main())

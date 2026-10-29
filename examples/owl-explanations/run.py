"""Run either authored synthetic inconsistency without PostgreSQL; output safe diagnostic facts only."""

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills" / "semantic-crosscheck" / "scripts"))

from semantic_crosscheck import config, owl, owl_explain  # noqa: E402
from semantic_crosscheck.result import Result  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("no_children", "disjoint"), default="no_children")
    parser.add_argument("--timeout-ms", type=int, default=60000)
    args = parser.parse_args()
    java = owl.java_executable()
    if not java:
        print("Java is required; set SXC_JAVA or add Java to PATH")
        return 3
    cfg = config.load(ROOT / "demo" / "crosscheck.toml")
    cfg.ontology.classes = {"product": "Product", "raw": "RawMaterial"}
    cfg.ontology.defined = []
    graph = owl.Graph({"synthetic_parent": {"raw"}, "synthetic_leaf": set()},
                      [("synthetic_parent", "synthetic_leaf")])
    if args.case == "disjoint":
        graph.nodes["synthetic_parent"] = {"raw", "product"}
        graph.edges = []
    baseline = owl.reason(cfg, graph, java)
    result = Result("reason")
    result.add("owl:consistency", "pass" if baseline.consistent else "fail", "ontology",
               individuals=baseline.individuals, edges=baseline.edges)
    if baseline.consistent:
        print("Synthetic inconsistency was not reproduced")
        return 1
    owl_explain.run(cfg, graph, java, SimpleNamespace(explain_timeout_ms=args.timeout_ms), result)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.checks[-1].status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

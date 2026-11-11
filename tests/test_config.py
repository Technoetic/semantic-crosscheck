"""[verify] accepted_gaps: known gaps of authored evidence, named so they stop being reported as new.

Why: an entry that names nothing of this configuration would silently accept nothing (or, after a rename, hide a
real gap), so every entry must name a measure, hierarchy or ontology.class_column of the configuration, one of their
facets, or a V4 check of a known operator on a target it applies to; anything else is a configuration error (exit 2)
whose message does not repeat the entry.
"""

import unittest

import pgfixture
from semantic_crosscheck import config
from semantic_crosscheck.result import EXIT_CONFIG, StageError

DEMO = pgfixture.DEMO_CONFIG.read_bytes()


def demo_with(line: str, base: bytes = DEMO) -> config.Config:
    raw = base.replace(b"mutant_bindings = 3\n", b"mutant_bindings = 3\n" + line.encode("utf-8") + b"\n", 1)
    assert raw != base
    return config.loads(raw, pgfixture.DEMO_CONFIG)


class AcceptedGapsTest(unittest.TestCase):
    def test_default_is_empty(self):
        self.assertEqual(config.loads(DEMO, pgfixture.DEMO_CONFIG).verify.accepted_gaps, [])

    def test_known_names_are_accepted(self):
        names = ["measures.on_hand", "hierarchies.has_component", "ontology.class_column", "part+m_firm_open",
                 "m_firm_open_dated.ts", "has_component_closure+m_firm_open", "has_component_descendant.*",
                 "has_component_closure.pairs", "part.part_class", "v4:measure_agg_to_max:on_hand",
                 "v4:closure_end_swap:has_component", "v4:dated_shift_one_day:firm_open_dated",
                 "v4:closure_join_swap:firm_open"]
        cfg = demo_with("accepted_gaps = [" + ", ".join(f'"{n}"' for n in names) + "]")
        self.assertEqual(cfg.verify.accepted_gaps, names)

    def test_bad_values_are_configuration_errors(self):
        bad = ['accepted_gaps = "measures.on_hand"', "accepted_gaps = [1]", 'accepted_gaps = ["measures.nope"]',
               'accepted_gaps = ["hierarchies.nope"]', 'accepted_gaps = ["m_nope.value"]',
               'accepted_gaps = ["m_on_hand.ts"]', 'accepted_gaps = ["part+m_firm_open_dated"]',
               'accepted_gaps = ["v4:nope:on_hand"]', 'accepted_gaps = ["v4:measure_agg_to_max:nope"]',
               'accepted_gaps = ["v4:dated_shift_one_day:on_hand"]', 'accepted_gaps = ["v4:measure_agg_to_max"]',
               'accepted_gaps = ["ontology.classes"]', 'accepted_gaps = ["SECRET-ENTRY"]', 'accepted_gaps = [""]']
        for line in bad:
            with self.subTest(line=line):
                with self.assertRaises(StageError) as e:
                    demo_with(line)
                self.assertEqual(e.exception.exit_code, EXIT_CONFIG)
                self.assertIn("verify.accepted_gaps", str(e.exception))
                self.assertNotIn("SECRET-ENTRY", str(e.exception))
                self.assertNotIn("nope", str(e.exception), "the entry is not repeated")

    def test_class_column_needs_one(self):
        without = DEMO.replace(b'class_column = "part_class"\n', b"", 1)
        self.assertNotEqual(without, DEMO)
        for line in ('accepted_gaps = ["ontology.class_column"]', 'accepted_gaps = ["part.part_class"]'):
            with self.subTest(line=line):
                with self.assertRaises(StageError) as e:
                    demo_with(line, without)
                self.assertEqual(e.exception.exit_code, EXIT_CONFIG)


if __name__ == "__main__":
    unittest.main()

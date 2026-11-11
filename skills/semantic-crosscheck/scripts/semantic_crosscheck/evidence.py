"""Evidence kinds: what the two sides of a check are, for every check id of the pipeline stages and report.

A check's kind says who or what produced each side, not whether a side is right:

  authored      one side is the rows of a gold SQL file a person wrote, with no generated text in it
  recomputed    one side is recomputed from source rows by code that imports no generator module
  generated     both sides come from crosscheck.toml through generators, or one generated layer meets itself
  structural    one side only: a rule on the data, the configuration, the environment, the served model, the scope
  mutation      a V4 mutant check (what detected the mutant is recorded separately)
  unclassified  no rule matched; never counted as authored or recomputed

RULES is ordered and the first matching rule wins. There is no default row: an id no rule names is unclassified. An
id pattern is a tuple of id segments (a literal, a `<placeholder>` for one segment, or `*` for one or more segments),
so no pattern here is an id-shaped string; render_table() writes them the way docs/design.md section 6 does, and a
unit test keeps the table in docs/design.md section 8 equal to it.

The only kind a stage records is the kind of a gold query, which applies to `v1:<gold>` and `v2:<gold>:<stratum>`.
It can lower authored to generated, never raise anything (check_kind). This module imports no engine module.
"""

from dataclasses import dataclass

KINDS = ("authored", "recomputed", "generated", "structural", "mutation", "unclassified")
RULE_VERSION = 1
# A side that imports one of these at module level is not recomputed.
GENERATOR_MODULES = ("cube_gen", "closure", "shapes", "shape_q", "mutate", "owl")
ANY_STAGE = "any"
WILDCARD = "*"
GOLD_KINDS = ("authored", "generated")


@dataclass(frozen=True)
class Rule:
    """One registry row.

    stages: stage names as the command line spells them, or ("any",) for every stage in STAGES.
    ids: id patterns (tuples of segments); None means every id of these stages.
    recorded: the kind of a gold query recorded by verify may lower this row's kind (check_kind).
    module: for a recomputed row, the module that computes its independent side.
    """
    stages: tuple
    ids: tuple | None
    kind: str
    why: str
    note: str = ""
    recorded: bool = False
    module: str = ""


RULES = (
    Rule((ANY_STAGE,),
         (("stage",), ("data_revision", "stable"), ("data_revision", "current"), ("runtime_evidence", "stable"),
          ("runtime_evidence", "current")),
         "structural", "the stage run and its data and runtime revisions: one side"),
    Rule(("doctor", "demo", "cube", "semantic-preflight", "report"), None,
         "structural", "the environment, the demo load, the served model, an offline preflight, report bookkeeping: "
                       "one side"),
    Rule(("build",), (("shape", "<id>"), ("closure", "<h>"), ("rule_view", "<id>"), ("rule_subjects", "view")),
         "structural", "a rule on the source data, or the build's own outcome: one side"),
    Rule(("build",), (("closure_apply", "<h>"),),
         "generated", "the closure table against a recomputation by the same builder in the same transaction"),
    Rule(("reason",),
         (("shacl", "meta", "generated"), ("owl", "consistency"), ("owl", "explanation"), ("scope", "shacl"),
          ("scope", "owl"), ("v5", "witness", "<name>")),
         "structural", "the generated shapes' syntax, the ontology's consistency, parts left out, and synthetic "
                       "graphs the engine wrote: one side, not this data"),
    Rule(("reason",), (("shacl", "pair", "<id>"), ("shacl", "<id>"), ("v5", "defined", "<name>"),
                       ("v5", "closure", "<h>")),
         "generated", "both sides come from one shape entry or one ontology definition; the OWL closure is built by "
                      "the ontology generator and compared with the generated closure table (a second engine is "
                      "not an independent side)"),
    Rule(("verify",), (("v1", "<gold>"), ("v2", "<gold>", "<stratum>")),
         "authored", "one side is the rows of a gold SQL file a person wrote; authored says who wrote it, not that "
                     "it is right", note="generated when the gold SQL contains generated text, unclassified without "
                                         "a recorded gold kind", recorded=True),
    Rule(("verify",), (("v3", "closure_oracle", "<h>"),),
         "recomputed", "the raw edges recomputed in Python by code that imports no generator module",
         module="closure_oracle"),
    Rule(("verify",), (("v3", WILDCARD), ("v3w", WILDCARD)),
         "generated", "Cube against SQL built from the configuration by generator code (the measure filter, the "
                      "closure table), or Cube against Cube"),
    Rule(("verify",),
         (("v4", "suite"), ("scope", "v1"), ("scope", "v2"), ("scope", "v3"), ("scope", "v4"),
          ("scope", "gold", "<gold>"), ("gold_evidence", "<element>")),
         "structural", "parts or gold queries a narrowed run left out, the suite on the unmutated model, and the "
                       "authored evidence of a configuration element: one side"),
    Rule(("verify",), (("v4", "<operator>", "<target>"),),
         "mutation", "a broken copy of the generated model served to the suite; pass means a suite item failed "
                     "against it"),
)
STAGES = frozenset(s for rule in RULES for s in rule.stages if s != ANY_STAGE)


def _stage(stage: str) -> str:
    return stage.replace("_", "-")


def _matches(pattern: tuple, segments: list) -> bool:
    if not all(segments):
        return False
    if pattern and pattern[-1] == WILDCARD:
        head = pattern[:-1]
        if len(segments) <= len(head):
            return False
    elif len(segments) != len(pattern):
        return False
    else:
        head = pattern
    for want, got in zip(head, segments):
        if not want.startswith("<") and want != got:
            return False
    return True


def rule_of(stage: str, check_id: str) -> Rule | None:
    """The first rule naming this check id in this stage, or None (the id is unclassified)."""
    stage = _stage(stage)
    if stage not in STAGES or not isinstance(check_id, str):
        return None
    segments = check_id.split(":")
    for rule in RULES:
        if ANY_STAGE not in rule.stages and stage not in rule.stages:
            continue
        if rule.ids is None or any(_matches(p, segments) for p in rule.ids):
            return rule
    return None


def kind_of(stage: str, check_id: str) -> str:
    """The registry kind of a check id (a gold query's recorded kind is applied by check_kind)."""
    rule = rule_of(stage, check_id)
    return rule.kind if rule else "unclassified"


def gold_kinds(entries) -> tuple[dict, int]:
    """Gold id -> authored or generated from the gold entries a verify result recorded, and how many were overridden.

    authored only when the recorded kind is authored and generated_text is an empty list; generated otherwise. An
    entry recorded above what that allows (authored with generated text, or a value other than authored and
    generated) counts as generated and as overridden. Entries without a string id are left out, so their checks stay
    unclassified. A gold id recorded twice takes the lower kind.
    """
    kinds, overridden = {}, 0
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        recorded, text = entry.get("kind"), entry.get("generated_text")
        if recorded == "authored" and isinstance(text, list) and not text:
            kind = "authored"
        else:
            kind = "generated"
            overridden += recorded != "generated"
        kinds[entry["id"]] = "generated" if kinds.get(entry["id"]) == "generated" else kind
    return kinds, overridden


def check_kind(stage: str, check_id: str, golds: dict) -> str:
    """The kind a check counts as: the registry kind, lowered for v1/v2 by the gold kinds of gold_kinds().

    v1:<gold> and v2:<gold>:<stratum> take the gold's kind (generated for a value other than authored), and are
    unclassified when the gold has none. Every other check keeps its registry kind whatever golds says.
    """
    rule = rule_of(stage, check_id)
    if rule is None:
        return "unclassified"
    if not rule.recorded:
        return rule.kind
    recorded = golds.get(check_id.split(":")[1]) if isinstance(golds, dict) else None
    if recorded is None:
        return "unclassified"
    return "authored" if recorded == "authored" else "generated"


# ---- V4 suite items --------------------------------------------------------------------------------
def _gold_of(item_id: str) -> str | None:
    """The gold id of a gold suite item (v1:<gold> or v2:<gold>:<stratum>[<j>]), None for any other item."""
    segments = item_id.split(":") if isinstance(item_id, str) else []
    if segments and ((segments[0] == "v1" and len(segments) == 2) or (segments[0] == "v2" and len(segments) == 3)) \
            and all(segments[1:]):
        return segments[1]
    return None


def item_kind(item_id: str, golds: dict) -> str:
    """The kind of a V4 suite item: a gold item takes its gold's stored kind (unclassified when none is stored),
    a v3 item (chunk and null suffixes included) is generated, anything else is unclassified."""
    gold = _gold_of(item_id)
    if gold is not None:
        kind = golds.get(gold)
        return kind if kind in GOLD_KINDS else "unclassified"
    segments = item_id.split(":") if isinstance(item_id, str) else []
    if len(segments) > 1 and segments[0] == "v3" and all(segments[1:]):
        return "generated"
    return "unclassified"


def suite_rank(item_id: str, golds: dict) -> int:
    """0: authored gold items and gold items of unknown kind; 1: generated gold items; 2: every other item."""
    if _gold_of(item_id) is None:
        return 2
    return 1 if item_kind(item_id, golds) == "generated" else 0


def order_suite(items, golds: dict, key=lambda item: item[0]) -> list:
    """The suite items as a new list, stably sorted by suite_rank of key(item) (the item id).

    Authored gold items run first, so the first failing item tells whether a gold item detected a mutant. Without a
    generated gold the planned order (gold items, then v3 items) is unchanged.
    """
    return sorted(items, key=lambda item: suite_rank(key(item), golds))


# ---- facets ----------------------------------------------------------------------------------------
# A facet is a part of a measure, hierarchy or class dimension that a gold query can compare. Its id names the
# generated Cube member it stands for, a whole cube (`<cube>.*`) or a join (`<cube>+<measure cube>`); the names follow
# naming.py (a unit test keeps them equal). The generated model holds no other joins: entity cubes have none of their
# own, a node entity joins its measures and the closure joins its ends and the measures declared with closure_join.
MUTATION_OPERATORS = ("closure_depth_shift", "closure_path_count_one", "closure_qty_single_path", "measure_filter_drop",
                      "measure_normalization_drop", "measure_agg_to_max", "dated_shift_one_day", "closure_join_swap",
                      "closure_end_swap")
FACET_REASONS = ("not_in_model", "no_values", "no_rows_compared", "gold_not_run", "generated_text", "filter_only",
                 "no_gold_reads")
CLASS_ELEMENT = "ontology.class_column"
_HIERARCHY_OPERATORS = {"closure_qty_single_path": "qty", "closure_path_count_one": "path_count",
                        "closure_depth_shift": "min_depth"}
_MEASURE_OPERATORS = {   # operator -> (applies to the measure, facet member of the measure cube)
    "measure_filter_drop": (lambda m: bool(m.filter), "value"),
    "measure_normalization_drop": (lambda m: bool(m.filter_normalized), "value"),
    "measure_agg_to_max": (lambda m: m.agg in ("sum", "count"), "value"),
    "dated_shift_one_day": (lambda m: bool(m.date_column), "ts"),
    "closure_join_swap": (lambda m: bool(m.closure_join), ""),
}


@dataclass(frozen=True)
class Facet:
    """kind member: compared when `member` is compared; cube: any member of `cube`; join: a member of `cube` together
    with `member` (the measure value). `cubes` must all be in the model, otherwise the facet's reason is not_in_model.
    Rows count as holding values through `member` (member, join) or any compared member of `cube` (cube)."""
    element: str
    id: str
    kind: str
    cube: str
    member: str
    cubes: tuple

    def compared(self, members) -> bool:
        cubes = {m.split(".", 1)[0] for m in members}
        if self.kind == "member":
            return self.member in members
        if self.kind == "cube":
            return self.cube in cubes
        return self.cube in cubes and self.member in members

    def value_members(self, members) -> list:
        """The members whose value_rows tell whether compared rows held a value of this facet."""
        if self.kind == "cube":
            return sorted(m for m in members if m.split(".", 1)[0] == self.cube)
        return [self.member] if self.member in members else []


def _measure_cube(m: str) -> str:
    return f"m_{m}"


def _closure(h: str) -> str:
    return f"{h}_closure"


def _only_hierarchy(cfg) -> str:
    return next(iter(cfg.hierarchies)) if len(cfg.hierarchies) == 1 else ""


def facets(cfg, model: dict | None = None) -> list[Facet]:
    """Every facet of the configuration, by element: measures, hierarchies, then ontology.class_column.

    The class facets are `<entity>.<class_column>` for every entity cube of the model whose dimensions hold the class
    column; without a model (configuration validation) for every entity."""
    out = []
    for m in cfg.measures.values():
        el, mc = f"measures.{m.name}", _measure_cube(m.name)
        value = f"{mc}.value"
        out.append(Facet(el, value, "member", mc, value, (mc,)))
        if m.date_column:
            out.append(Facet(el, f"{mc}.ts", "member", mc, f"{mc}.ts", (mc,)))
        h = _only_hierarchy(cfg)
        if m.closure_join and h:
            c = _closure(h)
            out.append(Facet(el, f"{c}+{mc}", "join", c, value, (c, mc)))
        node = cfg.entities.get(m.node) if m.node else None
        if node is not None and not m.date_column and len(node.key) == 1:
            out.append(Facet(el, f"{m.node}+{mc}", "join", m.node, value, (m.node, mc)))
    for h in cfg.hierarchies:
        el, c = f"hierarchies.{h}", _closure(h)
        for name in ("qty", "path_count", "min_depth", "max_depth", "pairs"):
            out.append(Facet(el, f"{c}.{name}", "member", c, f"{c}.{name}", (c,)))
        for end in (f"{h}_ancestor", f"{h}_descendant"):
            out.append(Facet(el, f"{end}.*", "cube", end, "", (end,)))
    column = cfg.ontology.class_column
    if column:
        dims = None
        if model is not None:
            dims = {c.get("name"): {d.get("name") for d in c.get("dimensions", [])} for c in model.get("cubes", [])}
        for e in cfg.entities:
            if dims is None or column in dims.get(e, set()):
                out.append(Facet(CLASS_ELEMENT, f"{e}.{column}", "member", e, f"{e}.{column}", (e,)))
    return out


def operator_targets(cfg) -> list[tuple[str, str]]:
    """(operator, target) of every V4 mutant the configuration has when every operator is selected, in the order of
    mutate's plan: hierarchy operators target each hierarchy, measure operators each measure they apply to."""
    out = []
    for op in MUTATION_OPERATORS:
        if op in _MEASURE_OPERATORS:
            applies = _MEASURE_OPERATORS[op][0]
            out.extend((op, m.name) for m in cfg.measures.values() if applies(m))
        else:
            out.extend((op, h) for h in cfg.hierarchies)
    return out


def operator_facet(cfg, operator: str, target: str) -> str:
    """The facet a V4 mutant changes, or "" for an unknown operator."""
    if operator in _HIERARCHY_OPERATORS:
        return f"{_closure(target)}.{_HIERARCHY_OPERATORS[operator]}"
    if operator == "closure_end_swap":
        return f"{target}_descendant.*"
    if operator == "closure_join_swap":
        return f"{_closure(_only_hierarchy(cfg))}+{_measure_cube(target)}"
    if operator in _MEASURE_OPERATORS:
        return f"{_measure_cube(target)}.{_MEASURE_OPERATORS[operator][1]}"
    return ""


def accepted_names(cfg) -> set[str]:
    """What verify.accepted_gaps may name: elements (measures.<m>, hierarchies.<h>, ontology.class_column when set),
    facets of the configuration, and V4 check ids of a known operator on a target it applies to."""
    names = {f"measures.{m}" for m in cfg.measures} | {f"hierarchies.{h}" for h in cfg.hierarchies}
    if cfg.ontology.class_column:
        names.add(CLASS_ELEMENT)
    names |= {f.id for f in facets(cfg)}
    names |= {"v4:" + op + ":" + target for op, target in operator_targets(cfg)}
    return names


# ---- documentation ---------------------------------------------------------------------------------
def pattern_text(pattern: tuple) -> str:
    return ":".join(pattern)


def render_table() -> str:
    """The registry as the Markdown table of docs/design.md section 8, "Evidence kinds"."""
    lines = ["| # | Stage | Check ids | Kind | Why |", "|---|---|---|---|---|"]
    for number, rule in enumerate(RULES, 1):
        ids = "every id" if rule.ids is None else ", ".join("`" + pattern_text(p) + "`" for p in rule.ids)
        kind = rule.kind + (" (" + rule.note + ")" if rule.note else "")
        lines.append("| " + " | ".join((str(number), ", ".join(rule.stages), ids, kind, rule.why)) + " |")
    return "\n".join(lines)

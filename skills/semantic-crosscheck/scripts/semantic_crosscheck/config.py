"""crosscheck.toml: the single source of truth for every stage.

The agent edits this file, never the generated SQL/YAML. Validation errors raise StageError(exit 2) and
name the offending entry (config_ref) so failures can be traced back to the configuration.

Credentials never live here: the *_dsn_env keys hold the NAME of an environment variable. A DSN pasted into one of
them is refused without repeating it, because messages reach results, reports and the agent's console. Numbers are
type-checked here so a wrong type is a configuration error naming its entry, not a crash in a later stage.
"""

import re
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import ident
from .result import EXIT_CONFIG, StageError

AGGS = ("sum", "count", "min", "max")
DEFINED_KINDS = ("has_child", "has_parent", "contains_class", "class_with_child", "class_used_as_part")
STRATA = ("top", "mid", "leaf", "leaf_parent", "deep", "shared", "empty")
CUBE_VERSION = "1.8.3"           # the adapter's package-lock and native hashes are pinned to this release
TABLES = ("version", "source", "entities", "hierarchies", "shapes", "measures", "ontology", "cube", "verify", "gold")
_MEMBER = re.compile(r"^[a-z_][a-z0-9_]{0,62}\.[a-z_][a-z0-9_]{0,62}$")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _err(ref: str, msg: str):
    raise StageError(f"crosscheck.toml [{ref}]: {msg}", EXIT_CONFIG)


def _int(ref: str, v, lo: int | None = None, hi: int | None = None) -> int:
    """An integer setting (booleans and floats refused) within [lo, hi]."""
    if lo is not None and hi is not None:
        want = f"an integer {lo}..{hi}"
    else:
        want = "an integer" + (f" >= {lo}" if lo is not None else "")
    if isinstance(v, bool) or not isinstance(v, int):
        _err(ref, f"must be {want} (got {type(v).__name__})")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        _err(ref, f"must be {want}")
    return v


def _env_name(ref: str, v) -> str:
    """The name of the environment variable holding a DSN; the value itself is never echoed (it may be a DSN)."""
    if isinstance(v, str) and _ENV_NAME.fullmatch(v):
        return v
    looks = isinstance(v, str) and ("://" in v or "@" in v)
    _err(ref, "must be the NAME of an environment variable that holds the DSN, for example \"SXC_PG_DSN\""
              + (" (the value looks like a DSN: move it into that environment variable)" if looks else ""))


def _credentials(raw: dict):
    for section in ("source", "cube"):
        table = raw.get(section, {})
        for k in table if isinstance(table, dict) else ():
            if "password" in k.lower() or k.lower() == "dsn":
                _err(f"{section}.{k}", "credentials are never stored in the config: put the DSN in an environment "
                                       "variable and set dsn_env to the NAME of that variable, for example "
                                       "dsn_env = \"SXC_PG_DSN\"")


def _top_level(raw: dict):
    if "output" in raw:
        _err("output", "the [output] table was removed (output.show_rows was never implemented): delete it")
    unknown = sorted(set(raw) - set(TABLES))
    if unknown:
        _err(unknown[0], f"unknown top-level key (expected {', '.join(TABLES)})")


def _known_keys(table: dict, allowed: tuple[str, ...], ref: str):
    """Reject unimplemented options without echoing values or arbitrary quoted key text."""
    if not isinstance(table, dict):
        _err(ref, "must be a table")
    if set(table) - set(allowed):
        _err(ref, f"unknown configuration key (allowed: {', '.join(allowed)})")


def _decimal(ref: str, v) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        _err(ref, f"bound must be a number, got {v!r}")
    try:
        return Decimal(str(v))
    except InvalidOperation:
        _err(ref, f"bound must be a number, got {v!r}")


@dataclass
class Source:
    dsn_env: str = "SXC_PG_DSN"
    owner_dsn_env: str = "SXC_PG_OWNER_DSN"
    schemas: list = field(default_factory=list)
    output_schema: str = "sxc_out"
    statement_timeout_ms: int = 60000


@dataclass
class Entity:
    name: str
    table: str
    key: list


@dataclass
class Hierarchy:
    name: str
    edge: str          # entity holding parent/child rows
    node: str          # entity whose (single-column) key identifies nodes
    parent: str
    child: str
    quantity: str
    max_depth: int = 32
    on_cycle: str = "error"   # error | report


@dataclass
class Shape:
    id: str
    target: str
    path: str = ""
    class_: str = ""                 # referential: value must be a key of this entity
    min_exclusive: object = None     # Decimal after validation
    min_inclusive: object = None
    max_inclusive: object = None
    sql_violation: str = ""          # trusted SQL predicate over alias t; listed in reports as trusted SQL
    severity: str = "violation"      # violation | warning
    fk_ddl: str = "none"             # none | review_only (written to a file, never executed)


@dataclass
class Measure:
    name: str
    entity: str
    column: str
    agg: str
    by: str                          # grain column (values are node keys)
    filter: dict = field(default_factory=dict)              # exact match on raw values
    filter_normalized: dict = field(default_factory=dict)   # match on upper(btrim(col)); values must be upper/trimmed
    date_column: str = ""            # row-level cube with a time dimension (for date-window queries)
    closure_join: str = ""           # "" | "ancestor" | "descendant": join this measure to the closure cube
    node: str = ""                   # entity whose key `by` refers to (default: the node entity of the only hierarchy)


@dataclass
class Defined:
    name: str
    kind: str
    cls: str = ""


@dataclass
class Ontology:
    iri: str = "https://example.org/sxc#"
    hierarchy: str = ""
    class_column: str = ""
    classes: dict = field(default_factory=dict)        # column value -> OWL class name
    no_children: list = field(default_factory=list)    # column values whose items may not have children (axioms)
    defined: list = field(default_factory=list)


@dataclass
class Param:
    name: str
    strata: list = field(default_factory=list)   # hierarchy strata (STRATA) sampled from node ids
    values_of: str = ""                          # "entity.column": every distinct non-null value
    values: list = field(default_factory=list)   # explicit values
    hierarchy: str = ""                          # for strata (default: the only hierarchy)


@dataclass
class Gold:
    id: str
    sql: str                       # path relative to the config file
    key: list                      # gold column(s) forming a unique row key ("key" may be a string in TOML)
    cube_query: dict = field(default_factory=dict)
    map: dict = field(default_factory=dict)            # every gold column -> Cube member
    zero_if_null: list = field(default_factory=list)   # gold columns the gold SQL coalesces to 0 (Cube null counts as 0)
    params: dict = field(default_factory=dict)         # param name -> Param (at most one swept parameter)
    param_sets: list = field(default_factory=list)     # explicit list of {param: value} dicts (multi-parameter queries)

    def sql_path(self, cfg: "Config") -> Path:
        return cfg.base_dir() / self.sql


@dataclass
class Cube:
    version: str = CUBE_VERSION
    port: int = 0                  # 0 = ephemeral port chosen by the adapter
    dsn_env: str = ""              # default: source.dsn_env (sessions are forced read-only)
    startup_timeout_s: int = 90
    query_timeout_s: int = 120


@dataclass
class Verify:
    sweep_k: int = 40              # parameter values per gold query, spread over its strata
    seed: int = 7                  # deterministic sampling
    mutation_operators: object = "default"   # "default" or a list of operator names
    mutant_bindings: int = 3       # bindings per stratum replayed against each mutant
    shacl_max_rows: int = 200000
    owl_max_individuals: int = 50000


@dataclass
class Config:
    path: Path
    version: int
    source: Source
    entities: dict
    hierarchies: dict
    shapes: list
    measures: dict
    ontology: Ontology
    cube: Cube
    gold: list
    verify: Verify

    def entity(self, name: str, ref: str) -> Entity:
        if name not in self.entities:
            _err(ref, f"unknown entity {name!r}")
        return self.entities[name]

    def base_dir(self) -> Path:
        return self.path.parent

    def hierarchy(self, name: str = "", ref: str = "hierarchy") -> Hierarchy:
        """Named hierarchy, or the only one when name is empty."""
        if name:
            if name not in self.hierarchies:
                _err(ref, f"unknown hierarchy {name!r}")
            return self.hierarchies[name]
        if len(self.hierarchies) != 1:
            _err(ref, "name the hierarchy (the config has zero or several)")
        return next(iter(self.hierarchies.values()))

    def cube_dsn_env(self) -> str:
        return self.cube.dsn_env or self.source.dsn_env


def _filter_values(ref: str, flt: dict, normalized: bool):
    for col, vals in flt.items():
        ident.check(col)
        if not isinstance(vals, list) or not vals:
            _err(ref, f"filter {col!r} needs a non-empty list of values")
        for v in vals:
            if isinstance(v, bool) or not isinstance(v, (str, int)):
                _err(ref, f"filter {col!r}: values must be strings or integers")
            if normalized and (not isinstance(v, str) or v != v.strip().upper()):
                _err(ref, f"filter_normalized {col!r}: values must be upper-case and trimmed ({v!r})")


def _member(ref: str, m) -> str:
    if not isinstance(m, str) or not _MEMBER.match(m):
        _err(ref, f"bad Cube member {m!r} (use cube.member)")
    return m


def load(path: str | Path) -> Config:
    """Parse and validate crosscheck.toml. Every malformed entry is a configuration error (exit 2), including
    those found by ident.check, a missing required key or a value of the wrong shape."""
    try:
        return _load(path)
    except KeyError as e:
        raise StageError(f"crosscheck.toml: missing required key {e.args[0]!r}", EXIT_CONFIG) from e
    except (ident.IdentError, TypeError, ValueError, AttributeError) as e:
        raise StageError(f"crosscheck.toml: {e}", EXIT_CONFIG) from e


def _load(path: str | Path) -> Config:
    p = Path(path)
    if not p.is_file():
        raise StageError(f"config not found: {p.name}", EXIT_CONFIG)
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8-sig"))   # editors and PowerShell 5.1 write a BOM
    except tomllib.TOMLDecodeError as e:
        raise StageError(f"crosscheck.toml: {e}", EXIT_CONFIG) from e
    if raw.get("version") != 1:
        _err("version", "only version = 1 is supported")
    _top_level(raw)
    _credentials(raw)
    try:
        src = Source(**raw.get("source", {}))
    except TypeError as e:
        _err("source", str(e))
    _env_name("source.dsn_env", src.dsn_env)
    _env_name("source.owner_dsn_env", src.owner_dsn_env)
    _int("source.statement_timeout_ms", src.statement_timeout_ms, 1000)
    if not isinstance(src.schemas, list) or not src.schemas:
        _err("source.schemas", "list the source schemas")
    for s in src.schemas + [src.output_schema]:
        ident.check(s)
    if src.output_schema in src.schemas:
        _err("source.output_schema", "the output schema must differ from the source schemas")

    entities = {}
    for name, e in raw.get("entities", {}).items():
        ref = f"entities.{name}"
        ident.check(name)
        _known_keys(e, ("table", "key"), ref)
        if not e.get("key"):
            _err(ref, "key is required")
        schema, _t = ident.split(e["table"])
        if schema not in src.schemas:
            _err(ref, f"table schema {schema!r} is not listed in source.schemas")
        for k in e["key"]:
            ident.check(k)
        entities[name] = Entity(name, e["table"], list(e["key"]))

    try:
        cube = Cube(**raw.get("cube", {}))
        verify = Verify(**raw.get("verify", {}))
    except TypeError as e:
        _err("cube/verify", str(e))
    if cube.version != CUBE_VERSION:
        _err("cube.version", f"this release pins Cube {CUBE_VERSION}")
    if cube.dsn_env != "":
        _env_name("cube.dsn_env", cube.dsn_env)
    _int("cube.port", cube.port, 0, 65535)
    _int("cube.startup_timeout_s", cube.startup_timeout_s, 1)
    _int("cube.query_timeout_s", cube.query_timeout_s, 1)
    _int("verify.sweep_k", verify.sweep_k, 1)
    _int("verify.seed", verify.seed)
    _int("verify.mutant_bindings", verify.mutant_bindings, 1)
    _int("verify.shacl_max_rows", verify.shacl_max_rows, 1)
    _int("verify.owl_max_individuals", verify.owl_max_individuals, 1)
    ops = verify.mutation_operators
    if ops != "default" and not (isinstance(ops, list) and all(isinstance(op, str) for op in ops)):
        _err("verify.mutation_operators", 'use "default" or a list of operator names')

    cfg = Config(p.resolve(), 1, src, entities, {}, [], {}, Ontology(), cube, [], verify)

    for name, h in raw.get("hierarchies", {}).items():
        ref = f"hierarchies.{name}"
        ident.check(name)
        try:
            hh = Hierarchy(name=name, **h)
        except TypeError as e:
            _err(ref, str(e))
        cfg.entity(hh.edge, ref)
        node = cfg.entity(hh.node, ref)
        if len(node.key) != 1:
            _err(ref, "the node entity needs a single-column key")
        for c in (hh.parent, hh.child, hh.quantity):
            ident.check(c)
        if hh.on_cycle not in ("error", "report"):
            _err(ref, "on_cycle must be error or report")
        _int(f"{ref}.max_depth", hh.max_depth, 1, 64)
        cfg.hierarchies[name] = hh

    seen = set()
    for i, s in enumerate(raw.get("shapes", [])):
        s = dict(s)
        if "class" in s:
            s["class_"] = s.pop("class")
        try:
            sh = Shape(**s)
        except TypeError as e:
            _err(f"shapes[{i}]", str(e))
        ref = f"shapes[{i}] {sh.id}"
        ident.check(sh.id)
        ident.check("v_shape_" + sh.id)
        if sh.id in seen:
            _err(ref, "duplicate shape id")
        seen.add(sh.id)
        cfg.entity(sh.target, ref)
        if sh.path:
            ident.check(sh.path)
        for b in ("min_exclusive", "min_inclusive", "max_inclusive"):
            if getattr(sh, b) is not None:
                if not sh.path:
                    _err(ref, f"{b} needs path")
                setattr(sh, b, _decimal(ref, getattr(sh, b)))
        if sh.class_:
            target_cls = cfg.entity(sh.class_, ref)
            if not sh.path:
                _err(ref, "class needs path")
            if len(target_cls.key) != 1:
                _err(ref, "class entity needs a single-column key")
        if not (sh.class_ or sh.sql_violation or sh.min_exclusive is not None or sh.min_inclusive is not None or sh.max_inclusive is not None):
            _err(ref, "a shape needs class, a bound, or sql_violation")
        if sh.severity not in ("violation", "warning") or sh.fk_ddl not in ("none", "review_only"):
            _err(ref, "bad severity or fk_ddl")
        if sh.fk_ddl == "review_only" and not sh.class_:
            _err(ref, "fk_ddl needs class")
        cfg.shapes.append(sh)

    for name, m in raw.get("measures", {}).items():
        ref = f"measures.{name}"
        ident.check(name)
        ident.check("m_" + name)
        try:
            mm = Measure(name=name, **m)
        except TypeError as e:
            _err(ref, str(e))
        cfg.entity(mm.entity, ref)
        for c in [mm.column, mm.by] + ([mm.date_column] if mm.date_column else []):
            ident.check(c)
        _filter_values(ref, mm.filter, False)
        _filter_values(ref, mm.filter_normalized, True)
        if set(mm.filter) & set(mm.filter_normalized):
            _err(ref, "a column cannot be in both filter and filter_normalized")
        if mm.agg not in AGGS:
            _err(ref, f"agg must be one of {AGGS}")
        if mm.closure_join not in ("", "ancestor", "descendant"):
            _err(ref, "closure_join must be '', ancestor or descendant")
        if mm.closure_join and mm.date_column:
            _err(ref, "a dated (row-level) measure cannot be joined to the closure")
        if not mm.node and len(cfg.hierarchies) == 1:
            mm.node = next(iter(cfg.hierarchies.values())).node
        if mm.node:
            cfg.entity(mm.node, ref)
        if mm.closure_join and len(cfg.hierarchies) != 1:
            _err(ref, "closure_join needs exactly one hierarchy")
        cfg.measures[name] = mm

    o = raw.get("ontology", {})
    if o:
        _known_keys(o, ("iri", "hierarchy", "class_column", "classes", "no_children", "defined"), "ontology")
        ont = Ontology(iri=o.get("iri", Ontology.iri), hierarchy=o.get("hierarchy", ""), class_column=o.get("class_column", ""),
                       classes=dict(o.get("classes", {})), no_children=list(o.get("no_children", [])))
        if not ont.hierarchy or ont.hierarchy not in cfg.hierarchies:
            _err("ontology.hierarchy", f"unknown hierarchy {ont.hierarchy!r}")
        if not re.match(r"^https?://[^\s<>\"{}|\\^`]+[#/]$", ont.iri):
            _err("ontology.iri", "iri must be an http(s) IRI ending with # or /")
        if ont.class_column:
            ident.check(ont.class_column)
        for cname in ont.classes.values():
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", cname):
                _err("ontology.classes", f"bad OWL class name {cname!r}")
        if len(set(ont.classes.values())) != len(ont.classes):
            _err("ontology.classes", "OWL class names must be distinct")
        for v in ont.no_children:
            if v not in ont.classes:
                _err("ontology.no_children", f"{v!r} is not a key of ontology.classes")
        for j, d in enumerate(o.get("defined", [])):
            _known_keys(d, ("name", "kind", "class"), f"ontology.defined[{j}]")
            dd = Defined(name=d["name"], kind=d["kind"], cls=d.get("class", ""))
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", dd.name) or dd.name in ont.classes.values():
                _err(f"ontology.defined[{j}]", f"bad or duplicate defined class name {dd.name!r}")
            if dd.kind not in DEFINED_KINDS:
                _err(f"ontology.defined[{j}]", f"kind must be one of {DEFINED_KINDS}")
            if dd.kind in ("contains_class", "class_with_child", "class_used_as_part") and dd.cls not in ont.classes:
                _err(f"ontology.defined[{j}]", f"class {dd.cls!r} must be a key of ontology.classes")
            ont.defined.append(dd)
        cfg.ontology = ont

    gold_ids = set()
    for i, g in enumerate(raw.get("gold", [])):
        g = dict(g)
        ref = f"gold[{i}] {g.get('id', '?')}"
        if isinstance(g.get("key"), str):
            g["key"] = [g["key"]]
        params = g.pop("params", {}) or {}
        try:
            gg = Gold(**g)
        except TypeError as e:
            _err(ref, str(e))
        ident.check(gg.id)
        if gg.id in gold_ids:
            _err(ref, "duplicate gold id")
        gold_ids.add(gg.id)
        if not gg.sql_path(cfg).is_file():
            _err(ref, f"gold SQL file not found: {gg.sql}")
        if not gg.key or not gg.map:
            _err(ref, "key and map are required")
        for col, member in gg.map.items():
            ident.check(col)
            _member(ref, member)
        for k in gg.key + gg.zero_if_null:
            if k not in gg.map:
                _err(ref, f"column {k!r} is not in map")
        for sect in ("dimensions", "measures"):
            for m in gg.cube_query.get(sect, []):
                _member(ref, m)
        mapped = set(gg.map.values())
        queried = set(gg.cube_query.get("dimensions", [])) | set(gg.cube_query.get("measures", []))
        if mapped != queried:
            _err(ref, "map must cover exactly the cube_query dimensions and measures")
        for td in gg.cube_query.get("timeDimensions", []):
            _member(ref, td.get("dimension"))
            if td.get("granularity"):
                _err(ref, "timeDimensions with granularity are not supported (use a dateRange filter)")
        for pname, spec in params.items():
            ident.check(pname)
            _known_keys(spec, ("strata", "values_of", "values", "hierarchy"), f"{ref} params.{pname}")
            pp = Param(name=pname, strata=list(spec.get("strata", [])), values_of=spec.get("values_of", ""),
                       values=list(spec.get("values", [])), hierarchy=spec.get("hierarchy", ""))
            given = [bool(pp.strata), bool(pp.values_of), bool(pp.values)]
            if sum(given) != 1:
                _err(f"{ref} params.{pname}", "give exactly one of strata, values_of, values")
            for s in pp.strata:
                if s not in STRATA:
                    _err(f"{ref} params.{pname}", f"unknown stratum {s!r} (one of {STRATA})")
            if pp.strata:
                cfg.hierarchy(pp.hierarchy, f"{ref} params.{pname}")
            if pp.values_of:
                ent, col = (pp.values_of.split(".") + [""])[:2]
                cfg.entity(ent, f"{ref} params.{pname}")
                ident.check(col)
            gg.params[pname] = pp
        if len(gg.params) > 1:
            _err(ref, "sweep one parameter per gold query; use param_sets for several")
        if gg.params and gg.param_sets:
            _err(ref, "use either params or param_sets")
        for ps in gg.param_sets:
            if not isinstance(ps, dict) or not ps:
                _err(ref, "param_sets entries must be non-empty tables")
            for pname in ps:
                ident.check(pname)
        cfg.gold.append(gg)
    return cfg

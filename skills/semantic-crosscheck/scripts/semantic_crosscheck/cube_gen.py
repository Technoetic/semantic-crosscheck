"""Cube Core data model generated from crosscheck.toml (never edited by hand).

Cubes (names from naming.py):
  <entity>            one per entity: every column with a supported type as a dimension, `count` measure.
                      A single-column key is the primary key (public); a composite key gets a hidden
                      synthetic `sxc_pk` (json_build_array of the key columns, collision free).
                      The node entity of a measure joins one_to_one to that measure's cube.
  <h>_closure         the closure table: ancestor, descendant, min_depth, max_depth, path_count dimensions,
                      `qty` (sum) and `pairs` (count); joins many_to_one to <h>_ancestor / <h>_descendant and to
                      measure cubes declared with closure_join.
  <h>_ancestor / <h>_descendant   copies of the node entity cube (no joins), so both ends can be described.
  m_<measure>         pre-aggregated per grain key: `key` (primary key, public) and `value`
                      (sum -> sum, count -> sum of counts, min -> min, max -> max).
  m_<measure> with date_column   row level: hidden `sxc_pk`, `key`, `ts` (time) and `value`.

Date and timestamp-without-time-zone columns are exposed as `(col::timestamp AT TIME ZONE 'UTC')` so Cube's
UTC date ranges select whole calendar days whatever the session time zone is.
Rows whose grain column is NULL are not part of a measure (they cannot join to a node).

The model is a plain dict ({"cubes": [...]}) so mutation tests can patch it; write_model() emits one YAML file
per cube (block style, JSON-quoted scalars) and the dict as model.json next to the model directory.
"""

import json
from pathlib import Path

from . import db, ident, naming

STRING_TYPES = {"text", "character varying", "character", "uuid", "name", "citext"}
NUMBER_TYPES = {"smallint", "integer", "bigint", "numeric", "real", "double precision"}
DATE_TYPES = {"date", "timestamp without time zone"}
TIMESTAMPTZ = "timestamp with time zone"


def _lit(v) -> str:
    """SQL literal for a filter value from the config (strings or integers only, validated by config)."""
    if isinstance(v, bool):
        raise ValueError("boolean filter values are not supported")
    if isinstance(v, int):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def cube_type(data_type: str) -> str | None:
    if data_type in STRING_TYPES:
        return "string"
    if data_type in NUMBER_TYPES:
        return "number"
    if data_type in DATE_TYPES or data_type == TIMESTAMPTZ:
        return "time"
    if data_type == "boolean":
        return "boolean"
    return None


def time_sql(expr: str, data_type: str) -> str:
    return expr if data_type == TIMESTAMPTZ else f"({expr}::timestamp AT TIME ZONE 'UTC')"


def columns(conn, table: str) -> list[tuple[str, str]]:
    """[(column, data_type)] in ordinal order."""
    schema, name = ident.split(table)
    rows = conn.run("""SELECT column_name, data_type FROM information_schema.columns
                       WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position""", s=schema, t=name)
    return [(r[0], r[1]) for r in rows]


def where_sql(m, alias: str = "t") -> str:
    """WHERE clause of a measure: exact filters, normalised filters, non-null grain."""
    parts = [f"{alias}.{ident.quote(m.by)} IS NOT NULL"]
    for col, vals in m.filter.items():
        parts.append(f"{alias}.{ident.quote(col)} IN ({', '.join(_lit(v) for v in vals)})")
    for col, vals in m.filter_normalized.items():
        parts.append(f"upper(btrim({alias}.{ident.quote(col)})) IN ({', '.join(_lit(v) for v in vals)})")
    return " AND ".join(parts)


def _pk_dims(key: list, coltypes: dict) -> tuple[list, set]:
    """Primary-key dimension(s) and the set of columns already covered."""
    if len(key) == 1:
        k = key[0]
        return [{"name": k, "sql": f'{{CUBE}}.{ident.quote(k)}', "type": cube_type(coltypes[k]) or "string",
                 "primary_key": True, "public": True}], {k}
    expr = "json_build_array(" + ", ".join(f"{{CUBE}}.{ident.quote(k)}" for k in key) + ")::text"
    return [{"name": naming.PK, "sql": expr, "type": "string", "primary_key": True}], set()


def entity_cube(name: str, ent, cols: list, notes: list) -> dict:
    coltypes = dict(cols)
    for k in ent.key:
        if k not in coltypes:
            raise ValueError(f"entities.{ent.name}: key column {k!r} not found in {ent.table}")
    dims, covered = _pk_dims(ent.key, coltypes)
    for col, dt in cols:
        if col in covered:
            continue
        try:
            ident.check(col)
        except ident.IdentError:
            notes.append(f"{ent.table}: column skipped (not a plain lower-case identifier)")
            continue
        if col == naming.PK:
            notes.append(f"{ent.table}: column {naming.PK} skipped (reserved name)")
            continue
        t = cube_type(dt)
        if t is None:
            notes.append(f"{ent.table}.{col}: type {dt} not exposed")
            continue
        sql = f"{{CUBE}}.{ident.quote(col)}"
        dims.append({"name": col, "sql": time_sql(sql, dt) if t == "time" else sql, "type": t})
    return {"name": name, "sql_table": ent.table, "dimensions": dims, "measures": [{"name": "count", "type": "count"}], "joins": []}


def value_kind(m, cols: dict) -> str:
    """Comparison kind of a measure value (canon.KINDS): sums and counts are numbers; min and max keep the kind of
    their column, so a min over text codes is compared as text ('007' != '7') and over dates as time."""
    if m.agg in ("sum", "count"):
        return "number"
    return cube_type(cols.get(m.column, "")) or "string"


def measure_cube(cfg, m, cols: dict) -> dict:
    ent = cfg.entity(m.entity, f"measures.{m.name}")
    table = ident.qualified(ent.table)
    key_type = cube_type(cols[m.by]) or "string"
    name = naming.cube_measure(m.name)
    col = f"t.{ident.quote(m.column)}"
    by = f"t.{ident.quote(m.by)}"
    if m.date_column:
        pk = "json_build_array(" + ", ".join(f"t.{ident.quote(k)}" for k in ent.key) + ")::text"
        ts = time_sql(f"t.{ident.quote(m.date_column)}", cols[m.date_column])
        sql = (f"SELECT {pk} AS {naming.PK}, {by} AS {naming.MEASURE_KEY}, {ts} AS {naming.MEASURE_TS}, {col} AS v "
               f"FROM {table} t WHERE {where_sql(m)}")
        return {"name": name, "sql": sql,
                "dimensions": [
                    {"name": naming.PK, "sql": f"{{CUBE}}.{naming.PK}", "type": "string", "primary_key": True},
                    {"name": naming.MEASURE_KEY, "sql": f"{{CUBE}}.{naming.MEASURE_KEY}", "type": key_type},
                    {"name": naming.MEASURE_TS, "sql": f"{{CUBE}}.{naming.MEASURE_TS}", "type": "time"}],
                "measures": [{"name": naming.MEASURE_VALUE, "sql": "{CUBE}.v", "type": m.agg,
                              "meta": {"sxc_kind": value_kind(m, cols)}}],
                "joins": []}
    sql = f"SELECT {by} AS {naming.MEASURE_KEY}, {m.agg}({col}) AS {naming.MEASURE_VALUE} FROM {table} t WHERE {where_sql(m)} GROUP BY {by}"
    reagg = {"sum": "sum", "count": "sum", "min": "min", "max": "max"}[m.agg]
    return {"name": name, "sql": sql,
            "dimensions": [{"name": naming.MEASURE_KEY, "sql": f"{{CUBE}}.{naming.MEASURE_KEY}", "type": key_type,
                            "primary_key": True, "public": True}],
            "measures": [{"name": naming.MEASURE_VALUE, "sql": f"{{CUBE}}.{naming.MEASURE_VALUE}", "type": reagg,
                          "meta": {"sxc_kind": value_kind(m, cols)}}],
            "joins": []}


def closure_cubes(cfg, h, node_cube: dict, key_type: str) -> list[dict]:
    c = naming.cube_closure(h.name)
    anc, desc = naming.cube_ancestor(h.name), naming.cube_descendant(h.name)
    node_key = cfg.entity(h.node, f"hierarchies.{h.name}").key[0]
    dims = [{"name": "pk", "sql": "json_build_array({CUBE}.ancestor, {CUBE}.descendant)::text", "type": "string", "primary_key": True},
            {"name": "ancestor", "sql": "{CUBE}.ancestor", "type": key_type},
            {"name": "descendant", "sql": "{CUBE}.descendant", "type": key_type},
            {"name": "min_depth", "sql": "{CUBE}.min_depth", "type": "number"},
            {"name": "max_depth", "sql": "{CUBE}.max_depth", "type": "number"},
            {"name": "path_count", "sql": "{CUBE}.path_count", "type": "number"}]
    joins = [{"name": anc, "relationship": "many_to_one", "sql": f"{{CUBE}}.ancestor = {{{anc}}}.{ident.quote(node_key)}"},
             {"name": desc, "relationship": "many_to_one", "sql": f"{{CUBE}}.descendant = {{{desc}}}.{ident.quote(node_key)}"}]
    for m in cfg.measures.values():
        if m.closure_join:
            mc = naming.cube_measure(m.name)
            joins.append({"name": mc, "relationship": "many_to_one", "sql": f"{{CUBE}}.{m.closure_join} = {{{mc}}}.{naming.MEASURE_KEY}"})
    closure = {"name": c, "sql_table": naming.closure_table(cfg, h.name), "dimensions": dims,
               "measures": [{"name": "qty", "sql": "{CUBE}.qty", "type": "sum"}, {"name": "pairs", "type": "count"}],
               "joins": joins}
    copies = []
    for nm in (anc, desc):
        cp = json.loads(json.dumps(node_cube))
        cp["name"], cp["joins"] = nm, []
        copies.append(cp)
    return [closure] + copies


def generate(cfg, conn) -> tuple[dict, list[str]]:
    """Build the model dict from the config and the live catalog. Returns (model, notes)."""
    notes: list[str] = []
    cols = {name: columns(conn, e.table) for name, e in cfg.entities.items()}
    for name, c in cols.items():
        if not c:
            raise ValueError(f"entities.{name}: table {cfg.entities[name].table} not found or not readable")
    cubes = {}
    for name, ent in cfg.entities.items():
        cubes[name] = entity_cube(naming.cube_entity(name), ent, cols[name], notes)
    measure_cubes = []
    for m in cfg.measures.values():
        mcols = dict(cols[m.entity])
        for c in [m.column, m.by] + ([m.date_column] if m.date_column else []) + list(m.filter) + list(m.filter_normalized):
            if c not in mcols:
                raise ValueError(f"measures.{m.name}: column {c!r} not found in {cfg.entities[m.entity].table}")
        measure_cubes.append(measure_cube(cfg, m, mcols))
        if m.node and not m.date_column:
            mc = naming.cube_measure(m.name)
            node_key = cfg.entities[m.node].key[0]
            if len(cfg.entities[m.node].key) == 1:
                cubes[m.node]["joins"].append({"name": mc, "relationship": "one_to_one",
                                               "sql": f"{{CUBE}}.{ident.quote(node_key)} = {{{mc}}}.{naming.MEASURE_KEY}"})
    hier = []
    for h in cfg.hierarchies.values():
        exists = db.scalar(conn, "SELECT to_regclass(:t) IS NOT NULL", t=naming.closure_table(cfg, h.name))
        if not exists:
            notes.append(f"hierarchies.{h.name}: closure table missing (run build); closure cubes not generated")
            continue
        edge_cols = dict(cols[h.edge])
        key_type = cube_type(edge_cols.get(h.parent, "text")) or "string"
        hier.extend(closure_cubes(cfg, h, cubes[h.node], key_type))
    model = {"cubes": list(cubes.values()) + measure_cubes + hier}
    for cube in model["cubes"]:
        if not cube["joins"]:
            del cube["joins"]
    return model, notes


def members(model: dict) -> set[str]:
    """Every queryable 'cube.member' (dimensions and measures)."""
    out = set()
    for c in model["cubes"]:
        for d in c.get("dimensions", []):
            out.add(naming.member(c["name"], d["name"]))
        for m in c.get("measures", []):
            out.add(naming.member(c["name"], m["name"]))
    return out


def _scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if v is None:
        return "null"
    return json.dumps(str(v), ensure_ascii=False)


def to_yaml(obj, indent: int = 0) -> str:
    """Block-style YAML for dicts/lists of scalars; strings are JSON-quoted (valid YAML double-quoted scalars)."""
    pad = " " * indent
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(to_yaml(v, indent + 2))
            elif isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}: {'{}' if isinstance(v, dict) else '[]'}")
            else:
                lines.append(f"{pad}{k}: {_scalar(v)}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                body = to_yaml(item, indent + 2).splitlines()
                lines.append(f"{pad}- {body[0].lstrip()}")
                lines.extend(body[1:])
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    return "\n".join(lines)


def write_model(model: dict, model_dir: Path) -> list[Path]:
    """Replace the YAML files in model_dir with one file per cube; also write ../model.json."""
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    for old in model_dir.glob("*.yml"):
        old.unlink()
    written = []
    for c in model["cubes"]:
        f = model_dir / f"{c['name']}.yml"
        f.write_text(to_yaml({"cubes": [c]}) + "\n", encoding="utf-8", newline="\n")
        written.append(f)
    (model_dir.parent / "model.json").write_text(json.dumps(model, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    return written


def load_model(out_dir: Path) -> dict:
    return json.loads((Path(out_dir) / "cube" / "model.json").read_text(encoding="utf-8"))

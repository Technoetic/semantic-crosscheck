"""V1/V2 strict comparison of one gold-query binding: gold SQL rows against Cube rows (design section 7).

Gold rows come from the gold SQL file (trusted code from the configuration, read as UTF-8 with or without a BOM),
executed as one prepared statement with bound parameters inside db.read_only. The extended protocol accepts a single
statement only, so a gold file cannot end the read-only transaction. Cube rows come from gold.cube_query after every
string equal to ":<param>" (also inside lists and dicts) is replaced by the bound value; gold.map renames them to
gold columns.

Rows are matched on the canonical key and a duplicate key on either side fails. Matched rows must be canonically
identical in every mapped column. Canonical forms follow the type of each column's Cube member (gold_kinds of
member_kinds of the model; canon.canon(v, kind)): text is compared exactly, numbers as exact decimals, dates and
timestamps in one UTC form, booleans as booleans. A caller without a model compares untyped (canon.canon(v)). The
only declared equivalence is zero_if_null: a Cube NULL counts as 0 in the listed columns, because the gold SQL
coalesces them. Both sides empty is a pass flagged `empty`.

The comparison dict holds counts only. Errors are reported by exception type, SQLSTATE or HTTP status: database
and Cube messages can quote row values, so they are never kept.
"""

import http.client
import json
import re

from . import canon, db
from .cube_client import CubeError

_PLACEHOLDER = re.compile(r"^:([a-z_][a-z0-9_]*)$")
RESERVED_PARAMS = frozenset({"stream", "types"})   # keyword arguments pg8000 uses for other purposes
VALUE_KINDS = ("string", "time", "boolean")        # Cube measure types that are not numbers


class UnboundParameter(ValueError):
    """A ':<name>' placeholder in cube_query has no value in the binding (configuration error)."""


def subst(obj, params: dict):
    """Replace every string exactly equal to ':<name>' with params[name], recursively in lists and dicts."""
    if isinstance(obj, str):
        m = _PLACEHOLDER.match(obj)
        if not m:
            return obj
        if m.group(1) not in params:
            raise UnboundParameter(m.group(1))
        return params[m.group(1)]
    if isinstance(obj, list):
        return [subst(v, params) for v in obj]
    if isinstance(obj, dict):
        return {k: subst(v, params) for k, v in obj.items()}
    return obj


def _json_value(v):
    """A bound value in a form Cube accepts in JSON: Decimal and dates become their canonical text."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return canon.canon(v)


def cube_query(gold, params: dict) -> dict:
    """gold.cube_query with the binding substituted."""
    return subst(gold.cube_query, {k: _json_value(v) for k, v in params.items()})


def member_kinds(model: dict) -> dict[str, str]:
    """Comparison kind (canon.KINDS) of every member of a generated model: a dimension's own type, and for a measure
    'number' unless its type is string, time or boolean. Dimensions of another type (geo) are left out."""
    kinds = {}
    for cube in model.get("cubes", []):
        for d in cube.get("dimensions", []):
            if d.get("type") in canon.KINDS:
                kinds[f"{cube.get('name')}.{d.get('name')}"] = d["type"]
        for m in cube.get("measures", []):
            kinds[f"{cube.get('name')}.{m.get('name')}"] = m["type"] if m.get("type") in VALUE_KINDS else "number"
    return kinds


def gold_kinds(gold, kinds: dict) -> dict[str, str]:
    """Kind of every gold column whose Cube member has one in member_kinds; other columns are compared untyped."""
    return {col: kinds[member] for col, member in gold.map.items() if member in kinds}


def gold_rows(conn, cfg, gold, params: dict) -> list[dict]:
    """Rows of the gold SQL for one binding (bound parameters, READ ONLY, statement timeout)."""
    reserved = sorted(RESERVED_PARAMS & set(params))
    if reserved:
        raise ValueError(f"gold {gold.id}: parameter names {reserved} are reserved by pg8000; rename them")
    sql = gold.sql_path(cfg).read_text(encoding="utf-8-sig")
    with db.read_only(conn, cfg.source.statement_timeout_ms):
        ps = conn.prepare(sql)
        try:
            data = ps.run(**params)
            cols = [c["name"] for c in (ps.columns or [])]
        finally:
            ps.close()
    return [dict(zip(cols, r)) for r in data or []]


def cube_rows(client, gold, params: dict) -> list[dict]:
    """Cube rows of gold.cube_query for one binding, renamed to gold columns through gold.map."""
    data = client.load(cube_query(gold, params))
    return [{col: r.get(member) for col, member in gold.map.items()} for r in data]


def _index(rows: list[dict], key: list, types: dict) -> tuple[dict, int]:
    """Rows by canonical key (the first row of a key wins) and the number of keys that occur more than once."""
    idx, dups = {}, set()
    for r in rows:
        k = tuple(canon.canon(r.get(c), types.get(c)) for c in key)
        if k in idx:
            dups.add(k)
        else:
            idx[k] = r
    return idx, len(dups)


def compare_rows(gold, g_rows: list[dict], c_rows: list[dict], types: dict | None = None) -> dict:
    """Strict comparison of gold rows with Cube rows (already renamed to gold columns). Counts only.

    types maps gold columns to the Cube member type of their member (gold_kinds); a column without one, or every
    column when types is None, is compared untyped."""
    types = types or {}
    cols = list(gold.map)
    zero = [c for c in gold.zero_if_null if c in gold.map]
    declared = 0
    cube = []
    for r in c_rows:
        r = dict(r)
        for col in zero:
            if r.get(col) is None:
                r[col] = 0
                declared += 1
        cube.append(r)
    g_idx, g_dup = _index(g_rows, gold.key, types)
    c_idx, c_dup = _index(cube, gold.key, types)
    mismatched: dict[str, int] = {}
    matched = 0
    for k, g in g_idx.items():
        c = c_idx.get(k)
        if c is None:
            continue
        matched += 1
        for col in cols:
            if not canon.same(g.get(col), c.get(col), types.get(col)):
                mismatched[col] = mismatched.get(col, 0) + 1
    missing = sum(1 for k in g_idx if k not in c_idx)
    extra = sum(1 for k in c_idx if k not in g_idx)
    absent = sorted(col for col in cols if g_rows and col not in g_rows[0])
    out = {
        "pass": not (missing or extra or g_dup or c_dup or mismatched or absent),
        "gold_rows": len(g_rows),
        "cube_rows": len(c_rows),
        "matched_rows": matched,
        "missing_in_cube": missing,
        "extra_in_cube": extra,
        "duplicate_keys_gold": g_dup,
        "duplicate_keys_cube": c_dup,
        "mismatched_columns": mismatched,
        "empty": not g_rows and not c_rows,
        "declared_null_as_zero": declared,
    }
    if absent:
        out["gold_columns_missing"] = absent
    return out


def expected_errors() -> tuple:
    """Exceptions meaning a query could not be answered; they become check errors instead of crashing a stage."""
    errors = (CubeError, OSError, ValueError, http.client.HTTPException)
    try:
        from pg8000.exceptions import Error as PgError
    except ImportError:
        return errors
    return errors + (PgError,)


def error_detail(e: BaseException) -> dict:
    """Exception type plus SQLSTATE or HTTP status; never the message, which can quote row values."""
    d = {"error": type(e).__name__}
    arg = e.args[0] if e.args else None
    if isinstance(arg, dict) and arg.get("C"):
        d["sqlstate"] = str(arg["C"])
    if isinstance(e, CubeError):
        text = str(e)
        m = re.match(r"HTTP (\d{3})\b", text)
        if m:
            d["http_status"] = int(m.group(1))
        elif text.startswith("result reached the limit"):
            d["cube"] = "row_limit"
        elif text.startswith("timed out"):
            d["cube"] = "timeout"
        else:
            d["cube"] = "query_error"
    return d


def status(r: dict) -> str:
    """Check status of one comparison dict."""
    if "error" in r:
        return "error"
    return "pass" if r["pass"] else "fail"


def run_binding(conn, client, cfg, gold, params: dict, cache: dict | None = None, types: dict | None = None) -> dict:
    """Compare one binding. Errors become {'pass': False, 'error': type, ...}; `cache` keeps gold rows for replays
    (the mutation suite queries the same bindings against every mutant, and gold rows do not change); `types` as in
    compare_rows."""
    errors = expected_errors()
    key = (gold.id, json.dumps(params, sort_keys=True, default=str))
    try:
        if cache is not None and key in cache:
            g = cache[key]
        else:
            g = gold_rows(conn, cfg, gold, params)
            if cache is not None:
                cache[key] = g
    except errors as e:
        return {"pass": False, "side": "gold", **error_detail(e)}
    try:
        c = cube_rows(client, gold, params)
    except errors as e:
        return {"pass": False, "side": "cube", "gold_rows": len(g), **error_detail(e)}
    return compare_rows(gold, g, c, types)

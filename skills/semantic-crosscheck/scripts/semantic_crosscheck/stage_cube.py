"""`cube` stage: generate the Cube model from the configuration, serve it and check that Cube sees exactly it.

Why: values are compared only in `verify`; before that the semantic layer must be the one the configuration
describes, and every member a gold query names must be queryable, or V1-V4 would fail for reasons unrelated
to the data.

  cube:model                 the model was generated from the live catalog (cubes and members counted);
                             generator notes (skipped columns, a closure table not built yet) go to not_proven
  cube:gold_members:<gold>   every member the gold entry names (cube_query dimensions, measures, segments,
                             filters including nested and/or, timeDimensions, order, and map) exists in the
                             model and is public: Cube answers a query on a hidden member with HTTP 500
  cube:meta                  /meta of the running adapter lists exactly the public members of the model
                             (a primary-key dimension without public: true is hidden and must not be listed);
                             whatever its outcome, its detail records the Cube version and the SQL planner the
                             adapter was started with (cube_version, sql_planner: cube_run.runtime_facts), which
                             report compares with the ones this checkout pins

The catalog is read in a READ ONLY transaction and Cube runs read-only sessions (cube_run). Cube errors are
recorded by HTTP status only, because response bodies can quote values. The adapter writes each error to
cube/server.log as its class, code and coarse category without message text (model compile errors keep theirs; the
full text only when the user sets SXC_CUBE_DEBUG=1, and then the log can hold row values), so the next action points
at the generated model and the configuration, not at the log.

When a closure cube is missing, the next action follows build's closure:<h> result: rerunning build cannot help
while that check fails on a cycle or the depth limit, so the agent is sent to the hierarchy entry instead.
"""

import http.client
import re
from pathlib import Path

from . import cube_gen, cube_run, db, naming
from .cube_client import CubeError
from .result import EXIT_CONFIG, Result, StageError, load_result

LIST_CAP = 20
_REF = re.compile(r"^(entities|measures|hierarchies)\.[a-z_][a-z0-9_]*$")


# ---- model members --------------------------------------------------------------------------------
def is_public(definition: dict, dimension: bool) -> bool:
    """Cube visibility of one member: public: false hides it, and primary keys are hidden unless public: true."""
    if definition.get("public") is False or definition.get("shown") is False:
        return False
    return not (dimension and definition.get("primary_key") and definition.get("public") is not True)


def public_members(model: dict) -> set[str]:
    """The 'cube.member' names Cube should list in /meta and accept in queries."""
    out = set()
    for c in model["cubes"]:
        if c.get("public") is False or c.get("shown") is False:
            continue
        out |= {naming.member(c["name"], d["name"]) for d in c.get("dimensions", []) if is_public(d, True)}
        out |= {naming.member(c["name"], m["name"]) for m in c.get("measures", []) if is_public(m, False)}
    return out


def meta_members(meta: dict) -> set[str]:
    """Members listed by /cubejs-api/v1/meta (entries flagged invisible are not counted as listed)."""
    out = set()
    for c in meta.get("cubes", []) or []:
        if c.get("public") is False or c.get("isVisible") is False:
            continue
        for section in ("dimensions", "measures"):
            for m in c.get(section, []) or []:
                if m.get("public") is not False and m.get("isVisible") is not False and isinstance(m.get("name"), str):
                    out.add(m["name"])
    return out


def query_members(query: dict) -> tuple[set[str], int]:
    """Members named anywhere in a Cube query, and the number of entries that name no member."""
    names = list(query.get("dimensions") or []) + list(query.get("measures") or []) + list(query.get("segments") or [])
    names += [td.get("dimension") if isinstance(td, dict) else None for td in query.get("timeDimensions") or []]
    order = query.get("order") or []
    if isinstance(order, dict):
        names += list(order)
    else:
        names += [o[0] if isinstance(o, (list, tuple)) and o else None for o in order]
    malformed, stack = 0, list(query.get("filters") or [])
    while stack:
        f = stack.pop()
        groups = [f[k] for k in ("and", "or") if isinstance(f, dict) and isinstance(f.get(k), list)]
        named = [f[k] for k in ("member", "dimension") if isinstance(f, dict) and k in f]
        for group in groups:
            stack.extend(group)
        names += named
        if not (groups or named):
            malformed += 1
    members = {n for n in names if isinstance(n, str)}
    return members, malformed + sum(1 for n in names if not isinstance(n, str))


# ---- checks -------------------------------------------------------------------------------------
def gold_check(gold, model_members: set[str], public: set[str]) -> tuple[str, dict]:
    used, malformed = query_members(gold.cube_query)
    used |= {m for m in gold.map.values() if isinstance(m, str)}
    missing = sorted(m for m in used if m not in model_members)
    hidden = sorted(m for m in used if m in model_members and m not in public)
    detail = {"members": len(used), "missing": len(missing), "hidden": len(hidden), "malformed": malformed}
    if missing:
        detail["missing_members"] = missing[:LIST_CAP]
    if hidden:
        detail["hidden_members"] = hidden[:LIST_CAP]
    return ("pass" if not (missing or hidden or malformed) else "fail"), detail


def meta_check(public: set[str], meta: dict) -> tuple[str, dict]:
    listed = meta_members(meta)
    missing, extra = sorted(public - listed), sorted(listed - public)
    detail = {"model_public": len(public), "meta_members": len(listed), "missing": len(missing), "extra": len(extra)}
    if missing:
        detail["missing_members"] = missing[:LIST_CAP]
    if extra:
        detail["extra_members"] = extra[:LIST_CAP]
    return ("pass" if not (missing or extra) else "fail"), detail


def http_status(e: Exception) -> int | None:
    m = re.match(r"^HTTP (\d{3})\b", str(e))
    return int(m.group(1)) if m else None


def config_message(e: ValueError) -> str:
    """A generator error as a configuration error naming the crosscheck.toml entry."""
    ref, sep, rest = str(e).partition(": ")
    if sep and _REF.match(ref):
        return f"crosscheck.toml [{ref}]: {rest} (Cube model generation)"
    return f"crosscheck.toml: Cube model generation failed: {e}"


def generate(cfg) -> tuple[dict, list[str]]:
    conn = db.connect(cfg.source.dsn_env)
    try:
        with db.read_only(conn, cfg.source.statement_timeout_ms):
            return cube_gen.generate(cfg, conn)
    except ValueError as e:
        raise StageError(config_message(e), EXIT_CONFIG) from e
    finally:
        conn.close()


def _closure_action(out: Path, missing: list[str]) -> str:
    """What makes the missing closure tables exist, from build's closure:<h> checks."""
    try:
        build = load_result(out, "build")
    except (OSError, ValueError):
        build = None
    listed = build.get("checks") if isinstance(build, dict) else None
    checks = {c.get("id"): c for c in listed if isinstance(c, dict)} if isinstance(listed, list) else {}
    for h in missing:
        c = checks.get(f"closure:{h}")
        if not c or c.get("status") == "pass":
            continue
        detail = c.get("detail")
        reason = detail.get("reason") if isinstance(detail, dict) else None
        if reason == "cycle":
            fix = (f"hierarchy {h} has cycles: fix the edge rows that form them, or set hierarchies.{h}.on_cycle = "
                   "\"report\" in crosscheck.toml")
        elif reason == "depth_limit":
            fix = f"hierarchy {h} is deeper than its max_depth: raise hierarchies.{h}.max_depth in crosscheck.toml"
        else:
            fix = f"closure:{h} did not pass in build: fix what build/result.json reports for it"
        return f"{fix}, rerun `sxc.py build` until closure:{h} passes, then rerun `sxc.py cube`"
    return "run `sxc.py build` so the closure tables exist, then rerun `sxc.py cube`"


def _next_action(res: Result, cfg, cube_names: set[str], out: Path) -> str:
    bad = {c.id: c for c in res.checks if c.status != "pass"}
    if not bad:
        return "run `sxc.py verify` to compare the gold queries with Cube"
    if "cube:meta" in bad:
        return ("Cube did not serve the generated model as expected: compare cube/model with crosscheck.toml and fix the "
                "entry it comes from, then rerun `sxc.py cube` (cube/server.log names only the error class; the user can "
                "rerun with SXC_CUBE_DEBUG=1 for the full Cube error text, which can quote row values and stays local)")
    missing = [h for h in cfg.hierarchies if naming.cube_closure(h) not in cube_names]
    if missing:
        return _closure_action(out, missing)
    return "fix the members of the failing [[gold]] entries (cube_query, map) in crosscheck.toml, then rerun `sxc.py cube`"


def run(cfg, args, out: Path) -> Result:
    res = Result("cube")
    cube_dir = Path(out) / "cube"
    model, notes = generate(cfg)
    cube_gen.write_model(model, cube_dir / "model")
    everything, public = cube_gen.members(model), public_members(model)
    cube_names = {c["name"] for c in model["cubes"]}
    res.add("cube:model", "pass", "", cubes=len(cube_names), cube_names=sorted(cube_names), members=len(everything),
            public_members=len(public), notes=len(notes))
    res.not_proven.extend(f"cube model: {n}" for n in notes)
    if not cfg.gold:
        res.not_proven.append("no [[gold]] entries: gold query members were not checked")
    for i, g in enumerate(cfg.gold):
        status, detail = gold_check(g, everything, public)
        res.add(f"cube:gold_members:{g.id}", status, f"gold[{i}] {g.id}", **detail)

    log_path = cube_dir / "server.log"
    log_path.write_text("", encoding="utf-8")
    meta = None
    with cube_run.serve(cfg, cube_dir, log_path) as client:
        # only what serve() recorded about the child it started; nothing is filled in when it is missing
        served = dict(getattr(client, "runtime", None) or {})
        try:
            meta = client.meta()
        except CubeError as e:
            res.add("cube:meta", "error", "", http_status=http_status(e), **served)
        except (OSError, ValueError, http.client.HTTPException) as e:   # reset, timeout, truncated or non-JSON body
            res.add("cube:meta", "error", "", reason=type(e).__name__, **served)
    if isinstance(meta, dict):
        status, detail = meta_check(public, meta)
        res.add("cube:meta", status, "", cubes=len(meta.get("cubes", []) or []), **detail, **served)
    elif meta is not None:
        res.add("cube:meta", "error", "", reason="unexpected_response", **served)
    res.next_safe_action = _next_action(res, cfg, cube_names, Path(out))
    return res

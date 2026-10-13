"""verify: V1 strict compare, V2 strata sweep, V3 query shapes and V4 mutation tests (design sections 6 and 7).

Prerequisites are the cube stage (cube/model.json, cube/model/*.yml) and, for every gold query or V3 test that
uses a hierarchy's closure, ancestor or descendant cube, a passing `closure:<h>` check in build. Without it those
checks are not_run and listed as not proven: agreement with a closure that did not build proves nothing.

One Cube process (cube_run.serve on <out>/cube, log in verify/server.log) serves V1-V3:
  v1:<gold>             the first binding of the gold query matches exactly; a gold query without any binding
                        (every stratum empty) is not_run, because it was never exercised
  v2:<gold>:<stratum>   every binding of the stratum matches exactly (rows_compared counts gold rows); an empty
                        stratum passes vacuously and is listed as not proven
  v3:<test>             the shape_q tests derived from the model
V4 hands mutate.run_mutants a suite that replays V1 (first binding of every gold query), V2 (the first
verify.mutant_bindings bindings of every stratum) and V3 against a mutant's Cube and stops at the first item that
does not pass: the mutant is killed and the reason is that item's check id plus `fail` or `error`. Only items that
pass against the unmutated model are replayed, so a mutant is never "killed" by a defect the model already has;
gold rows are computed once and reused for every mutant.

Results hold counts, check ids and positions in the deterministic binding order, never parameter values or rows.
"""

import contextlib
from pathlib import Path

from . import compare, cube_gen, cube_run, db, naming, shape_q, sweep
from .result import EXIT_CONFIG, Check, Result, StageError, check_status, require_stage

PARTS = ("v1", "v2", "v3", "v4")
MAX_INDEXES = 10
_SUMS = ("missing_in_cube", "extra_in_cube", "duplicate_keys_gold", "duplicate_keys_cube", "declared_null_as_zero")


def run(cfg, args, out: Path) -> Result:
    out = Path(out)
    only = _only(args)
    golds = _golds(cfg, args)
    require_stage(out, "cube")
    model = _model(out)
    v = _Verify(cfg, out, model, only)
    conn = db.connect(cfg.source.dsn_env)
    try:
        v.run(conn, golds)
    finally:
        conn.close()
    v.res.next_safe_action = _next_action(v.res)
    return v.res


def _only(args) -> list[str]:
    only = list(getattr(args, "only", None) or PARTS)
    bad = [x for x in only if x not in PARTS]
    if bad:
        raise StageError(f"--only accepts {','.join(PARTS)} (got {','.join(bad)})", EXIT_CONFIG)
    return only


def _golds(cfg, args) -> list[tuple[int, object]]:
    wanted = list(getattr(args, "gold", None) or [])
    unknown = sorted(set(wanted) - {g.id for g in cfg.gold})
    if unknown:
        raise StageError(f"--gold: unknown gold id {', '.join(unknown)}", EXIT_CONFIG)
    return [(i, g) for i, g in enumerate(cfg.gold) if not wanted or g.id in wanted]


def _model(out: Path) -> dict:
    if not (out / "cube" / "model").is_dir() or not (out / "cube" / "model.json").is_file():
        raise StageError(f"run `sxc.py cube` first (no generated model in {out / 'cube'})", EXIT_CONFIG)
    return cube_gen.load_model(out)


def _members(g) -> set[str]:
    """Every Cube member a gold query uses: mapped columns, filters, time dimensions and segments."""
    found = set(g.map.values())

    def walk(o, key: str = ""):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v, key)
        elif isinstance(o, str) and key in ("dimensions", "measures", "segments", "member", "dimension"):
            found.add(o)

    walk(g.cube_query)
    return found


def _first(bindings: list) -> tuple | None:
    return next(((s, p) for s, p in bindings if p is not None), None)


def _by_stratum(bindings: list) -> dict[str, list]:
    out: dict[str, list] = {}
    for s, p in bindings:
        out.setdefault(s, [])
        if p is not None:
            out[s].append(p)
    return out


def _detail(r: dict) -> dict:
    return {k: v for k, v in r.items() if k != "pass"}


class _Sweep:
    """Counts over the bindings of one stratum."""

    def __init__(self):
        self.bindings = self.failed = self.errored = self.empty = self.rows = 0
        self.sums = dict.fromkeys(_SUMS, 0)
        self.mismatched: dict[str, int] = {}
        self.errors: dict[str, int] = {}
        self.sqlstates: dict[str, int] = {}
        self.failed_indexes: list[int] = []

    def add(self, j: int, r: dict):
        self.bindings += 1
        self.rows += r.get("gold_rows", 0)
        self.empty += 1 if r.get("empty") else 0
        for k in _SUMS:
            self.sums[k] += r.get(k, 0)
        for col, n in r.get("mismatched_columns", {}).items():
            self.mismatched[col] = self.mismatched.get(col, 0) + n
        if r["pass"]:
            return
        self.failed += 1
        if len(self.failed_indexes) < MAX_INDEXES:
            self.failed_indexes.append(j)
        if "error" in r:
            self.errored += 1
            self.errors[r["error"]] = self.errors.get(r["error"], 0) + 1
            if r.get("sqlstate"):
                self.sqlstates[r["sqlstate"]] = self.sqlstates.get(r["sqlstate"], 0) + 1

    def status(self) -> str:
        return "error" if self.errored else ("fail" if self.failed else "pass")

    def detail(self) -> dict:
        d = {"bindings": self.bindings, "failed_bindings": self.failed, "error_bindings": self.errored,
             "empty_bindings": self.empty, "rows_compared": self.rows, **self.sums, "mismatched_columns": self.mismatched}
        if self.failed_indexes:
            d["failed_indexes"] = self.failed_indexes
        if self.errors:
            d["errors"] = self.errors
        if self.sqlstates:
            d["sqlstates"] = self.sqlstates
        return d


class _Verify:
    def __init__(self, cfg, out: Path, model: dict, only: list[str]):
        self.cfg, self.out, self.model, self.only = cfg, out, model, only
        self.res = Result("verify")
        self.known = cube_gen.members(model)
        self.closure = {h: check_status(out, "build", f"closure:{h}") for h in cfg.hierarchies}
        self.mb = cfg.verify.mutant_bindings
        self.cache: dict | None = {} if "v4" in only else None
        self.items: list[tuple[str, str, object]] = []   # V4 suite: (item id, check id, function(client) -> status)
        self.baseline: dict[str, str] = {}               # item id -> status against the unmutated model

    # ---- planning (database only, before Cube starts) ---------------------------------------------
    def gate(self, members) -> tuple[str, str] | None:
        """(hierarchy, build status) when the members use the cubes of a closure that did not pass in build."""
        cubes = {m.split(".", 1)[0] for m in members}
        for h, st in self.closure.items():
            if st != "pass" and cubes & {naming.cube_closure(h), naming.cube_ancestor(h), naming.cube_descendant(h)}:
                return h, st or "missing"
        return None

    def plan_gold(self, conn, i: int, g) -> dict:
        members = _members(g)
        plan = {"gold": g, "ref": f"gold[{i}] {g.id}", "bindings": [], "gate": self.gate(members), "problem": None}
        if plan["gate"]:
            return plan
        missing = sorted(members - self.known)
        if missing:
            plan["problem"] = {"reason": "members_not_in_model", "missing_members": missing}
            return plan
        try:
            plan["bindings"] = sweep.bindings(conn, self.cfg, g)
        except compare.expected_errors() as e:
            plan["problem"] = {"reason": "bindings_failed", **compare.error_detail(e)}
            return plan
        first = _first(plan["bindings"])
        if first:
            self.items.append((f"v1:{g.id}", f"v1:{g.id}", self._binding_fn(conn, g, first[1])))
        for stratum, plist in _by_stratum(plan["bindings"]).items():
            for j, params in enumerate(plist[: self.mb]):
                cid = f"v2:{g.id}:{stratum}"
                self.items.append((f"{cid}[{j}]", cid, self._binding_fn(conn, g, params)))
        return plan

    def plan_tests(self, conn) -> list[tuple[dict, tuple | None]]:
        planned = []
        for t in shape_q.tests(self.cfg, self.model):
            gate = self.gate(t["members"])
            planned.append((t, gate))
            if not gate:
                cid = f"v3:{t['id']}"
                self.items.append((cid, cid, self._test_fn(conn, t)))
        return planned

    def _binding_fn(self, conn, g, params: dict):
        def fn(client) -> str:
            return compare.status(compare.run_binding(conn, client, self.cfg, g, params, cache=self.cache))
        return fn

    def _test_fn(self, conn, t: dict):
        def fn(client) -> str:
            return shape_q.run_test(conn, client, self.cfg, self.model, t)[0]
        return fn

    def _needs_cube(self, plans: list, tests: list) -> bool:
        runnable = any(not p["gate"] and not p["problem"] and _first(p["bindings"]) for p in plans)
        return ((("v1" in self.only or "v2" in self.only) and runnable)
                or ("v3" in self.only and any(not gate for _t, gate in tests))
                or ("v4" in self.only and bool(self.items)))

    # ---- checks ----------------------------------------------------------------------------------
    def run(self, conn, golds: list):
        plans = [self.plan_gold(conn, i, g) for i, g in golds]
        tests = self.plan_tests(conn)
        if self._needs_cube(plans, tests):
            log = self.out / "verify" / "server.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.unlink(missing_ok=True)
            serving = cube_run.serve(self.cfg, self.out / "cube", log)
        else:
            serving = contextlib.nullcontext(None)
        with serving as client:
            if "v1" in self.only:
                for plan in plans:
                    self.v1(conn, client, plan)
            if "v2" in self.only:
                for plan in plans:
                    self.v2(conn, client, plan)
            if "v3" in self.only:
                for t, gate in tests:
                    self.v3(conn, client, t, gate)
            if "v4" in self.only:
                for iid, _cid, fn in self.items:
                    if iid not in self.baseline:
                        self.baseline[iid] = fn(client)
        if "v4" in self.only:
            self.v4(conn)

    def _blocked(self, cid: str, plan: dict) -> bool:
        if plan["gate"]:
            self._gated(cid, plan["gate"])
            return True
        if plan["problem"]:
            self.res.add(cid, "error", plan["ref"], **plan["problem"])
            return True
        return False

    def _gated(self, cid: str, gate: tuple):
        h, st = gate
        self.res.add(cid, "not_run", f"hierarchies.{h}", reason="closure_not_passed", hierarchy=h, build_check=st)
        self.res.not_proven.append(f"{cid}: closure:{h} did not pass in build ({st})")

    def v1(self, conn, client, plan: dict):
        g, ref = plan["gold"], plan["ref"]
        cid = f"v1:{g.id}"
        if self._blocked(cid, plan):
            return
        first = _first(plan["bindings"])
        if first is None:
            self.res.add(cid, "not_run", ref, reason="no_binding")
            self.res.not_proven.append(f"{cid}: no binding (every stratum is empty)")
            return
        r = compare.run_binding(conn, client, self.cfg, g, first[1], cache=self.cache)
        self.baseline[cid] = compare.status(r)
        self.res.add(cid, compare.status(r), ref, stratum=first[0], **_detail(r))

    def v2(self, conn, client, plan: dict):
        g, ref = plan["gold"], plan["ref"]
        if plan["gate"] or plan["problem"]:
            for s in sweep.strata_names(g):
                self._blocked(f"v2:{g.id}:{s}", plan)
            return
        for stratum, plist in _by_stratum(plan["bindings"]).items():
            cid = f"v2:{g.id}:{stratum}"
            if not plist:
                self.res.add(cid, "pass", ref, bindings=0, empty_stratum=True)
                self.res.not_proven.append(f"{cid}: empty stratum (no node id qualifies)")
                continue
            agg = _Sweep()
            for j, params in enumerate(plist):
                r = compare.run_binding(conn, client, self.cfg, g, params, cache=self.cache if j < self.mb else None)
                if j < self.mb:
                    self.baseline[f"{cid}[{j}]"] = compare.status(r)
                agg.add(j, r)
            self.res.add(cid, agg.status(), ref, **agg.detail())

    def v3(self, conn, client, t: dict, gate: tuple | None):
        cid = f"v3:{t['id']}"
        if gate:
            self._gated(cid, gate)
            return
        st, detail = shape_q.run_test(conn, client, self.cfg, self.model, t)
        self.baseline[cid] = st
        self.res.add(cid, st, t.get("config_ref", ""), **detail)

    def v4(self, conn):
        items = [(cid, fn) for iid, cid, fn in self.items if self.baseline.get(iid) == "pass"]
        dropped = len(self.items) - len(items)
        if dropped:
            self.res.not_proven.append(f"v4: {dropped} suite item(s) do not pass on the unmutated model and are not "
                                       "replayed against mutants")
        if not items:
            self.res.add("v4:suite", "not_run", "verify", reason="no_passing_suite_item", suite_items=len(self.items))
            return
        try:
            from . import mutate
        except ModuleNotFoundError as e:
            if e.name != f"{__package__}.mutate":
                raise
            self.res.add("v4:suite", "error", "", reason="mutate_module_missing")
            return

        def suite(client) -> tuple[bool, str]:
            for cid, fn in items:
                st = fn(client)
                if st != "pass":
                    return True, f"{cid} {st}"
            return False, ""

        checks, not_proven = mutate.run_mutants(self.cfg, self.out, conn, self.model, suite)
        for cid, st, ref, detail in checks:
            self.res.checks.append(Check(cid, st, dict(detail or {}), ref))
        self.res.not_proven.extend(not_proven)


def _next_action(res: Result) -> str:
    st = res.status
    if st == "pass":
        return "run `sxc.py report` to summarise every stage"
    first = next((c for c in res.checks if c.status == st), None)
    if first is None:
        return "nothing was verified: add gold queries or measures to crosscheck.toml, run build and cube, then verify"
    if st == "error":
        why = first.detail.get("error") or first.detail.get("reason") or "error"
        return (f"{first.id} could not run ({why}): fix the gold SQL, model or environment it points to (Cube errors are "
                "in verify/server.log), then rerun verify")
    if st == "fail":
        return (f"{first.id} disagrees: fix the crosscheck.toml entry {first.config_ref or 'named by the check'} or its "
                "gold SQL, rerun build and cube, then rerun verify")
    why = first.detail.get("reason", "not_run")
    return (f"{first.id} did not run ({why}): make its prerequisite pass (for a closure: build until closure:<h> passes, "
            "then cube), then rerun verify")

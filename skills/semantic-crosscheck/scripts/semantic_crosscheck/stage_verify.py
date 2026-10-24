"""verify: V1 strict compare, V2 strata sweep, V3 closure oracle/query shapes and V4 mutation tests.

Prerequisites are the cube stage (cube/model.json, cube/model/*.yml) and, for every gold query or V3 test that
uses a hierarchy's closure, ancestor or descendant cube, a passing `closure:<h>` check in build. Without it those
checks are not_run and listed as not proven: agreement with a closure that did not build proves nothing. A
verify.mutation_operators entry that names no operator is a configuration error (exit 2) before any check runs.

Before Cube starts, v3:closure_oracle:<h> independently compares the generated closure to exact Python raw-edge
calculations of qty, path_count, min_depth and max_depth. Both inputs share one READ ONLY REPEATABLE READ snapshot.
Resource bounds and unsupported types are not_run and not_proven; error-policy cycles fail. For on_cycle = report
the oracle removes the edges inside strongly connected components itself (iterative Tarjan) and a pass lists the
removed edge count in not_proven. An empty graph passes with a coverage disclosure. The oracle is not replayed
against V4's Cube mutants.

One Cube process (cube_run.serve on <out>/cube, log in verify/server.log) serves the other V1-V3 checks:
  v1:<gold>             the first binding of the gold query matches exactly; a gold query without any binding
                        (every stratum empty) is not_run, because it was never exercised
  v2:<gold>:<stratum>   every binding of the stratum matches exactly (rows_compared counts gold rows); an empty
                        stratum passes vacuously and is listed as not proven
  v3:<test>             the shape_q tests derived from the model; a pair test over more keys than
                        shape_q.PAIR_KEY_CAP but not more than verify.pair_key_budget compares every key in chunks
                        plus the NULL key group and says in not_proven that the chunks share no snapshot; above the
                        budget it compares a deterministic sample and says so in not_proven, with the sample's
                        detection bound and the budget. V4 replays such a test against its sample (the first chunk),
                        says so too, and marks a survivor of a suite with such an item sample_blind_possible; the
                        measure / class-column pairs Cube can join that get no additive test (a join path with a
                        one_to_many step) are listed in not_proven (shape_q.not_applicable)
Gold and Cube values are compared in the form of the Cube member type of each mapped column (compare.member_kinds of
the model): text exactly, numbers as exact decimals, dates and timestamps in UTC, booleans as booleans.

V4 hands mutate.run_mutants a suite that replays V1 (first binding of every gold query), V2 (the first
verify.mutant_bindings bindings of every stratum) and V3 against a mutant's Cube and stops at the first item that
does not pass. A failing item kills the mutant (killed_by names the item); an item that still errors after one retry
leaves the mutant unjudged (an error check with reason suite_error), because a query that could not run proves
nothing. Only items that pass against the unmutated model are replayed, so a mutant is never "killed" by a defect
the model already has; gold rows are computed once and reused for every mutant. The closure operators are gated by
build's closure:<h> like V1-V3. When items are missing from the suite because a closure gated them, their gold query
could not be planned or --gold left it out, a surviving mutant is not_run (reason suite_incomplete) rather than
failed: the missing items might have killed it.

--only and --gold narrow a run without hiding it: every part --only leaves out is a not_run check scope:<part>
(reason excluded_by_only) and every gold query --gold leaves out a not_run check scope:gold:<id> (reason
excluded_by_gold), so a narrowed run never passes.

Results hold counts, check ids and positions in the deterministic binding order, never parameter values or rows.
"""

import contextlib
from pathlib import Path
from typing import Callable

from . import closure_oracle, compare, cube_gen, cube_run, db, mutate, naming, shape_q, sweep
from .result import EXIT_CONFIG, Check, Result, StageError, check_status, require_stage

PARTS = ("v1", "v2", "v3", "v4")
MAX_INDEXES = 10
_SUMS = ("missing_in_cube", "extra_in_cube", "duplicate_keys_gold", "duplicate_keys_cube", "declared_null_as_zero")


def run(cfg, args, out: Path) -> Result:
    out = Path(out)
    only = _only(args)
    golds = _golds(cfg, args)
    mutate.selected(cfg)
    require_stage(out, "cube")
    model = _model(out)
    v = _Verify(cfg, out, model, only, golds)
    conn = db.connect(cfg.source.dsn_env)
    try:
        v.run(conn)
    finally:
        conn.close()
    v.scope()
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
        raise StageError("run `sxc.py cube` first (no generated model in the output directory)", EXIT_CONFIG)
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


def mutation_suite(items: list) -> Callable:
    """The V4 suite over (item id, function(client) -> status) pairs, as mutate.run_mutants expects it: the first item
    that does not pass as (status, item id), or ("pass", "") when every item passes. An item that errors is retried
    once, so a passing lock wait or timeout is not taken for a verdict."""
    def suite(client) -> tuple[str, str]:
        for iid, fn in items:
            st = fn(client)
            if st == "error":
                st = fn(client)
            if st != "pass":
                return st, iid
        return "pass", ""
    return suite


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
    def __init__(self, cfg, out: Path, model: dict, only: list[str], golds: list | None = None):
        self.cfg, self.out, self.model, self.only = cfg, out, model, only
        self.golds = list(enumerate(cfg.gold)) if golds is None else golds
        self.res = Result("verify")
        self.known = cube_gen.members(model)
        self.kinds = compare.member_kinds(model)
        self.closure = {h: check_status(out, "build", f"closure:{h}") for h in cfg.hierarchies}
        self.mb = cfg.verify.mutant_bindings
        self.cache: dict | None = {} if "v4" in only else None
        self.items: list[tuple[str, str, object]] = []   # V4 suite: (item id, check id, function(client) -> status)
        self.baseline: dict[str, str] = {}               # item id -> status against the unmutated model
        self.suite_tests: list[dict] = []                # V3 tests in the V4 suite
        # why suite items are missing (a survivor of an incomplete suite is not judged)
        self.excluded: set[str] = {"excluded_by_gold"} if len(self.golds) < len(cfg.gold) else set()

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
        plan = {"gold": g, "ref": f"gold[{i}] {g.id}", "bindings": [], "gate": self.gate(members), "problem": None,
                "types": compare.gold_kinds(g, self.kinds)}
        if plan["gate"]:
            self.excluded.add("closure_not_passed")
            return plan
        missing = sorted(members - self.known)
        if missing:
            plan["problem"] = {"reason": "members_not_in_model", "missing_members": missing}
            self.excluded.add("gold_not_planned")
            return plan
        try:
            plan["bindings"] = sweep.bindings(conn, self.cfg, g)
        except compare.expected_errors() as e:
            plan["problem"] = {"reason": "bindings_failed", **compare.error_detail(e)}
            self.excluded.add("gold_not_planned")
            return plan
        first = _first(plan["bindings"])
        if first:
            self.items.append((f"v1:{g.id}", f"v1:{g.id}", self._binding_fn(conn, g, first[1], plan["types"])))
        for stratum, plist in _by_stratum(plan["bindings"]).items():
            for j, params in enumerate(plist[: self.mb]):
                cid = f"v2:{g.id}:{stratum}"
                self.items.append((f"{cid}[{j}]", cid, self._binding_fn(conn, g, params, plan["types"])))
        return plan

    def plan_tests(self, conn) -> list[tuple[dict, tuple | None]]:
        planned = []
        for t in shape_q.tests(self.cfg, self.model):
            gate = self.gate(t["members"])
            planned.append((t, gate))
            if gate:
                self.excluded.add("closure_not_passed")
            else:
                cid = f"v3:{t['id']}"
                self.items.append((cid, cid, self._test_fn(conn, t)))
                self.suite_tests.append(t)
        return planned

    def _binding_fn(self, conn, g, params: dict, types: dict):
        def fn(client) -> str:
            return compare.status(compare.run_binding(conn, client, self.cfg, g, params, cache=self.cache, types=types))
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
    def run(self, conn):
        if "v3" in self.only:
            self.closure_oracles(conn)
        plans = [self.plan_gold(conn, i, g) for i, g in self.golds]
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
                self.res.not_proven.extend(shape_q.not_applicable(self.cfg, self.model))
            if "v4" in self.only:
                for iid, _cid, fn in self.items:
                    if iid not in self.baseline:
                        self.baseline[iid] = fn(client)
        if "v4" in self.only:
            self.v4(conn)

    def closure_oracles(self, conn):
        """Raw-edge expected closures are database checks, so finish their snapshots before Cube starts."""
        for name, hierarchy in self.cfg.hierarchies.items():
            cid = f"v3:closure_oracle:{name}"
            build_status = self.closure[name]
            if build_status != "pass":
                self._gated(cid, (name, build_status or "missing"))
                continue
            status, detail = closure_oracle.run(conn, self.cfg, hierarchy)
            self.res.add(cid, status, f"hierarchies.{name}", **detail)
            if status == "not_run":
                self.res.not_proven.append(f"{cid}: independent raw-edge closure comparison not proven "
                                           f"({detail['reason']})")
            elif detail.get("empty_graph") and detail.get("dropped_cycle_edges"):
                self.res.not_proven.append(f"{cid}: every logical edge lay inside a cycle and was removed by "
                                           "on_cycle = report; no reachable pair was exercised")
            elif detail.get("empty_graph"):
                self.res.not_proven.append(f"{cid}: empty raw-edge graph; no reachable pair was exercised")
            if status == "pass" and detail.get("dropped_cycle_edges"):
                self.res.not_proven.append(f"{cid}: on_cycle = report removed {detail['dropped_cycle_edges']} logical "
                                           "edge(s) inside cycles before comparing; paths through them are left out "
                                           "of the closure by policy")

    def scope(self):
        """One not_run check per part --only left out and per gold query --gold left out."""
        for part in PARTS:
            if part not in self.only:
                self.res.add(f"scope:{part}", "not_run", "", reason="excluded_by_only")
        kept = {g.id for _i, g in self.golds}
        for g in self.cfg.gold:
            if g.id not in kept:
                self.res.add(f"scope:gold:{g.id}", "not_run", "", reason="excluded_by_gold")

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
        r = compare.run_binding(conn, client, self.cfg, g, first[1], cache=self.cache, types=plan["types"])
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
                r = compare.run_binding(conn, client, self.cfg, g, params, cache=self.cache if j < self.mb else None,
                                        types=plan["types"])
                if j < self.mb:
                    self.baseline[f"{cid}[{j}]"] = compare.status(r)
                agg.add(j, r)
            self.res.add(cid, agg.status(), ref, **agg.detail())

    def v3(self, conn, client, t: dict, gate: tuple | None):
        cid = f"v3:{t['id']}"
        if gate:
            self._gated(cid, gate)
            return
        st, detail = shape_q.run_test(conn, client, self.cfg, self.model, t, budget=self.cfg.verify.pair_key_budget)
        self.baseline[cid] = st
        self.res.add(cid, st, t.get("config_ref", ""), **detail)
        if "sampled_keys" in detail:
            self.res.not_proven.append(f"{cid}: {shape_q.sample_note(detail)}")
        elif detail.get("mode") == "chunked" and st != "error":
            self.res.not_proven.append(f"{cid}: {shape_q.chunk_note(detail)}")

    def v4(self, conn):
        items = [(iid, fn) for iid, _cid, fn in self.items if self.baseline.get(iid) == "pass"]
        dropped = len(self.items) - len(items)
        if dropped:
            self.res.not_proven.append(f"v4: {dropped} suite item(s) do not pass on the unmutated model and are not "
                                       "replayed against mutants")
        if not items:
            self.res.add("v4:suite", "not_run", "verify", reason="no_passing_suite_item", suite_items=len(self.items))
            return
        replayed = {iid for iid, _fn in items}
        sampled = [f"v3:{t['id']}" for t in self.suite_tests
                   if shape_q.sampled(t) and f"v3:{t['id']}" in replayed and len(t["sample_keys"]) < t["total_keys"]]
        checks, not_proven = mutate.run_mutants(self.cfg, self.out, conn, self.model, mutation_suite(items),
                                                closure=self.closure, sampled_items=sampled)
        for cid, st, ref, detail in checks:
            detail = dict(detail or {})
            if st == "fail" and self.excluded:
                st, detail = "not_run", {**detail, "reason": "suite_incomplete", "excluded": sorted(self.excluded)}
                self.res.not_proven.append(f"{cid}: survived a suite with items left out ({', '.join(sorted(self.excluded))}),"
                                           " which might have killed it; not judged")
            self.res.checks.append(Check(cid, st, detail, ref))
        self.res.not_proven.extend(not_proven)
        partial = [f"v3:{t['id']}" for t in self.suite_tests if shape_q.sampled(t)]
        if partial:
            self.res.not_proven.append(f"v4: the suite's pair tests compare a sample of their keys ({', '.join(partial)}), "
                                       "so a mutant that changes only other keys can survive them")


# ---- next action -------------------------------------------------------------------------------------
def _error_action(c: Check) -> str:
    d, ref = c.detail, c.config_ref or "named by the check"
    reason = d.get("reason")
    if reason == "suite_error":
        culprit = f"suite item {d['item']}" if d.get("item") else f"the suite ({d.get('exception', 'error')})"
        return (f"{c.id} was not judged: {culprit} could not run against the mutant; make sure PostgreSQL and Cube stay "
                "reachable for the whole run (connection limits, timeouts, privileges), then rerun verify")
    if reason == "scc_removal_left_cycle":
        return (f"{c.id}: the independent oracle still found a cycle after removing the edges inside strongly "
                "connected components (an internal defect of the oracle, not of the data); report it with the check "
                "detail and do not treat the closure as verified")
    if reason == "write":
        return f"{c.id}: the mutant model could not be written under verify/mutants; check the --out directory, then rerun verify"
    if reason in ("startup", "cube"):
        return (f"{c.id}: the mutant's Cube process could not serve ({reason}); check Node.js and the adapter with "
                "`sxc.py doctor --require cube`, then rerun verify")
    if reason == "model_mismatch":
        return "the Cube model does not match crosscheck.toml: rerun `sxc.py cube`, then rerun verify"
    if reason == "members_not_in_model":
        return f"{c.id} uses Cube members the model lacks: fix cube_query and map of {ref}, or rerun `sxc.py cube`"
    if d.get("cube") == "timeout":
        return f"{c.id} timed out in Cube: raise [cube].query_timeout_s or narrow the query of {ref}, then rerun verify"
    if d.get("cube") == "row_limit":
        return f"{c.id} reached Cube's row limit: narrow the query of {ref} (filters or parameters), then rerun verify"
    if d.get("sqlstate") == "57014":
        return f"{c.id} hit source.statement_timeout_ms: raise it in crosscheck.toml, then rerun verify"
    if d.get("http_status"):
        return (f"Cube rejected the query of {c.id} (HTTP {d['http_status']}): check the members, filters and "
                f"parameters of {ref}, then rerun verify")
    why = d.get("sqlstate") or d.get("error") or reason or "error"
    return (f"{c.id} could not run ({why}): fix the gold SQL, the crosscheck.toml entry {ref} or the environment it "
            "points to (the check detail has the error type, SQLSTATE or HTTP status), then rerun verify")


def _not_run_action(c: Check) -> str:
    why = c.detail.get("reason", "not_run")
    if c.id.startswith("v3:closure_oracle:") and why == "budget_exceeded":
        return (f"{c.id} exceeded the independent oracle limit {c.detail['limit']}: use a smaller hierarchy or review "
                "closure_oracle.Limits before raising the bound; no partial closure was certified")
    if why == "suite_incomplete":
        return (f"{c.id} survived a suite with items left out ({', '.join(c.detail.get('excluded', []))}): make every "
                "gold query and closure run (full verify, passing builds), then rerun verify")
    if why == "no_passing_suite_item":
        return "V4 needs suite items that pass on the unmutated model: fix the V1-V3 checks first, then rerun verify"
    if why == "no_binding":
        return f"{c.id} has no binding (every stratum is empty): check the params of {c.config_ref} or the data"
    return (f"{c.id} did not run ({why}): make its prerequisite pass (for a closure: build until closure:<h> passes, "
            "then cube), then rerun verify")


def _next_action(res: Result) -> str:
    st = res.status
    if st == "pass":
        return "run `sxc.py report` to summarise every stage"
    checks = [c for c in res.checks if not c.id.startswith("scope:")]
    first = next((c for c in checks if c.status == st), None)
    if first is None:
        if len(checks) < len(res.checks):
            return ("--only or --gold left parts of verify out (the scope:* checks) and everything that ran passed: "
                    "rerun verify without them before reporting a result")
        return "nothing was verified: add gold queries or measures to crosscheck.toml, run build and cube, then verify"
    if st == "error":
        return _error_action(first)
    if st == "fail" and first.id.startswith("v4:"):
        return (f"{first.id} survived: no gold query observes this kind of error in {first.config_ref}; add a gold "
                "query, a mapped column or a stratum that would, then rerun verify (do not drop the operator)")
    if st == "fail" and first.id.startswith("v3:closure_oracle:"):
        return (f"{first.id}: raw-edge expectations disagree with the generated closure in {first.config_ref}; "
                "inspect the source edges and generated closure, rerun build and cube, then rerun verify")
    if st == "fail":
        return (f"{first.id} disagrees: fix the crosscheck.toml entry {first.config_ref or 'named by the check'} or its "
                "gold SQL, rerun build and cube, then rerun verify")
    return _not_run_action(first)

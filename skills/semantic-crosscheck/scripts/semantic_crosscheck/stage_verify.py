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
                        detection bound and the budget. V4 replays every bounded chunk plus the NULL group; only
                        above that key budget is a survivor marked sample_blind_possible; the
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

A passing V4 check means a suite item failed against the mutant, not that a gold query did: the suite also holds V3
items, which compare generated layers. The suite therefore runs authored gold items (and gold items of unknown kind)
first, then gold items whose SQL contains generated text, then every other item (a stable sort, so without such a gold
the planned order stays). Each V4 check with killed_by also records detection (authored, generated_only,
unattributed or none: the kind of the item that killed it; generated_only means every replayed authored gold item
passed against the mutant), facet (what its operator changes) and gold_comparing (the authored gold queries whose map
compares that facet). A kill by a generated item or by an item of no known kind gets a not_proven line with a remedy
for its operator, unless verify.accepted_gaps lists the check. verify/mutation-coverage.json gains the detection of
each killed or surviving mutant and the kind of each receipt item.

--only and --gold narrow a run without hiding it: every part --only leaves out is a not_run check scope:<part>
(reason excluded_by_only) and every gold query --gold leaves out a not_run check scope:gold:<id> (reason
excluded_by_gold), so a narrowed run never passes.

Evidence (the `evidence` key of verify/result.json; no other stage writes it; statuses do not change): at plan time
each gold file is read once and matched for generated text (generated_text: the output schema as a qualifier, the bare
name of a generated relation, a verbatim measure filter clause); a match makes the gold's kind generated, and a
not_proven line names what matched. Per gold query verify counts the V1/V2 comparisons that ran, those with a row on
either side and, per compared member (gold.map, not filters, time dimensions or segments), the matched rows whose gold
value is not NULL, 0 or empty (value_rows, kept out of check details); a gold query whose every comparison had no row
gets a not_proven line, and every V1/V2 check that compared no row is listed. Only on full-scope runs (every part, no
--gold) is every facet of a measure, hierarchy and the class column judged: compared with rows holding values by an
authored gold query, detected by an authored V4 kill, accepted by verify.accepted_gaps, or a gap with its reason, and
every element with a gap or an accepted gap gets one not_proven line. Its mutation key counts the selected operators
and puts every selected mutant in one bucket (killed with an authored detection, killed by generated checks only,
unattributed, accepted, survived, not judged, equivalent, not applicable).

verify.gold_observation = "require" (opt-in; the default "disclose" changes no status) turns these disclosures into
statuses, and only ever a pass into a fail or not_run: a V4 kill that only a generated item (or an item of no known
kind) made fails with reason generated_only or unattributed, or is not_run (suite_incomplete) when suite items were left
out, unless verify.accepted_gaps lists the check; killed_by still names the first failing item, so read detection. On
full-scope runs every measure, hierarchy and ontology.class_column gets a check gold_evidence:<element>: pass when none
of its facets is a gap, not_run when every gap is gold_not_run or not_in_model, else fail with the reason of its first
other gap. A passing v1:<gold> of a gold query whose every comparison had zero rows is not_run (no_rows_compared); an
error or a fail stays. Operators verify.mutation_operators leaves out are named in a not_proven line and in the detail
of every gold_evidence check.

Results hold counts, check ids and positions in the deterministic binding order, never parameter values or rows.
"""

import contextlib
import re
from pathlib import Path
from typing import Callable

from . import (closure_oracle, compare, cube_gen, cube_run, data_rules, db, evidence, mutate, mutation_coverage, naming,
               shape_q, sweep)
from .result import EXIT_CONFIG, Check, Result, StageError, check_status, require_stage

PARTS = ("v1", "v2", "v3", "v4")
MAX_INDEXES = 10
MAX_LISTED = 10                    # ids named in one evidence line; the rest are counted
EVIDENCE_SCHEMA = 1
_SUMS = ("missing_in_cube", "extra_in_cube", "duplicate_keys_gold", "duplicate_keys_cube", "declared_null_as_zero")
NOT_RUN_REASONS = ("gold_not_run", "not_in_model")   # facet gaps that leave a gold_evidence:<element> check not_run
_SPACE = re.compile(r"\s+")
# Any rule view v_rule_<id>: verify does not read the data-rules file, so the match is reported as one fixed item and
# never as the text it matched (that text may be the content of a literal). ASCII: [a-z] must not match U+017F.
_RULE_VIEW = re.compile(r'(?<![A-Za-z0-9_."])"?' + re.escape(data_rules.VIEW_PREFIX)
                        + r'[a-z0-9_]{1,56}"?(?![A-Za-z0-9_])', re.IGNORECASE | re.ASCII)
RULE_VIEWS = f"relation:{data_rules.VIEW_PREFIX}*"


# ---- generated text in gold SQL ------------------------------------------------------------------------
def gold_text(raw: str) -> str:
    """Gold SQL as it is matched for generated text: comments (-- to the end of the line, /* */, nested) removed outside
    quoted literals and identifiers, whitespace runs collapsed to one space. No other normalisation."""
    out, i, n, quote = [], 0, len(raw), ""
    while i < n:
        ch = raw[i]
        if quote:
            out.append(ch)
            quote = "" if ch == quote else quote
            i += 1
        elif ch in "'\"":
            out.append(ch)
            quote = ch
            i += 1
        elif raw.startswith("--", i):
            end = raw.find("\n", i)
            i = n if end < 0 else end
            out.append(" ")
        elif raw.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if raw.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif raw.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return _SPACE.sub(" ", "".join(out))


def _relation(name: str) -> re.Pattern:
    return re.compile(r'(?<![A-Za-z0-9_."])"?' + re.escape(name) + r'"?(?![A-Za-z0-9_])', re.IGNORECASE | re.ASCII)


def generated_text(cfg, sql: str) -> list[str]:
    """What of the generated layers a gold SQL text contains verbatim, matched in its gold_text form, in this order:

      output_schema           the output schema as a qualifier ("sxc_out".x, sxc_out . x)
      relation:<name>         the bare name of a generated relation: <h>_closure and <h>_cycles of every hierarchy,
                              v_shape_<id> of every shape, any v_rule_<id> (one item, relation:v_rule_*, whatever it
                              matched) and v_rules_subjects (gold SQL runs with the connection's own search_path, so a
                              gold can read them without naming the schema)
      filter:measures.<m>     a filter clause of cube_gen.where_clauses(m) after the first (the non-null grain clause
                              is too common to count)

    Paraphrased copies are not found. A source table or CTE named like a generated relation is also found; that only
    lowers the kind of the gold query. Every item is built from the configuration or is fixed text: nothing the gold SQL
    holds is returned. Case folding is ASCII only, as PostgreSQL folds unquoted identifiers."""
    text = gold_text(sql)
    found = []
    schema = re.compile(r'(?<![A-Za-z0-9_"])"?' + re.escape(cfg.source.output_schema) + r'"?\s*\.',
                        re.IGNORECASE | re.ASCII)
    if schema.search(text):
        found.append("output_schema")
    names = [n for h in cfg.hierarchies for n in (naming.closure_table(cfg, h).split(".", 1)[1],
                                                 naming.cycles_table(cfg, h).split(".", 1)[1])]
    names += [naming.shape_view(cfg, s.id).split(".", 1)[1] for s in cfg.shapes]
    found += [f"relation:{n}" for n in names if _relation(n).search(text)]
    if _RULE_VIEW.search(text):
        found.append(RULE_VIEWS)
    if _relation(data_rules.SUBJECTS_VIEW).search(text):
        found.append(f"relation:{data_rules.SUBJECTS_VIEW}")
    for m in cfg.measures.values():
        if any(_SPACE.sub(" ", clause) in text for clause in cube_gen.where_clauses(m)[1:]):
            found.append(f"filter:measures.{m.name}")
    return list(dict.fromkeys(found))


def _cut(ids: list) -> str:
    """Ids for a line: at most MAX_LISTED, then how many more."""
    text = ", ".join(ids[:MAX_LISTED])
    return text + (f", and {len(ids) - MAX_LISTED} more" if len(ids) > MAX_LISTED else "")


def _killer(d: dict) -> str:
    """The item that killed a mutant no authored gold item detected, with what kind of check it is."""
    if d.get("detection") == "generated_only":
        return f"{d.get('killed_by')}, a check between generated layers"
    return f"{d.get('killed_by')}, which no evidence rule classifies"


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
    v.res.evidence = v.evidence_object()
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
    """A comparison as a check detail: without its verdict and its value_rows (kept for the evidence object)."""
    return {k: v for k, v in r.items() if k not in ("pass", "value_rows")}


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
        self.value_rows: dict[str, int] = {}   # per gold column, summed for the evidence object (not in detail())

    def add(self, j: int, r: dict):
        self.bindings += 1
        self.rows += r.get("gold_rows", 0)
        self.empty += 1 if r.get("empty") else 0
        for k in _SUMS:
            self.sums[k] += r.get(k, 0)
        for col, n in r.get("mismatched_columns", {}).items():
            self.mismatched[col] = self.mismatched.get(col, 0) + n
        for col, n in r.get("value_rows", {}).items():
            self.value_rows[col] = self.value_rows.get(col, 0) + n
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
        # evidence: facets are judged only when every part ran over every gold query
        self.mode = cfg.verify.gold_observation   # disclose (default) or require
        self.full = set(only) == set(PARTS) and len(self.golds) == len(cfg.gold)
        self.gold_kinds: dict[str, str] = {}             # gold id -> authored | generated (stored at plan time)
        self.gold_found: dict[str, list[str]] = {}       # gold id -> generated_text matches
        self.counts: dict[str, dict] = {}                # gold id -> comparisons per part and value_rows per member
        self.facet_rows: list[dict] | None = None
        self.ledger: list[dict] = []                     # V4 coverage rows (verify/mutation-coverage.json "mutants")

    # ---- planning (database only, before Cube starts) ---------------------------------------------
    def gate(self, members) -> tuple[str, str] | None:
        """(hierarchy, build status) when the members use the cubes of a closure that did not pass in build."""
        cubes = {m.split(".", 1)[0] for m in members}
        for h, st in self.closure.items():
            if st != "pass" and cubes & {naming.cube_closure(h), naming.cube_ancestor(h), naming.cube_descendant(h)}:
                return h, st or "missing"
        return None

    def note_gold(self, g):
        """Read the gold file once (the file inputs_fingerprint hashes) and store the gold's kind: generated when its
        text contains generated text, else authored. Only the matches are kept, never the text. A file that cannot be
        read is not flagged; its comparisons error as before."""
        try:
            raw = g.sql_path(self.cfg).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            raw = None
        found = generated_text(self.cfg, raw) if raw is not None else []
        self.gold_found[g.id] = found
        self.gold_kinds[g.id] = "generated" if found else "authored"
        self.counts[g.id] = {"v1": [0, 0], "v2": [0, 0], "value_rows": dict.fromkeys(sorted(set(g.map.values())), 0)}

    def _count(self, g, part: str, comparisons: int, nonempty: int, value_rows: dict):
        """Comparisons that ran without error, those with a row on either side, and value_rows per compared member."""
        c = self.counts.get(g.id)
        if c is None:
            return
        c[part][0] += comparisons
        c[part][1] += nonempty
        for col, n in value_rows.items():
            member = g.map.get(col)
            if member is not None:
                c["value_rows"][member] = c["value_rows"].get(member, 0) + n

    def plan_gold(self, conn, i: int, g) -> dict:
        self.note_gold(g)
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
            return shape_q.replay_test(conn, client, self.cfg, self.model, t)[0]
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
        self.gold_evidence()
        if "v4" in self.only:
            self.v4(conn)
        self.operators_line()
        self.facets()

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
        if "error" not in r:
            self._count(g, "v1", 1, 0 if r.get("empty") else 1, r.get("value_rows", {}))

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
            ran = agg.bindings - agg.errored
            self._count(g, "v2", ran, ran - agg.empty, agg.value_rows)

    def v3(self, conn, client, t: dict, gate: tuple | None):
        cid = f"v3:{t['id']}"
        if gate:
            self._gated(cid, gate)
            return
        witness = (shape_q.PairWitness(conn, self.cfg, t)
                   if self.cfg.verify.existence_witness and t["kind"] == "pair" else None)
        st, detail = shape_q.run_test(conn, client, self.cfg, self.model, t, budget=self.cfg.verify.pair_key_budget,
                                      **({"witness": witness} if witness is not None else {}))
        self.baseline[cid] = st
        self.res.add(cid, st, t.get("config_ref", ""), **detail)
        if "sampled_keys" in detail:
            self.res.not_proven.append(f"{cid}: {shape_q.sample_note(detail)}")
        elif detail.get("mode") == "chunked" and st != "error":
            self.res.not_proven.append(f"{cid}: {shape_q.chunk_note(detail)}")
        outcome = witness.result(st) if witness is not None else None
        if outcome is not None:
            # a separate check: the pair test's own status, detail and V4 baseline stay as they are
            wid = shape_q.witness_id(t)
            self.res.add(wid, outcome[0], t.get("config_ref", ""), **outcome[1])
            if outcome[1].get("reason") != "pair_error":
                self.res.not_proven.append(f"{wid}: {shape_q.witness_note()}")

    def v4(self, conn):
        items = [(iid, fn) for iid, _cid, fn in self.items if self.baseline.get(iid) == "pass"]
        excluded_items = [{"id": iid, "baseline_status": self.baseline.get(iid, "not_run")}
                          for iid, _cid, _fn in self.items if self.baseline.get(iid) != "pass"]
        dropped = len(self.items) - len(items)
        if dropped:
            self.excluded.add("baseline_not_passed")
            self.res.not_proven.append(f"v4: {dropped} suite item(s) do not pass on the unmutated model and are not "
                                       "replayed against mutants")
        if not items:
            self.res.add("v4:suite", "not_run", "verify", reason="no_passing_suite_item", suite_items=len(self.items))
            suite = mutation_coverage.Suite(evidence.order_suite([(iid, fn) for iid, _cid, fn in self.items],
                                                                 self.gold_kinds),
                                            max_items=self.cfg.verify.mutant_max_items)
            self.ledger = mutate.unrun_coverage(self.cfg, suite, "no_passing_suite_item")
            self._annotate(self.ledger, [])
            mutation_coverage.write(self.out, self.ledger, max_items=self.cfg.verify.mutant_max_items,
                                    reason="no_passing_suite_item", excluded_items=excluded_items)
            return
        pair_tests = {f"v3:{t['id']}": t for t in self.suite_tests}
        expanded = []
        for iid, fn in items:
            parts = shape_q.replay_parts(pair_tests[iid]) if iid in pair_tests else []
            if parts:
                expanded.extend((iid + suffix, part) for suffix, part in parts)
            else:
                expanded.append((iid, fn))
        replayed = {iid for iid, _fn in items}
        sampled = [f"v3:{t['id']}" for t in self.suite_tests
                   if shape_q.sampled(t) and t.get("replay_keys") is None and f"v3:{t['id']}" in replayed
                   and len(t["sample_keys"]) < t["total_keys"]]
        ledger = []
        # Authored gold items first, so the first failing item tells whether a gold item detected the mutant.
        checks, not_proven = mutate.run_mutants(
            self.cfg, self.out, conn, self.model,
            mutation_coverage.Suite(evidence.order_suite(expanded, self.gold_kinds),
                                    max_items=self.cfg.verify.mutant_max_items),
            closure=self.closure, sampled_items=sampled, coverage=ledger)
        self.ledger = ledger
        summaries = next((dict(detail["summary"]) for _cid, _st, _ref, detail in checks
                          if detail and "summary" in detail), None)
        accepted = set(self.cfg.verify.accepted_gaps)
        changed = []
        for cid, st, ref, detail in checks:
            detail = dict(detail or {})
            if "killed_by" in detail:
                detail = self._attribute(cid, detail)
            if self.mode == "require" and st == "pass" and cid not in accepted \
                    and detail.get("detection") in ("generated_only", "unattributed"):
                # require mode: no replayed authored gold item detected the mutant, so the kill is not a pass. It
                # counts as survived (or, with suite items left out that might have detected it, as not judged).
                if self.excluded:
                    row = {"outcome": "not_run", "reason": "suite_incomplete", "excluded": sorted(self.excluded)}
                else:
                    row = {"outcome": "survived", "reason": detail["detection"]}
                st = "fail" if row["outcome"] == "survived" else "not_run"
                detail = {**detail, **{k: v for k, v in row.items() if k != "outcome"}}
                if summaries is not None:
                    summaries["killed"] -= 1
                    summaries[row["outcome"]] += 1
                for ledger_row in ledger:
                    if ledger_row["id"] == cid:
                        ledger_row.update(row)
            elif st == "fail" and self.excluded:
                if summaries is not None:
                    summaries["survived"] -= 1
                    summaries["not_run"] += 1
                st, detail = "not_run", {**detail, "reason": "suite_incomplete", "excluded": sorted(self.excluded)}
                for row in ledger:
                    if row["id"] == cid:
                        row.update(outcome="not_run", reason="suite_incomplete", excluded=sorted(self.excluded))
                self.res.not_proven.append(f"{cid}: survived a suite with items left out ({', '.join(sorted(self.excluded))}),"
                                           " which might have killed it; not judged")
            changed.append(Check(cid, st, detail, ref))
        for check in changed:
            if summaries is not None and "summary" in check.detail:
                check.detail["summary"] = dict(summaries)
            self.res.checks.append(check)
        self._annotate(ledger, changed)
        mutation_coverage.write(self.out, ledger, max_items=self.cfg.verify.mutant_max_items,
                                excluded_items=excluded_items)
        self.res.not_proven.extend(not_proven)
        self.res.not_proven.extend(self._kill_lines(changed))
        partial =[f"v3:{t['id']}" for t in self.suite_tests if shape_q.sampled(t) and t.get("replay_keys") is None]
        if partial:
            self.res.not_proven.append(f"v4: the suite's pair tests compare a sample of their keys ({', '.join(partial)}), "
                                       "so a mutant that changes only other keys can survive them")

    # ---- V4 attribution --------------------------------------------------------------------------
    def _facet_of(self, cid: str) -> str:
        """The facet a V4 mutant check changes ("" for an id that names no known operator)."""
        parts = cid.split(":")
        return evidence.operator_facet(self.cfg, parts[1], parts[2]) if len(parts) == 3 else ""

    def _attribute(self, cid: str, detail: dict) -> dict:
        """A V4 detail with a killed_by key gains, right after it, detection (the kind of the killing item), facet (what
        the operator changes) and gold_comparing (the authored gold queries of this run whose map compares that
        facet). A detail without killed_by stays as it is."""
        facet = self._facet_of(cid)
        f = next((x for x in evidence.facets(self.cfg, self.model) if x.id == facet), None)
        comparing = [g.id for _i, g in self.golds
                     if f is not None and self.gold_kinds.get(g.id) == "authored" and f.compared(set(g.map.values()))]
        out = {}
        for key, value in detail.items():
            if key in ("detection", "facet", "gold_comparing"):
                continue
            out[key] = value
            if key == "killed_by":
                out.update(detection=evidence.detection(value, self.gold_kinds), facet=facet, gold_comparing=comparing)
        return out

    def _annotate(self, ledger: list, checks: list):
        """verify/mutation-coverage.json: each mutant row with killed_by gains the detection of its check, and each
        receipt item (in the rows and in the V4 check details) gains its kind."""
        detections = {c.id: c.detail["detection"] for c in checks if "detection" in c.detail}
        receipts = [row.get("coverage") for row in ledger] + [c.detail.get("coverage") for c in checks]
        for receipt in receipts:
            for item in (receipt or {}).get("items", []) if isinstance(receipt, dict) else []:
                item["kind"] = evidence.item_kind(item.get("id"), self.gold_kinds)
        for row in ledger:
            if "killed_by" in row:
                row["detection"] = detections.get(row["id"], evidence.detection(row["killed_by"], self.gold_kinds))

    def _kill_lines(self, checks: list) -> list[str]:
        """not_proven lines of the V4 mutants that only a generated item (N5) or an item of no known kind (N6) killed;
        a kill listed in verify.accepted_gaps gets the accepted line (N5a) instead. In require mode such a kill is a
        fail, whose status says it, or, with suite items left out, not judged with its own line (N5b)."""
        accepted = set(self.cfg.verify.accepted_gaps)
        lines = []
        for c in checks:
            d = c.detail
            if d.get("detection") not in ("generated_only", "unattributed"):
                continue
            item = d["killed_by"]
            if c.id in accepted:
                lines.append(f"{c.id}: killed only by {item}, a check between generated layers or of no known kind; "
                             "accepted by verify.accepted_gaps, so this is not proven")
            elif self.mode != "disclose":
                if c.status == "not_run" and d.get("reason") == "suite_incomplete":
                    lines.append(f"{c.id}: killed only by {_killer(d)}; gold items were left out "
                                 f"({', '.join(d.get('excluded', []))}), which might have detected it; not judged")
            elif d["detection"] == "generated_only":
                parts = c.id.split(":") + ["", ""]
                lines.append(f"{c.id}: killed by {item}, a check between generated layers; no replayed gold item "
                             f"detected this change to {c.config_ref} ({self._readers(d)}); "
                             f"{evidence.remedy(parts[1], parts[2])}")
            else:
                lines.append(f"{c.id}: killed by {item}, which no evidence rule classifies; no replayed gold item is "
                             f"shown to have detected this change to {c.config_ref}")
        return lines

    @staticmethod
    def _readers(d: dict) -> str:
        facet = d.get("facet") or "the changed element"
        if d.get("gold_comparing"):
            return f"gold queries that compare {facet}: {_cut(d['gold_comparing'])}"
        return f"no authored gold query compares {facet}"

    def _bucket(self, c: Check, accepted: set) -> str:
        """The bucket of a mutant that has a V4 check (first that applies)."""
        if c.status in ("not_run", "error"):
            return "not_judged"
        killer, found = c.detail.get("killed_by", ""), c.detail.get("detection")
        if not killer:
            return "survived" if c.status == "fail" else "unattributed"
        if found in ("generated_only", "unattributed") and c.id in accepted:
            return "accepted"
        return {"authored": "killed_authored", "generated_only": "killed_generated_only"}.get(found, "unattributed")

    def mutation_split(self) -> dict:
        """The selected operators and every selected mutant in one bucket, counted from the coverage ledger rows (a
        mutant without a check: equivalent, not_applicable, or not judged) and the V4 checks."""
        selected = mutate.selected(self.cfg)
        accepted = set(self.cfg.verify.accepted_gaps)
        checks = {c.id: c for c in self.res.checks if c.id.startswith("v4:") and len(c.id.split(":")) == 3}
        outcomes = {row["id"]: row.get("outcome") for row in self.ledger}
        split = dict.fromkeys(evidence.MUTANT_BUCKETS, 0)
        generated_only = []
        for cid in dict.fromkeys([row["id"] for row in self.ledger] + list(checks)):
            if cid in checks:
                bucket = self._bucket(checks[cid], accepted)
            else:
                bucket = outcomes[cid] if outcomes.get(cid) in ("equivalent", "not_applicable") else "not_judged"
            split[bucket] += 1
            if bucket == "killed_generated_only":
                generated_only.append(cid)
        return {"operators_selected": len(selected),
                "operators_not_selected": [op for op in mutate.OPERATORS if op not in selected], **split,
                "generated_only": generated_only}

    # ---- evidence --------------------------------------------------------------------------------
    def _ran(self, gid: str) -> tuple[int, int]:
        """(comparisons that ran without error, those with a row on either side) of a gold query, V1 and V2 together."""
        c = self.counts.get(gid, {"v1": [0, 0], "v2": [0, 0]})
        return c["v1"][0] + c["v2"][0], c["v1"][1] + c["v2"][1]

    def gold_evidence(self):
        """not_proven lines of the gold queries: generated text in the gold SQL, and every comparison without a row."""
        for _i, g in self.golds:
            found = self.gold_found.get(g.id, [])
            tail = f"v1:{g.id} and v2:{g.id}:* count as generated evidence, not authored"
            if "output_schema" in found:
                self.res.not_proven.append(f"gold:{g.id}: the gold SQL names the output schema "
                                           f"{self.cfg.source.output_schema}; {tail}")
            relations = [x.split(":", 1)[1] for x in found if x.startswith("relation:")]
            if relations:
                self.res.not_proven.append(f"gold:{g.id}: the gold SQL names the generated relation "
                                           f"{_cut(relations)}; {tail}")
            refs = [x.split(":", 1)[1] for x in found if x.startswith("filter:")]
            if refs:
                self.res.not_proven.append(f"gold:{g.id}: the gold SQL contains the generated filter of {_cut(refs)} "
                                           f"verbatim; {tail}")
            n, nonempty = self._ran(g.id)
            if n and not nonempty:
                c = self.counts[g.id]
                parts = ([f"v1:{g.id}"] if c["v1"][0] else []) + ([f"v2:{g.id}:*"] if c["v2"][0] else [])
                self.res.not_proven.append(f"gold:{g.id}: every comparison that ran ({n}) had zero rows on both sides; "
                                           f"{' and '.join(parts)} pass without comparing any row; add a binding, "
                                           "stratum or parameter set with rows")
                if self.mode == "require":
                    self._zero_rows_v1(g)

    def _zero_rows_v1(self, g):
        """require mode: a passing v1 of a gold query whose every comparison had zero rows is not_run
        (no_rows_compared). Any other status stays: an error or a fail is never rewritten."""
        cid = f"v1:{g.id}"
        for i, c in enumerate(self.res.checks):
            if c.id == cid and c.status == "pass":
                self.res.checks[i] = Check(cid, "not_run", {**c.detail, "reason": "no_rows_compared"}, c.config_ref)

    def operators_line(self):
        """require mode: one not_proven line naming the operators verify.mutation_operators leaves out (N7)."""
        left_out = [op for op in mutate.OPERATORS if op not in mutate.selected(self.cfg)]
        if self.mode == "require" and left_out:
            self.res.not_proven.append(f"verify.mutation_operators leaves out {_cut(left_out)}; require mode judges "
                                       "only the selected operators, so changes of those kinds are not proven to be "
                                       "detected")

    def _detections(self) -> dict[str, list[str]]:
        """facet -> the V4 checks on it whose mutant an authored gold item detected."""
        out: dict[str, list[str]] = {}
        for c in self.res.checks:
            parts = c.id.split(":")
            if len(parts) == 3 and parts[0] == "v4" and c.detail.get("detection") == "authored":
                out.setdefault(evidence.operator_facet(self.cfg, parts[1], parts[2]), []).append(c.id)
        return out

    def _judge_facet(self, f, compares: dict, uses: dict, detected: dict, cubes: set) -> tuple[dict, list]:
        """One facet row of the evidence object, and the gold ids its gap line names."""
        golds = [g.id for _i, g in self.golds]
        comparing = [gid for gid in golds if f.compared(compares[gid])]
        using = [gid for gid in golds if gid not in comparing and f.compared(uses[gid])]
        authored = [gid for gid in comparing if self.gold_kinds.get(gid) == "authored"]
        ran = [gid for gid in authored if self._ran(gid)[0]]
        with_rows = [gid for gid in ran if self._ran(gid)[1]]
        with_values = [gid for gid in with_rows
                       if any(self.counts[gid]["value_rows"].get(m, 0) > 0 for m in f.value_members(compares[gid]))]
        accepted = set(self.cfg.verify.accepted_gaps)
        status, reason, named = "gap", "", []
        generated = [gid for gid in comparing if gid not in authored]
        if with_values:
            status = "compared"
        elif detected.get(f.id):
            status = "detected"
        elif f.id in accepted or f.element in accepted:
            status = "accepted"
        elif not set(f.cubes) <= cubes:
            reason = "not_in_model"
        elif with_rows:
            reason, named = "no_values", authored
        elif ran:
            reason, named = "no_rows_compared", authored
        elif authored:
            reason, named = "gold_not_run", authored
        elif generated:
            reason, named = "generated_text", generated
        elif any(self.gold_kinds.get(gid) == "authored" for gid in using):
            reason, named = "filter_only", [gid for gid in using if self.gold_kinds.get(gid) == "authored"]
        elif using:
            reason, named = "generated_text", using
        else:
            reason = "no_gold_reads"
        row = {"element": f.element, "facet": f.id, "status": status, "reason": reason, "compared_by": comparing,
               "uses_only_by": using, "detected_by": detected.get(f.id, [])}
        return row, named

    def facets(self):
        """Full-scope runs only: every facet of the configuration with its authored evidence or the reason there is
        none, and one not_proven line per element with a gap (N4) and per element with accepted gaps (N4a)."""
        if not self.full:
            return
        compares = {g.id: set(g.map.values()) for _i, g in self.golds}
        uses = {g.id: _members(g) for _i, g in self.golds}
        detected = self._detections()
        cubes = {c.get("name") for c in self.model.get("cubes", [])}
        judged = [self._judge_facet(f, compares, uses, detected, cubes) for f in evidence.facets(self.cfg, self.model)]
        self.facet_rows = [row for row, _named in judged]
        for element in dict.fromkeys(row["element"] for row in self.facet_rows):
            gaps = [f"{row['facet']} ({row['reason']}" + (f": {_cut(named)}" if named else "") + ")"
                    for row, named in judged if row["element"] == element and row["status"] == "gap"]
            if gaps and self.mode == "disclose":
                self.res.not_proven.append(f"{element}: no authored evidence for {_cut(gaps)}; only checks between "
                                           "generated layers cover these facets; add a gold query, stratum or "
                                           "parameter set that compares them, or list them in verify.accepted_gaps")
            accepted = [row["facet"] for row in self.facet_rows
                        if row["element"] == element and row["status"] == "accepted"]
            if accepted:
                self.res.not_proven.append(f"{element}: no authored evidence for {_cut(accepted)}; accepted by "
                                           "verify.accepted_gaps, so this is not proven")
        if self.mode == "require":
            self.element_checks()

    def element_checks(self):
        """require mode, full-scope runs: one gold_evidence:<element> check per measure, hierarchy and
        ontology.class_column (when set), after every other check. pass: no facet is a gap; not_run: every gap is
        gold_not_run or not_in_model (reason: the first of them), and an element without any facet in the model is
        not_in_model; fail: reason = the first other gap in facet order."""
        left_out = [op for op in mutate.OPERATORS if op not in mutate.selected(self.cfg)]
        elements = [f"measures.{m}" for m in self.cfg.measures] + [f"hierarchies.{h}" for h in self.cfg.hierarchies]
        if self.cfg.ontology.class_column:
            elements.append(evidence.CLASS_ELEMENT)
        for element in elements:
            rows = [row for row in self.facet_rows if row["element"] == element]
            gaps = [row for row in rows if row["status"] == "gap"]
            other = [row for row in gaps if row["reason"] not in NOT_RUN_REASONS]
            if not rows:
                status, reason = "not_run", "not_in_model"
            elif not gaps:
                status, reason = "pass", ""
            elif other:
                status, reason = "fail", other[0]["reason"]
            else:
                status, reason = "not_run", gaps[0]["reason"]
            counts = {"facets": len(rows),
                      "with_evidence": sum(1 for row in rows if row["status"] in ("compared", "detected")),
                      "accepted": sum(1 for row in rows if row["status"] == "accepted"), "gaps": len(gaps),
                      "operators_not_selected": left_out}
            detail = {"reason": reason, **counts} if reason else counts
            self.res.add(f"gold_evidence:{element}", status, element, **detail)

    def _zero_row_checks(self) -> tuple[list[str], list[str]]:
        """V1 and V2 checks that compared no row (V1 empty; V2 with every binding empty, or an empty stratum), those
        of a stratum named `empty` (a node with no children, which many gold queries answer with no row) apart."""
        zero, empty = [], []
        for c in self.res.checks:
            parts = c.id.split(":")
            d = c.detail
            if parts[0] == "v1" and len(parts) == 2 and d.get("empty") is True:
                stratum = d.get("stratum", "")
            elif parts[0] == "v2" and len(parts) == 3 and (
                    d.get("empty_stratum") or (d.get("bindings", 0) > 0 and d.get("empty_bindings") == d["bindings"])):
                stratum = parts[2]
            else:
                continue
            (empty if stratum == "empty" else zero).append(c.id)
        return zero, empty

    def evidence_object(self) -> dict:
        """The `evidence` key of verify/result.json: counts, ids and Cube member names only, never gold text, rows or
        parameter values."""
        gold = []
        for _i, g in self.golds:
            if g.id not in self.counts:
                continue
            compares = sorted(set(g.map.values()))
            n, nonempty = self._ran(g.id)
            gold.append({"id": g.id, "kind": self.gold_kinds[g.id], "generated_text": list(self.gold_found[g.id]),
                         "compares": compares, "uses_only": sorted(_members(g) - set(compares)), "comparisons": n,
                         "nonempty_comparisons": nonempty,
                         "value_rows": dict(sorted(self.counts[g.id]["value_rows"].items()))})
        zero, empty = self._zero_row_checks()
        return {"schema_version": EVIDENCE_SCHEMA, "rule_version": evidence.RULE_VERSION, "mode": self.mode,
                "scope": "full" if self.full else "narrowed", "gold": gold,
                "facets": self.facet_rows if self.full else None, "facets_reason": "" if self.full else "narrowed",
                "zero_row_checks": zero, "empty_stratum_zero_row_checks": empty,
                "gold_no_rows": [x["id"] for x in gold if x["comparisons"] and not x["nonempty_comparisons"]],
                "mutation": self.mutation_split()}


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
    if d.get("cube") == "invalid_response":
        return (f"Cube answered the query of {c.id} with something that is not a load result for it (no data list of "
                "row objects, or a row without a requested member), so nothing was compared: check the adapter with "
                "`sxc.py doctor --require cube`, rerun `sxc.py cube`, then rerun verify")
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
    if why == "suite_budget":
        return (f"{c.id} reached verify.mutant_max_items before the suite finished: raise that explicit item budget "
                "or narrow the selected model, then rerun verify; no survival verdict was established")
    if c.id.startswith("v3:closure_oracle:") and why == "budget_exceeded":
        return (f"{c.id} exceeded the independent oracle limit {c.detail['limit']}: use a smaller hierarchy or review "
                "closure_oracle.Limits before raising the bound; no partial closure was certified")
    if why == "suite_incomplete" and c.detail.get("killed_by"):
        return (f"{c.id}: killed only by {_killer(c.detail)}, while gold items were left out "
                f"({', '.join(c.detail.get('excluded', []))}): make every gold query and closure run (full verify, "
                "passing builds), then rerun verify")
    if why == "no_rows_compared":
        return (f"{c.id}: every comparison of this gold query had zero rows on both sides; check its parameters, "
                "strata and the data, then rerun verify")
    if why == "suite_incomplete":
        return (f"{c.id} survived a suite with items left out ({', '.join(c.detail.get('excluded', []))}): make every "
                "gold query and closure run (full verify, passing builds), then rerun verify")
    if why == "no_passing_suite_item":
        return "V4 needs suite items that pass on the unmutated model: fix the V1-V3 checks first, then rerun verify"
    if why == "no_binding":
        return f"{c.id} has no binding (every stratum is empty): check the params of {c.config_ref} or the data"
    if why == "witness_incomplete":
        d = c.detail
        return (f"{c.id} left {d.get('undecided_keys')} key(s) undecided (together_absent {d.get('together_absent')} "
                f"key(s); undecided absences, per key and measure an alone answer left out: not_witnessed "
                f"{d.get('not_witnessed')}, witness_type {d.get('witness_type')}, witness_error "
                f"{d.get('witness_error')}, witness_changed {d.get('witness_changed')}): rerun verify while the source "
                f"data is not changing, and check that the key columns of {c.config_ref} accept the key values Cube's "
                "equals filter receives")
    if why == "pair_error":
        return f"{c.id} did not run because its pair test could not run: fix that error first, then rerun verify"
    if why == "keys_not_answered":
        d = c.detail
        return (f"{c.id} was not judged (keys_not_answered): Cube's together answers held {d.get('answered_keys')} of "
                f"the {d.get('compared_keys')} keys its chunks asked for, so the others were never compared; rerun "
                f"verify while the source data is not changing, and if keys stay missing, check that Cube's equals "
                f"filter on the key member of {c.config_ref} matches the key's type")
    return (f"{c.id} did not run ({why}): make its prerequisite pass (for a closure: build until closure:<h> passes, "
            "then cube), then rerun verify")


def _kill_action(c: Check) -> str:
    """require mode: a V4 mutant that only a generated item (or an item of no known kind) killed."""
    d = c.detail
    parts = c.id.split(":") + ["", ""]
    readers = _Verify._readers(d)
    if d.get("detection") == "generated_only":
        seen = f"no replayed gold item did ({readers})"
    else:
        seen = f"no replayed gold item is shown to have detected it ({readers})"
    return (f"{c.id}: only {_killer(d)}, detected the mutant; {seen}; {evidence.remedy(parts[1], parts[2])}, or list "
            f"{c.id} in verify.accepted_gaps, then rerun verify (do not drop the operator)")


def _element_action(res: Result, c: Check) -> str:
    """require mode: a gold_evidence:<element> check that did not pass, naming the facets it lacks evidence for (from
    the evidence object of the result)."""
    recorded = getattr(res, "evidence", None)
    rows = [row for row in ((recorded or {}).get("facets") or []) if isinstance(row, dict)
            and row.get("element") == c.config_ref and row.get("status") == "gap"]
    if c.status == "not_run":
        facets = _cut([row["facet"] for row in rows]) or c.config_ref
        return (f"{c.id}: the gold queries that compare {facets} did not run, or the model lacks their cube; fix their "
                "errors or gates (rerun build and cube for a missing closure cube), then rerun verify")
    if c.detail.get("reason") == "no_values":
        facets = _cut([row["facet"] for row in rows if row.get("reason") == "no_values"]) or c.config_ref
        return (f"{c.id}: no authored evidence for {facets}: compared rows held no value of them (NULL, 0 or empty); "
                "add a binding or parameter set whose rows hold values, then rerun verify")
    facets = _cut([row["facet"] for row in rows if row.get("reason") not in NOT_RUN_REASONS]) or c.config_ref
    return (f"{c.id}: no authored evidence for {facets}; add a gold query that maps them and compares rows, or list "
            "them in verify.accepted_gaps, then rerun verify")


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
    if first.id.startswith("gold_evidence:"):
        return _element_action(res, first)
    if st == "fail" and first.id.startswith("v4:") and first.detail.get("reason") in ("generated_only", "unattributed"):
        return _kill_action(first)
    if st == "fail" and first.id.startswith("v4:"):
        return (f"{first.id} survived: no replayed suite item detects this kind of error in {first.config_ref}; add a "
                "gold query, a mapped column or a stratum that would, then rerun verify (do not drop the operator)")
    if st == "fail" and first.id.startswith("v3w:"):
        d = first.detail
        return (f"{first.id}: Cube's standalone answers left out keys that have qualifying source rows under "
                f"crosscheck.toml's filters ({d.get('absent_with_facts')} key-measure absence(s), absent_with_facts), "
                f"or left out keys without such rows that had a non-empty together value "
                f"({d.get('witness_value_mismatch')} key-measure absence(s), witness_value_mismatch); "
                f"compare the generated measure cubes of {first.config_ref} with crosscheck.toml, rerun cube, then "
                "rerun verify")
    if st == "fail" and first.id.startswith("v3:closure_oracle:"):
        return (f"{first.id}: raw-edge expectations disagree with the generated closure in {first.config_ref}; "
                "inspect the source edges and generated closure, rerun build and cube, then rerun verify")
    if st == "fail":
        return (f"{first.id} disagrees: fix the crosscheck.toml entry {first.config_ref or 'named by the check'} or its "
                "gold SQL, rerun build and cube, then rerun verify")
    return _not_run_action(first)

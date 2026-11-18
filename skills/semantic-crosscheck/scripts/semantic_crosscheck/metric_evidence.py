"""Summarize current verify observations for the measure facets declared in the configuration."""

from collections import defaultdict

from . import evidence


LABELS = {
    "authored_comparison": "Authored Gold compared values",
    "mutation_only": "Authored mutation detection only",
    "generated_only": "Generated checks only",
    "zero_rows": "Zero rows compared",
    "no_values": "No matched value exercised",
    "missing": "Missing authored evidence",
    "not_run": "Not judged",
}
REASONS = {
    "verify_missing": "Run verify",
    "verify_stale": "Rerun stale stages, then verify",
    "verify_narrowed": "Run verify at full scope",
    "prerequisite_missing": "Restore current build and Cube results, then rerun verify",
    "observation_missing": "Rerun verify to record evidence",
    "facet_not_recorded": "Rerun verify for this facet",
    "not_in_model": "Facet absent from the served model",
    "gold_not_run": "Configured Gold did not run",
    "no_rows_compared": "Gold compared empty results",
    "no_values": "Gold had no matched row carrying a value for this facet",
    "filter_only": "Gold only uses this facet in the query",
    "no_gold_reads": "No Gold reads this facet",
    "generated_text": "Gold contains generated text",
    "accepted": "Gap accepted in configuration",
}


def _generated_checks(cfg, checks):
    """Members compared by completed, nonempty V3 total, additive and pair checks.

    A node pair's id names its two measures. Their common configured node supplies
    the grouping dimension. A standalone measure total never covers its joins.
    """
    values = {f"m_{m.name}.value": m for m in cfg.measures.values()}
    closure_qty = {f"{h}_closure.qty" for h in cfg.hierarchies}
    class_cubes = set(cfg.entities) | {f"{h}_{end}" for h in cfg.hierarchies for end in ("ancestor", "descendant")}
    dimensions = {f"{cube}.{cfg.ontology.class_column}" for cube in class_cubes if cfg.ontology.class_column}
    for c in checks:
        if c["status"] not in ("pass", "fail") or c.get("detail", {}).get("empty"):
            continue
        cid = c["id"]
        parts = cid.split(":")
        if len(parts) == 3 and parts[:2] == ["v3", "total"] and parts[2] in values:
            yield cid, [parts[2]]
        elif len(parts) == 4 and parts[:2] == ["v3", "additive"] and parts[2] in values and parts[3] in dimensions:
            yield cid, parts[2:]
        elif len(parts) == 3 and parts[:2] == ["v3", "pair"]:
            members = parts[2].split("+")
            if len(members) != 2 or members[0] == members[1] or members[1] not in values:
                continue
            right = values[members[1]]
            if members[0] in closure_qty and right.closure_join:
                yield cid, members
            elif members[0] in values and right.node in cfg.entities and values[members[0]].node == right.node:
                node = cfg.entities[right.node]
                if len(node.key) == 1 and not (values[members[0]].date_column or right.date_column):
                    yield cid, [*members, f"{right.node}.{node.key[0]}"]


def _observations(cfg, recorded, checks):
    """Sources whose check and observation can be attributed to a configured facet.

    The optional details of older receipts may be absent. Do not promote an
    authored label on a facet without a matching Gold observation with value rows.
    """
    golds = {g.id: g for g in cfg.gold}
    kinds, _ = evidence.gold_kinds(recorded["gold"])
    matched = set()
    for c in checks:
        parts = c["id"].split(":")
        if c["status"] not in ("pass", "fail"):
            continue
        d = c.get("detail", {})
        if len(parts) == 2 and parts[0] == "v1":
            count = d.get("matched_rows")
            if type(count) is int and count > 0 and d.get("empty") is not True:
                matched.add(parts[1])
        elif len(parts) == 3 and parts[0] == "v2":
            rows, missing, duplicates = (d.get(k) for k in ("rows_compared", "missing_in_cube", "duplicate_keys_gold"))
            if all(type(n) is int and n >= 0 for n in (rows, missing, duplicates)) and rows > missing + duplicates:
                matched.add(parts[1])
    observations = []
    for g in recorded["gold"]:
        if not isinstance(g, dict) or not isinstance(g.get("id"), str) or g["id"] not in golds:
            continue
        gid = g["id"]
        members = set(golds[gid].map.values())
        compared = g.get("compares")
        if not isinstance(compared, list) or not all(isinstance(m, str) for m in compared) or set(compared) != members:
            continue
        ran, nonempty = g.get("comparisons"), g.get("nonempty_comparisons")
        counts = g.get("value_rows")
        if (gid not in matched or type(ran) is not int or type(nonempty) is not int or not 0 < nonempty <= ran
                or not isinstance(counts, dict)):
            continue
        with_values = {m for m in members if type(counts.get(m)) is int and counts[m] > 0}
        observations.append((gid, kinds[gid], members, with_values))
    return observations


def _resolve(facet, rows, gold, generated, kills):
    if len(rows) != 1 or rows[0].get("element") != facet.element:
        return "not_run", "facet_not_recorded", []
    row = rows[0]
    readers = row.get("compared_by")
    readers = readers if isinstance(readers, list) else []
    sources = {"authored": [], "generated": []}
    for gid, kind, members, values in gold:
        if gid in readers and facet.compared(members) and any(m in values for m in facet.value_members(members)):
            sources[kind].append(gid)
    if row["status"] == "compared":
        if sources["authored"]:
            return "authored_comparison", "", sources["authored"]
        return "not_run", "facet_not_recorded", []
    if row["status"] == "detected":
        recorded = row.get("detected_by")
        detected = [cid for cid, kind in kills if kind == "authored" and isinstance(recorded, list) and cid in recorded]
        if detected:
            return "mutation_only", "", detected
        return "not_run", "facet_not_recorded", []
    generated_ids = sources["generated"] + [cid for cid, members in generated if facet.compared(members)]
    generated_ids += [cid for cid, kind in kills if kind == "generated_only"]
    reason = "accepted" if row["status"] == "accepted" else row["reason"]
    if reason in ("no_rows_compared", "no_values"):
        basis = "zero_rows" if reason == "no_rows_compared" else "no_values"
    elif reason in ("not_in_model", "gold_not_run"):
        basis = "not_run"
    else:
        basis = "generated_only" if generated_ids else "missing"
    return basis, reason, generated_ids


def summarize(cfg, verify, summary, stale):
    """One row per configured measure facet. STALE, narrowed or unreadable evidence gives no credit."""
    facets = [f for f in evidence.facets(cfg) if f.element.startswith("measures.")]
    if verify is None:
        unavailable = "verify_missing"
    elif stale:
        unavailable = "verify_stale"
    elif not summary["observation_recorded"] or "verify" not in summary["included_stages"]:
        unavailable = "observation_missing"
    elif not {"build", "cube"}.issubset(summary["included_stages"]):
        unavailable = "prerequisite_missing"
    elif summary["facets_reason"] == "narrowed" or any(c["id"].startswith("scope:") for c in verify["checks"]):
        unavailable = "verify_narrowed"
    else:
        unavailable = ""
    by_facet, kills = defaultdict(list), defaultdict(list)
    if not unavailable:
        recorded, checks = verify["evidence"], verify["checks"]
        for row in recorded["facets"]:
            by_facet[row["facet"]].append(row)
        gold = _observations(cfg, recorded, checks)
        generated = list(_generated_checks(cfg, checks))
        targets = set(evidence.operator_targets(cfg))
        configured_gold = {g.id: "authored" for g in cfg.gold}
        for c in checks:
            parts = c["id"].split(":")
            if (len(parts) != 3 or parts[0] != "v4" or tuple(parts[1:]) not in targets
                    or c["status"] not in ("pass", "fail")):
                continue
            d = c.get("detail", {})
            if d.get("detection") == "authored" and evidence.item_kind(d.get("killed_by"), configured_gold) != "authored":
                continue
            if d.get("killed_by") and d.get("detection") in ("authored", "generated_only"):
                kills[evidence.operator_facet(cfg, *parts[1:])].append((c["id"], d["detection"]))
    rows = []
    for f in facets:
        basis, reason, sources = (("not_run", unavailable, []) if unavailable else
                                  _resolve(f, by_facet[f.id], gold, generated, kills[f.id]))
        rows.append({"measure": f.element.removeprefix("measures."), "facet": f.id, "basis": basis,
                     "reason": reason, "sources": sources})
    return rows


def markdown(rows):
    compared = sum(row["basis"] == "authored_comparison" for row in rows)
    detected = sum(row["basis"] == "mutation_only" for row in rows)
    if all(row["basis"] == "not_run" for row in rows):
        overview = f"Authored coverage is undetermined for all {len(rows)} configured measure facets."
    else:
        overview = (f"Of {len(rows)} configured measure facets, current evidence shows **{compared}** with authored "
                    f"Gold value comparisons and **{detected}** more with authored mutation detection only. "
                    "Generated checks can pass when both sides share a wrong definition.")
    lines = ["## Metric evidence", "", overview,
             "This table reads `verify`. Standalone `measure-oracle` and `expect` runs have separate receipts.", "",
             "| Measure | Facet | Evidence observed | Source or gap |", "|---|---|---|---|"]
    for row in rows:
        detail = [REASONS[row["reason"]]] if row["reason"] else []
        sources = row["sources"]
        detail.extend(f"`{s}`" for s in sources[:3])
        if len(sources) > 3:
            detail.append(f"+{len(sources) - 3} more in report.json")
        lines.append(f"| `{row['measure']}` | `{row['facet']}` | {LABELS[row['basis']]} | {', '.join(detail)} |")
    lines.append("")
    if any(r["basis"] not in ("not_run", "authored_comparison") for r in rows):
        lines.extend(["For a gap, exercise the facet with a populated fixture and compare against an independently "
                      "authored business expectation.", ""])
    return lines

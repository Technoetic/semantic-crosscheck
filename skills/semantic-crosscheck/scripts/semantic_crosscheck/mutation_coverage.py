"""Bounded mutation-suite execution receipts: safe ids and counts, never query values."""

import json
import os
import re
import tempfile
from pathlib import Path

from .result import EXIT_CONFIG, StageError


def discard_matrix(out, configuration=None):
    """Invalidate a prior attempt without following output-directory or input aliases."""
    folder = Path(out) / "verify"
    artifact = folder / "mutation-matrix.json"
    if not folder.resolve().is_relative_to(Path(out).resolve()):
        raise StageError("verify: diagnostic_outside_output", EXIT_CONFIG)
    if configuration is not None and Path(configuration).resolve() == artifact.resolve():
        raise StageError("verify: diagnostic_aliases_configuration", EXIT_CONFIG)
    artifact.unlink(missing_ok=True)


class Suite:
    """Callable suite with the legacy (status, item) interface and detached receipts.

    The budget counts logical items. An error is retried once within its item;
    a second mutant always starts with a fresh budget and fresh receipt.
    """

    def __init__(self, items, *, max_items=10000, observation="first"):
        if type(max_items) is not int or max_items < 1:
            raise ValueError("max_items must be a positive integer")
        if observation not in ("first", "all"):
            raise ValueError('observation must be "first" or "all"')
        self.items = list(items)
        self.max_items = max_items
        self.observation = observation
        self.reset()

    def skipped(self, reason):
        return {"complete": False, "max_items": self.max_items, "planned_items": len(self.items),
                "executed_items": 0,
                "items": [{"id": iid, "status": "not_run", "attempts": 0, "reason": reason}
                          for iid, _fn in self.items]}

    def reset(self):
        self._receipt = self.skipped("not_started")

    def receipt(self):
        return {**self._receipt, "items": [dict(row) for row in self._receipt["items"]]}

    def __call__(self, client):
        self.reset()
        rows = self._receipt["items"]
        first = None
        for index, (iid, fn) in enumerate(self.items):
            if index >= self.max_items:
                for row in rows[index:]:
                    row["reason"] = "item_budget"
                return first or ("budget_exceeded", iid)
            row = rows[index]
            row.pop("reason")
            self._receipt["executed_items"] += 1
            for attempt in range(2):
                # Final-attempt categories describe this cell, not a recovered earlier error.
                if self.observation == "all":
                    row.pop("sqlstate", None)
                    row.pop("cube", None)
                row["attempts"] = attempt + 1
                try:
                    answer = fn(client)
                    status, detail = answer if isinstance(answer, tuple) else (answer, {})
                except Exception as exc:  # messages may contain source rows or credentials
                    status, detail = "error", {"exception": type(exc).__name__}
                if status not in ("pass", "fail", "error", "not_run"):
                    status, detail = "error", {}
                row["status"] = status
                # Only explicitly safe, categorical error fields cross the boundary.
                sqlstate = detail.get("sqlstate")
                if isinstance(sqlstate, str) and re.fullmatch(r"[A-Z0-9]{5}", sqlstate):
                    row["sqlstate"] = sqlstate
                if detail.get("cube") in ("timeout", "row_limit", "invalid_response", "http", "transport"):
                    row["cube"] = detail["cube"]
                if self.observation == "all" and status == "not_run":
                    row["reason"] = ("keys_not_answered" if detail.get("reason") == "keys_not_answered"
                                     else "item_not_run")
                if status != "error":
                    break
            if status != "pass":
                if first is None:
                    first = (status, iid)
                if self.observation == "first":
                    for later in rows[index + 1:]:
                        later["reason"] = "early_stop"
                    self._receipt["complete"] = index + 1 == len(self.items)
                    return first
        self._receipt["complete"] = True
        return first or ("pass", "")


def write(out, mutants, *, max_items, reason="completed", excluded_items=()):
    """Atomically replace a diagnostic artifact; it grants no report authority."""
    _atomic_write(out, "mutation-coverage.json",
                  {"schema_version": 1, "scope": "selected_mutants_selected_suite_items",
                   "reason": reason, "max_items_per_mutant": max_items,
                   "excluded_items": list(excluded_items), "mutants": mutants})


def write_matrix(out, mutants, items, *, max_items, identity, reason="completed", excluded_items=()):
    """Observed mismatches only: later diagnostic cells cannot grant report authority.

    A unique mutant has exactly one observed detector. Redundancy means another
    selected item observed that same mismatch, not that either item is dispensable.
    """
    selected = [dict(item) for item in items]
    by_item = {row["id"]: {**row, "mutants": [], "unique_mutants": [], "redundant_mutants": []}
               for row in selected}
    rows = []
    executed = not_run = mismatches = 0
    statuses = dict.fromkeys(("pass", "fail", "error", "not_run"), 0)
    for mutant in mutants:
        coverage = mutant.get("coverage", {})
        cells = coverage.get("items", [])
        detectors = [{"id": cell["id"], "kind": cell.get("kind", "unclassified")}
                     for cell in cells if cell["status"] == "fail" and cell.get("attempts", 0) > 0]
        executed += sum(cell.get("attempts", 0) > 0 for cell in cells)
        not_run += sum(cell.get("attempts", 0) == 0 for cell in cells)
        for cell in cells:
            statuses[cell["status"]] += 1
        mismatches += len(detectors)
        for detector in detectors:
            observed = by_item[detector["id"]]
            observed["mutants"].append(mutant["id"])
            observed["unique_mutants" if len(detectors) == 1 else "redundant_mutants"].append(mutant["id"])
        # These ledger fields are safe IDs, counts and categorical states, never runtime messages.
        rows.append({**mutant, "observed_detectors": detectors})
    _atomic_write(out, "mutation-matrix.json", {
        "schema_version": 1, "diagnostic_only": True,
        "scope": "selected_mutants_baseline_passing_selected_suite_items", "observation": "all",
        "reason": reason, "identity": dict(identity), "max_items_per_mutant": max_items,
        "selected_items": selected, "excluded_items": list(excluded_items),
        "counts": {"selected_mutants": len(rows), "selected_items": len(selected),
                   "planned_cells": len(rows) * len(selected), "executed_cells": executed,
                   "not_run_cells": not_run, "mismatch_cells": mismatches},
        "complete": bool(rows and selected) and all(row.get("coverage", {}).get("complete", False) for row in rows),
        "cell_status_counts": statuses,
        "mutants": rows, "detector_sets": [row for row in by_item.values() if row["mutants"]]})


def _atomic_write(out, filename, payload):
    folder = Path(out) / "verify"
    folder.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".mutation-coverage-", suffix=".part", dir=folder)
    part = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
        os.replace(part, folder / filename)
    finally:
        part.unlink(missing_ok=True)

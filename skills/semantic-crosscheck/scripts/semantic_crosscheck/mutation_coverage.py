"""Bounded mutation-suite execution receipts: safe ids and counts, never query values."""

import json
import os
import re
import tempfile
from pathlib import Path


class Suite:
    """Callable suite with the legacy (status, item) interface and detached receipts.

    The budget counts logical items. An error is retried once within its item;
    a second mutant always starts with a fresh budget and fresh receipt.
    """

    def __init__(self, items, *, max_items=10000):
        if type(max_items) is not int or max_items < 1:
            raise ValueError("max_items must be a positive integer")
        self.items = list(items)
        self.max_items = max_items
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
        for index, (iid, fn) in enumerate(self.items):
            if index >= self.max_items:
                for row in rows[index:]:
                    row["reason"] = "item_budget"
                return "budget_exceeded", iid
            row = rows[index]
            row.pop("reason")
            self._receipt["executed_items"] += 1
            for attempt in range(2):
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
                if status != "error":
                    break
            if status != "pass":
                for later in rows[index + 1:]:
                    later["reason"] = "early_stop"
                self._receipt["complete"] = index + 1 == len(self.items)
                return status, iid
        self._receipt["complete"] = True
        return "pass", ""


def write(out, mutants, *, max_items, reason="completed", excluded_items=()):
    """Atomically replace a diagnostic artifact; it grants no report authority."""
    folder = Path(out) / "verify"
    folder.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".mutation-coverage-", suffix=".part", dir=folder)
    part = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump({"schema_version": 1, "scope": "selected_mutants_selected_suite_items",
                       "reason": reason, "max_items_per_mutant": max_items,
                       "excluded_items": list(excluded_items), "mutants": mutants},
                      stream, ensure_ascii=True, indent=2)
            stream.write("\n")
        os.replace(part, folder / "mutation-coverage.json")
    finally:
        part.unlink(missing_ok=True)

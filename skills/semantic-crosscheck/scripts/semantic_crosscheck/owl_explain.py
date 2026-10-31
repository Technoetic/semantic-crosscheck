"""Bounded deletion explanations using the same HermiT as the failed source consistency check.

Every oracle call uses a local subprocess. It receives a key-free, logically isomorphic graph through a pipe; it
never receives DSNs, SQL, paths from cfg or any source key. A deadline covers the entire diagnostic, including all
workers. This is a diagnostic over removable generated axiom groups, not independent certification or a search for
a globally smallest explanation.
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import owl
from .result import EXIT_CONFIG, StageError

CHECK_ID = "owl:explanation"
WORKER = Path(__file__).with_name("_owl_explain_worker.py")
LIMITS = {"max_axioms": 4096, "max_checks": 4096, "timeout_ms": 300000}
NOTE = ("owl:explanation uses the same HermiT and generated ontology as owl:consistency; subset minimality and "
        "uniqueness concern only the generated removable axiom groups over fixed declarations. It is not independent "
        "certification and does not establish a globally minimum explanation or exclude causes outside those groups")


def validate_limits(max_axioms=128, max_checks=64, timeout_ms=10000):
    for name, value in (("max_axioms", max_axioms), ("max_checks", max_checks), ("timeout_ms", timeout_ms)):
        if type(value) is not int or not 1 <= value <= LIMITS[name]:
            raise StageError(f"--explain-{name.replace('_', '-')} must be an integer from 1 to {LIMITS[name]}",
                             EXIT_CONFIG)


def validate_args(args):
    validate_limits(getattr(args, "explain_max_axioms", 128), getattr(args, "explain_max_checks", 64),
                    getattr(args, "explain_timeout_ms", 10000))


class _WindowsJob:
    """Kill-on-close job, assigned before the worker receives permission to start HermiT.

    The pipe handshake removes the spawn/assignment race: no Java process exists before assign(). Failure to set
    up containment fails closed. Handles are explicitly closed on every path, including timeouts.
    """
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]
        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
                         "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("process containment unavailable")
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise OSError("process containment unavailable")

    def assign(self, process):
        if not self.api.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError("process containment unavailable")

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _group_manifest(cfg, graph, key_map=None):
    """Bind each group id to its parent-sorted positions and opaque assertion terms.

    Worker recomputation must match the complete parent manifest before any Java process starts. This checks
    correspondence of node/edge assertions and class-position tokens, rather than accepting equal group counts.
    """
    nodes = list(graph.nodes)
    def encoded(key):
        return key if key_map is None else key_map[key]
    records = []
    for group in owl.axiom_groups(cfg, graph, max_groups=LIMITS["max_axioms"], _ordered=True):
        record = {"id": group.id, "kind": group.kind, "config_ref": group.config_ref,
                  "token": list(group.token)}
        if group.kind == "node_type":
            record["assertion"] = [encoded(nodes[group.token[1]]), group.token[2]]
        elif group.kind == "edge_assertion":
            record["assertion"] = [encoded(key) for key in graph.edges[group.token[1]]]
        records.append(record)
    return records


def _payload(cfg, graph, java, active):
    # Every key becomes an ordinal before serialization. Class values are normalized as well; class and property
    # names remain the configured names, so the generated logical axioms retain their original structure.
    graph = owl._ordered_graph(graph)
    o, h = cfg.ontology, cfg.hierarchies[cfg.ontology.hierarchy]
    values = {value: f"c{j}" for j, value in enumerate(o.classes)}
    all_keys = dict.fromkeys(graph.nodes)
    for pair in graph.edges:
        for key in pair:
            all_keys.setdefault(key, None)
    keys = {key: f"k{j:012d}" for j, key in enumerate(sorted(all_keys, key=owl._key_order))}
    return {"java": java, "active": sorted(active), "groups": _group_manifest(cfg, graph, keys),
            "hierarchy": {"name": h.name, "node": h.node},
            "ontology": {"iri": o.iri, "hierarchy": h.name,
                         "classes": {values[v]: name for v, name in o.classes.items()},
                         "no_children": [values[v] for v in o.no_children],
                         "defined": [{"name": d.name, "kind": d.kind, "cls": values.get(d.cls, "")}
                                     for d in o.defined]},
            "nodes": [[keys[key], [values[value] for value in o.classes if value in classes]]
                      for key, classes in graph.nodes.items()],
            "edges": [[keys[parent], keys[child]] for parent, child in graph.edges]}


def _worker_env(private):
    # Local workers do not need database credentials or arbitrary user environment settings. Absolute executable
    # paths avoid requiring shell lookup; retain only basic OS/locale variables needed by the installed runtimes.
    allowed = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "LANG", "LC_ALL", "LC_CTYPE"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(TEMP=private, TMP=private, TMPDIR=private)
    return env


def _process(payload: bytes, deadline: float) -> bool:
    """One key-free worker request, hard wall timeout and process-tree cleanup on both supported platforms."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    process, job = None, None
    with tempfile.TemporaryDirectory(prefix="sxc_owl_explain_") as private:
        try:
            if os.name == "nt":
                job = _WindowsJob()
            process = subprocess.Popen([sys.executable, "-I", str(WORKER)], cwd=private, env=_worker_env(private),
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                       start_new_session=os.name != "nt")
            if job is not None:
                job.assign(process)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            try:
                answer, _unused = process.communicate(payload, timeout=remaining)
            except subprocess.TimeoutExpired:
                raise TimeoutError() from None
            if time.monotonic() > deadline:
                raise TimeoutError()
            if process.returncode != 0 or answer not in (b"consistent\n", b"inconsistent\n"):
                raise owl.ReasonerError("worker_failure")
            return answer == b"consistent\n"
        finally:
            if job is not None:
                job.close()  # descendants, even if the Python worker exited early
            if process is not None:
                if os.name != "nt":
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                if process.poll() is None:
                    process.kill()
                with contextlib.suppress(Exception):
                    process.wait(timeout=2)
                for stream in (process.stdin, process.stdout):
                    if stream:
                        stream.close()


def _check(cfg, graph, java, active, deadline):
    data = json.dumps(_payload(cfg, graph, java, active), ensure_ascii=True).encode("ascii")
    if len(data) > 16 * 1024 * 1024:
        raise owl.ReasonerError("worker_input_limit")
    return _process(data, deadline)


def _preflight(cfg, graph, maximum, deadline):
    """(count, complete, stop_reason), without sorting rows or allocating any descriptors.

    Every node contributes at least one type group. Lengths alone therefore reject oversized data/configurations
    in constant time. Only inputs within that bound scan configured class membership, stopping at the first
    extra assertion over the cap or at the deadline. An unfinished count is a lower bound, never an exact total.
    """
    o = cfg.ontology
    count = len(o.classes) + int(len(o.classes) > 1) + 4 + len(o.no_children) + len(o.defined)
    count += len(graph.nodes) + len(graph.edges)
    if time.monotonic() >= deadline:
        return count, False, "timeout"
    if count > maximum:
        return count, False, "axiom_limit"
    for values in graph.nodes.values():
        found = 0
        for value in o.classes:
            if time.monotonic() >= deadline:
                return count, False, "timeout"
            if value in values:
                if found:
                    count += 1
                    if count > maximum:
                        return count, False, "axiom_limit"
                found += 1
    if time.monotonic() >= deadline:
        return count, False, "timeout"
    return count, True, ""


def explain(cfg, graph, java, *, max_axioms=128, max_checks=64, timeout_ms=10000):
    """Return counts and safe descriptors only. All interrupted diagnostics explicitly remain not_run.

    A full-set check comes first; only an observed inconsistent set can be reported as a sufficient core. A final
    pass checks deletion of every retained group from the exact final core before subset_minimal is claimed.
    After that, one check per retained group removes only that group from the full set: unique needs every such
    check finished and consistent. Caps, deadline or errors there leave uniqueness not_proven, never the status.
    """
    validate_limits(max_axioms, max_checks, timeout_ms)
    deadline = time.monotonic() + timeout_ms / 1000
    count, complete, stop = _preflight(cfg, graph, max_axioms, deadline)
    result = {"status": "not_run", "reason": "not_started", "reasoner": "HermiT",
              "independent_certification": False, "scope": "removable_axiom_groups",
              "minimality": "not_proven", "sufficient": False, "axiom_groups": count,
              "axiom_groups_count_complete": complete,
              "retained_axiom_groups": 0, "checks_completed": 0, "deletion_checks_completed": 0,
              "minimality_checks_completed": 0, "max_axioms": max_axioms, "max_checks": max_checks,
              "timeout_ms": timeout_ms, "core": [], "uniqueness": "not_proven",
              "uniqueness_reason": "minimality_not_proven", "uniqueness_checks_completed": 0}
    if stop:
        result["reason"] = stop
        return result
    try:
        groups = owl.axiom_groups(cfg, graph, max_groups=max_axioms, deadline=deadline)
    except TimeoutError:
        result["reason"] = "timeout"
        return result
    except owl.AxiomGroupLimit:
        result.update(reason="axiom_limit", axiom_groups=max_axioms + 1, axiom_groups_count_complete=False)
        return result
    core = {group.id for group in groups}
    every = frozenset(core)
    def checked(active):
        if time.monotonic() >= deadline:
            raise TimeoutError()
        if result["checks_completed"] >= max_checks:
            raise _CheckLimit()
        answer = _check(cfg, graph, java, active, deadline)
        if type(answer) is not bool:
            raise owl.ReasonerError("invalid_answer")
        if time.monotonic() > deadline:
            raise TimeoutError()
        result["checks_completed"] += 1
        return answer
    try:
        if checked(core):
            result["reason"] = "initial_consistent"
            return result
        result["sufficient"] = True
        for group in groups:
            candidate = core - {group.id}
            consistent = checked(candidate)
            result["deletion_checks_completed"] += 1
            if not consistent:
                core = candidate
        for group in groups:
            if group.id in core:
                consistent = checked(core - {group.id})
                result["minimality_checks_completed"] += 1
                if not consistent:
                    result["reason"] = "verification_disagreement"
                    break
        else:
            result.update(status="pass", reason="completed", minimality="subset_minimal")
    except _CheckLimit:
        result["reason"] = "check_limit"
    except TimeoutError:
        result["reason"] = "timeout"
    except Exception:  # local process/reasoner failures are diagnostic incompleteness, with no private error text
        result["reason"] = "reasoner_error"
    if result["sufficient"]:
        result["core"] = [{"id": group.id, "kind": group.kind, "config_ref": group.config_ref}
                          for group in groups if group.id in core]
        result["retained_axiom_groups"] = len(core)
    if result["status"] == "pass":
        # "Union and Intersection of all Justifications" (ESWC 2022, arXiv 2109.11216): a group is in every
        # justification iff removing only it from the full set is consistent, and a justification is the only one
        # iff all its groups are. Groups only add axioms over the fixed declarations, so inconsistency is monotone.
        try:
            if _repeated_axioms(cfg, graph):
                result["uniqueness_reason"] = "duplicate_axiom_groups"
            else:
                for group in groups:
                    if group.id in core:
                        consistent = checked(every - {group.id})
                        result["uniqueness_checks_completed"] += 1
                        if not consistent:
                            result.update(uniqueness="not_unique", uniqueness_reason="completed")
                            break
                else:
                    result.update(uniqueness="unique", uniqueness_reason="completed")
        except _CheckLimit:
            result["uniqueness_reason"] = "check_limit"
        except TimeoutError:
            result["uniqueness_reason"] = "timeout"
        except Exception:  # same handling as the explanation: incompleteness only, never private error text
            result["uniqueness_reason"] = "reasoner_error"
    return result


def _repeated_axioms(cfg, graph):
    """Two removable groups would generate one identical axiom: then the other group survives a single removal."""
    no_children = cfg.ontology.no_children
    return len(set(no_children)) != len(no_children) or len(set(graph.edges)) != len(graph.edges)


class _CheckLimit(Exception):
    pass


def run(cfg, graph, java, args, res):
    got = explain(cfg, graph, java, max_axioms=getattr(args, "explain_max_axioms", 128),
                  max_checks=getattr(args, "explain_max_checks", 64),
                  timeout_ms=getattr(args, "explain_timeout_ms", 10000))
    status = got.pop("status")
    res.add(CHECK_ID, status, "ontology", **got)
    res.not_proven.append(NOTE)
    if status != "pass":
        res.not_proven.append("owl:explanation was bounded or incomplete; the original owl:consistency failure "
                              "remains and no subset-minimal explanation was established")
    elif got["uniqueness"] == "not_unique":
        res.not_proven.append("owl:explanation showed that another subset-minimal explanation over the generated "
                              "axiom groups exists; only one is reported and the others were not enumerated")
    elif got["uniqueness"] != "unique":
        res.not_proven.append(f"owl:explanation did not establish uniqueness ({got['uniqueness_reason']}); other "
                              "subset-minimal explanations over the generated axiom groups were not ruled out")

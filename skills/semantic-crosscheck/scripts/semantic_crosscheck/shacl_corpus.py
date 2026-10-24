"""Independent expected SHACL reports on a fixed synthetic nonrecursive corpus.

The production exporter builds the graphs and shapes. Expected report tuples are
hand-written below, never computed by that exporter or either validator. Both
engines must independently match every expected constraint, including its value.
Check IDs are pyshacl:<case> and jena:<case>, one per engine and corpus case.

The opt-in W3C mode (--w3c-suite) takes the expected reports from the W3C data-shapes
core test suite instead: third-party expectations that sxc's exporter and corpus
cannot share a mistake with. Only cases inside the sxc profile run; everything else
is not_run with a reason. Check IDs are w3c:<engine>:<case>, the case being the
suite-relative test name. The suite is read, never copied into this repository.
"""
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.util
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import urllib.parse
import warnings

from . import config, owl, shacl_check, shapes
from .result import EXIT_CONFIG, EXIT_ENV, Result, StageError
from .runtime_provenance import _WindowsProbeJob, _available_pipe_chunk, _probe_env

SH = 'http://www.w3.org/ns/shacl#'
XSD = 'http://www.w3.org/2001/XMLSchema#'
NS = 'https://example.org/sxc-corpus#'
MAX_OUTPUT_BYTES = 262144

MF = 'http://www.w3.org/2001/sw/DataAccess/tests/test-manifest#'
SHT = 'http://www.w3.org/ns/shacl-test#'
RDFS_CLASS = 'http://www.w3.org/2000/01/rdf-schema#Class'
OWL_CLASS = 'http://www.w3.org/2002/07/owl#Class'
# w3c/data-shapes at this commit (2026-10-09). The digest covers the sorted suite-relative paths and bytes of
# every core manifest and graph file load_w3c_suite reads; it, not the commit label, decides "pinned".
W3C_SUITE_COMMIT = '1dfcd9f30cab70ac02b049611eeba451fa1308e7'
W3C_SUITE_SHA256 = '27d212e662936162ed5707f57ab927e1a6a33e7e487844c71d2e0b704e53725b'
W3C_TESTS = 'data-shapes-test-suite/tests/'
W3C_CORE = W3C_TESTS + 'core/'
MAX_SUITE_FILES = 1024
MAX_SUITE_FILE_BYTES = 1048576
# Suite files are parsed against this synthetic base, so resolved IRIs never carry a local path and a
# document-relative term in an expected report is recognisable (relative_iri).
_BASE = 'http://sxc.invalid/w3c-suite/'
_ABSENT = ('absent',)
# The sxc profile is what shapes.shapes_ttl() emits: sh:class and the three numeric BOUNDS with sh:severity
# Violation or Warning, on property shapes with one IRI sh:path reached by sh:property from a node shape with
# sh:targetClass. The suite reaches the same constraints on IRI focus nodes through sh:targetNode, implicit
# class targets (a shape that is an rdfs:Class) and node-level constraints; those count as inside the profile.
_CONSTRAINTS = ('class', 'minExclusive', 'minInclusive', 'maxInclusive')
_NODE_KEYS = frozenset(('targetClass', 'targetNode', 'property', 'severity') + _CONSTRAINTS)
_PROPERTY_KEYS = frozenset(('path', 'targetClass', 'targetNode', 'severity') + _CONSTRAINTS)
_NUMERIC = frozenset(XSD + name for name in ('decimal', 'integer', 'int', 'long'))
_SEVERITIES = frozenset(SH + name for name in ('Violation', 'Warning'))
_SPARQL = frozenset(('sparql', 'select', 'ask', 'construct', 'update', 'declare', 'prefixes', 'target',
                     'parameter', 'validator', 'nodeValidator', 'propertyValidator', 'rule', 'js', 'jsLibrary'))
# SHACL 1.0 section 2.1: a node is a shape when it has a target, a parameter or sh:path (shape characteristics
# are included so nothing a shape declares escapes the profile), or is the value of a shape-expecting parameter.
_SHAPE_MARKS = frozenset((
    'targetClass', 'targetNode', 'targetSubjectsOf', 'targetObjectsOf', 'target', 'path', 'class', 'datatype',
    'nodeKind', 'minCount', 'maxCount', 'minExclusive', 'minInclusive', 'maxExclusive', 'maxInclusive', 'minLength',
    'maxLength', 'pattern', 'flags', 'languageIn', 'uniqueLang', 'equals', 'disjoint', 'lessThan',
    'lessThanOrEquals', 'not', 'and', 'or', 'xone', 'node', 'property', 'qualifiedValueShape', 'qualifiedMinCount',
    'qualifiedMaxCount', 'qualifiedValueShapesDisjoint', 'closed', 'ignoredProperties', 'hasValue', 'in', 'sparql',
    'severity', 'deactivated', 'message', 'name', 'description', 'order', 'group', 'defaultValue'))
_PATHS = frozenset(('inversePath', 'alternativePath', 'zeroOrMorePath', 'oneOrMorePath', 'zeroOrOnePath'))
# A test file is data, shapes and manifest at once: its expected report's vocabulary is not a shape feature.
_REPORT_TERMS = frozenset(('conforms', 'result', 'focusNode', 'resultPath', 'resultSeverity',
                           'sourceConstraintComponent', 'sourceShape', 'value', 'resultMessage', 'detail'))
_PROFILE_ORDER = ('sparql', 'complex_path', 'closed_shape', 'unsupported_target', 'non_iri_focus',
                  'unsupported_component', 'unsupported_value')
OWL_IMPORTS = 'http://www.w3.org/2002/07/owl#imports'
_SAFE_ID = re.compile(r'[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*')
_COMMIT = re.compile(r'[0-9a-f]{40}')


class CorpusError(Exception):
    """Only a fixed reason identifier; subprocess output never reaches callers."""
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Report:
    conforms: bool
    violations: tuple = ()


@dataclass(frozen=True)
class Case:
    id: str
    data: str
    shapes: str
    expected: Report


@dataclass(frozen=True)
class W3CCase:
    """One suite entry: reason '' means inside the profile with an expected report; data/shapes are suite paths."""
    id: str
    reason: str = ''
    features: tuple = ()
    expected: Report | None = None
    data: str = ''
    shapes: str = ''


@dataclass(frozen=True)
class W3CSuite:
    cases: tuple
    files: dict     # suite-relative path -> bytes read, the exact content the engines receive
    sha256: str
    commit: str


def _number(text):
    try:
        number = Decimal(text)
        if not number.is_finite():
            raise ValueError
        # Decimal.normalize() uses the current context and can round large values.
        value = format(number, 'f')
        if '.' in value:
            value = value.rstrip('0').rstrip('.')
        return '0' if number == 0 else value
    except (ValueError, InvalidOperation):
        raise CorpusError('malformed_report') from None


def _term(value):
    from rdflib import URIRef, Literal
    if isinstance(value, URIRef):
        return ('iri', str(value))
    if not isinstance(value, Literal):
        # This fixed profile has IRI focus/value nodes and literal values only.
        # Report/result blank nodes are intentionally not part of normalization.
        raise CorpusError('unsupported_report_term')
    if value.ill_typed:
        raise CorpusError('malformed_report')
    datatype = str(value.datatype or XSD + 'string')
    if datatype in {XSD + 'decimal', XSD + 'integer', XSD + 'int', XSD + 'long'}:
        return ('number', _number(str(value)))
    return ('literal', str(value), datatype, (value.language or '').lower())


def _quiet_graph(data, public_id=None):
    """Parse Turtle text or bytes. rdflib reports malformed literal text in warnings/log records; suppress
    that diagnostic channel (it would echo values), then callers explicitly reject ill-typed literals."""
    import rdflib
    logger = logging.getLogger('rdflib.term')
    parse_thread = threading.get_ident()
    quiet = lambda record: record.thread != parse_thread
    logger.addFilter(quiet)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            return rdflib.Graph().parse(data=data, format='turtle', publicID=public_id)
    finally:
        logger.removeFilter(quiet)


def _report_graph(text):
    if not isinstance(text, str) or not text or len(text.encode('utf-8')) > MAX_OUTPUT_BYTES:
        raise CorpusError('missing_report')
    return _quiet_graph(text)


def _conforms(graph, root, reason):
    from rdflib import Literal, URIRef
    conforms = list(graph.objects(root, URIRef(SH + 'conforms')))
    if (len(conforms) != 1 or not isinstance(conforms[0], Literal)
            or conforms[0].datatype != URIRef(XSD + 'boolean')
            or conforms[0].ill_typed
            or str(conforms[0]) not in ('true', 'false', '1', '0')):
        raise CorpusError(reason)
    return str(conforms[0]) in ('true', '1')


def _report_root(graph):
    import rdflib
    roots = set(graph.subjects(rdflib.RDF.type, rdflib.URIRef(SH + 'ValidationReport')))
    if len(roots) != 1:
        raise CorpusError('malformed_report')
    return next(iter(roots))


def parse_report(text):
    """Strict SHACL report parser; a missing report is never an empty success.

    Ignore result node labels, sourceShape labels and diagnostic messages. Keep
    a multiset of (focus, path, constraint component, severity, semantic value).
    The fixed corpus requires all five fields and excludes recursive paths.
    """
    import rdflib
    from rdflib import URIRef
    try:
        graph = _report_graph(text)
        sh = lambda name: URIRef(SH + name)
        root = _report_root(graph)
        valid = _conforms(graph, root, 'malformed_report')
        nodes = set(graph.objects(root, sh('result')))
        if nodes != set(graph.subjects(rdflib.RDF.type, sh('ValidationResult'))):
            raise CorpusError('malformed_report')
        violations = []
        for node in nodes:
            terms = []
            for prop in ('focusNode', 'resultPath', 'sourceConstraintComponent', 'resultSeverity', 'value'):
                values = list(graph.objects(node, sh(prop)))
                if len(values) != 1:
                    raise CorpusError('malformed_report')
                terms.append(values[0])
            if not all(isinstance(v, URIRef) for v in terms[:4]):
                raise CorpusError('unsupported_report_term')
            if str(terms[3]) not in {SH + 'Violation', SH + 'Warning', SH + 'Info'}:
                raise CorpusError('malformed_report')
            violations.append(tuple(str(v) for v in terms[:4]) + (_term(terms[4]),))
        if valid != (not violations):
            raise CorpusError('malformed_report')
        return Report(valid, tuple(sorted(violations)))
    except CorpusError:
        raise
    except Exception:
        raise CorpusError('malformed_report') from None


def _fields(graph, node, reason):
    """(focus, path, component, severity, value) of one result: focus, component and severity exactly once;
    sh:resultPath (absent for node shapes) and sh:value (absent for some components) at most once."""
    from rdflib import URIRef
    out = []
    for prop, optional in (('focusNode', False), ('resultPath', True), ('sourceConstraintComponent', False),
                           ('resultSeverity', False), ('value', True)):
        values = list(graph.objects(node, URIRef(SH + prop)))
        if len(values) > 1 or (not values and not optional):
            raise CorpusError(reason)
        out.append(values[0] if values else None)
    if not all(isinstance(v, URIRef) for v in out[2:4]):
        raise CorpusError(reason)
    return out


def parse_w3c_report(text):
    """An engine report for a W3C case, as strict as parse_report except that the path and value may be absent.

    Terms keep their kind: a blank node canonicalizes to ('blank',), which no expected term can equal, so an
    engine reporting an unidentifiable node fails instead of being dropped from the comparison.
    """
    import rdflib
    from rdflib import BNode, URIRef
    try:
        graph = _report_graph(text)
        root = _report_root(graph)
        valid = _conforms(graph, root, 'malformed_report')
        nodes = set(graph.objects(root, URIRef(SH + 'result')))
        if nodes != set(graph.subjects(rdflib.RDF.type, URIRef(SH + 'ValidationResult'))):
            raise CorpusError('malformed_report')
        term = lambda v: _ABSENT if v is None else ('blank',) if isinstance(v, BNode) else _term(v)
        results = []
        for node in nodes:
            focus, path, component, severity, value = _fields(graph, node, 'malformed_report')
            results.append((term(focus), term(path), str(component), str(severity), term(value)))
        if valid != (not results):
            raise CorpusError('malformed_report')
        return Report(valid, tuple(sorted(results)))
    except CorpusError:
        raise
    except Exception:
        raise CorpusError('malformed_report') from None


def _w3c_expected(graph, node):
    """The expected report of an entry (mf:result), canonicalized like parse_w3c_report.

    sh:sourceShape is optional in the suite and ignored, as are result node typing and messages. A term the
    comparison cannot identify across graphs is refused with a reason instead of being compared.
    """
    from rdflib import BNode, Literal, RDF, URIRef
    if (node, RDF.type, URIRef(SH + 'ValidationReport')) not in graph:
        raise CorpusError('malformed_expected')
    valid = _conforms(graph, node, 'malformed_expected')
    results = []
    for result in set(graph.objects(node, URIRef(SH + 'result'))):
        focus, path, component, severity, value = _fields(graph, result, 'malformed_expected')
        if any(isinstance(v, BNode) for v in (focus, path, value)):
            raise CorpusError('blank_node_term')
        if isinstance(focus, Literal):
            raise CorpusError('non_iri_focus')
        if isinstance(path, Literal):
            raise CorpusError('malformed_expected')
        if any(isinstance(v, URIRef) and str(v).startswith(_BASE) for v in (focus, path, value)):
            raise CorpusError('relative_iri')
        try:
            results.append((_term(focus), _ABSENT if path is None else _term(path), str(component), str(severity),
                            _ABSENT if value is None else _term(value)))
        except CorpusError:
            raise CorpusError('malformed_expected') from None
    if valid != (not results):
        raise CorpusError('malformed_expected')
    return Report(valid, tuple(sorted(results)))


def compare_reports(expected, actual):
    want, got = Counter(expected.violations), Counter(actual.violations)
    return {'matches': expected.conforms == actual.conforms and want == got,
            'expected_violations': len(expected.violations), 'actual_violations': len(actual.violations),
            'missing_constraints': sum((want - got).values()), 'unexpected_constraints': sum((got - want).values())}


def _capture(argv, timeout_ms):
    """Capture two bounded pipes under one deadline and terminate owned descendants.

    Existing runtime-probe primitives provide nonblocking reads and a Windows job
    object. The environment whitelist excludes Java agents, DSNs and credentials.
    """
    process = tree = None
    output, errors = bytearray(), bytearray()
    deadline = time.monotonic() + timeout_ms / 1000
    try:
        if os.name == 'nt':
            tree = _WindowsProbeJob()
            options = {'creationflags': subprocess.CREATE_NO_WINDOW}
        else:
            options = {'start_new_session': True}
        process = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False, env=_probe_env(), bufsize=0, **options)
        if tree is not None:
            tree.assign(process)
        streams = {process.stdout: output, process.stderr: errors}
        while streams:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CorpusError('timeout')
            progress = False
            for stream, captured in list(streams.items()):
                chunk = _available_pipe_chunk(stream)
                if chunk == b'':
                    del streams[stream]
                elif chunk is not None:
                    progress = True
                    if len(output) + len(errors) + len(chunk) > MAX_OUTPUT_BYTES:
                        raise CorpusError('output_limit')
                    captured.extend(chunk)
            if not progress:
                time.sleep(min(.005, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CorpusError('timeout')
        if process.wait(timeout=remaining) != 0:
            raise CorpusError('engine_nonzero')
        return bytes(output)
    except CorpusError:
        raise
    except subprocess.TimeoutExpired:
        raise CorpusError('timeout') from None
    except Exception:
        raise CorpusError('engine_unavailable') from None
    finally:
        try:
            try:
                if tree is not None:
                    tree.close()
                elif process is not None and os.name != 'nt':
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            finally:
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=1)
                    process.stdout.close()
                    process.stderr.close()
        except Exception:
            raise CorpusError('cleanup_failed') from None


def _decoded(raw, parse):
    try:
        return parse(raw.decode('utf-8'))
    except UnicodeError:
        raise CorpusError('malformed_report') from None


def validate_pyshacl_files(data, shape_graph, timeout_ms, parse=parse_report):
    """pySHACL on existing Turtle files; the W3C mode passes one file twice when data and shapes coincide."""
    raw = _capture([sys.executable, str(Path(__file__).with_name('_shacl_worker.py')), str(data), str(shape_graph)],
                   timeout_ms)
    return _decoded(raw, parse)


def validate_pyshacl(data, shape_graph, timeout_ms):
    with tempfile.TemporaryDirectory(prefix='sxc-shacl-python-') as temporary:
        root = Path(temporary)
        dp, sp = root / 'data.ttl', root / 'shapes.ttl'
        dp.write_text(data, encoding='utf-8')
        sp.write_text(shape_graph, encoding='utf-8')
        return validate_pyshacl_files(dp, sp, timeout_ms)


def _jena_command(home, java, data, shape_graph):
    return [java, '-cp', str(Path(home) / 'lib' / '*'), 'shacl.shacl', 'validate',
            '--shapes', Path(shape_graph).resolve().as_uri(), '--data', Path(data).resolve().as_uri()]


def validate_jena(home, java, data, shape_graph, timeout_ms, parse=parse_report):
    raw = _capture(_jena_command(home, java, data, shape_graph), timeout_ms)
    return _decoded(raw, parse)


def _components(args):
    if any(importlib.util.find_spec(name) is None for name in ('rdflib', 'pyshacl')):
        raise StageError('shacl-corpus requires rdflib and pySHACL (requirements/reason.txt)', EXIT_ENV)
    configured = getattr(args, 'jena_home', None) or os.environ.get('SXC_JENA_HOME')
    home = Path(configured) if configured else None
    if home is None or not (home / 'lib').is_dir() or not list((home / 'lib').glob('jena-shacl-*.jar')):
        raise StageError('shacl-corpus requires Apache Jena; set --jena-home or SXC_JENA_HOME', EXIT_ENV)
    java = owl.java_executable()
    if not java:
        raise StageError('shacl-corpus requires Java compatible with Apache Jena; set SXC_JAVA', EXIT_ENV)
    return home.resolve(), java


def run(cfg, args, out):
    if cfg is not None:
        raise StageError('shacl-corpus uses a fixed synthetic corpus and accepts no operational configuration', EXIT_CONFIG)
    timeout = getattr(args, 'timeout_ms', 30000)
    if type(timeout) is not int or not 1 <= timeout <= 600000:
        raise StageError('--timeout-ms must be an integer from 1 to 600000', EXIT_CONFIG)
    # The W3C mode is opt-in by the option only: SXC_W3C_SHACL_SUITE alone never replaces the fixed corpus.
    suite = getattr(args, 'w3c_suite', None)
    allow_unpinned = getattr(args, 'allow_unpinned_suite', False) is True
    if suite is None and allow_unpinned:
        raise StageError('--allow-unpinned-suite applies only with --w3c-suite', EXIT_CONFIG)
    home, java = _components(args)
    if suite is not None:
        suite = suite or os.environ.get('SXC_W3C_SHACL_SUITE')
        if not suite:
            raise StageError('--w3c-suite needs DIR or SXC_W3C_SHACL_SUITE naming a w3c/data-shapes checkout', EXIT_ENV)
        return run_w3c(load_w3c_suite(suite), allow_unpinned, home, java, timeout)
    result = Result('shacl_corpus')
    result.not_proven.append('Synthetic nonrecursive SHACL corpus only; it does not prove production data, '
                             'SQL equivalence, arbitrary or recursive SHACL, or operational V1-V5 coverage.')
    with tempfile.TemporaryDirectory(prefix='sxc-shacl-corpus-') as temporary:
        root = Path(temporary)
        for case in corpus():
            dp, sp = root / 'data.ttl', root / 'shapes.ttl'
            dp.write_text(case.data, encoding='utf-8')
            sp.write_text(case.shapes, encoding='utf-8')
            for engine in ('pyshacl', 'jena'):
                try:
                    actual = (validate_pyshacl(case.data, case.shapes, timeout) if engine == 'pyshacl'
                              else validate_jena(home, java, dp, sp, timeout))
                    detail = compare_reports(case.expected, actual)
                    result.add(engine + ':' + case.id, 'pass' if detail['matches'] else 'fail', '', **detail)
                except CorpusError as problem:
                    result.add(engine + ':' + case.id, 'error', '', reason=problem.reason)
                except Exception:
                    result.add(engine + ':' + case.id, 'error', '', reason='validation_failed')
    result.next_safe_action = ('Review corpus scope before applying conclusions to production.' if result.status == 'pass'
                               else 'Resolve engine errors or investigate constraint mismatches, then rerun the corpus.')
    return result


def _case(case_id, configured_shapes, rows, class_keys, expected):
    cfg = SimpleNamespace(ontology=config.Ontology(iri=NS), shapes=configured_shapes,
                          entities={name: config.Entity(name, 'synthetic.' + name, ['line_no' if name == 'bom_line' else 'part_id'])
                                    for name in ('bom_line', 'part', 'stock')})
    graph = shacl_check.build_graph(cfg, configured_shapes, {'bom_line': rows}, class_keys)
    ttl, _skipped = shapes.shapes_ttl(cfg)
    return Case(case_id, graph.serialize(format='turtle'), ttl, Report(not expected, tuple(sorted(expected))))


def corpus():
    """Hand-specified expected tuples. No naming/export/validator computes these.

    Synthetic bom_line has one key, line_no. Decimal expected strings below are
    canonical exact values. Rows with NULL export no value triple.
    """
    Shape = config.Shape
    C, MI, ME, MA = (SH + name for name in ('ClassConstraintComponent', 'MinInclusiveConstraintComponent',
                                           'MinExclusiveConstraintComponent', 'MaxInclusiveConstraintComponent'))
    V, W = SH + 'Violation', SH + 'Warning'
    # These are literal RDF identities, specified independently of naming.py.
    r1 = 'https://example.org/sxc-corpus#bom_line/%5B%221%22%5D'
    r2 = 'https://example.org/sxc-corpus#bom_line/%5B%222%22%5D'
    r3 = 'https://example.org/sxc-corpus#bom_line/%5B%223%22%5D'
    child = 'https://example.org/sxc-corpus#bom_line.child_id'
    value = 'https://example.org/sxc-corpus#bom_line.child_id/value'
    qty = 'https://example.org/sxc-corpus#bom_line.qty_per'
    p4 = ('iri', 'https://example.org/sxc-corpus#part/%5B%224%22%5D')
    p9 = ('iri', 'https://example.org/sxc-corpus#part/%5B%229%22%5D')
    s1 = ('iri', 'https://example.org/sxc-corpus#stock/%5B%221%22%5D')
    cases = [
        _case('class', [Shape('known_part', 'bom_line', 'child_id', class_='part')],
              [[1, 9], [2, 1], [3, None]], {'part': [1]}, [(r1, child, C, V, p9)]),
        _case('null_no_triple', [Shape('known_part', 'bom_line', 'child_id', class_='part'),
                                Shape('positive', 'bom_line', 'qty_per', min_exclusive=Decimal('0'))],
              [[1, None, None]], {'part': []}, []),
        _case('min_exclusive', [Shape('positive', 'bom_line', 'qty_per', min_exclusive=Decimal('0'))],
              [[1, Decimal('0.000')], [2, Decimal('-0.001')], [3, Decimal('0.001')], [4, None]], {},
              [(r1, qty, ME, V, ('number', '0')), (r2, qty, ME, V, ('number', '-0.001'))]),
        _case('decimal_inclusive', [Shape('range', 'bom_line', 'qty_per', min_inclusive=Decimal('1.100'),
                                         max_inclusive=Decimal('1.200'), severity='warning')],
              [[1, Decimal('1.099999999999999999999999999999')], [2, Decimal('1.100')],
               [3, Decimal('1.200000000000000000000000000001')], [4, Decimal('1.200')], [5, None]], {},
              [(r1, qty, MI, W, ('number', '1.099999999999999999999999999999')),
               (r3, qty, MA, W, ('number', '1.200000000000000000000000000001'))]),
        _case('combined', [Shape('part_capped', 'bom_line', 'child_id', class_='part', max_inclusive=Decimal('5'))],
              [[1, 9], [2, 4], [3, 7], [4, 1], [5, None]], {'part': [7, 1]},
              [(r1, child, C, V, p9), (r1, value, MA, V, ('number', '9')), (r2, child, C, V, p4),
               (r3, value, MA, V, ('number', '7'))]),
    ]
    colliding = [Shape('part_ref', 'bom_line', 'child_id', class_='part'),
                 Shape('child_capped', 'bom_line', 'child_id', max_inclusive=Decimal('5')),
                 Shape('stock_ref', 'bom_line', 'child_id', class_='stock')]
    # Explicit expectations are bound to expected groups, not generated from the
    # group implementation: missing, merged or reordered groups cannot pass.
    expected_groups = {
        'main': [(r1, child, C, V, p9)],
        'extra:1': [(r1, child, MA, V, ('number', '9'))],
        'extra:2': [(r2, child, C, V, s1)],
    }
    actual_groups = shacl_check.groups(colliding)
    if [name for name, _members in actual_groups] != list(expected_groups):
        raise CorpusError('corpus_group_mismatch')
    for name, members in actual_groups:
        cases.append(_case('collision_' + name.replace(':', '_'), members, [[1, 9], [2, 1], [3, None]],
                           {'part': [1], 'stock': [9]}, expected_groups[name]))
    return tuple(cases)


# ---- W3C data-shapes core subset ------------------------------------------------------------------
def suite_digest(files):
    """SHA-256 over the sorted suite-relative paths and bytes, each framed by its length."""
    digest = hashlib.sha256()
    for rel in sorted(files):
        data = files[rel]
        digest.update(rel.encode('utf-8') + b'\0' + str(len(data)).encode('ascii') + b'\0' + data)
    return digest.hexdigest()


def _rdf_list(graph, head, limit=4096):
    """Members of an RDF collection; a branching, cyclic or overlong list raises ValueError."""
    from rdflib import RDF
    items, node, seen = [], head, set()
    while node != RDF.nil:
        firsts, rests = list(graph.objects(node, RDF.first)), list(graph.objects(node, RDF.rest))
        if len(firsts) != 1 or len(rests) != 1 or node in seen or len(items) >= limit:
            raise ValueError('malformed list')
        seen.add(node)
        items.append(firsts[0])
        node = rests[0]
    return items


def _w3c_profile(graph):
    """(reason, features) for a shapes graph: ('', ()) inside the sxc profile, else the first reason of
    _PROFILE_ORDER found and the sorted SHACL local names that caused it.

    SHACL vocabulary is checked graph-wide (an unused or ill-formed path node is still outside the profile),
    shape keys and values per shape. Data triples are not shapes; the expected report's terms are allowed.
    """
    from rdflib import Literal, RDF, URIRef
    sh = lambda name: URIRef(SH + name)
    local = lambda p: str(p)[len(SH):] if isinstance(p, URIRef) and str(p).startswith(SH) else None
    found = {}
    flag = lambda reason, feature: found.setdefault(reason, set()).add(feature)
    used = {}
    for s, p, _o in graph:
        name = local(p)
        if name is not None:
            used.setdefault(name, set()).add(s)
    for name in used.keys() & _SPARQL:
        flag('sparql', name)
    if any(not isinstance(o, URIRef) for o in graph.objects(None, sh('path'))):
        flag('complex_path', 'path')
    for name in used.keys() & _PATHS:
        flag('complex_path', name)
    for name in ('closed', 'ignoredProperties'):
        if name in used:
            flag('closed_shape', name)
    for name in ('targetSubjectsOf', 'targetObjectsOf'):
        if name in used:
            flag('unsupported_target', name)
    for name in used.keys() - _NODE_KEYS - _PROPERTY_KEYS - _REPORT_TERMS:
        flag('unsupported_component', name)
    if (None, URIRef(OWL_IMPORTS), None) in graph:
        # Engines could follow an import; nothing outside the digested files may enter a case.
        flag('unsupported_component', 'owl:imports')
    shape_nodes = set()
    for name in used.keys() & _SHAPE_MARKS:
        shape_nodes |= used[name]
    for kind in ('NodeShape', 'PropertyShape'):
        shape_nodes |= set(graph.subjects(RDF.type, sh(kind)))
    for name in ('node', 'property', 'not', 'qualifiedValueShape'):
        shape_nodes |= set(graph.objects(None, sh(name)))
    for name in ('and', 'or', 'xone'):
        for head in graph.objects(None, sh(name)):
            try:
                shape_nodes |= set(_rdf_list(graph, head))
            except ValueError:
                flag('unsupported_component', name)
    for shape in shape_nodes:
        if isinstance(shape, Literal):
            flag('unsupported_value', 'shape')
            continue
        types = set(graph.objects(shape, RDF.type))
        if URIRef(OWL_CLASS) in types and URIRef(RDFS_CLASS) not in types:
            flag('unsupported_target', 'owlClass')
        paths = list(graph.objects(shape, sh('path')))
        names = {local(p) for p in graph.predicates(shape) if local(p) is not None}
        for name in names - (_PROPERTY_KEYS if paths else _NODE_KEYS):
            flag('unsupported_component', name)
        if len(paths) > 1:
            flag('unsupported_value', 'path')
        if any(not isinstance(node, URIRef) for node in graph.objects(shape, sh('targetNode'))):
            flag('non_iri_focus', 'targetNode')
        for name in ('targetClass', 'class'):
            if any(not isinstance(value, URIRef) for value in graph.objects(shape, sh(name))):
                flag('unsupported_value', name)
        for name in _CONSTRAINTS[1:]:
            for bound in graph.objects(shape, sh(name)):
                if not (isinstance(bound, Literal) and str(bound.datatype) in _NUMERIC and not bound.ill_typed):
                    flag('unsupported_value', name)
        if any(value not in _SEVERITIES for value in graph.objects(shape, sh('severity'))):
            flag('unsupported_value', 'severity')
        if any(not list(graph.objects(value, sh('path'))) for value in graph.objects(shape, sh('property'))):
            flag('unsupported_value', 'property')
    for reason in _PROFILE_ORDER:
        if reason in found:
            return reason, tuple(sorted(found[reason]))
    return '', ()


class _SuiteFiles:
    """Bounded reads of suite-relative paths under data-shapes-test-suite/tests/core/, each read once."""

    def __init__(self, root):
        self.root, self.real, self.data, self.graphs = Path(root), Path(root).resolve(), {}, {}

    @staticmethod
    def relative(iri):
        """The suite-relative path an IRI resolved against _BASE names, or None outside the core suite."""
        from rdflib import URIRef
        text = str(iri)
        if not isinstance(iri, URIRef) or not text.startswith(_BASE):
            return None
        parts = urllib.parse.urlsplit(text)
        if parts.query or parts.fragment:
            return None
        rel = urllib.parse.unquote(text[len(_BASE):])
        segments = rel.split('/')
        if not rel.startswith(W3C_CORE) or any(s in ('', '.', '..') or any(c in s for c in '\\:\0') for s in segments):
            return None
        return rel

    def read(self, rel):
        """Bytes of rel, None when it is not a regular file inside the suite root."""
        if rel in self.data:
            return self.data[rel]
        path = self.root.joinpath(*rel.split('/'))
        try:
            if not path.is_file() or not path.resolve().is_relative_to(self.real):
                return None
            if len(self.data) >= MAX_SUITE_FILES or path.stat().st_size > MAX_SUITE_FILE_BYTES:
                raise StageError('the W3C suite exceeds the file count or file size limit', EXIT_CONFIG)
            data = path.read_bytes()
        except OSError:
            return None
        if len(data) > MAX_SUITE_FILE_BYTES:
            raise StageError('the W3C suite exceeds the file count or file size limit', EXIT_CONFIG)
        self.data[rel] = data
        return data

    def graph(self, rel):
        """The parsed Turtle of a read file (document IRI _BASE + rel), None when it is not valid Turtle."""
        if rel not in self.graphs:
            try:
                self.graphs[rel] = _quiet_graph(self.data[rel], _BASE + urllib.parse.quote(rel))
            except Exception:
                self.graphs[rel] = None
        return self.graphs[rel]


def _case_id(entry, rel, index):
    """The suite-relative test name (core/node/class-001), or a stable positional id for an unusable IRI."""
    from rdflib import URIRef
    text = str(entry)
    if isinstance(entry, URIRef) and text.startswith(_BASE + W3C_TESTS):
        name = urllib.parse.unquote(text[len(_BASE + W3C_TESTS):])
        if _SAFE_ID.fullmatch(name):
            return name
    base = rel[len(W3C_TESTS):].removesuffix('.ttl')
    if not _SAFE_ID.fullmatch(base):
        base = 'entry-' + hashlib.sha256(rel.encode('utf-8')).hexdigest()[:12]
    return f'{base}/entry-{index}'


def _w3c_entry(files, graph, entry, case_id):
    """Classify one manifest entry. Its graph files are read whatever its type or status, so the suite digest
    does not depend on the profile rules."""
    from rdflib import RDF, URIRef
    mf, sht = (lambda name: URIRef(MF + name)), (lambda name: URIRef(SHT + name))
    graph_reason, rels = '', []
    actions = list(graph.objects(entry, mf('action')))
    for prop in ('dataGraph', 'shapesGraph'):
        values = list(graph.objects(actions[0], sht(prop))) if len(actions) == 1 else []
        if len(values) != 1:
            graph_reason = graph_reason or 'missing_graph'
            continue
        rel = files.relative(values[0])
        if rel is None:
            graph_reason = graph_reason or 'outside_suite'
        elif not rel.endswith('.ttl'):
            graph_reason = graph_reason or 'non_turtle'
        elif files.read(rel) is None:
            graph_reason = graph_reason or 'missing_graph'
        rels.append(rel)
    if sht('Validate') not in set(graph.objects(entry, RDF.type)):
        return W3CCase(case_id, 'unsupported_entry_type')
    # Only approved entries count; sht:proposed, sht:rejected or a missing status are not run.
    if set(graph.objects(entry, mf('status'))) != {sht('approved')}:
        return W3CCase(case_id, 'status_not_approved')
    if graph_reason:
        return W3CCase(case_id, graph_reason)
    data, shape_file = rels
    shapes_graph = files.graph(shape_file)
    if shapes_graph is None:
        return W3CCase(case_id, 'malformed_graph')
    reason, features = _w3c_profile(shapes_graph)
    if reason:
        return W3CCase(case_id, reason, features)
    results = list(graph.objects(entry, mf('result')))
    try:
        if len(results) != 1:
            raise CorpusError('malformed_expected')
        expected = _w3c_expected(graph, results[0])
    except CorpusError as problem:
        return W3CCase(case_id, problem.reason)
    return W3CCase(case_id, '', (), expected, data, shape_file)


def load_w3c_suite(root):
    """Read the core manifests of a w3c/data-shapes checkout (mf:include recursion) and classify every entry.

    root holds data-shapes-test-suite/ and optionally PINNED_COMMIT. A missing, escaping, non-Turtle or
    non-manifest include makes the suite unusable (StageError): its entries could not be enumerated.
    """
    from rdflib import RDF, URIRef
    files = _SuiteFiles(root)
    top = W3C_CORE + 'manifest.ttl'
    if files.read(top) is None:
        raise StageError('--w3c-suite must name a w3c/data-shapes checkout containing '
                         'data-shapes-test-suite/tests/core/manifest.ttl', EXIT_ENV)
    cases, visited = {}, set()

    def manifest(rel, depth):
        if rel in visited:
            return
        visited.add(rel)
        if depth > 32 or files.read(rel) is None or files.graph(rel) is None:
            raise StageError('a W3C suite manifest is missing, too deeply nested or not valid Turtle', EXIT_CONFIG)
        graph, me = files.graph(rel), URIRef(_BASE + urllib.parse.quote(rel))
        if (me, RDF.type, URIRef(MF + 'Manifest')) not in graph:
            raise StageError('an included W3C suite file is not a test manifest', EXIT_CONFIG)
        targets = [files.relative(iri) for iri in graph.objects(me, URIRef(MF + 'include'))]
        if any(target is None or not target.endswith('.ttl') for target in targets):
            raise StageError('a W3C suite manifest includes a file outside the core Turtle suite', EXIT_CONFIG)
        index = 0
        for head in graph.objects(me, URIRef(MF + 'entries')):
            try:
                entries = _rdf_list(graph, head)
            except ValueError:
                raise StageError('a W3C suite manifest has a malformed entry list', EXIT_CONFIG) from None
            for entry in entries:
                index += 1
                case_id = _case_id(entry, rel, index)
                if case_id not in cases:
                    cases[case_id] = _w3c_entry(files, graph, entry, case_id)
        for target in sorted(targets):
            manifest(target, depth + 1)

    manifest(top, 0)
    commit = ''
    marker = Path(root) / 'PINNED_COMMIT'
    try:
        if marker.is_file() and marker.stat().st_size <= 128:
            text = marker.read_text(encoding='ascii', errors='replace').strip()
            commit = text if _COMMIT.fullmatch(text) else ''
    except OSError:
        commit = ''
    return W3CSuite(tuple(cases[key] for key in sorted(cases)), dict(files.data), suite_digest(files.data), commit)


def run_w3c(suite, allow_unpinned, home, java, timeout):
    """Run both engines on every in-profile case of a loaded suite, exactly as the fixed corpus runs them.

    Engines read private copies of the digested bytes (same relative layout, so a case whose data and shapes
    are one document still is). An unpinned suite runs nothing unless explicitly allowed.
    """
    pinned = suite.sha256 == W3C_SUITE_SHA256
    runnable = pinned or allow_unpinned
    result = Result('shacl_corpus')
    result.inputs = {'w3c_suite_commit': suite.commit, 'w3c_suite_sha256': suite.sha256,
                     'w3c_suite_files': len(suite.files), 'w3c_suite_pinned': pinned}
    inside = sum(1 for case in suite.cases if not case.reason)
    result.not_proven.append(
        f'W3C SHACL core subset inside the sxc profile only ({inside} of {len(suite.cases)} entries; the rest are '
        'not_run): it does not prove production data, SQL equivalence, other SHACL features, recursive SHACL, '
        'SHACL-SPARQL or operational V1-V5 coverage.')
    if not pinned:
        result.not_proven.append('The W3C suite content digest differs from the pinned commit; '
                                 + ('results are not comparable with the recorded baseline.' if allow_unpinned
                                    else 'no case was run (--allow-unpinned-suite runs it anyway).'))
    with tempfile.TemporaryDirectory(prefix='sxc-shacl-w3c-') as temporary:
        copy = Path(temporary).resolve()
        if runnable:
            for rel, data in suite.files.items():
                path = copy.joinpath(*rel.split('/'))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        for case in suite.cases:
            reason = case.reason if runnable else 'suite_not_pinned'
            for engine in ('pyshacl', 'jena'):
                check_id = 'w3c:' + engine + ':' + case.id
                if reason:
                    features = {'features': list(case.features)} if case.features and runnable else {}
                    result.add(check_id, 'not_run', '', reason=reason, **features)
                    continue
                dp, sp = copy.joinpath(*case.data.split('/')), copy.joinpath(*case.shapes.split('/'))
                try:
                    actual = (validate_pyshacl_files(dp, sp, timeout, parse=parse_w3c_report) if engine == 'pyshacl'
                              else validate_jena(home, java, dp, sp, timeout, parse=parse_w3c_report))
                    detail = compare_reports(case.expected, actual)
                    result.add(check_id, 'pass' if detail['matches'] else 'fail', '', **detail)
                except CorpusError as problem:
                    result.add(check_id, 'error', '', reason=problem.reason)
                except Exception:
                    result.add(check_id, 'error', '', reason='validation_failed')
    states = {check.status for check in result.checks}
    result.next_safe_action = (
        'Resolve engine errors, then rerun the W3C subset.' if 'error' in states else
        'Investigate the engine that disagrees with the W3C expected report before relying on that feature.'
        if 'fail' in states else
        'Out-of-profile W3C cases are not_run, not passed; review the profile before applying conclusions.')
    return result

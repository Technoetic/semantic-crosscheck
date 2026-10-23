"""Independent expected SHACL reports on a fixed synthetic nonrecursive corpus.

The production exporter builds the graphs and shapes. Expected report tuples are
hand-written below, never computed by that exporter or either validator. Both
engines must independently match every expected constraint, including its value.
Check IDs are pyshacl:<case> and jena:<case>, one per engine and corpus case.
"""
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import importlib.util
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import warnings

from . import config, owl, shacl_check, shapes
from .result import EXIT_CONFIG, EXIT_ENV, Result, StageError
from .runtime_provenance import _WindowsProbeJob, _available_pipe_chunk, _probe_env

SH = 'http://www.w3.org/ns/shacl#'
XSD = 'http://www.w3.org/2001/XMLSchema#'
NS = 'https://example.org/sxc-corpus#'
MAX_OUTPUT_BYTES = 262144


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


def parse_report(text):
    """Strict SHACL report parser; a missing report is never an empty success.

    Ignore result node labels, sourceShape labels and diagnostic messages. Keep
    a multiset of (focus, path, constraint component, severity, semantic value).
    The fixed corpus requires all five fields and excludes recursive paths.
    """
    import rdflib
    from rdflib import URIRef, Literal
    try:
        if not isinstance(text, str) or not text or len(text.encode('utf-8')) > MAX_OUTPUT_BYTES:
            raise CorpusError('missing_report')
        # rdflib reports malformed literal text in warnings/log records. Suppress
        # that diagnostic channel, then explicitly reject ill-typed literals.
        logger = logging.getLogger('rdflib.term')
        parse_thread = threading.get_ident()
        quiet = lambda record: record.thread != parse_thread
        logger.addFilter(quiet)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                graph = rdflib.Graph().parse(data=text, format='turtle')
        finally:
            logger.removeFilter(quiet)
        sh = lambda name: URIRef(SH + name)
        roots = set(graph.subjects(rdflib.RDF.type, sh('ValidationReport')))
        if len(roots) != 1:
            raise CorpusError('malformed_report')
        root = next(iter(roots))
        conforms = list(graph.objects(root, sh('conforms')))
        if (len(conforms) != 1 or not isinstance(conforms[0], Literal)
                or conforms[0].datatype != URIRef(XSD + 'boolean')
                or conforms[0].ill_typed
                or str(conforms[0]) not in ('true', 'false', '1', '0')):
            raise CorpusError('malformed_report')
        valid = str(conforms[0]) in ('true', '1')
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


def validate_pyshacl(data, shape_graph, timeout_ms):
    with tempfile.TemporaryDirectory(prefix='sxc-shacl-python-') as temporary:
        root = Path(temporary)
        dp, sp = root / 'data.ttl', root / 'shapes.ttl'
        dp.write_text(data, encoding='utf-8')
        sp.write_text(shape_graph, encoding='utf-8')
        raw = _capture([sys.executable, str(Path(__file__).with_name('_shacl_worker.py')), str(dp), str(sp)], timeout_ms)
    try:
        return parse_report(raw.decode('utf-8'))
    except UnicodeError:
        raise CorpusError('malformed_report') from None


def _jena_command(home, java, data, shape_graph):
    return [java, '-cp', str(Path(home) / 'lib' / '*'), 'shacl.shacl', 'validate',
            '--shapes', Path(shape_graph).resolve().as_uri(), '--data', Path(data).resolve().as_uri()]


def validate_jena(home, java, data, shape_graph, timeout_ms):
    raw = _capture(_jena_command(home, java, data, shape_graph), timeout_ms)
    try:
        return parse_report(raw.decode('utf-8'))
    except UnicodeError:
        raise CorpusError('malformed_report') from None


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
    home, java = _components(args)
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

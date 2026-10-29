"""Bounded, authored synthetic before/after SHACL fixtures; never an automatic repair."""
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile

from . import shacl_corpus as corpus
from .result import EXIT_CONFIG, EXIT_ENV, Result, StageError

MANIFEST_LIMIT = 256 * 1024
FILE_LIMIT = 1024 * 1024
TOTAL_LIMIT = 8 * 1024 * 1024
CASE_LIMIT = 16
TRIPLE_LIMIT = 10000
FACT_LIMIT = 1024
_IRI = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*:[^\s<>"{}|^`\\]+\Z')
_ID = re.compile(r'[A-Za-z0-9._-]{1,64}\Z')
_COMPONENTS = frozenset(('ClassConstraintComponent', 'MinExclusiveConstraintComponent',
    'MinInclusiveConstraintComponent', 'MaxInclusiveConstraintComponent',
    'LessThanConstraintComponent', 'LessThanOrEqualsConstraintComponent'))
_REASONS = frozenset(('sparql', 'complex_path', 'closed_shape', 'unsupported_target',
    'non_iri_focus', 'unsupported_component', 'unsupported_value', 'imports_not_supported',
    'blank_data_node', 'relative_iri', 'triple_limit', 'empty_scope', 'malformed_graph',
    'malformed_report', 'unsupported_report_term', 'validation_failed'))


def _bad():
    raise StageError('shacl-repair: invalid bounded local synthetic fixture manifest or input', EXIT_CONFIG)


def _object(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        _bad()


def _list(value, maximum=FACT_LIMIT, nonempty=False):
    if not isinstance(value, list) or len(value) > maximum or (nonempty and not value):
        _bad()
    return value


def _string(value):
    if not isinstance(value, str) or len(value) > 4096:
        _bad()
    return value


def _iri(value):
    if not _IRI.fullmatch(_string(value)):
        _bad()
    return value


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        _bad()
    return value


def _term(value, immutable=False):
    if not isinstance(value, dict):
        _bad()
    kind = value.get('kind')
    if kind == 'iri':
        _object(value, ('kind', 'value'))
        return ('iri', _iri(value['value']))
    if kind == 'number' and not immutable:
        _object(value, ('kind', 'value'))
        number = _string(value['value'])
        if len(number) > 256 or not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)', number):
            _bad()
        try:
            return ('number', corpus._number(number))
        except corpus.CorpusError:
            _bad()
    if kind == 'literal':
        _object(value, ('kind', 'value', 'datatype', 'language'))
        lexical = _string(value['value'])
        datatype, language = _iri(value['datatype']), _string(value['language'])
        if language and not re.fullmatch(r'[A-Za-z]+(?:-[A-Za-z0-9]+)*', language):
            _bad()
        if bool(language) != (datatype == 'http://www.w3.org/1999/02/22-rdf-syntax-ns#langString'):
            _bad()
        if not immutable and datatype in corpus._NUMERIC:
            _bad()
        return ('literal', lexical, datatype, language.lower())
    _bad()


def _expected(value, phase):
    _object(value, ('conforms', 'results'))
    if type(value['conforms']) is not bool or value['conforms'] != (phase == 'after'):
        _bad()
    facts = []
    for fact in _list(value['results']):
        _object(fact, ('focus', 'path', 'component', 'severity', 'value'))
        focus = ('iri', _iri(fact['focus']))
        path = ('absent',) if fact['path'] is None else ('iri', _iri(fact['path']))
        if fact['component'] not in _COMPONENTS or fact['severity'] not in ('Violation', 'Warning'):
            _bad()
        term = ('absent',) if fact['value'] is None else _term(fact['value'])
        facts.append((focus, path, corpus.SH + fact['component'], corpus.SH + fact['severity'], term))
    if value['conforms'] != (not facts):
        _bad()
    return corpus.Report(value['conforms'], tuple(sorted(facts)))


def _same_file(one, two):
    try:
        return one.resolve() == two.resolve() or (one.exists() and two.exists() and one.samefile(two))
    except (OSError, ValueError, RuntimeError):
        _bad()


def _owned(out):
    directory = Path(out) / 'shacl_repair'
    return (directory / 'result.json', directory / 'result.json.part')


def prepare_run(args, out):
    """Guard all declared input aliases BEFORE CLI discard; this performs no writes.

    Parse key/value pairs without discarding duplicate keys, so even an invalid
    duplicate declaration cannot hide an input alias from cleanup protection.
    A complete bounded UTF-8 JSON parse is required before cleanup is safe.
    Schema validation and ordinary failure cleanup remain run()'s responsibility.
    """
    try:
        source = Path(args.spec)
    except (AttributeError, TypeError, ValueError):
        _bad()
    outputs = _owned(out)

    def protect(path):
        if any(_same_file(path, target) for target in outputs):
            raise StageError('shacl-repair: output artifact aliases an input; choose another output directory', EXIT_CONFIG)

    protect(source)
    try:
        with source.open('rb') as stream:
            raw = stream.read(MANIFEST_LIMIT + 1)
        if len(raw) > MANIFEST_LIMIT:
            _bad()
        pairs = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=lambda items: items)
    except (OSError, ValueError, UnicodeError, RecursionError):
        _bad()

    def visit(node):
        if not isinstance(node, list):
            return
        for member in node:
            if isinstance(member, tuple) and len(member) == 2:
                key, value = member
                if key in ('before', 'after', 'shapes') and isinstance(value, str):
                    protect(source.parent / value)
                visit(value)
            else:
                visit(member)
    try:
        visit(pairs)
    except RecursionError:
        _bad()


def _discard(out):
    try:
        for path in _owned(out):
            path.unlink(missing_ok=True)
    except OSError:
        raise StageError('shacl-repair: cannot clear prior output artifacts', EXIT_CONFIG) from None


def _json(raw):
    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError
            value[key] = item
        return value

    def invalid(_value):
        raise ValueError
    return json.loads(raw, object_pairs_hook=object_pairs, parse_constant=invalid, parse_float=invalid)


def _load(path):
    try:
        source = Path(path)
        with source.open('rb') as stream:
            raw = stream.read(MANIFEST_LIMIT + 1)
        if len(raw) > MANIFEST_LIMIT:
            _bad()
        document = _json(raw)
        _object(document, ('schema_version', 'synthetic', 'cases'))
        if type(document['schema_version']) is not int or document['schema_version'] != 1 or document['synthetic'] is not True:
            _bad()
        cases = _list(document['cases'], CASE_LIMIT, True)
        root = source.parent.resolve()
        files, ids = {}, set()
        for case in cases:
            _object(case, ('id', 'before', 'after', 'shapes', 'retained_targets',
                'immutable_predicates', 'immutable_triples', 'edit_costs', 'expected'))
            if not _ID.fullmatch(_string(case['id'])) or case['id'] in ids:
                _bad()
            ids.add(case['id'])
            for field in ('before', 'after', 'shapes'):
                name = _string(case[field])
                relative = PurePosixPath(name)
                if (not name or '\\' in name or ':' in name or relative.is_absolute()
                        or '..' in relative.parts or relative.suffix != '.ttl'):
                    _bad()
                target = (root / name).resolve()
                if not target.is_relative_to(root):
                    _bad()
                if name not in files:
                    with target.open('rb') as stream:
                        content = stream.read(FILE_LIMIT + 1)
                    if len(content) > FILE_LIMIT or sum(map(len, files.values())) + len(content) > TOTAL_LIMIT:
                        _bad()
                    files[name] = content
            retention = _list(case['retained_targets'], nonempty=True)
            obligations = []
            for retained in retention:
                _object(retained, ('shape', 'node'))
                obligations.append((_iri(retained['shape']), _iri(retained['node'])))
            if len(set(obligations)) != len(obligations):
                _bad()
            predicates = [_iri(p) for p in _list(case['immutable_predicates'])]
            if len(set(predicates)) != len(predicates):
                _bad()
            triples = []
            for triple in _list(case['immutable_triples']):
                if not isinstance(triple, list) or len(triple) != 3:
                    _bad()
                terms = tuple(_term(t, immutable=True) for t in triple)
                if any(t[0] != 'iri' for t in terms[:2]):
                    _bad()
                triples.append(terms)
            if len(set(triples)) != len(triples):
                _bad()
            _object(case['edit_costs'], ('add', 'remove'))
            for cost in case['edit_costs'].values():
                _integer(cost, 0, 1000000)
            _object(case['expected'], ('before', 'after'))
            for phase in ('before', 'after'):
                _expected(case['expected'][phase], phase)
        digest = hashlib.sha256(raw)
        for name, content in sorted(files.items()):
            encoded = name.encode('utf-8')
            digest.update(len(encoded).to_bytes(8, 'big') + encoded)
            digest.update(len(content).to_bytes(8, 'big') + content)
        return document, files, digest.hexdigest()
    except StageError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError, RuntimeError):
        _bad()


def _worker_checks(raw, case_count):
    """Allow only bounded fixed identifiers and integer statistics across the child boundary."""
    try:
        values = _json(raw)
        if not isinstance(values, list) or not 1 <= len(values) <= case_count * 6:
            raise ValueError
        seen, checks = set(), []
        count_keys = frozenset(('expected_violations', 'actual_violations', 'missing_constraints',
            'unexpected_constraints', 'obligations', 'retained_before', 'retained_after',
            'immutable_predicates', 'immutable_triples', 'changed_predicates', 'missing_before',
            'missing_after', 'added_triples', 'removed_triples', 'edit_cost'))
        for value in values:
            if not isinstance(value, dict) or set(value) != {'id', 'status', 'detail'}:
                raise ValueError
            match = re.fullmatch(r'fixture:([0-9]{4}):(profile|before|after|retention|immutable|edits)', value['id'])
            if not match or not 1 <= int(match[1]) <= case_count or value['id'] in seen:
                raise ValueError
            seen.add(value['id'])
            if value['status'] not in ('pass', 'fail', 'error', 'not_run') or not isinstance(value['detail'], dict):
                raise ValueError
            for key, item in value['detail'].items():
                if key == 'reason' and item in _REASONS:
                    continue
                if key == 'matches' and type(item) is bool:
                    continue
                if key not in count_keys or type(item) is not int or not 0 <= item <= 10**12:
                    raise ValueError
            checks.append(value)
        # A successful case must carry every required gate, never just a report.
        for index in range(1, case_count + 1):
            prefix = 'fixture:%04d:' % index
            group = [v for v in values if v['id'].startswith(prefix)]
            if not group or (all(v['status'] == 'pass' for v in group) and
                    {v['id'][len(prefix):] for v in group} != {'profile', 'before', 'after', 'retention', 'immutable', 'edits'}):
                raise ValueError
        return checks
    except (ValueError, TypeError, KeyError, RecursionError):
        raise corpus.CorpusError('malformed_report') from None


def run(cfg, args, out):
    prepare_run(args, out)
    _discard(out)
    if cfg is not None:
        raise StageError('shacl-repair: synthetic fixtures accept no operational configuration', EXIT_CONFIG)
    timeout = getattr(args, 'timeout_ms', 30000)
    _integer(timeout, 1, 600000)
    document, files, digest = _load(getattr(args, 'spec', None))
    if not importlib.util.find_spec('rdflib') or not importlib.util.find_spec('pyshacl'):
        raise StageError('shacl-repair requires rdflib and pySHACL (requirements/reason.txt)', EXIT_ENV)
    result = Result('shacl_repair')
    result.inputs = {'fixture_sha256': digest, 'fixture_cases': len(document['cases']), 'fixture_files': len(files)}
    result.not_proven.extend([
        'Authored synthetic before/after fixtures only; no automatic repair, operational data write or repair search runs.',
        'Only explicit retained per-shape targets and immutable declarations are checked; undeclared preservation is not proven.',
        'The single pySHACL engine checks class, range and property-pair shapes; no independent-engine or global correctness claim.',
        'Report bags compare focus, path, component, severity and normalized value; sourceShape, messages and result blank-node labels are ignored.',
        'Soft edit costs describe supplied graph changes only; no optimality, unique repair or feasibility beyond these fixtures is proven.',
    ])
    result.next_safe_action = 'Review failed fixture gates and independently authored expectations before changing a supplied fixture.'
    try:
        with tempfile.TemporaryDirectory(prefix='sxc-shacl-repair-') as temporary:
            root = Path(temporary)
            names = {}
            for index, (name, content) in enumerate(sorted(files.items())):
                private_name = '%04d.ttl' % index
                (root / private_name).write_bytes(content)
                names[name] = private_name
            for case in document['cases']:
                for field in ('before', 'after', 'shapes'):
                    case[field] = names[case[field]]
            payload = root / 'fixture.json'
            payload.write_text(json.dumps(document), encoding='utf-8')
            raw = corpus._capture([sys.executable, str(Path(__file__).with_name('_shacl_repair_worker.py')), str(payload)], timeout)
            for check in _worker_checks(raw, len(document['cases'])):
                result.add(check['id'], check['status'], '', **check['detail'])
    except corpus.CorpusError as problem:
        result.add('fixture:execution', 'error', '', reason=problem.reason)
    except Exception:
        result.add('fixture:execution', 'error', '', reason='validation_failed')
    return result


def _targets(data, shapes, shape):
    """SHACL targetClass includes data-graph subclass closure, even with inference='none'."""
    from rdflib import RDF, RDFS, URIRef
    sh = lambda name: URIRef(corpus.SH + name)
    result = set(shapes.objects(shape, sh('targetNode')))
    classes = set(shapes.objects(shape, sh('targetClass')))
    if (shape, RDF.type, RDFS.Class) in shapes:
        classes.add(shape)
    for target in classes:
        subclasses, pending = {target}, [target]
        while pending:
            for child in data.subjects(RDFS.subClassOf, pending.pop()):
                if child not in subclasses:
                    subclasses.add(child)
                    pending.append(child)
        for child in subclasses:
            result.update(data.subjects(RDF.type, child))
    return result


def _rdf_term(value):
    from rdflib import Literal, URIRef
    term = _term(value, immutable=True)
    if term[0] == 'iri':
        return URIRef(term[1])
    return Literal(term[1], lang=term[3] or None, datatype=None if term[3] else URIRef(term[2]), normalize=False)


def _execute(document, root):
    """Runs only inside the bounded worker; raw RDF terms never enter returned checks."""
    import rdflib
    from rdflib import BNode, Literal, RDF, RDFS, URIRef
    from pyshacl import validate
    checks = []
    rdflib.NORMALIZE_LITERALS = False
    sh = lambda name: URIRef(corpus.SH + name)

    def add(prefix, gate, status, **detail):
        checks.append({'id': prefix + gate, 'status': status, 'detail': detail})

    for index, case in enumerate(document['cases'], 1):
        prefix = 'fixture:%04d:' % index
        try:
            graphs = {name: corpus._quiet_graph((root / case[name]).read_bytes(), public_id='http://sxc.invalid/repair/')
                      for name in ('before', 'after', 'shapes')}
            reason = ''
            for name, graph in graphs.items():
                if len(graph) > TRIPLE_LIMIT:
                    reason = 'triple_limit'
                elif (None, URIRef(corpus.OWL_IMPORTS), None) in graph:
                    reason = 'imports_not_supported'
                elif name != 'shapes' and any(isinstance(term, BNode) for triple in graph for term in triple):
                    reason = 'blank_data_node'
                elif any(isinstance(term, Literal) and term.ill_typed for triple in graph for term in triple):
                    reason = 'unsupported_value'
                elif any(isinstance(term, URIRef) and (not _IRI.fullmatch(str(term)) or str(term).startswith('http://sxc.invalid/'))
                         or isinstance(term, Literal) and term.datatype and (not _IRI.fullmatch(str(term.datatype))
                             or str(term.datatype).startswith('http://sxc.invalid/')) for triple in graph for term in triple):
                    reason = 'relative_iri'
                if reason:
                    break
            before, after, shapes = (graphs[name] for name in ('before', 'after', 'shapes'))
            if not reason:
                reason, _features = corpus._w3c_profile(shapes)
            targeted = set(shapes.subjects(sh('targetClass'), None)) | set(shapes.subjects(sh('targetNode'), None))
            targeted |= set(shapes.subjects(RDF.type, RDFS.Class))
            if not reason and any(not isinstance(shape, URIRef) for shape in targeted):
                reason = 'unsupported_target'
            if not reason and (not targeted or not any((None, sh(key), None) in shapes
                    for key in corpus._CONSTRAINTS + corpus._PAIRS)):
                reason = 'empty_scope'
            all_targets = set().union(*(_targets(data, shapes, shape) for data in (before, after) for shape in targeted)) if targeted else set()
            if not reason and any(not isinstance(node, URIRef) for node in all_targets):
                reason = 'non_iri_focus'
            if reason:
                add(prefix, 'profile', 'not_run', reason=reason)
                continue
            add(prefix, 'profile', 'pass')
            for phase, graph in (('before', before), ('after', after)):
                _conforms, report, _text = validate(graph, shacl_graph=shapes, inference='none', advanced=False,
                    abort_on_first=False, do_owl_imports=False, js=False, meta_shacl=False, inplace=False)
                if not isinstance(report, rdflib.Graph):
                    raise corpus.CorpusError('malformed_report')
                actual = corpus.parse_w3c_report(report.serialize(format='turtle'))
                detail = corpus.compare_reports(_expected(case['expected'][phase], phase), actual)
                add(prefix, phase, 'pass' if detail['matches'] else 'fail', **detail)
            retained = []
            for graph in (before, after):
                retained.append(sum(URIRef(item['node']) in _targets(graph, shapes, URIRef(item['shape']))
                    and (URIRef(item['node']), None, None) in graph for item in case['retained_targets']))
            obligations = len(case['retained_targets'])
            add(prefix, 'retention', 'pass' if retained == [obligations, obligations] else 'fail',
                obligations=obligations, retained_before=retained[0], retained_after=retained[1])
            changed = sum(set(before.triples((None, URIRef(predicate), None))) !=
                          set(after.triples((None, URIRef(predicate), None))) for predicate in case['immutable_predicates'])
            triples = [tuple(_rdf_term(term) for term in triple) for triple in case['immutable_triples']]
            missing_before = sum(triple not in before for triple in triples)
            missing_after = sum(triple not in after for triple in triples)
            add(prefix, 'immutable', 'fail' if changed or missing_before or missing_after else 'pass',
                immutable_predicates=len(case['immutable_predicates']), immutable_triples=len(triples),
                changed_predicates=changed, missing_before=missing_before, missing_after=missing_after)
            additions, removals = len(set(after) - set(before)), len(set(before) - set(after))
            add(prefix, 'edits', 'pass', added_triples=additions, removed_triples=removals,
                edit_cost=additions * case['edit_costs']['add'] + removals * case['edit_costs']['remove'])
        except corpus.CorpusError as problem:
            add(prefix, 'after', 'error', reason=problem.reason if problem.reason in _REASONS else 'malformed_report')
        except Exception:
            # All parser/engine exceptions are intentionally collapsed; no message, path or value crosses the boundary.
            gate = 'after' if any(c['id'] == prefix + 'profile' for c in checks) else 'profile'
            if any(c['id'] == prefix + gate for c in checks):
                gate = 'before'
            add(prefix, gate, 'error', reason='validation_failed' if gate != 'profile' else 'malformed_graph')
    return checks

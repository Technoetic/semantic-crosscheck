"""Opt-in, offline PostgreSQL 17 syntax comparison against authored expectations.

This is a structural diagnostic, not a catalog/type checker or a semantic oracle.
The optional GPL pglast binding is installed separately, never vendored.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from . import semantic_preflight as guarded
from .result import EXIT_CONFIG, EXIT_ENV, Result

STAGE = 'sql_structure'
SQL_LIMIT = 64 * 1024
PGLAST_VERSION = 'v7.22'
AGGREGATES = {'sum', 'count', 'avg', 'min', 'max'}
OPERATORS = {'=', '<>', '<', '<=', '>', '>='}


class _Unsupported(Exception):
    pass


class _Incomplete(Exception):
    pass


def _keys(value, required, optional=(), exception=guarded._Invalid):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional):
        raise exception()
    if set(required) - set(value):
        raise exception()
    return value


def _identifier(value):
    if not isinstance(value, str) or not value or '\x00' in value or len(value.encode('utf-8')) > 63:
        raise guarded._Invalid()
    return value


def _operand(value):
    if not isinstance(value, dict) or len(value) != 1:
        raise guarded._Invalid()
    if 'parameter' in value:
        number = value['parameter']
        if type(number) is not int or not 1 <= number <= 65535:
            raise guarded._Invalid()
        return ('parameter', number)
    if 'literal' in value:
        scalar = value['literal']
        if type(scalar) is bool:
            return ('boolean', scalar)
        if type(scalar) is int and -(2 ** 31) <= scalar < 2 ** 31:
            return ('integer', scalar)
        if isinstance(scalar, str) and '\x00' not in scalar and len(scalar.encode('utf-8')) <= 4096:
            return ('string', scalar)
    raise guarded._Invalid()


def _expect(doc):
    # Reuse duplicate-key/depth/node/finite-number rules for callers of evaluate.
    doc = guarded._decode(json.dumps(doc, ensure_ascii=False).encode('utf-8'))
    required = {'version', 'relation', 'select', 'group_by', 'where'}
    if isinstance(doc, dict) and not set(doc) - required and required - set(doc):
        raise _Incomplete()
    _keys(doc, required)
    if type(doc['version']) is not int or doc['version'] != 1:
        raise guarded._Invalid()
    relation = _keys(doc['relation'], {'name'}, {'schema'})
    relation = (None if 'schema' not in relation else _identifier(relation['schema']),
                _identifier(relation['name']))
    if any(not isinstance(doc[key], list) for key in ('select', 'group_by', 'where')) or not doc['select']:
        raise guarded._Invalid()
    selected = []
    for item in doc['select']:
        _keys(item, {'column'}, {'aggregate'})
        column = _identifier(item['column'])
        if 'aggregate' in item:
            if item['aggregate'] not in AGGREGATES or (column == '*' and item['aggregate'] != 'count'):
                raise guarded._Invalid()
            selected.append(('aggregate', item['aggregate'], column))
        else:
            if column == '*':
                raise guarded._Invalid()
            selected.append(('column', column))
    grouping = sorted(_identifier(item) for item in doc['group_by'])
    predicates = []
    for item in doc['where']:
        if not isinstance(item, dict):
            raise guarded._Invalid()
        operator = item.get('op')
        if operator == 'between':
            _keys(item, {'column', 'op', 'lower', 'upper'})
            predicates.append((_identifier(item['column']), operator, _operand(item['lower']), _operand(item['upper'])))
        else:
            _keys(item, {'column', 'op', 'value'})
            if operator not in OPERATORS:
                raise guarded._Invalid()
            predicates.append((_identifier(item['column']), operator, _operand(item['value'])))
    return relation, selected, grouping, sorted(predicates, key=repr)


def _node(value, tag, required=(), optional=()):
    _keys(value, {tag}, exception=_Unsupported)
    return _keys(value[tag], required, optional, exception=_Unsupported)


def _strings(values):
    if not isinstance(values, list):
        raise _Unsupported()
    return [_node(item, 'String', {'sval'})['sval'] for item in values]


def _column(value):
    node = _node(value, 'ColumnRef', {'fields'}, {'location'})
    fields = _strings(node['fields'])
    if len(fields) != 1:
        raise _Unsupported()
    return _identifier(fields[0])


def _ast_operand(value):
    if isinstance(value, dict) and 'ParamRef' in value:
        node = _node(value, 'ParamRef', {'number'}, {'location'})
        return _operand({'parameter': node['number']})
    node = _node(value, 'A_Const', (), {'ival', 'sval', 'boolval', 'location'})
    fields = set(node) - {'location'}
    if fields == {'ival'}:
        scalar = _keys(node['ival'], (), {'ival'}, exception=_Unsupported).get('ival', 0)
    elif fields == {'sval'}:
        scalar = _keys(node['sval'], (), {'sval'}, exception=_Unsupported).get('sval', '')
    elif fields == {'boolval'}:
        scalar = _keys(node['boolval'], (), {'boolval'}, exception=_Unsupported).get('boolval', False)
    else:
        raise _Unsupported()
    return _operand({'literal': scalar})


def _selection(value):
    target = _node(value, 'ResTarget', {'val'}, {'location'})
    value = target['val']
    if isinstance(value, dict) and 'ColumnRef' in value:
        return ('column', _column(value))
    call = _node(value, 'FuncCall', {'funcname', 'funcformat'}, {'args', 'agg_star', 'location'})
    names = _strings(call['funcname'])
    if len(names) != 1 or names[0] not in AGGREGATES or call['funcformat'] != 'COERCE_EXPLICIT_CALL':
        raise _Unsupported()
    if call.get('agg_star') is True:
        if names[0] != 'count' or call.get('args'):
            raise _Unsupported()
        return ('aggregate', 'count', '*')
    args = call.get('args')
    if not isinstance(args, list) or len(args) != 1:
        raise _Unsupported()
    return ('aggregate', names[0], _column(args[0]))


def _predicates(value):
    if isinstance(value, dict) and 'BoolExpr' in value:
        node = _node(value, 'BoolExpr', {'boolop', 'args'}, {'location'})
        if node['boolop'] != 'AND_EXPR' or not isinstance(node['args'], list) or len(node['args']) < 2:
            raise _Unsupported()
        return [item for child in node['args'] for item in _predicates(child)]
    node = _node(value, 'A_Expr', {'kind', 'name', 'lexpr', 'rexpr'}, {'location'})
    column = _column(node['lexpr'])
    names = _strings(node['name'])
    if node['kind'] == 'AEXPR_BETWEEN' and names == ['BETWEEN']:
        bounds = _node(node['rexpr'], 'List', {'items'})['items']
        if not isinstance(bounds, list) or len(bounds) != 2:
            raise _Unsupported()
        return [(column, 'between', _ast_operand(bounds[0]), _ast_operand(bounds[1]))]
    if node['kind'] != 'AEXPR_OP' or len(names) != 1 or names[0] not in OPERATORS:
        raise _Unsupported()
    return [(column, names[0], _ast_operand(node['rexpr']))]


def _structure(doc):
    _keys(doc, {'version', 'stmts'}, exception=_Unsupported)
    if type(doc['version']) is not int or doc['version'] // 10000 != 17:
        raise _Unsupported()
    if not isinstance(doc['stmts'], list) or len(doc['stmts']) != 1:
        raise _Unsupported()
    raw = _keys(doc['stmts'][0], {'stmt'}, {'stmt_location', 'stmt_len'}, exception=_Unsupported)
    select = _node(raw['stmt'], 'SelectStmt', {'targetList', 'fromClause', 'limitOption', 'op'},
                   {'whereClause', 'groupClause'})
    if select['op'] != 'SETOP_NONE' or select['limitOption'] != 'LIMIT_OPTION_DEFAULT':
        raise _Unsupported()
    if not isinstance(select['fromClause'], list) or len(select['fromClause']) != 1:
        raise _Unsupported()
    relation = _node(select['fromClause'][0], 'RangeVar', {'relname', 'inh', 'relpersistence'},
                     {'schemaname', 'location'})
    if relation['inh'] is not True or relation['relpersistence'] != 'p':
        raise _Unsupported()
    relation = (None if 'schemaname' not in relation else _identifier(relation['schemaname']),
                _identifier(relation['relname']))
    if not isinstance(select['targetList'], list) or not select['targetList']:
        raise _Unsupported()
    selected = [_selection(item) for item in select['targetList']]
    grouping = sorted(_column(item) for item in select.get('groupClause', []))
    predicates = _predicates(select['whereClause']) if 'whereClause' in select else []
    return relation, selected, grouping, sorted(predicates, key=repr)


def _parser():
    """Import only when explicitly invoked; never install or contact a service."""
    try:
        import pglast
        from pglast import parser
        if pglast.__version__ != PGLAST_VERSION or parser.get_postgresql_version()[0] != 17:
            return None
        return parser.parse_sql_json
    except Exception:
        return None


def _result(status, reason):
    result = Result(STAGE)
    result.add('structure_admission', status, reason=reason)
    result.not_proven = ['Business meaning, SQL execution, catalog binding, types, parameter values and data correctness.']
    result.next_safe_action = 'Review the authored expectations and unsupported syntax locally.'
    return result


def evaluate(sql, expected):
    """Compare admitted syntax only; no authored identifiers or values escape."""
    try:
        if not isinstance(sql, str) or '\x00' in sql or not sql.strip() or len(sql.encode('utf-8')) > SQL_LIMIT:
            raise guarded._Invalid()
        wanted = _expect(expected)
    except _Incomplete:
        return _result('not_run', 'needs_clarification')
    except Exception:
        return _result('error', 'invalid_expectation_or_sql')
    parse = _parser()
    if parse is None:
        return _result('not_run', 'parser_unavailable')
    try:
        actual = _structure(guarded._decode(parse(sql).encode('utf-8')))
    except Exception:
        return _result('not_run', 'unsupported_or_invalid_sql')
    result = Result(STAGE)
    for check_id, observed, required in zip(('relation', 'selection', 'grouping', 'predicates'), actual, wanted):
        result.add(check_id, 'pass' if observed == required else 'fail',
                   reason='structure_matches' if observed == required else 'structure_mismatch')
    result.not_proven = ['Business meaning, SQL execution, catalog binding, aggregate resolution, types, parameter values and data correctness.']
    result.next_safe_action = 'Review mismatches locally; independently verify business meaning and runtime behavior.'
    return result


class _Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise guarded._Invalid()


def _arguments(argv):
    if not argv or argv[0] != 'sql-structure':
        raise guarded._Invalid()
    parser = _Parser(prog='sxc.py sql-structure', allow_abbrev=False,
                     description='Compare PostgreSQL 17 syntax with explicit structural expectations offline.')
    for name in ('sql', 'expect'):
        parser.add_argument('--' + name, action='append', required=True, metavar='FILE')
    parser.add_argument('--out', action='append', metavar='DIR')
    parser.add_argument('--quiet', action='count', default=0)
    args = parser.parse_args(argv[1:])
    for name in ('sql', 'expect', 'out'):
        values = getattr(args, name)
        if values is not None:
            if len(values) != 1 or not values[0] or '\x00' in values[0]:
                raise guarded._Invalid()
            setattr(args, name, values[0])
    if args.quiet > 1:
        raise guarded._Invalid()
    return args


def _protect(inputs, out):
    """Use the same guarded capture and output policy without changing its stage."""
    directory = out / STAGE
    guarded._inspect(out, directory=True, missing=True)
    guarded._inspect(directory, directory=True, missing=True)
    targets = [out / '.gitignore', directory / 'result.json', directory / 'result.json.part']
    for target in targets:
        guarded._inspect(target, missing=True)
    for source in inputs:
        source.unchanged()
        if any(guarded._aliases(source.path, target) for target in [out, directory, *targets]):
            raise guarded._Invalid()
        if source.path.resolve() in out.resolve().parents:
            raise guarded._Invalid()


def _publish(result, inputs, out):
    _protect(inputs, out)
    created = not out.exists()
    out.mkdir(parents=True, exist_ok=True)
    _protect(inputs, out)
    if created:
        fd = os.open(out / '.gitignore', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write('*\n')
    _protect(inputs, out)
    result.write(out)


def cli(argv, _parser_factory, summary):
    try:
        args = _arguments(argv)
    except SystemExit as exc:
        return exc.code
    except Exception:
        sys.stderr.write('[sxc sql_structure] ERROR invalid_arguments\n')
        return EXIT_CONFIG
    try:
        inputs = [guarded._Captured.read(getattr(args, name)) for name in ('sql', 'expect')]
        out = Path(args.out).absolute() if args.out else inputs[0].path.parent / '.sxc'
        _protect(inputs, out)
        (out / STAGE / 'result.json').unlink(missing_ok=True)
        try:
            result = evaluate(inputs[0].raw.decode('utf-8-sig'), guarded._decode(inputs[1].raw))
        except Exception:
            result = _result('error', 'invalid_expectation_or_sql')
        result.inputs = {name + '_sha256': hashlib.sha256(source.raw).hexdigest()
                         for name, source in zip(('sql', 'expectation'), inputs)}
        result.inputs['generator_sha256'] = hashlib.sha256(
            Path(__file__).read_bytes() + Path(guarded.__file__).read_bytes()).hexdigest()
        result.provenance = {'schema_version': 1, 'binding': 'not_applicable'}
        result.runtime_evidence = {'schema_version': 1, 'binding': 'not_applicable'}
        result._suppress_paths = True
        _publish(result, inputs, out)
    except Exception:
        sys.stderr.write('[sxc sql_structure] ERROR invalid_input\n')
        return EXIT_CONFIG
    print(summary(result, None, bool(args.quiet)))
    if any(check.detail.get('reason') == 'parser_unavailable' for check in result.checks):
        return EXIT_ENV
    return EXIT_CONFIG if result.status == 'error' else result.exit_code()

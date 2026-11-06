"""Pure, bounded checks of authored business contracts and structured plans.

Checks compare declarations only. They neither inspect SQL nor authenticate a
review assertion. All emitted strings are fixed enums or bounded index labels.
"""
import datetime
import re
import unicodedata

from . import naming
from .result import EXIT_CONFIG, Result, StageError

_ID = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_CONFIG_ID = re.compile(r'[a-z_][a-z0-9_]{0,62}\Z')
_MEMBER = re.compile(r'[a-z_][a-z0-9_]{0,62}\.[a-z_][a-z0-9_]{0,62}\Z')
_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}\Z')
_BINDING = ('measure', 'gold', 'cube_member')
_CONTEXT = ('unit', 'grain', 'population', 'aggregation', 'denominator', 'time_basis', 'timezone')
_METRIC = ('id', 'label', 'aliases', 'definition', 'review', 'binding', 'context', 'period_kind')
_SELECTION = ('metric_id', 'term', 'binding', 'context', 'period')
_KINDS = ('range', 'as_of', 'none')


def _invalid():
    raise StageError('invalid semantic preflight document', EXIT_CONFIG) from None


def _object(value, keys, *, partial=False):
    if type(value) is not dict or (set(value) - set(keys)):
        _invalid()
    if not partial and set(value) != set(keys):
        _invalid()


def _text(value, limit):
    if type(value) is not str or not value.strip() or len(value) > limit:
        _invalid()
    # Lone surrogates are not public Unicode text and cannot be safely encoded.
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        _invalid()


def _identifier(value, pattern=_ID):
    if type(value) is not str or pattern.fullmatch(value) is None:
        _invalid()


def _binding(value, *, partial=False):
    _object(value, _BINDING, partial=partial)
    for key, item in value.items():
        _identifier(item, _MEMBER if key == 'cube_member' else _CONFIG_ID)


def _context(value, *, partial=False):
    _object(value, _CONTEXT, partial=partial)
    for item in value.values():
        _identifier(item)


def _document(value, collection):
    _object(value, ('version', collection))
    if type(value['version']) is not int or value['version'] != 1:
        _invalid()
    items = value[collection]
    if type(items) is not list or not 1 <= len(items) <= 64:
        _invalid()
    return items


def _period(value):
    _object(value, ('kind', 'start', 'end', 'date'), partial=True)
    kind = value.get('kind')
    if type(kind) is not str or kind not in _KINDS:
        _invalid()
    keys = {'range': ('kind', 'start', 'end'), 'as_of': ('kind', 'date'), 'none': ('kind',)}[kind]
    _object(value, keys)
    dates = []
    for key in keys[1:]:
        raw = value[key]
        if type(raw) is not str or _DATE.fullmatch(raw) is None:
            _invalid()
        try:
            dates.append(datetime.date.fromisoformat(raw))
        except ValueError:
            _invalid()
    if kind == 'range' and dates[0] >= dates[1]:
        _invalid()


def _validate(contract, plan):
    metrics = _document(contract, 'metrics')
    selections = _document(plan, 'selections')
    seen = set()
    for metric in metrics:
        _object(metric, _METRIC)
        _identifier(metric['id'])
        if metric['id'] in seen:
            _invalid()
        seen.add(metric['id'])
        _text(metric['label'], 160)
        _text(metric['definition'], 2048)
        aliases = metric['aliases']
        if type(aliases) is not list or len(aliases) > 16:
            _invalid()
        for alias in aliases:
            _text(alias, 160)
        if type(metric['review']) is not str or metric['review'] not in ('candidate', 'reviewed'):
            _invalid()
        if type(metric['period_kind']) is not str or metric['period_kind'] not in _KINDS:
            _invalid()
        _binding(metric['binding'])
        _context(metric['context'])
    for selection in selections:
        _object(selection, _SELECTION, partial=True)
        if 'metric_id' in selection:
            _identifier(selection['metric_id'])
        if 'term' in selection:
            _text(selection['term'], 160)
        if 'binding' in selection:
            _binding(selection['binding'], partial=True)
        if 'context' in selection:
            _context(selection['context'], partial=True)
        if 'period' in selection:
            _period(selection['period'])
    return metrics, selections


def _normalize(text):
    return unicodedata.normalize('NFC', text).strip().casefold()


def _contract_reason(cfg, metric):
    binding, context = metric['binding'], metric['context']
    measure = cfg.measures.get(binding['measure'])
    if measure is None:
        return 'measure_missing'
    gold = next((g for g in cfg.gold if g.id == binding['gold']), None)
    if gold is None:
        return 'gold_missing'
    expected = naming.member(naming.cube_measure(measure.name), naming.MEASURE_VALUE)
    if binding['cube_member'] != expected:
        return 'cube_member_mismatch'
    if expected not in gold.cube_query.get('measures', []) or expected not in gold.map.values():
        return 'gold_membership_mismatch'
    if context['aggregation'] != measure.agg:
        return 'aggregation_mismatch'
    if context['grain'] != measure.by:
        return 'grain_mismatch'
    return 'declarations_match'


def _selection_outcome(selection, by_id, by_term):
    term = _normalize(selection['term']) if 'term' in selection else None
    if 'metric_id' in selection:
        metric = by_id.get(selection['metric_id'])
    else:
        matches = by_term.get(term, [])
        metric = matches[0] if len(matches) == 1 else None
    if metric is None or metric['review'] != 'reviewed':
        return 'not_run', 'needs_clarification'
    # Absent information is an incomplete plan, even if another supplied value
    # conflicts. A complete plan is required before a mismatch verdict.
    if any(key not in selection for key in ('binding', 'context', 'period')):
        return 'not_run', 'needs_clarification'
    if set(selection['binding']) != set(_BINDING) or set(selection['context']) != set(_CONTEXT):
        return 'not_run', 'needs_clarification'
    if term is not None and term not in {_normalize(t) for t in [metric['label'], *metric['aliases']]}:
        return 'fail', 'term_mismatch'
    if selection['binding'] != metric['binding']:
        return 'fail', 'binding_mismatch'
    if selection['context'] != metric['context']:
        return 'fail', 'context_mismatch'
    if selection['period']['kind'] != metric['period_kind']:
        return 'fail', 'period_kind_mismatch'
    return 'pass', 'declarations_match'


def evaluate(cfg, contract_document, plan_document) -> Result:
    """Compare decoded documents to existing Config declarations without IO."""
    metrics, selections = _validate(contract_document, plan_document)
    result = Result('semantic_preflight')
    result.not_proven = [
        'Business definitions, population, unit, denominator and time assertions are not independently verified.',
        'Reviewed is an unauthenticated caller assertion.',
        'SQL content, filtering, parameter binding and database results are not verified.',
        'This optional diagnostic is not operational report evidence.',
    ]
    result.next_safe_action = 'Clarify incomplete plans and review conflicting declarations before execution.'
    by_id, by_term = {}, {}
    for index, metric in enumerate(metrics):
        reason = _contract_reason(cfg, metric)
        result.add(f'contract_{index:03d}', 'pass' if reason == 'declarations_match' else 'fail', reason=reason)
        by_id[metric['id']] = metric
        for term in {_normalize(t) for t in [metric['label'], *metric['aliases']]}:
            by_term.setdefault(term, []).append(metric)
    for index, selection in enumerate(selections):
        status, reason = _selection_outcome(selection, by_id, by_term)
        result.add(f'selection_{index:03d}', status, reason=reason)
    return result

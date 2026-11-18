"""A bounded two-table semijoin: every eligible fact contributes at most once."""
from . import ident
from .measure_oracle import NotRun, _Invalid, _keys, evaluate


def validate(spec):
    relation = spec['related_exists']
    _keys(relation, {'entity', 'fact_key', 'foreign_key'})
    for value in relation.values():
        ident.check(value)
    if relation['entity'] == spec['entity'] or 'group_by' in spec or 'time_window' in spec:
        raise ValueError('two distinct entities and a global measure required')


def _key(value, expected_type):
    if value is None or type(value) is expected_type:
        return value
    raise NotRun('unsupported_value')


def source(cfg, spec, conn, read_table):
    """Use the caller's one read-only snapshot for both complete projections."""
    relation = spec['related_exists']
    fact = cfg.entities.get(spec['entity'])
    related = cfg.entities.get(relation['entity'])
    if fact is None or related is None:
        raise _Invalid('entity_missing')
    if fact.table == related.table:
        raise NotRun('distinct_tables_required')
    identity = relation['fact_key']
    foreign = relation['foreign_key']
    columns = {identity, spec['column']} | {predicate['column'] for predicate in spec['where']}
    fact_types, facts = read_table(conn, fact.table, sorted(columns), spec['max_rows'])
    related_types, related_rows = read_table(conn, related.table, [foreign], spec['max_rows'])
    kind = fact_types[identity]
    if kind not in ('integer', 'text') or related_types[foreign] != kind:
        raise NotRun('unsupported_join_key_type')
    key_type = int if kind == 'integer' else str

    matches = {}
    for row in facts:
        key = _key(row[identity], key_type)
        if key is None or key in matches:
            raise NotRun('invalid_fact_identity')
        matches[key] = 0

    nulls = orphans = 0
    for row in related_rows:
        key = _key(row[foreign], key_type)
        if key is None:
            nulls += 1
        elif key not in matches:
            orphans += 1
        else:
            matches[key] += 1
    eligible = [row for row in facts if matches[row[identity]]]
    qualifying_multi = 0

    def count_multi_match(row):
        nonlocal qualifying_multi
        if matches[row[identity]] > 1:
            qualifying_multi += 1

    answer = evaluate(spec, fact_types, eligible, on_qualified=count_multi_match)
    answer.update(source_rows=len(facts), related_rows=len(related_rows), matched_facts=len(eligible),
                  multi_match_facts=sum(n > 1 for n in matches.values()),
                  qualifying_multi_match_facts=qualifying_multi,
                  null_foreign_keys=nulls, orphan_related_rows=orphans)
    return answer

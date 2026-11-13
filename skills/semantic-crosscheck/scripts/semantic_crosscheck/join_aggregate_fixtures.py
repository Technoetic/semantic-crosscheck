"""Bounded, independently authored arithmetic for synthetic join/aggregate cases.

This is not an SQL interpreter or an operational verification oracle. Only fixed
two-column inputs, an inner equijoin, and three integer aggregates are supported.
The companion tests characterize the bridge to PostgreSQL 17.
"""


def _rows(rows, limit):
    """Validate finite two-column tables with a homogeneous type per column."""
    if not isinstance(rows, (list, tuple)) or len(rows) > limit:
        raise ValueError('expected a bounded list or tuple of two-column rows')
    result = []
    kinds = [set(), set()]
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ValueError('expected two-column rows')
        for column, value in enumerate(row):
            if value is None:
                continue
            if type(value) is int:
                if not -1000000 <= value <= 1000000:
                    raise ValueError('integer input exceeds the synthetic bound')
            elif type(value) is str:
                if len(value) > 256:
                    raise ValueError('text input exceeds the synthetic bound')
            else:
                raise ValueError('expected integer, text or NULL cells')
            kinds[column].add(type(value))
            if len(kinds[column]) > 1:
                raise ValueError('each input column must have a homogeneous non-NULL type')
        result.append(tuple(row))
    return result


def inner_join(left, right) -> list[tuple]:
    """Join (key,payload) tables and emit (key,left_payload,right_payload).

    Each input has at most 64 rows; keys must have a compatible integer or text
    type. NULL keys never match, including another NULL. Every matching input
    row pair produces one output row, retaining duplicates from both sides.
    """
    lhs, rhs = _rows(left, 64), _rows(right, 64)
    key_types = {type(key) for key, _ in lhs + rhs if key is not None}
    if len(key_types) > 1:
        raise ValueError('join keys must have a compatible non-NULL type')
    return [(key, value, other_value)
            for key, value in lhs if key is not None
            for other_key, other_value in rhs
            if other_key is not None and key == other_key]


def aggregate(rows, *, grouped: bool = True) -> list[tuple]:
    """Aggregate at most 4096 (group,integer-or-NULL) rows.

    Grouped outputs are (group,COUNT(*),COUNT(value),SUM(value)); global outputs
    omit the group column. NULL groups coalesce. COUNT(value) excludes NULL and
    SUM ignores NULL, returning NULL when no non-NULL value exists. Empty grouped
    input has no rows; empty global input has one (0,0,NULL) row. Output ordering
    is incidental, and all sums are exact Python integer arithmetic.
    """
    if type(grouped) is not bool:
        raise ValueError('grouped must be a boolean')
    source = _rows(rows, 4096)
    if any(value is not None and type(value) is not int for _, value in source):
        raise ValueError('aggregate values must be integers or NULL')
    groups = {} if grouped else {None: [0, 0, None]}
    for group, value in source:
        state = groups.setdefault(group if grouped else None, [0, 0, None])
        state[0] += 1
        if value is not None:
            state[1] += 1
            state[2] = value if state[2] is None else state[2] + value
    return [(group, *state) if grouped else tuple(state) for group, state in groups.items()]

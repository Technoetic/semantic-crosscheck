"""Small offline reference operations for the authored provenance fixture corpus.

This is independently written executable arithmetic, not an extraction of the
Lean development or an SQL evaluator. Rows have exactly one text-or-NULL column.
The PostgreSQL bridge is characterized only by the companion synthetic tests.
Nothing here participates in normal verification or establishes query equivalence.
"""

from collections import Counter


def bag(rows) -> Counter:
    """Count one-column text-or-NULL rows, without conflating duplicates or NULL."""
    counts = Counter()
    for row in rows:
        if (not isinstance(row, (list, tuple)) or len(row) != 1
                or (row[0] is not None and not isinstance(row[0], str))):
            raise ValueError('expected one-column text-or-NULL rows')
        counts[tuple(row)] += 1
    return counts


def distinct(rows) -> Counter:
    """One copy of every input row; NULLs form one duplicate group here."""
    return Counter({row: 1 for row in bag(rows)})


def except_bag(left, right, *, all_rows: bool = False) -> Counter:
    """EXCEPT uses presence; EXCEPT ALL subtracts counts, saturating at zero."""
    lhs, rhs = bag(left), bag(right)
    if all_rows:
        return lhs - rhs
    return Counter({row: 1 for row in lhs if row not in rhs})


def select_equal(rows, literal) -> Counter:
    """A text SQL equality filter keeps TRUE only; equality with NULL is UNKNOWN."""
    if literal is not None and not isinstance(literal, str):
        raise ValueError('expected a text-or-NULL literal')
    return Counter({row: count for row, count in bag(rows).items()
                    if literal is not None and row[0] is not None and row[0] == literal})


def select_null(rows) -> Counter:
    """IS NULL keeps NULL rows, including their original multiplicities."""
    return Counter({row: count for row, count in bag(rows).items() if row[0] is None})


def dedup_diff_annotations(left, right) -> dict:
    """Nat annotations for only `(Dedup R1) Diff R2`, with base annotations 1.

    Each left dedup slot retains the sum of its base annotations, minus the
    matching right annotations using truncated subtraction. Values are annotation
    weights, NOT SQL output-row counts; zero-weight slots remain explicit. NULL,
    arbitrary annotations, nested expressions and general provenance are excluded.
    """
    lhs, rhs = bag(left), bag(right)
    if (None,) in lhs or (None,) in rhs:
        raise ValueError('the annotation example requires non-null text rows')
    return {row: max(count - rhs[row], 0) for row, count in lhs.items()}

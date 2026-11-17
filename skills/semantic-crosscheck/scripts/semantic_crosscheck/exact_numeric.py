"""Bounded finite Decimal arithmetic that never uses the ambient Decimal context."""

from collections.abc import Iterable
from decimal import (Context, Decimal, DecimalException, Inexact, InvalidOperation, Overflow,
                     ROUND_DOWN, Rounded, Underflow)

DEFAULT_MAX_DIGITS = 4096
DEFAULT_MAX_TERMS = 100000


class NumericLimit(ValueError):
    """Exact arithmetic cannot fit the caller's declared resource bounds."""


def _coefficient(digits: tuple[int, ...]) -> int:
    # Small chunks avoid Python's process-wide integer/string conversion limit.
    value = 0
    for start in range(0, len(digits), 18):
        part = digits[start:start + 18]
        chunk = 0
        for digit in part:
            chunk = chunk * 10 + digit
        value = value * 10 ** len(part) + chunk
    return value


def _digits(value: int) -> tuple[int, ...]:
    parts = []
    while value:
        value, chunk = divmod(value, 10 ** 18)
        parts.append(f"{chunk:018d}" if value else str(chunk))
    return tuple(int(digit) for part in reversed(parts) for digit in part)


def exact_sum(values: Iterable[Decimal | None], *, max_digits: int = DEFAULT_MAX_DIGITS,
              max_terms: int = DEFAULT_MAX_TERMS) -> Decimal | None:
    """Sum Decimal values exactly, ignoring NULLs; empty/all-NULL returns None.

    Every entry, including NULL/zero, counts toward max_terms. max_digits bounds
    each input coefficient, the absolute exponent after trimming trailing zeros,
    integer digits, the span needed to align all nonzero inputs, and the final
    normalized coefficient/result. Zero has no exponent cost and returns +0.
    Transient carry digits may exceed the result limit before cancellation.
    Bounds are checked before powers/alignments; even cancelling inputs must fit
    their shared span. Invalid types/nonfinite values raise ValueError and an
    exceeded bound raises NumericLimit. Neither error includes an input value.
    """
    if any(type(limit) is not int or limit < 1 for limit in (max_digits, max_terms)):
        raise ValueError("numeric limits must be positive integers")

    # Validate before as_tuple allocates one Python entry per input digit. This
    # private context only rejects oversized representations; its result is not
    # used in the arithmetic and all accumulation below uses integers.
    guard = Context(prec=max_digits, rounding=ROUND_DOWN, Emin=-max_digits, Emax=max_digits - 1,
                    clamp=0, flags=[], traps=[Inexact, InvalidOperation, Overflow, Rounded, Underflow])
    total, scale, low, high = 0, None, None, None
    present = False
    for index, value in enumerate(values):
        if index >= max_terms:
            raise NumericLimit("numeric entry limit exceeded")
        if value is None:
            continue
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError("exact arithmetic requires finite Decimal values")
        present = True
        if value.is_zero():
            continue
        try:
            guard.create_decimal(value)
        except DecimalException as error:
            raise NumericLimit("numeric coefficient or exponent limit exceeded") from error
        sign, digits, exponent = value.as_tuple()
        if len(digits) > max_digits:
            raise NumericLimit("numeric coefficient limit exceeded")
        end = len(digits)
        while digits[end - 1] == 0:
            end -= 1
            exponent += 1
        digits = digits[:end]
        top = exponent + end - 1
        if abs(exponent) > max_digits or top >= max_digits:
            raise NumericLimit("numeric exponent limit exceeded")
        low = exponent if low is None else min(low, exponent)
        high = top if high is None else max(high, top)
        if high - low + 1 > max_digits:
            raise NumericLimit("numeric alignment limit exceeded")

        coefficient = _coefficient(digits)
        if sign:
            coefficient = -coefficient
        if scale is None:
            total, scale = coefficient, exponent
        elif exponent < scale:
            total = total * 10 ** (scale - exponent) + coefficient
            scale = exponent
        else:
            total += coefficient * 10 ** (exponent - scale)

    if not present:
        return None
    if not total:
        return Decimal(0)
    while total % 10 == 0:
        total //= 10
        scale += 1
    magnitude = abs(total)
    if magnitude >= 10 ** max_digits:
        raise NumericLimit("numeric result limit exceeded")
    digits = _digits(magnitude)
    if abs(scale) > max_digits or len(digits) + max(scale, 0) > max_digits:
        raise NumericLimit("numeric result limit exceeded")
    return Decimal((int(total < 0), digits, scale))

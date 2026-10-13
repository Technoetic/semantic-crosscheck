"""Strict value canonicalisation for comparisons.

Only the representation changes; values must then be identical:
  - decimals become trimmed strings ('2.500000' -> '2.5', '-0.0' -> '0'), no numeric tolerance
  - booleans stay booleans ('true'/'false' strings from APIs become booleans)
  - dates and timestamps become ISO strings (dates without time zone shifts)
  - other text is compared exactly (no trimming, no case folding); NULL differs from ''
  - numeric-looking text with a leading zero ('01') stays text
"""

import datetime as _dt
from decimal import Decimal, InvalidOperation
import re

_NUM = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?$")


def _dec(text: str) -> str:
    try:
        d = Decimal(text)
    except InvalidOperation:
        return text
    if d == 0:
        return "0"
    s = format(d.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def canon(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, Decimal)):
        return _dec(str(v))
    if isinstance(v, float):
        return _dec(repr(v))
    if isinstance(v, _dt.datetime):
        return v.isoformat()
    if isinstance(v, _dt.date):
        return v.isoformat()
    s = str(v)
    if s == "true":
        return True
    if s == "false":
        return False
    if _NUM.match(s):
        return _dec(s)
    # Cube returns dates of type 'time' as 'YYYY-MM-DDT00:00:00.000'; compare date part only when the time is zero
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T00:00:00(\.0+)?Z?$", s)
    if m:
        return m.group(1)
    return s


def same(a, b) -> bool:
    return canon(a) == canon(b)

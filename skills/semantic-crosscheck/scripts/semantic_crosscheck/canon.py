"""Strict value canonicalisation for comparisons.

Only the representation changes; values must then be identical: no numeric tolerance, no trimming, no case
folding, and NULL differs from ''. Which representation is right depends on the type of the compared column, and
only the Cube model knows that type, so there are two modes.

canon(v, kind), where kind is the Cube member type of the column (KINDS):
  string   text exactly as written: '1.10' != '1.1', '1e3' != '1000', 'true' stays text. A value that is not text
           (a gold column of another SQL type) takes its untyped form.
  number   exact decimals as trimmed strings: '2.500000' -> '2.5', '-0.0' -> '0', '1e3' -> '1000'
  time     one fixed form for dates and timestamps, 'YYYY-MM-DDTHH:MM:SS.ffffff' in UTC: a date is its midnight, an
           aware timestamp is converted to UTC, and ISO text such as Cube's '2026-01-05T10:30:00.000' is parsed (a
           naive text or timestamp is taken as UTC, which is how Cube renders the generated time members)
  boolean  booleans; the texts 'true'/'false' and 't'/'f' become booleans
  A value the kind cannot read (text that is not a number under number, ...) keeps its untyped form, so it differs
  from every value the kind did read.

canon(v) without a kind keeps the rules for callers that know no type: decimals become trimmed strings, booleans stay
booleans ('true'/'false' text included), dates and timestamps become ISO text and a Cube timestamp at exactly midnight
its date, numeric-looking text is folded like a decimal ('1.10' == '1.1') unless it has a leading zero ('01'), and
other text is exact.

key(v) is the form of key values in RDF IRIs and key sets: text exactly, any other value in its untyped form, so two
different text keys never share a form.
"""

import datetime as _dt
from decimal import Decimal, InvalidOperation
import re

KINDS = ("string", "number", "time", "boolean")
_NUM = re.compile(r"^-?(0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?$")
_NUMBER = re.compile(r"^[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?$|^(NaN|[-+]?Infinity|[-+]?inf)$", re.IGNORECASE)
_TIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})"
                   r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?(Z|[+-]\d{2}(?::?\d{2})?)?)?$")
_BOOLEAN = {"true": True, "t": True, "false": False, "f": False}


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


def _untyped(v):
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


def _string(v):
    return v if isinstance(v, str) else _untyped(v)


def _number(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, Decimal)):
        return _dec(str(v))
    if isinstance(v, float):
        return _dec(repr(v))
    if isinstance(v, str) and _NUMBER.match(v):
        return _dec(v)
    return _untyped(v)


def _offset(text: str) -> _dt.timezone:
    if text == "Z":
        return _dt.timezone.utc
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    return _dt.timezone(sign * _dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:] or 0)))


def _parse_time(text: str) -> _dt.datetime | None:
    m = _TIME.match(text)
    if not m:
        return None
    year, month, day, hour, minute, second, fraction, zone = m.groups()
    try:
        t = _dt.datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0),
                         int((fraction or "0").ljust(6, "0")))
        return t.replace(tzinfo=_offset(zone)) if zone else t
    except ValueError:
        return None


def _time(v):
    if isinstance(v, _dt.datetime):
        t = v
    elif isinstance(v, _dt.date):
        t = _dt.datetime(v.year, v.month, v.day)
    elif isinstance(v, str):
        t = _parse_time(v)
    else:
        t = None
    if t is None:
        return _untyped(v)
    try:
        if t.utcoffset() is not None:
            t = t.astimezone(_dt.timezone.utc)
    except (OverflowError, ValueError):
        return _untyped(v)
    # Cube reads timestamps with millisecond resolution (it truncates the microseconds), so both sides are
    # compared at milliseconds; anything finer cannot be checked through Cube.
    t = t.replace(tzinfo=None, microsecond=t.microsecond // 1000 * 1000)
    return t.isoformat(timespec="milliseconds")


def _boolean(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in _BOOLEAN:
        return _BOOLEAN[v.lower()]
    return _untyped(v)


_TYPED = {"string": _string, "number": _number, "time": _time, "boolean": _boolean}


def canon(v, kind: str | None = None):
    """Canonical form of v. kind is the Cube member type of its column (one of KINDS); None compares untyped."""
    if v is None:
        return None
    if kind is None:
        return _untyped(v)
    typed = _TYPED.get(kind)
    if typed is None:
        raise ValueError(f"unknown comparison kind {kind!r} (one of {', '.join(KINDS)})")
    return typed(v)


def same(a, b, kind: str | None = None) -> bool:
    return canon(a, kind) == canon(b, kind)


def key(v):
    """A key value as RDF IRIs and key sets hold it: text exactly, any other value in its untyped form."""
    return _string(v)

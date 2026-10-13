"""SQL identifier safety. Every identifier that reaches SQL text goes through these helpers."""

import re

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class IdentError(ValueError):
    pass


def check(name: str) -> str:
    """Return the name if it is a plain lower-case identifier, else raise."""
    if not isinstance(name, str) or not _IDENT.match(name):
        raise IdentError(f"invalid identifier: {name!r} (use ^[a-z_][a-z0-9_]*$)")
    return name


def quote(name: str) -> str:
    """Quoted identifier for SQL text (after validation)."""
    return '"' + check(name) + '"'


def qualified(table: str) -> str:
    """'schema.table' -> '"schema"."table"' (both parts validated)."""
    parts = table.split(".")
    if len(parts) != 2:
        raise IdentError(f"table must be schema-qualified: {table!r}")
    return ".".join(quote(p) for p in parts)


def split(table: str) -> tuple[str, str]:
    schema, name = table.split(".")
    return check(schema), check(name)

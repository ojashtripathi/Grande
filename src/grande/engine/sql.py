"""Safe SQL construction.

The original Grande interpolated column names into SQL as ``f'"{col}"'`` with no
escaping of embedded double quotes, so a CSV whose header row contained a ``"``
could rewrite the query. Everything here goes through :func:`ident` or a bound
parameter instead; nothing user-supplied is ever pasted in raw.
"""

from __future__ import annotations

from typing import Iterable

#: Aggregators the UI may ask for, mapped to a SQL template.
#: ``{c}`` is substituted with an already-quoted identifier. This is an allowlist:
#: an aggregator name that is not a key here is rejected, never interpolated.
AGGREGATORS: dict[str, str] = {
    "sum": "SUM({c})",
    "count": "COUNT({c})",
    "count_all": "COUNT(*)",
    "count_distinct": "COUNT(DISTINCT {c})",
    "avg": "AVG({c})",
    "min": "MIN({c})",
    "max": "MAX({c})",
    "median": "MEDIAN({c})",
    "stddev": "STDDEV_SAMP({c})",
    "first": "FIRST({c})",
}

#: Aggregators that are *decomposable*: a total over sub-totals equals the total
#: over the raw rows. Grand totals for anything else must be recomputed from the
#: base table, never summed across cells. The original summed every aggregator's
#: cells, which silently produced wrong totals for AVG/MIN/MAX/COUNT DISTINCT.
DECOMPOSABLE = {"sum", "count", "count_all"}


class SqlError(ValueError):
    """Raised when a caller supplies something that cannot be made safe."""


def ident(name: str) -> str:
    """Quote an identifier for DuckDB, doubling embedded quotes.

    >>> ident('total "net" amount')
    '"total ""net"" amount"'
    """
    if name is None:
        raise SqlError("Column name is required.")
    text = str(name)
    if "\x00" in text:
        raise SqlError("Column name contains a null byte.")
    if not text:
        raise SqlError("Column name is empty.")
    return '"' + text.replace('"', '""') + '"'


def idents(names: Iterable[str]) -> str:
    """Quote and comma-join a list of identifiers."""
    joined = ", ".join(ident(n) for n in names)
    if not joined:
        raise SqlError("At least one column is required.")
    return joined


def literal(value: object) -> str:
    """Quote a value as a SQL literal.

    Prefer bound parameters. This exists only for the handful of places where a
    value must appear inside a generated column alias or a FILTER clause that is
    built once per pivot column.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if "\x00" in text:
        raise SqlError("Value contains a null byte.")
    return "'" + text.replace("'", "''") + "'"


def alias(name: str) -> str:
    """Quote a generated column alias (same rules as an identifier)."""
    text = "" if name is None else str(name)
    # An empty alias is legal in our output (it means "the blank category"),
    # so unlike ident() we substitute a placeholder rather than raising.
    return '"' + (text or "(blank)").replace('"', '""') + '"'


def agg_expr(aggregator: str, column: str | None) -> str:
    """Build an aggregate expression from the allowlist.

    ``column`` is ignored for ``count_all``; every other aggregator requires one.
    """
    key = (aggregator or "").strip().lower()
    template = AGGREGATORS.get(key)
    if template is None:
        raise SqlError(f"Unsupported aggregation: {aggregator!r}")
    if key == "count_all":
        return template
    if not column:
        raise SqlError(f"{key} needs a column to aggregate.")
    return template.format(c=ident(column))


def numeric_cast(column: str) -> str:
    """Cast a column to DOUBLE for aggregation, yielding NULL rather than failing.

    TRY_CAST keeps one unparseable row from aborting a whole pivot, which matters
    on the messy exports this tool exists to open.
    """
    return f"TRY_CAST({ident(column)} AS DOUBLE)"


def sort_direction(direction: str | None) -> str:
    """Normalise a sort direction to ASC/DESC (never interpolate the raw string)."""
    return "DESC" if str(direction or "").strip().lower() in {"desc", "descending", "-1"} else "ASC"

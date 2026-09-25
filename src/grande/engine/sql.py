"""Safe SQL construction.

The original Grande interpolated column names into SQL as ``f'"{col}"'`` with no
escaping of embedded double quotes, so a CSV whose header row contained a ``"``
could rewrite the query. Everything here goes through :func:`ident` or a bound
parameter instead; nothing user-supplied is ever pasted in raw.
"""

from __future__ import annotations

import re
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


def glob_escape(path: object) -> str:
    """A filesystem path as DuckDB's file readers will take it *literally*.

    ``read_csv``, ``read_parquet``, ``read_json_auto`` and ``sniff_csv`` treat
    their path as a glob pattern, so a real file named ``report[2024].csv``
    loaded ``report2.csv`` instead — or several such files, concatenated —
    whenever one sat in the same folder. Wrapping each metacharacter in a
    one-character class makes it match only itself.

    >>> glob_escape('C:/data/report[2024].csv')
    'C:/data/report[[]2024[]].csv'
    """
    return re.sub(r"([\[\]*?])", r"[\1]", str(path))


def numeric_cast(column: str) -> str:
    """Cast a column to DOUBLE for aggregation, yielding NULL rather than failing.

    TRY_CAST keeps one unparseable row from aborting a whole pivot, which matters
    on the messy exports this tool exists to open.
    """
    return f"TRY_CAST({ident(column)} AS DOUBLE)"


#: Statements the SQL console may begin with.
_READ_ONLY_STARTS = {
    "select", "with", "describe", "desc", "show", "explain", "summarize",
    "table", "values", "from", "pivot", "unpivot",
}

#: Words that make a statement do more than read. ``COPY`` writes files,
#: ``ATTACH``/``INSTALL``/``LOAD`` reach outside the workspace, and the DDL/DML
#: verbs would edit the user's loaded data behind the app's back.
_FORBIDDEN = {
    "copy", "attach", "detach", "install", "load", "export", "import",
    "insert", "update", "delete", "drop", "alter", "create", "replace",
    "call", "set", "reset", "pragma", "vacuum", "checkpoint", "truncate",
    "grant", "revoke", "begin", "commit", "rollback",
}

_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_STRING = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", re.DOTALL)
_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def check_read_only(statement: str) -> str:
    """Return a single read-only statement, or explain why it is refused.

    The console used to be sandboxed with ``SET disabled_filesystems``, which is
    a *global* DuckDB setting: running one query disabled file access for the
    whole process, so no file could be opened afterwards until Grande was
    restarted. Reading the statement is both correct and harmless.
    """
    text = (statement or "").strip()
    if not text:
        raise SqlError("Write a query first.")

    # Look at the statement with comments and string literals removed, so a
    # value like 'please delete this' cannot trip the checks below.
    bare = _STRING.sub("''", _COMMENT.sub(" ", text))
    trimmed = bare.strip().rstrip(";")
    if ";" in trimmed:
        raise SqlError("Run one statement at a time.")

    words = [w.lower() for w in _WORD.findall(trimmed)]
    if not words:
        raise SqlError("Write a query first.")
    if words[0] not in _READ_ONLY_STARTS:
        raise SqlError(
            f"Only queries that read data are allowed here, so a statement "
            f"cannot start with “{words[0].upper()}”. "
            "Use the Clean tab to change your data."
        )
    for word in words:
        if word in _FORBIDDEN:
            raise SqlError(
                f"“{word.upper()}” is not allowed in the SQL box — it would "
                "change something rather than read it. Use the Clean and Export "
                "tabs for that."
            )
    return text.rstrip().rstrip(";")


def sort_direction(direction: str | None) -> str:
    """Normalise a sort direction to ASC/DESC (never interpolate the raw string)."""
    return "DESC" if str(direction or "").strip().lower() in {"desc", "descending", "-1"} else "ASC"

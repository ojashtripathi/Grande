"""Excel-style AutoFilter, compiled to parameterised SQL.

Every filter the UI can build becomes a ``WHERE`` fragment plus a list of bound
parameters. Values are never interpolated into the SQL text, so a search box is
not a SQL console.

A filter is a dict::

    {"column": "region", "op": "in", "values": ["North", "South"]}
    {"column": "revenue", "op": "between", "value": 10, "value2": 100}
    {"column": "notes", "op": "contains", "value": "urgent"}
    {"column": "ship_date", "op": "is_null"}
"""

from __future__ import annotations

from typing import Any, Sequence

from .sql import SqlError, ident

#: Operators that take no value at all.
NULLARY = {"is_null", "is_not_null", "is_blank", "is_not_blank"}
#: Operators that take a list of values.
LIST_OPS = {"in", "not_in"}
#: Operators that take two values.
RANGE_OPS = {"between", "not_between"}

#: Text operators are matched case-insensitively by default, because that is what
#: someone typing into a filter box expects. Excel's own filters behave this way.
TEXT_OPS = {
    "contains": "{c} ILIKE '%' || ? || '%'",
    "not_contains": "({c} IS NULL OR {c} NOT ILIKE '%' || ? || '%')",
    "starts_with": "{c} ILIKE ? || '%'",
    "ends_with": "{c} ILIKE '%' || ?",
    "equals_text": "{c} = ?",
    "not_equals_text": "({c} IS NULL OR {c} <> ?)",
    "regex": "regexp_matches(CAST({c} AS VARCHAR), ?)",
}

COMPARE_OPS = {
    "eq": "{c} = ?",
    "ne": "({c} IS NULL OR {c} <> ?)",
    "gt": "{c} > ?",
    "gte": "{c} >= ?",
    "lt": "{c} < ?",
    "lte": "{c} <= ?",
}

MAX_IN_VALUES = 10_000


def _escape_like(value: str) -> str:
    """Neutralise LIKE wildcards so a literal % or _ in a search box matches itself."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def compile_filter(spec: dict[str, Any]) -> tuple[str, list[Any]]:
    """Compile one filter spec into ``(sql_fragment, params)``."""
    if not isinstance(spec, dict):
        raise SqlError("Each filter must be an object.")

    column = spec.get("column")
    op = str(spec.get("op") or "").strip().lower()

    if op == "any_contains":
        # The toolbar search box: match the text in any of the named columns.
        # Expanded here so the OR stays inside one parenthesised group and
        # cannot loosen the other, AND-combined filters.
        needle = str(spec.get("value") or "")
        columns = spec.get("columns") or []
        if not needle:
            return "TRUE", []
        if not columns:
            return "FALSE", []
        parts = [
            f"CAST({ident(name)} AS VARCHAR) ILIKE '%' || ? || '%' ESCAPE '\\'"
            for name in columns
        ]
        return "(" + " OR ".join(parts) + ")", [_escape_like(needle)] * len(parts)

    col = ident(column)

    if op in NULLARY:
        if op == "is_null":
            return f"{col} IS NULL", []
        if op == "is_not_null":
            return f"{col} IS NOT NULL", []
        if op == "is_blank":
            return f"({col} IS NULL OR CAST({col} AS VARCHAR) = '')", []
        return f"({col} IS NOT NULL AND CAST({col} AS VARCHAR) <> '')", []

    if op in LIST_OPS:
        values: Sequence[Any] = spec.get("values") or []
        if not isinstance(values, (list, tuple)):
            raise SqlError("'values' must be a list.")
        if not values:
            # An empty "in" selects nothing; an empty "not in" excludes nothing.
            return ("FALSE", []) if op == "in" else ("TRUE", [])
        if len(values) > MAX_IN_VALUES:
            raise SqlError(f"Too many selected values (limit {MAX_IN_VALUES:,}).")

        # NULL never satisfies IN/NOT IN in SQL, so handle it as an explicit branch.
        concrete = [v for v in values if v is not None]
        has_null = len(concrete) != len(values)
        placeholders = ", ".join("?" for _ in concrete)

        if op == "in":
            parts = []
            if concrete:
                parts.append(f"CAST({col} AS VARCHAR) IN ({placeholders})")
            if has_null:
                parts.append(f"{col} IS NULL")
            return "(" + " OR ".join(parts) + ")", [str(v) for v in concrete]

        parts = []
        if concrete:
            parts.append(f"CAST({col} AS VARCHAR) NOT IN ({placeholders})")
        if not has_null:
            # "not in [a, b]" should still keep NULL rows, matching Excel.
            parts.append(f"{col} IS NULL")
            return "(" + " OR ".join(parts) + ")", [str(v) for v in concrete]
        parts.append(f"{col} IS NOT NULL")
        return "(" + " AND ".join(parts) + ")", [str(v) for v in concrete]

    if op in RANGE_OPS:
        lo, hi = spec.get("value"), spec.get("value2")
        if lo is None or hi is None:
            raise SqlError("A range filter needs both a start and an end value.")
        negate = "NOT " if op == "not_between" else ""
        return f"{col} {negate}BETWEEN ? AND ?", [lo, hi]

    if op in TEXT_OPS:
        value = spec.get("value")
        if value is None:
            raise SqlError(f"'{op}' needs a value.")
        text = str(value)
        template = TEXT_OPS[op]
        if op in {"contains", "not_contains", "starts_with", "ends_with"}:
            # Compare as text so the filter works on numeric and date columns too.
            fragment = template.format(c=f"CAST({col} AS VARCHAR)") + " ESCAPE '\\'"
            return fragment, [_escape_like(text)]
        if op in {"equals_text", "not_equals_text"}:
            return template.format(c=f"CAST({col} AS VARCHAR)"), [text]
        return template.format(c=col), [text]

    if op in COMPARE_OPS:
        if "value" not in spec:
            raise SqlError(f"'{op}' needs a value.")
        return COMPARE_OPS[op].format(c=col), [spec.get("value")]

    raise SqlError(f"Unsupported filter operation: {op!r}")


def compile_filters(specs: Sequence[dict[str, Any]] | None) -> tuple[str, list[Any]]:
    """Compile a list of filters into a single ``WHERE`` clause (AND-combined).

    Returns ``("", [])`` when there is nothing to filter, so callers can append
    the result unconditionally.
    """
    if not specs:
        return "", []
    fragments: list[str] = []
    params: list[Any] = []
    for spec in specs:
        fragment, values = compile_filter(spec)
        fragments.append(fragment)
        params.extend(values)
    if not fragments:
        return "", []
    return " WHERE " + " AND ".join(fragments), params


def describe_filter(spec: dict[str, Any]) -> str:
    """Render a filter as a short human phrase for the UI's filter chips."""
    column = spec.get("column", "?")
    op = str(spec.get("op") or "").lower()
    value = spec.get("value")
    words = {
        "is_null": "is empty",
        "is_not_null": "is not empty",
        "is_blank": "is blank",
        "is_not_blank": "is not blank",
        "contains": f"contains “{value}”",
        "not_contains": f"does not contain “{value}”",
        "starts_with": f"starts with “{value}”",
        "ends_with": f"ends with “{value}”",
        "equals_text": f"is “{value}”",
        "not_equals_text": f"is not “{value}”",
        "regex": f"matches /{value}/",
        "eq": f"= {value}",
        "ne": f"≠ {value}",
        "gt": f"> {value}",
        "gte": f"≥ {value}",
        "lt": f"< {value}",
        "lte": f"≤ {value}",
        "between": f"between {value} and {spec.get('value2')}",
        "not_between": f"outside {value}–{spec.get('value2')}",
    }
    if op == "any_contains":
        return f"any column contains “{value}”"
    if op in LIST_OPS:
        values = spec.get("values") or []
        shown = ", ".join(str(v) for v in values[:3])
        more = f" +{len(values) - 3}" if len(values) > 3 else ""
        verb = "is any of" if op == "in" else "is none of"
        return f"{column} {verb} {shown}{more}"
    return f"{column} {words.get(op, op)}"

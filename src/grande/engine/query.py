"""Reading rows out of a dataset: paging, sorting, filtering, stats, profiles.

Everything here operates on the **whole** dataset, not on a preview slice. In the
original, sorting and filtering were JavaScript array operations over at most
1,000 loaded rows, while the UI badge advertised ">10M+ rows" — so "sort by
revenue" showed the largest of the first thousand rows, not of the file. Here a
sort is an ``ORDER BY`` over every row and paging is ``LIMIT/OFFSET``.
"""

from __future__ import annotations

import math
from typing import Any, Sequence

from .filters import compile_filters
from .session import Dataset, Workspace
from .sql import SqlError, ident, sort_direction

#: Never hand the browser more than this in one page; the grid is virtualised and
#: asks for what it can show plus a margin.
MAX_PAGE = 5_000
DEFAULT_PAGE = 200

#: Distinct-value lists for a filter checklist are capped; above this we tell the
#: user to search instead of silently truncating the list.
DISTINCT_CAP = 1_000


def _order_clause(dataset: Dataset, sort: Sequence[dict[str, Any]] | None) -> str:
    if not sort:
        return ""
    parts = []
    for rule in sort:
        name = rule.get("column")
        if not name or dataset.column(name) is None:
            raise SqlError(f"Unknown column: {name!r}")
        direction = sort_direction(rule.get("direction"))
        # Empty cells sort last in both directions, which is what a spreadsheet does.
        parts.append(f"{ident(name)} {direction} NULLS LAST")
    return " ORDER BY " + ", ".join(parts)


def _select_list(dataset: Dataset, columns: Sequence[str] | None) -> tuple[str, list[str]]:
    if not columns:
        names = [c.name for c in dataset.columns]
    else:
        names = []
        for name in columns:
            if dataset.column(name) is None:
                raise SqlError(f"Unknown column: {name!r}")
            names.append(name)
    if not names:
        raise SqlError("Select at least one column.")
    return ", ".join(ident(n) for n in names), names


def count_filtered(workspace: Workspace, dataset: Dataset, filters: Sequence[dict] | None) -> int:
    where, params = compile_filters(filters)
    row = workspace.execute_one(f"SELECT count(*) FROM {dataset.relation}{where}", params)
    return int(row[0]) if row else 0


def page(
    workspace: Workspace,
    dataset: Dataset,
    *,
    offset: int = 0,
    limit: int = DEFAULT_PAGE,
    filters: Sequence[dict] | None = None,
    sort: Sequence[dict] | None = None,
    columns: Sequence[str] | None = None,
    with_count: bool = True,
) -> dict[str, Any]:
    """Return one window of rows, with filters and sort applied to the whole table."""
    limit = max(1, min(int(limit), MAX_PAGE))
    offset = max(0, int(offset))

    select_sql, names = _select_list(dataset, columns)
    where, params = compile_filters(filters)
    order = _order_clause(dataset, sort)

    sql = (
        f"SELECT {select_sql} FROM {dataset.relation}{where}{order} "
        f"LIMIT {limit} OFFSET {offset}"
    )
    cur = workspace.cursor()
    try:
        rows = cur.execute(sql, params).fetchall()
    finally:
        cur.close()

    total = count_filtered(workspace, dataset, filters) if with_count else None
    return {
        "columns": names,
        "rows": [[_cell(v) for v in row] for row in rows],
        "offset": offset,
        "limit": limit,
        "returned": len(rows),
        "total_rows": dataset.row_count,
        "filtered_rows": total,
    }


def _cell(value: Any) -> Any:
    """Make a DuckDB value JSON-safe without losing precision.

    Large integers beyond IEEE-754 exact range are sent as strings, because
    JavaScript would silently round them — the very corruption this tool exists
    to prevent.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 9_007_199_254_740_991 else str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        return value
    return str(value)


def distinct_values(
    workspace: Workspace,
    dataset: Dataset,
    column: str,
    *,
    search: str | None = None,
    limit: int = 200,
    filters: Sequence[dict] | None = None,
) -> dict[str, Any]:
    """Values for a filter checklist, most frequent first, with counts."""
    if dataset.column(column) is None:
        raise SqlError(f"Unknown column: {column!r}")
    limit = max(1, min(int(limit), DISTINCT_CAP))

    where, params = compile_filters(filters)
    col = ident(column)
    clauses = [where[len(" WHERE "):]] if where else []
    if search:
        clauses.append(f"CAST({col} AS VARCHAR) ILIKE '%' || ? || '%'")
        params = list(params) + [str(search)]
    where_sql = (" WHERE " + " AND ".join(c for c in clauses if c)) if clauses else ""

    sql = (
        f"SELECT CAST({col} AS VARCHAR) AS value, count(*) AS n "
        f"FROM {dataset.relation}{where_sql} "
        f"GROUP BY 1 ORDER BY n DESC, 1 ASC LIMIT {limit + 1}"
    )
    rows = workspace.execute(sql, params)
    truncated = len(rows) > limit
    rows = rows[:limit]

    total_distinct = workspace.execute_one(
        f"SELECT approx_count_distinct({col}) FROM {dataset.relation}{where_sql}", params
    )
    return {
        "column": column,
        "values": [{"value": r[0], "count": int(r[1])} for r in rows],
        "truncated": truncated,
        "approx_distinct": int(total_distinct[0]) if total_distinct and total_distinct[0] else len(rows),
    }


def summary(
    workspace: Workspace,
    dataset: Dataset,
    column: str,
    *,
    filters: Sequence[dict] | None = None,
) -> dict[str, Any]:
    """The spreadsheet status-bar numbers for one column, over every matching row."""
    meta = dataset.column(column)
    if meta is None:
        raise SqlError(f"Unknown column: {column!r}")
    where, params = compile_filters(filters)
    col = ident(column)

    if meta.kind == "number":
        sql = (
            f"SELECT count(*), count({col}), sum({col}), avg({col}), "
            f"min({col}), max({col}), median({col}) "
            f"FROM {dataset.relation}{where}"
        )
        row = workspace.execute_one(sql, params) or (0, 0, None, None, None, None, None)
        return {
            "column": column,
            "kind": "number",
            "rows": int(row[0] or 0),
            "count": int(row[1] or 0),
            "empty": int((row[0] or 0) - (row[1] or 0)),
            "sum": _num(row[2]),
            "average": _num(row[3]),
            "min": _num(row[4]),
            "max": _num(row[5]),
            "median": _num(row[6]),
        }

    sql = (
        f"SELECT count(*), count({col}), approx_count_distinct({col}), "
        f"min(CAST({col} AS VARCHAR)), max(CAST({col} AS VARCHAR)) "
        f"FROM {dataset.relation}{where}"
    )
    row = workspace.execute_one(sql, params) or (0, 0, 0, None, None)
    return {
        "column": column,
        "kind": meta.kind,
        "rows": int(row[0] or 0),
        "count": int(row[1] or 0),
        "empty": int((row[0] or 0) - (row[1] or 0)),
        "distinct": int(row[2] or 0),
        "min": row[3],
        "max": row[4],
    }


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out


def profile(
    workspace: Workspace,
    dataset: Dataset,
    column: str,
    *,
    bins: int = 24,
    filters: Sequence[dict] | None = None,
) -> dict[str, Any]:
    """A column profile: completeness, cardinality, and a small distribution.

    Drawn as a sparkline in the column header so the shape of a 40M-row column is
    visible without scrolling — something the original had no equivalent of.
    """
    meta = dataset.column(column)
    if meta is None:
        raise SqlError(f"Unknown column: {column!r}")

    base = summary(workspace, dataset, column, filters=filters)
    where, params = compile_filters(filters)
    col = ident(column)
    out: dict[str, Any] = {**base, "type": meta.type, "histogram": [], "top": []}

    if meta.kind == "number" and base.get("count"):
        lo, hi = base.get("min"), base.get("max")
        if lo is not None and hi is not None and hi > lo:
            width = (hi - lo) / bins
            sql = (
                f"SELECT least(CAST(floor(({col} - {lo!r}) / {width!r}) AS INTEGER), {bins - 1}) AS b, "
                f"count(*) FROM {dataset.relation}{where}"
                f"{' AND ' if where else ' WHERE '}{col} IS NOT NULL GROUP BY 1 ORDER BY 1"
            )
            try:
                counts = {int(r[0]): int(r[1]) for r in workspace.execute(sql, params)}
                out["histogram"] = [
                    {"from": lo + i * width, "to": lo + (i + 1) * width, "count": counts.get(i, 0)}
                    for i in range(bins)
                ]
            except Exception:
                out["histogram"] = []
        elif lo is not None:
            out["histogram"] = [{"from": lo, "to": lo, "count": base.get("count", 0)}]
    else:
        top = distinct_values(workspace, dataset, column, limit=12, filters=filters)
        out["top"] = top["values"]

    return out


def sql_preview(
    workspace: Workspace,
    dataset: Dataset,
    *,
    filters: Sequence[dict] | None = None,
    sort: Sequence[dict] | None = None,
    columns: Sequence[str] | None = None,
) -> str:
    """The SQL behind the current view, shown so the user can check our work."""
    select_sql, _ = _select_list(dataset, columns)
    where, params = compile_filters(filters)
    order = _order_clause(dataset, sort)
    text = f"SELECT {select_sql}\nFROM {ident(dataset.display_name)}{where}{order}"
    for value in params:
        text = text.replace("?", repr(value), 1)
    return text

"""Pivot tables, modelled on Excel's.

The shape of the result mirrors what Excel's PivotTable produces, because the
people using this already know that model: several row fields nested in compact
form, several column fields nested as stacked headers, several value fields,
subtotals at every row level, and a Grand Total row and column.

Three things the original got wrong, fixed here by construction:

1. **Totals.** The original summed the visible cells. That is only valid for
   SUM and COUNT; an average of averages, a min of mins over different
   denominators, or a sum of distinct-counts is the wrong number. Here every
   subtotal and grand total comes from ``GROUP BY ROLLUP`` over the base rows,
   so each is the aggregate of the rows it covers — correct for every
   aggregation, including MEDIAN and COUNT DISTINCT.

2. **Dropped categories.** Past a cap the original discarded categories, so row
   totals stopped matching the data. The remainder is collected into an explicit
   "Other" column here, and the user is told.

3. **Aliases as SQL.** Category values were pasted into the query as column
   aliases. Every generated column is aliased ``v0``, ``v1``, … and the human
   labels travel back as data, so no value from the file becomes SQL.

Dates and numbers can be grouped the way Excel groups them — by year, quarter,
month, week or day, or into numeric bands. Without that, dropping an order-date
column into Rows produces one row per distinct day, and "sales by month" — the
most common pivot there is — cannot be built at all.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any, Sequence

from .filters import compile_filters
from .session import Dataset, Workspace
from .sql import AGGREGATORS, SqlError, agg_expr, ident, literal

#: Distinct column-header combinations beyond this become "Other". Wider than
#: this stops being a table anyone can read.
DEFAULT_MAX_COLUMNS = 40
HARD_MAX_COLUMNS = 200
#: Result rows returned in one response.
DEFAULT_ROW_LIMIT = 2_000
HARD_ROW_LIMIT = 50_000

#: Excel's "Show Values As".
SHOW_AS = {
    "value", "percent_of_total", "percent_of_row", "percent_of_column",
    "running_total", "rank",
}

#: Excel's date grouping levels, mapped to a DuckDB truncation.
DATE_GROUPS: dict[str, str] = {
    "year": "year",
    "quarter": "quarter",
    "month": "month",
    "week": "week",
    "day": "day",
    "hour": "hour",
}


class PivotError(ValueError):
    """A pivot request that cannot be satisfied, with a message for a human."""


# --------------------------------------------------------------------- fields


def _normalise_field(spec: Any) -> dict[str, Any]:
    """Accept either a bare column name or ``{column, group, bin}``."""
    if isinstance(spec, str):
        return {"column": spec, "group": None, "bin": None}
    if not isinstance(spec, dict):
        raise PivotError("A field must be a column name or an object.")
    group = spec.get("group") or None
    if group and group not in DATE_GROUPS:
        raise PivotError(f"Unknown date grouping: {group!r}")
    size = spec.get("bin")
    try:
        size = float(size) if size not in (None, "") else None
    except (TypeError, ValueError):
        raise PivotError("Group size must be a number.") from None
    if size is not None and size <= 0:
        raise PivotError("Group size must be greater than zero.")
    return {"column": spec.get("column"), "group": group, "bin": size}


def _field_expr(dataset: Dataset, field: dict[str, Any]) -> str:
    """SQL for one row/column field, applying any grouping."""
    name = field["column"]
    meta = dataset.column(name)
    if meta is None:
        raise PivotError(f"There is no column called “{name}”.")
    col = ident(name)

    if field["group"]:
        if meta.kind != "date":
            raise PivotError(f"“{name}” is not a date, so it cannot be grouped by {field['group']}.")
        return f"date_trunc({literal(DATE_GROUPS[field['group']])}, {col})"

    if field["bin"]:
        if meta.kind != "number":
            raise PivotError(f"“{name}” is not a number, so it cannot be grouped into bands.")
        width = field["bin"]
        return f"floor({col} / {width!r}) * {width!r}"

    return col


def _field_label(field: dict[str, Any]) -> str:
    """What the field is called in the layout pane, Excel-style."""
    name = field["column"]
    if field["group"]:
        return f"{name} ({field['group']})"
    if field["bin"]:
        return f"{name} (bands of {_trim(field['bin'])})"
    return name


def _trim(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _value_specs(values: Sequence[dict] | None) -> list[dict[str, Any]]:
    """Normalise value fields, defaulting to Excel's Count of rows."""
    if not values:
        return [{"column": None, "agg": "count_all", "label": "Count", "show_as": "value"}]
    out: list[dict[str, Any]] = []
    for spec in values:
        agg = str(spec.get("agg") or "sum").lower()
        if agg not in AGGREGATORS:
            raise PivotError(f"Unsupported aggregation: {spec.get('agg')!r}")
        column = spec.get("column")
        if agg != "count_all" and not column:
            raise PivotError("Choose a column to summarise.")
        show_as = str(spec.get("show_as") or "value")
        if show_as not in SHOW_AS:
            show_as = "value"
        out.append({
            "column": column,
            "agg": agg,
            "label": spec.get("label") or default_value_label(agg, column),
            "show_as": show_as,
        })
    return out


def default_value_label(agg: str, column: str | None) -> str:
    """Excel's own naming: "Sum of Revenue", "Count of Orders"."""
    words = {
        "sum": "Sum of {}", "count": "Count of {}", "count_all": "Count",
        "count_distinct": "Distinct Count of {}", "avg": "Average of {}",
        "min": "Min of {}", "max": "Max of {}", "median": "Median of {}",
        "stddev": "StdDev of {}", "first": "First of {}",
    }
    template = words.get(agg, agg + " of {}")
    return template.format(column) if column else template.replace(" of {}", "")


# ------------------------------------------------------------------- compute


def compute(
    workspace: Workspace,
    dataset: Dataset,
    *,
    rows: Sequence[Any] | None = None,
    columns: Sequence[Any] | None = None,
    column: Any = None,                       # accepted for older callers
    values: Sequence[dict] | None = None,
    filters: Sequence[dict] | None = None,
    max_columns: int = DEFAULT_MAX_COLUMNS,
    limit: int = DEFAULT_ROW_LIMIT,
    subtotals: bool = True,
    sort: dict | None = None,
) -> dict[str, Any]:
    """Build a pivot table.

    ``rows`` and ``columns`` are field specs; ``values`` are the measures.
    Subtotals and grand totals come from ROLLUP, so they aggregate the
    underlying rows rather than summing the cells on screen.
    """
    row_fields = [_normalise_field(f) for f in (rows or []) if f]
    col_source = columns if columns is not None else ([column] if column else [])
    col_fields = [_normalise_field(f) for f in (col_source or []) if f]
    specs = _value_specs(values)

    limit = max(1, min(int(limit), HARD_ROW_LIMIT))
    max_columns = max(1, min(int(max_columns), HARD_MAX_COLUMNS))
    where, params = compile_filters(filters)

    # ---- a base projection: grouping expressions computed once ---------------
    # Row and column fields become r0/r1… and c0/c1…, so a column may appear
    # more than once at different granularities (Excel's Years + Months) without
    # colliding, and GROUPING() has a plain column to work with.
    base_parts: list[str] = []
    for index, field in enumerate(row_fields):
        base_parts.append(f"{_field_expr(dataset, field)} AS r{index}")
    for index, field in enumerate(col_fields):
        expr = _field_expr(dataset, field)
        base_parts.append(f"{expr} AS c{index}")
        base_parts.append(f"CAST({expr} AS VARCHAR) AS c{index}_s")

    metric_columns = {s["column"] for s in specs if s["column"]}
    for name in sorted(metric_columns):
        if dataset.column(name) is None:
            raise PivotError(f"There is no column called “{name}”.")
        base_parts.append(ident(name))

    if not base_parts:
        base_parts.append("1 AS _one")

    base_sql = f"SELECT {', '.join(base_parts)} FROM {dataset.relation}{where}"

    # ---- the column headers --------------------------------------------------
    categories: list[tuple[Any, ...]] = []
    category_keys: list[tuple[str, ...]] = []
    has_other = False
    if col_fields:
        categories, category_keys, has_other = _column_categories(
            workspace, base_sql, params, len(col_fields), max_columns
        )

    # ---- the select list -----------------------------------------------------
    select_parts: list[str] = [f"r{i}" for i in range(len(row_fields))]
    if row_fields and subtotals:
        select_parts.append(
            "(" + " + ".join(
                f"CAST(GROUPING(r{i}) AS INTEGER)" for i in range(len(row_fields))
            ) + ") AS _depth"
        )

    out_columns: list[dict[str, Any]] = []
    alias_index = 0

    def add(expr: str, meta: dict[str, Any]) -> None:
        nonlocal alias_index
        name = f"v{alias_index}"
        select_parts.append(f"{expr} AS {name}")
        out_columns.append({"key": name, **meta})
        alias_index += 1

    for spec_index, spec in enumerate(specs):
        measure = agg_expr(spec["agg"], spec["column"])
        for cat_index, keys in enumerate(category_keys):
            predicate = " AND ".join(
                f"c{level}_s IS NOT DISTINCT FROM {literal(key)}"
                if key is not None else f"c{level}_s IS NULL"
                for level, key in enumerate(keys)
            )
            add(f"{measure} FILTER (WHERE {predicate})", {
                "labels": [None if k is None else str(k) for k in categories[cat_index]],
                "value_index": spec_index,
                "value_label": spec["label"],
                "category_index": cat_index,
                "role": "cell",
            })
        if has_other:
            kept = " OR ".join(
                "(" + " AND ".join(
                    f"c{level}_s IS NOT DISTINCT FROM {literal(key)}"
                    if key is not None else f"c{level}_s IS NULL"
                    for level, key in enumerate(keys)
                ) + ")"
                for keys in category_keys
            )
            add(f"{measure} FILTER (WHERE NOT ({kept}))", {
                "labels": ["Other"] + [""] * (len(col_fields) - 1),
                "value_index": spec_index,
                "value_label": spec["label"],
                "category_index": len(category_keys),
                "role": "other",
            })
        # The row total is the aggregate over the group's rows — never the sum
        # of the cells to its left.
        add(measure, {
            "labels": ["Grand Total"] + [""] * max(0, len(col_fields) - 1),
            "value_index": spec_index,
            "value_label": spec["label"],
            "category_index": None,
            "role": "row_total" if col_fields else "value",
        })

    # ---- group, order, run ---------------------------------------------------
    if row_fields:
        keys = ", ".join(f"r{i}" for i in range(len(row_fields)))
        group_sql = f" GROUP BY {'ROLLUP(' + keys + ')' if subtotals else keys}"
    else:
        group_sql = ""

    order_sql = _order_sql(len(row_fields), out_columns, sort, subtotals)
    sql = (
        f"WITH base AS ({base_sql})\n"
        f"SELECT {', '.join(select_parts)} FROM base{group_sql}{order_sql} LIMIT {limit + 1}"
    )

    cur = workspace.cursor()
    try:
        raw = cur.execute(sql, params).fetchall()
    except Exception as exc:
        raise PivotError(_friendly(exc)) from exc
    finally:
        cur.close()

    truncated = len(raw) > limit
    raw = raw[:limit]

    n_keys = len(row_fields)
    depth_offset = 1 if (row_fields and subtotals) else 0

    body: list[dict[str, Any]] = []
    grand: dict[str, Any] | None = None
    for record in raw:
        keys_out = [_scalar(v) for v in record[:n_keys]]
        depth = int(record[n_keys]) if depth_offset else 0
        cells = [_scalar(v) for v in record[n_keys + depth_offset:]]
        entry = {"keys": keys_out, "cells": cells, "depth": depth}
        if n_keys and depth == n_keys:
            grand = entry                      # every dimension rolled up
        else:
            body.append(entry)

    if not row_fields:
        grand = body[0] if body else None
        body = []

    result = {
        "row_fields": [{**f, "label": _field_label(f)} for f in row_fields],
        "column_fields": [{**f, "label": _field_label(f)} for f in col_fields],
        "values": specs,
        "columns": out_columns,
        "categories": [[None if k is None else str(k) for k in c] for c in categories],
        "rows": body,
        "grand_total": grand,
        "row_count": len(body),
        "truncated": truncated,
        "has_other": has_other,
        "subtotals": bool(row_fields) and subtotals,
        "sql": _readable_sql(sql, params),
    }
    _apply_show_as(result)
    return result


def _column_categories(
    workspace: Workspace,
    base_sql: str,
    params: list[Any],
    depth: int,
    max_columns: int,
) -> tuple[list[tuple[Any, ...]], list[tuple[str, ...]], bool]:
    """Distinct combinations of the column fields, most frequent first."""
    raw_cols = ", ".join(f"c{i}" for i in range(depth))
    str_cols = ", ".join(f"c{i}_s" for i in range(depth))
    rows = workspace.execute(
        f"WITH base AS ({base_sql}) "
        f"SELECT {raw_cols}, {str_cols}, count(*) AS n FROM base "
        f"GROUP BY {raw_cols}, {str_cols} ORDER BY n DESC, 1 LIMIT {max_columns + 1}",
        params,
    )
    has_other = len(rows) > max_columns
    rows = rows[:max_columns]

    values = [tuple(r[:depth]) for r in rows]
    keys = [tuple(r[depth:depth * 2]) for r in rows]
    # Read left to right in a sensible order rather than by frequency.
    order = sorted(range(len(values)), key=lambda i: tuple(
        (v is None, str(v)) for v in values[i]
    ))
    return [values[i] for i in order], [keys[i] for i in order], has_other


def _order_sql(
    n_rows: int,
    out_columns: Sequence[dict[str, Any]],
    sort: dict | None,
    subtotals: bool,
) -> str:
    """Order rows, keeping each subtotal directly beneath what it totals."""
    if not n_rows:
        return ""
    default = ", ".join(f"r{i} ASC NULLS LAST" for i in range(n_rows))
    if not sort:
        return f" ORDER BY {default}"

    key = sort.get("key")
    direction = "DESC" if str(sort.get("direction", "desc")).lower().startswith("d") else "ASC"

    if key in {c["key"] for c in out_columns}:
        if subtotals:
            # Sorting by a measure must not scatter subtotals among detail rows.
            depth = " + ".join(f"CAST(GROUPING(r{i}) AS INTEGER)" for i in range(n_rows))
            return f" ORDER BY ({depth}) ASC, {key} {direction} NULLS LAST"
        return f" ORDER BY {key} {direction} NULLS LAST"

    if isinstance(key, int) and 0 <= key < n_rows:
        rest = [f"r{i} ASC NULLS LAST" for i in range(n_rows) if i != key]
        return " ORDER BY " + ", ".join([f"r{key} {direction} NULLS LAST"] + rest)

    return f" ORDER BY {default}"


# --------------------------------------------------------------- show values as


def _apply_show_as(result: dict[str, Any]) -> None:
    """Excel's "Show Values As", with the right denominator for each."""
    specs = result["values"]
    if all(s["show_as"] == "value" for s in specs):
        return

    columns = result["columns"]
    grand = result.get("grand_total")
    rows = result["rows"]

    by_value: dict[int, list[int]] = {}
    totals_index: dict[int, int] = {}
    for position, meta in enumerate(columns):
        by_value.setdefault(meta["value_index"], []).append(position)
        if meta["role"] in {"row_total", "value"}:
            totals_index[meta["value_index"]] = position

    for spec_index, spec in enumerate(specs):
        mode = spec["show_as"]
        if mode == "value":
            continue
        positions = by_value.get(spec_index, [])
        total_pos = totals_index.get(spec_index)

        if mode == "percent_of_total":
            denom = _as_float(grand["cells"][total_pos]) if grand and total_pos is not None else None
            for row in rows:
                for pos in positions:
                    row["cells"][pos] = _ratio(row["cells"][pos], denom)
            if grand:
                for pos in positions:
                    grand["cells"][pos] = _ratio(grand["cells"][pos], denom)

        elif mode == "percent_of_row":
            for row in rows + ([grand] if grand else []):
                denom = _as_float(row["cells"][total_pos]) if total_pos is not None else None
                for pos in positions:
                    row["cells"][pos] = _ratio(row["cells"][pos], denom)

        elif mode == "percent_of_column":
            for pos in positions:
                denom = _as_float(grand["cells"][pos]) if grand else None
                for row in rows:
                    row["cells"][pos] = _ratio(row["cells"][pos], denom)
                if grand:
                    grand["cells"][pos] = _ratio(grand["cells"][pos], denom)

        elif mode == "running_total":
            for pos in positions:
                total = 0.0
                for row in rows:
                    if row["depth"]:
                        continue                # subtotals sit outside the run
                    value = _as_float(row["cells"][pos])
                    if value is not None:
                        total += value
                        row["cells"][pos] = total

        elif mode == "rank":
            for pos in positions:
                detail = [r for r in rows if not r["depth"]]
                order = sorted(
                    detail,
                    key=lambda r: (_as_float(r["cells"][pos]) is None,
                                   -(_as_float(r["cells"][pos]) or 0)),
                )
                for place, row in enumerate(order, start=1):
                    row["cells"][pos] = place if _as_float(row["cells"][pos]) is not None else None


def _ratio(numerator: Any, denominator: float | None) -> Any:
    value = _as_float(numerator)
    if value is None or not denominator:
        return None
    ratio = value / denominator
    return ratio if math.isfinite(ratio) else None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 9_007_199_254_740_991 else str(value)
    if isinstance(value, float):
        # No JSON form for NaN or infinity (an AVG over them, say): one such cell
        # made the whole pivot reply unreadable and the view sat on "Calculating…".
        return value if math.isfinite(value) else None
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    return str(value)


# ----------------------------------------------------------------- drill down


def drill_down(
    workspace: Workspace,
    dataset: Dataset,
    *,
    row_fields: Sequence[Any],
    keys: Sequence[Any],
    columns: Sequence[Any] | None = None,
    column: Any = None,
    category: Any = None,
    categories: Sequence[Any] | None = None,
    filters: Sequence[dict] | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """The rows behind one cell — Excel's double-click "Show Details"."""
    extra: list[dict[str, Any]] = list(filters or [])

    fields = [_normalise_field(f) for f in (row_fields or [])]
    for field, key in zip(fields, keys):
        extra.extend(_match_filter(dataset, field, key))

    col_source = categories if categories is not None else (
        [category] if category is not None else []
    )
    col_defs = [_normalise_field(f) for f in (
        columns if columns is not None else ([column] if column else [])
    )]
    for field, key in zip(col_defs, col_source):
        extra.extend(_match_filter(dataset, field, key))

    from .query import page

    return page(workspace, dataset, limit=limit, filters=extra, with_count=True)


def _match_filter(dataset: Dataset, field: dict[str, Any], key: Any) -> list[dict[str, Any]]:
    """Filters that select exactly the rows a pivot cell was built from.

    A grouped field needs a range, not an equality: "March 2024" means every
    timestamp in that month, not rows whose value equals the first of the month.
    """
    name = field["column"]
    if key is None:
        return [{"column": name, "op": "is_null"}]

    if field["group"]:
        start = _parse_moment(key)
        if start is None:
            return [{"column": name, "op": "equals_text", "value": str(key)}]
        end = _advance(start, field["group"])
        return [
            {"column": name, "op": "gte", "value": start.isoformat(sep=" ")},
            {"column": name, "op": "lt", "value": end.isoformat(sep=" ")},
        ]

    if field["bin"]:
        low = float(key)
        return [
            {"column": name, "op": "gte", "value": low},
            {"column": name, "op": "lt", "value": low + float(field["bin"])},
        ]

    return [{"column": name, "op": "equals_text", "value": str(key)}]


def _parse_moment(value: Any) -> datetime | None:
    """A date or datetime from whatever the browser sent back."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if " " in text or "T" in text else text, pattern)
        except ValueError:
            continue
    return None


def _advance(moment: datetime, group: str) -> datetime:
    """The start of the next period — the exclusive upper bound for a drill-down."""
    if group == "year":
        return moment.replace(year=moment.year + 1, month=1, day=1)
    if group == "quarter":
        month = moment.month + 3
        return moment.replace(
            year=moment.year + (month - 1) // 12, month=(month - 1) % 12 + 1, day=1
        )
    if group == "month":
        month = moment.month + 1
        return moment.replace(
            year=moment.year + (month - 1) // 12, month=(month - 1) % 12 + 1, day=1
        )
    return moment + {
        "week": timedelta(days=7),
        "day": timedelta(days=1),
        "hour": timedelta(hours=1),
    }[group]


def _readable_sql(sql: str, params: Sequence[Any]) -> str:
    text = sql
    for value in params:
        text = text.replace("?", repr(value), 1)
    return text


def _friendly(exc: Exception) -> str:
    text = str(exc)
    if "No function matches" in text or "Binder Error" in text:
        return ("That summary does not fit the column you chose — "
                "try Count, or pick a numeric column.")
    if "Out of Memory" in text:
        return ("This pivot needed more memory than is available. "
                "Add a filter, or use fewer column categories.")
    return f"The pivot could not be built: {text}"

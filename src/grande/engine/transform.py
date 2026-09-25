"""Cleaning operations, with undo.

The verbs here are the ones people reach for in Excel most often after sorting
and filtering: remove duplicates, find and replace, trim, split a column, change
a type, rename. None of them existed in the original.

Each step writes a **new table** and pushes the previous one onto a history
stack, so undo is exact and instant rather than a best-effort inverse. The stack
is capped, and superseded tables are dropped as it rolls over.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

import duckdb

from .filters import compile_filters
from .session import Dataset, Workspace
from .sql import SqlError, ident, literal

#: Undo levels kept per dataset. Each level is one materialised table.
HISTORY_DEPTH = 12


class TransformError(ValueError):
    """A transform could not be applied, with a message written for a human."""


@dataclass
class Step:
    """One applied operation, replayable onto another file."""

    op: str
    params: dict[str, Any]
    description: str
    rows_before: int = 0
    rows_after: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "params": self.params,
            "description": self.description,
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "rows_changed": self.rows_after - self.rows_before,
        }


@dataclass
class History:
    """Table versions behind one dataset, newest last."""

    tables: list[str] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)


_histories: dict[str, History] = {}


def history_for(dataset: Dataset) -> History:
    return _histories.setdefault(dataset.id, History())


def recipe(dataset: Dataset) -> list[dict[str, Any]]:
    """The applied steps, as a re-runnable recipe."""
    return [s.as_dict() for s in history_for(dataset).steps]


# ------------------------------------------------------------------ internals


def _apply(
    workspace: Workspace,
    dataset: Dataset,
    select_sql: str,
    step: Step,
    *,
    source: str | None = None,
) -> Dataset:
    """Materialise ``SELECT {select_sql} FROM <current>`` as the dataset's new table."""
    history = history_for(dataset)
    previous = dataset.table
    new_table = workspace.new_table_name()

    cur = workspace.cursor()
    try:
        cur.execute(
            f"CREATE OR REPLACE TABLE {ident(new_table)} AS "
            f"SELECT {select_sql} FROM {ident(source or previous)}"
        )
    except duckdb.Error as exc:
        try:
            cur.execute(f"DROP TABLE IF EXISTS {ident(new_table)}")
        except duckdb.Error:
            pass
        raise TransformError(_friendly(exc)) from exc
    finally:
        cur.close()

    step.rows_before = dataset.row_count
    history.tables.append(previous)
    history.steps.append(step)

    # Roll the oldest version off the stack and reclaim its space.
    while len(history.tables) > HISTORY_DEPTH:
        stale = history.tables.pop(0)
        history.steps.pop(0)
        try:
            workspace.execute(f"DROP TABLE IF EXISTS {ident(stale)}")
        except duckdb.Error:
            pass

    dataset.table = new_table
    dataset.columns = workspace.describe_table(new_table)
    dataset.row_count = workspace.count_rows(new_table)
    step.rows_after = dataset.row_count
    return dataset


def undo(workspace: Workspace, dataset: Dataset) -> Dataset:
    """Step back one operation."""
    history = history_for(dataset)
    if not history.tables:
        raise TransformError("There is nothing to undo.")
    previous = history.tables.pop()
    history.steps.pop()
    discarded = dataset.table
    dataset.table = previous
    dataset.columns = workspace.describe_table(previous)
    dataset.row_count = workspace.count_rows(previous)
    try:
        workspace.execute(f"DROP TABLE IF EXISTS {ident(discarded)}")
    except duckdb.Error:
        pass
    return dataset


def can_undo(dataset: Dataset) -> bool:
    return bool(history_for(dataset).tables)


def _check(dataset: Dataset, *names: str) -> None:
    for name in names:
        if name and dataset.column(name) is None:
            raise TransformError(f"There is no column called “{name}”.")


def _all_columns(dataset: Dataset) -> list[str]:
    return [c.name for c in dataset.columns]


# -------------------------------------------------------------------- the ops


def remove_duplicates(
    workspace: Workspace,
    dataset: Dataset,
    *,
    columns: Sequence[str] | None = None,
    keep: str = "first",
) -> Dataset:
    """Excel's Remove Duplicates. ``columns`` empty means every column."""
    keys = list(columns) if columns else _all_columns(dataset)
    _check(dataset, *keys)
    if keep not in {"first", "last"}:
        keep = "first"

    # QUALIFY runs after the window function, so this keeps exactly one row per
    # key without a self-join. Ordering by rowid makes "first" mean file order.
    order = "ASC" if keep == "first" else "DESC"
    partition = ", ".join(ident(k) for k in keys)
    numbered = (
        f"SELECT *, row_number() OVER "
        f"(PARTITION BY {partition} ORDER BY rowid {order}) AS _rn "
        f"FROM {ident(dataset.table)}"
    )
    new_table = workspace.new_table_name()
    keep_cols = ", ".join(ident(c) for c in _all_columns(dataset))
    cur = workspace.cursor()
    try:
        cur.execute(
            f"CREATE OR REPLACE TABLE {ident(new_table)} AS "
            f"SELECT {keep_cols} FROM ({numbered}) WHERE _rn = 1"
        )
    except duckdb.Error as exc:
        raise TransformError(_friendly(exc)) from exc
    finally:
        cur.close()

    history = history_for(dataset)
    step = Step(
        op="remove_duplicates",
        params={"columns": keys, "keep": keep},
        description=(
            "Removed duplicate rows"
            + ("" if not columns else f" by {', '.join(keys)}")
        ),
        rows_before=dataset.row_count,
    )
    history.tables.append(dataset.table)
    history.steps.append(step)
    dataset.table = new_table
    dataset.columns = workspace.describe_table(new_table)
    dataset.row_count = workspace.count_rows(new_table)
    step.rows_after = dataset.row_count
    return dataset


def find_replace(
    workspace: Workspace,
    dataset: Dataset,
    *,
    # Every argument here has a default. The UI omits a field the user left
    # empty, so a required keyword arrives as a TypeError — an internal 500
    # where the user should simply have been told what to choose.
    columns: Sequence[str] | None = None,
    find: str = "",
    replace: str = "",
    match_case: bool = False,
    whole_cell: bool = False,
    regex: bool = False,
) -> Dataset:
    """Excel's Find & Replace, across a column or the whole sheet."""
    if find is None or find == "":
        raise TransformError("Enter something to find.")
    targets = list(columns) if columns else [
        c.name for c in dataset.columns if c.kind == "text"
    ]
    _check(dataset, *targets)
    if not targets:
        raise TransformError("There are no text columns to search.")

    if regex:
        try:
            re.compile(find)
        except re.error as exc:
            raise TransformError(f"That is not a valid pattern: {exc}") from exc

    parts: list[str] = []
    for col in _all_columns(dataset):
        if col not in targets:
            parts.append(ident(col))
            continue
        expression = _replace_expr(col, find, replace, match_case, whole_cell, regex)
        parts.append(f"{expression} AS {ident(col)}")

    step = Step(
        op="find_replace",
        params={
            "columns": targets, "find": find, "replace": replace,
            "match_case": match_case, "whole_cell": whole_cell, "regex": regex,
        },
        description=f"Replaced “{find}” with “{replace}”"
        + (f" in {', '.join(targets)}" if columns else " everywhere"),
    )
    return _apply(workspace, dataset, ", ".join(parts), step)


def _replace_expr(
    column: str, find: str, replace: str, match_case: bool, whole_cell: bool, regex: bool
) -> str:
    col = f"CAST({ident(column)} AS VARCHAR)"
    if whole_cell:
        comparison = f"{col} = {literal(find)}" if match_case else \
            f"lower({col}) = lower({literal(find)})"
        return f"CASE WHEN {comparison} THEN {literal(replace)} ELSE {ident(column)} END"
    if regex:
        flags = "g" if match_case else "gi"
        return f"regexp_replace({col}, {literal(find)}, {literal(replace)}, {literal(flags)})"
    if match_case:
        return f"replace({col}, {literal(find)}, {literal(replace)})"
    # Case-insensitive plain replace: escape the needle so it is matched literally.
    escaped = re.escape(find)
    return f"regexp_replace({col}, {literal(escaped)}, {literal(replace)}, 'gi')"


def clean_text(
    workspace: Workspace,
    dataset: Dataset,
    *,
    columns: Sequence[str] | None = None,
    trim: bool = True,
    collapse_spaces: bool = False,
    case: str | None = None,
    remove_non_printing: bool = False,
) -> Dataset:
    """TRIM / CLEAN / UPPER / LOWER / PROPER, applied to chosen columns."""
    targets = list(columns or [])
    _check(dataset, *targets)
    if not targets:
        raise TransformError("Choose at least one column to clean.")

    parts: list[str] = []
    for col in _all_columns(dataset):
        if col not in targets:
            parts.append(ident(col))
            continue
        expr = f"CAST({ident(col)} AS VARCHAR)"
        if remove_non_printing:
            expr = f"regexp_replace({expr}, '[\\x00-\\x1F\\x7F]', '', 'g')"
        if collapse_spaces:
            expr = f"regexp_replace({expr}, '\\s+', ' ', 'g')"
        if trim:
            expr = f"trim({expr})"
        if case == "upper":
            expr = f"upper({expr})"
        elif case == "lower":
            expr = f"lower({expr})"
        elif case == "proper":
            # DuckDB has no PROPER; title-case each word.
            expr = (
                f"list_aggregate(list_transform(string_split({expr}, ' '), "
                f"x -> upper(x[1]) || lower(x[2:])), 'string_agg', ' ')"
            )
        parts.append(f"{expr} AS {ident(col)}")

    actions = [
        label for flag, label in (
            (trim, "trimmed"), (collapse_spaces, "collapsed spaces"),
            (remove_non_printing, "removed control characters"),
        ) if flag
    ]
    if case:
        actions.append(f"set to {case}case")
    step = Step(
        op="clean_text",
        params={"columns": targets, "trim": trim, "collapse_spaces": collapse_spaces,
                "case": case, "remove_non_printing": remove_non_printing},
        description=f"Cleaned {', '.join(targets)} ({', '.join(actions) or 'no change'})",
    )
    return _apply(workspace, dataset, ", ".join(parts), step)


def split_column(
    workspace: Workspace,
    dataset: Dataset,
    *,
    column: str = "",
    delimiter: str = "",
    into: int = 2,
    keep_original: bool = False,
) -> Dataset:
    """Excel's Text to Columns."""
    if not column:
        raise TransformError("Choose a column to split.")
    _check(dataset, column)
    if not delimiter:
        raise TransformError("Choose what to split on.")
    into = max(2, min(int(into), 32))

    existing = set(_all_columns(dataset))
    parts: list[str] = []
    for col in _all_columns(dataset):
        if col == column:
            if keep_original:
                parts.append(ident(col))
            split = f"string_split(CAST({ident(col)} AS VARCHAR), {literal(delimiter)})"
            for i in range(1, into + 1):
                name = _free_name(f"{column}_{i}", existing)
                existing.add(name)
                # DuckDB lists are 1-based; a missing piece yields NULL, not an error.
                parts.append(f"{split}[{i}] AS {ident(name)}")
        else:
            parts.append(ident(col))

    step = Step(
        op="split_column",
        params={"column": column, "delimiter": delimiter, "into": into,
                "keep_original": keep_original},
        description=f"Split {column} on “{delimiter}” into {into} columns",
    )
    return _apply(workspace, dataset, ", ".join(parts), step)


#: Date layouts people actually have, ordered so the unambiguous ones win.
#: ``try_strptime`` takes the whole list and uses the first that fits each value,
#: so one column may legitimately mix several of these.
_ISO_FORMATS = [
    "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S.%f", "%Y/%m/%d", "%Y.%m.%d",
]
_DAY_FIRST = [
    "%d/%m/%Y", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d-%m-%Y",
    "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y",
]
_MONTH_FIRST = [
    "%m/%d/%Y", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m-%d-%Y",
    "%m.%d.%Y", "%m/%d/%y", "%m-%d-%y",
]
_NAMED = [
    "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y",
    "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y",
    "%d-%b-%y", "%d %b %y",
]
_COMPACT = ["%Y%m%d", "%Y%m%d%H%M%S"]

#: What an export wraps a number in, removed before it is read (RE2 syntax).
#: Currency codes only as a separate word, so "SKU-123" keeps its letters.
_CURRENCY_CODES = "USD|EUR|GBP|ILS|NIS|JPY|CHF|CAD|AUD|INR|CNY|SEK|NOK|DKK|PLN|CZK|HUF|MXN|BRL|ZAR"
_NUMBER_WRAPPING = (
    rf"^({_CURRENCY_CODES})\s+|\s+({_CURRENCY_CODES})$"
    r"|[\s\x{00A0}\x{202F}'’$€£¥₪%()]|-\s*$"
)
#: 1,234 / 1,234,567.89 — commas are thousands (the usual reading of 1,234).
_COMMA_THOUSANDS = r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$"
#: 1.234,56 / 1.234.567 — dots are thousands; a lone 1.234 stays a decimal.
_DOT_THOUSANDS = r"^[+-]?\d{1,3}(\.\d{3})+,\d+$|^[+-]?\d{1,3}(\.\d{3}){2,}$"
#: 12,5 / 1234,56 — the comma is the decimal point.
_DECIMAL_COMMA = r"^[+-]?\d*,\d+$"

TYPE_TARGETS = {
    "text": "VARCHAR", "number": "DOUBLE", "integer": "BIGINT",
    "date": "DATE", "datetime": "TIMESTAMP", "boolean": "BOOLEAN",
}


def date_formats(day_first: bool = True) -> list[str]:
    """The candidate layouts, with the ambiguous pair ordered as asked."""
    ambiguous = (_DAY_FIRST + _MONTH_FIRST) if day_first else (_MONTH_FIRST + _DAY_FIRST)
    return _ISO_FORMATS + ambiguous + _NAMED + _COMPACT


def _cast_expression(
    column: str,
    sql_type: str,
    *,
    date_format: str | None = None,
    day_first: bool = True,
) -> str:
    """How a column is converted.

    For dates this is ``try_strptime`` over a list of layouts rather than a bare
    ``TRY_CAST``. ``TRY_CAST`` only understands ISO-ish text, so ``15/03/2024``,
    ``15-Mar-2024``, ``Mar 15, 2024`` and ``20240315`` all silently became empty
    — which is exactly the quiet data loss this tool exists to prevent.
    """
    col = ident(column)
    text = f"nullif(trim(CAST({col} AS VARCHAR)), '')"

    if sql_type in {"DATE", "TIMESTAMP"}:
        formats = [date_format] if date_format else date_formats(day_first)
        as_list = "[" + ", ".join(literal(f) for f in formats) + "]"
        # Keep TRY_CAST as a fallback: it handles values DuckDB already stores
        # as a date or timestamp, where strptime would not apply.
        return (
            f"COALESCE("
            f"TRY_CAST(try_strptime({text}, {as_list}) AS {sql_type}), "
            f"TRY_CAST({col} AS {sql_type}))"
        )

    if sql_type in {"DOUBLE", "BIGINT"}:
        # Strip what exports wrap a number in (currency marks and codes, spaces,
        # apostrophe thousands, percent signs, accounting parentheses, a
        # trailing minus) but never letters inside the value: "SKU-123" must
        # fail and be listed, not become -123.
        bare = f"regexp_replace({text}, {literal(_NUMBER_WRAPPING)}, '', 'g')"
        # Then decide which mark is the decimal point. Stripping every comma
        # turned 12,5 into 125 and 1.234,56 into 1.23456, both counted as
        # converted. Commas grouping threes (1,234,567.8) are thousands; dots
        # grouping threes before a comma (1.234,56) are too; any other comma
        # (12,5 or 1234,56) is a decimal point.
        cleaned = (
            f"CASE WHEN regexp_matches({bare}, {literal(_COMMA_THOUSANDS)}) "
            f"THEN replace({bare}, ',', '') "
            f"WHEN regexp_matches({bare}, {literal(_DOT_THOUSANDS)}) "
            f"THEN replace(replace({bare}, '.', ''), ',', '.') "
            f"WHEN regexp_matches({bare}, {literal(_DECIMAL_COMMA)}) "
            f"THEN replace({bare}, ',', '.') "
            f"ELSE {bare} END"
        )
        negative = f"regexp_matches({text}, '^\\(.*\\)$|-\\s*$')"
        magnitude = f"TRY_CAST({cleaned} AS {sql_type})"
        return (
            f"COALESCE(TRY_CAST({col} AS {sql_type}), "
            f"CASE WHEN {negative} THEN -abs({magnitude}) ELSE {magnitude} END)"
        )

    return f"TRY_CAST({col} AS {sql_type})"


def preview_change_type(
    workspace: Workspace,
    dataset: Dataset,
    *,
    column: str = "",
    to: str = "",
    date_format: str | None = None,
    day_first: bool = True,
) -> dict[str, Any]:
    """What a type change would do — *before* doing it.

    Returns how many values convert, the ones that do not (with counts, so the
    user can see the actual offenders), and whether a day-first or month-first
    reading would disagree.
    """
    if not column:
        raise TransformError("Choose a column to convert.")
    _check(dataset, column)
    sql_type = TYPE_TARGETS.get(str(to).lower())
    if sql_type is None:
        raise TransformError(f"Cannot convert to “{to}”.")

    table = ident(dataset.table)
    col = ident(column)
    cast = _cast_expression(column, sql_type, date_format=date_format, day_first=day_first)

    row = workspace.execute_one(
        f"SELECT count(*), count({col}), count({cast}) FROM {table}"
    ) or (0, 0, 0)
    total, filled, converted = int(row[0]), int(row[1]), int(row[2])
    failed = max(0, filled - converted)

    failures = []
    if failed:
        failures = [
            {"value": r[0], "count": int(r[1])}
            for r in workspace.execute(
                f"SELECT CAST({col} AS VARCHAR), count(*) FROM {table} "
                f"WHERE {col} IS NOT NULL AND {cast} IS NULL "
                f"GROUP BY 1 ORDER BY 2 DESC LIMIT 12"
            )
        ]

    result: dict[str, Any] = {
        "column": column,
        "to": to,
        "rows": total,
        "filled": filled,
        "converted": converted,
        "failed": failed,
        "blank": total - filled,
        "failures": failures,
        "ambiguous": False,
        "ambiguous_rows": 0,
        "samples": [],
    }

    if sql_type in {"DATE", "TIMESTAMP"} and not date_format:
        # Would reading d/m/y instead of m/d/y change any answer? If so the user
        # must choose; guessing silently turns 3 May into 5 March.
        day = _cast_expression(column, sql_type, day_first=True)
        month = _cast_expression(column, sql_type, day_first=False)
        clash = workspace.execute_one(
            f"SELECT count(*) FROM {table} "
            f"WHERE {col} IS NOT NULL AND {day} IS DISTINCT FROM {month}"
        )
        result["ambiguous_rows"] = int(clash[0]) if clash else 0
        result["ambiguous"] = result["ambiguous_rows"] > 0

    if converted:
        result["samples"] = [
            {"before": r[0], "after": str(r[1])}
            for r in workspace.execute(
                f"SELECT CAST({col} AS VARCHAR), {cast} FROM {table} "
                f"WHERE {cast} IS NOT NULL LIMIT 5"
            )
        ]
    return result


def change_type(
    workspace: Workspace,
    dataset: Dataset,
    *,
    column: str = "",
    to: str = "",
    date_format: str | None = None,
    day_first: bool = True,
    keep_original: bool = False,
) -> Dataset:
    """Change a column's type.

    ``keep_original`` copies the untouched values into a second column first, so
    a conversion that loses something is recoverable without an undo.
    """
    if not column:
        raise TransformError("Choose a column to convert.")
    _check(dataset, column)
    sql_type = TYPE_TARGETS.get(str(to).lower())
    if sql_type is None:
        raise TransformError(f"Cannot convert to “{to}”.")

    cast = _cast_expression(column, sql_type, date_format=date_format, day_first=day_first)
    preview = preview_change_type(
        workspace, dataset, column=column, to=to,
        date_format=date_format, day_first=day_first,
    )
    lost = preview["failed"]

    parts: list[str] = []
    existing = set(_all_columns(dataset))
    for name in _all_columns(dataset):
        if name != column:
            parts.append(ident(name))
            continue
        parts.append(f"{cast} AS {ident(name)}")
        if keep_original:
            kept = _free_name(f"{column} (original)", existing)
            existing.add(kept)
            parts.append(f"CAST({ident(name)} AS VARCHAR) AS {ident(kept)}")

    note = f"Changed {column} to {to}"
    if lost:
        note += f" — {lost:,} value(s) did not convert"
        note += " and were kept in a second column" if keep_original else " and are now empty"
    step = Step(
        op="change_type",
        params={
            "column": column, "to": to, "date_format": date_format,
            "day_first": day_first, "keep_original": keep_original,
            "unconverted": lost,
        },
        description=note,
    )
    return _apply(workspace, dataset, ", ".join(parts), step)


def rename_column(
    workspace: Workspace, dataset: Dataset, *, column: str = "", to: str = ""
) -> Dataset:
    if not column:
        raise TransformError("Choose a column to rename.")
    _check(dataset, column)
    new_name = str(to or "").strip()
    if not new_name:
        raise TransformError("Enter a new name.")
    if new_name != column and dataset.column(new_name) is not None:
        raise TransformError(f"There is already a column called “{new_name}”.")
    parts = [
        (f"{ident(c)} AS {ident(new_name)}" if c == column else ident(c))
        for c in _all_columns(dataset)
    ]
    step = Step(
        op="rename_column",
        params={"column": column, "to": new_name},
        description=f"Renamed {column} to {new_name}",
    )
    return _apply(workspace, dataset, ", ".join(parts), step)


def remove_columns(
    workspace: Workspace, dataset: Dataset, *, columns: Sequence[str] | None = None
) -> Dataset:
    drop = set(columns or [])
    if not drop:
        raise TransformError("Choose at least one column to delete.")
    _check(dataset, *drop)
    keep = [c for c in _all_columns(dataset) if c not in drop]
    if not keep:
        raise TransformError("You cannot remove every column.")
    step = Step(
        op="remove_columns",
        params={"columns": sorted(drop)},
        description=f"Removed {len(drop)} column(s): {', '.join(sorted(drop))}",
    )
    return _apply(workspace, dataset, ", ".join(ident(c) for c in keep), step)


def fill_blanks(
    workspace: Workspace, dataset: Dataset, *,
    columns: Sequence[str] | None = None, value: str = ""
) -> Dataset:
    targets = list(columns or [])
    if not targets:
        raise TransformError("Choose at least one column to fill.")
    _check(dataset, *targets)
    parts = []
    for col in _all_columns(dataset):
        if col in targets:
            parts.append(f"COALESCE({ident(col)}, {literal(value)}) AS {ident(col)}")
        else:
            parts.append(ident(col))
    step = Step(
        op="fill_blanks",
        params={"columns": targets, "value": value},
        description=f"Filled blanks in {', '.join(targets)} with “{value}”",
    )
    return _apply(workspace, dataset, ", ".join(parts), step)


def keep_filtered(
    workspace: Workspace, dataset: Dataset, *,
    filters: Sequence[dict] | None = None, invert: bool = False
) -> Dataset:
    """Turn the current filter into a permanent edit — keep or delete those rows."""
    where, params = compile_filters(filters)
    if not where:
        raise TransformError("Set a filter first.")
    clause = where[len(" WHERE "):]
    # A row whose filter column is blank did not match, so deleting the matches
    # must keep it. NOT (NULL) is NULL, and WHERE NULL drops the row, so the
    # bare negation also deleted every blank row.
    matched = f"coalesce(({clause}), false)"
    predicate = f"NOT {matched}" if invert else matched

    new_table = workspace.new_table_name()
    cur = workspace.cursor()
    try:
        cur.execute(
            f"CREATE OR REPLACE TABLE {ident(new_table)} AS "
            f"SELECT * FROM {ident(dataset.table)} WHERE {predicate}",
            params,
        )
    except duckdb.Error as exc:
        raise TransformError(_friendly(exc)) from exc
    finally:
        cur.close()

    history = history_for(dataset)
    step = Step(
        op="keep_filtered",
        params={"filters": list(filters), "invert": invert},
        description="Deleted the matching rows" if invert else "Kept only the matching rows",
        rows_before=dataset.row_count,
    )
    history.tables.append(dataset.table)
    history.steps.append(step)
    dataset.table = new_table
    dataset.columns = workspace.describe_table(new_table)
    dataset.row_count = workspace.count_rows(new_table)
    step.rows_after = dataset.row_count
    return dataset


def _free_name(preferred: str, taken: set[str]) -> str:
    if preferred not in taken:
        return preferred
    n = 2
    while f"{preferred}_{n}" in taken:
        n += 1
    return f"{preferred}_{n}"


def _friendly(exc: Exception) -> str:
    text = str(exc)
    if "Conversion Error" in text:
        return "Some values could not be converted. Try changing the type to text first."
    if "Out of Memory" in text:
        return "That operation needed more memory than is available. Try filtering first."
    return f"That operation failed: {text}"

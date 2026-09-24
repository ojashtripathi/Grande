"""Reading a file into the workspace.

Handles the formats people actually have lying around: CSV/TSV (optionally
gzipped or zipped), Parquet, JSON/JSONL, and Excel workbooks. The original only
read CSV, TSV and Parquet — someone whose 3M-row export arrived as .xlsx could
not open it at all.

Two stages, deliberately separate:

* :func:`sniff` looks at the file and *proposes* how to read it, without loading
  anything. The UI shows that proposal and lets the user correct it.
* :func:`ingest` commits: it creates one DuckDB table and returns a
  :class:`~grande.engine.session.Dataset`.
"""

from __future__ import annotations

import csv
import io
import os
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from .session import Column, Dataset, Workspace, classify
from .sql import ident

CSV_SUFFIXES = {".csv", ".tsv", ".txt", ".tab", ".psv", ".dat"}
PARQUET_SUFFIXES = {".parquet", ".pq"}
JSON_SUFFIXES = {".json", ".jsonl", ".ndjson"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xlsb", ".xls", ".ods"}
COMPRESSED_SUFFIXES = {".gz", ".bz2", ".zst", ".zip"}

#: Excel's hard ceilings. We never write past these; we warn when a source exceeds them.
EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_DATA_ROWS = EXCEL_MAX_ROWS - 1  # one row is the header
EXCEL_MAX_COLS = 16_384

DELIMITER_NAMES = {",": "comma", "\t": "tab", ";": "semicolon", "|": "pipe", " ": "space"}


class IngestError(RuntimeError):
    """A file could not be read, with a message written for a human."""


@dataclass
class SniffResult:
    """What we think a file is, offered to the user for confirmation."""

    path: str
    kind: str  # csv | parquet | json | excel
    size_bytes: int
    delimiter: str | None = None
    encoding: str | None = None
    has_header: bool = True
    sheet: str | None = None
    sheets: list[str] = field(default_factory=list)
    date_format: str | None = None
    columns: list[dict[str, Any]] = field(default_factory=list)
    sample_rows: list[list[Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "delimiter": self.delimiter,
            "delimiter_name": DELIMITER_NAMES.get(self.delimiter or "", self.delimiter),
            "encoding": self.encoding,
            "has_header": self.has_header,
            "sheet": self.sheet,
            "sheets": self.sheets,
            "date_format": self.date_format,
            "columns": self.columns,
            "sample_rows": self.sample_rows,
            "notes": self.notes,
        }


# --------------------------------------------------------------------- helpers


def _literal(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def classify_path(path: Path) -> str:
    """Decide which reader a path needs, seeing through a compression suffix."""
    suffixes = [s.lower() for s in path.suffixes]
    if not suffixes:
        return "csv"
    effective = suffixes[-1]
    if effective in COMPRESSED_SUFFIXES and len(suffixes) > 1:
        effective = suffixes[-2]
    if effective in PARQUET_SUFFIXES:
        return "parquet"
    if effective in JSON_SUFFIXES:
        return "json"
    if effective in EXCEL_SUFFIXES:
        return "excel"
    if effective in CSV_SUFFIXES:
        return "csv"
    # Unknown extension: treat as delimited text, which is the usual case for
    # exports named things like "report.20240101".
    return "csv"


def detect_encoding(path: Path, probe: int = 262_144) -> str:
    """Pick an encoding that will not throw. Deliberately conservative.

    We only distinguish the cases that actually occur in business exports: a
    UTF-8 BOM, valid UTF-8, a UTF-16 BOM, and "something else" (treated as
    cp1252, which is the usual source of stray £ and é in Windows CSVs).
    """
    with open(path, "rb") as handle:
        head = handle.read(probe)
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8"  # DuckDB strips the BOM itself when encoding is utf-8
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    try:
        head.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        # A multi-byte sequence may simply straddle the probe boundary.
        try:
            head[:-4].decode("utf-8")
            return "utf-8"
        except UnicodeDecodeError:
            return "windows-1252"


def sniff_delimiter(sample: str) -> str:
    """Guess a delimiter, preferring the one that gives a consistent column count."""
    candidates = [",", "\t", ";", "|"]
    lines = [ln for ln in sample.splitlines() if ln.strip()][:50]
    if not lines:
        return ","
    best, best_score = ",", -1.0
    for delim in candidates:
        counts = []
        for line in lines:
            try:
                counts.append(len(next(csv.reader([line], delimiter=delim))))
            except csv.Error:
                counts.append(0)
        fields = max(counts) if counts else 0
        if fields < 2:
            continue
        consistent = sum(1 for c in counts if c == counts[0]) / len(counts)
        # Favour consistency first, then a higher field count as a tiebreak.
        score = consistent * 100 + min(fields, 50)
        if score > best_score:
            best, best_score = delim, score
    return best


def looks_like_header(first: list[str], second: list[str] | None) -> bool:
    """Header detection: a header row is all-text where the next row is not."""
    if not first:
        return True
    if any(cell.strip() == "" for cell in first):
        return False

    def numeric(cell: str) -> bool:
        try:
            float(cell.replace(",", ""))
            return True
        except ValueError:
            return False

    if any(numeric(c) for c in first):
        return False
    if second and any(numeric(c) for c in second):
        return True
    return True


def _read_text_head(path: Path, encoding: str, limit: int = 262_144) -> str:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            if not names:
                raise IngestError("That .zip archive is empty.")
            with archive.open(names[0]) as member:
                return io.TextIOWrapper(member, encoding=encoding, errors="replace").read(limit)
    if path.suffixes and path.suffixes[-1].lower() == ".gz":
        import gzip

        with gzip.open(path, "rt", encoding=encoding, errors="replace") as handle:
            return handle.read(limit)
    with open(path, "r", encoding=encoding, errors="replace", newline="") as handle:
        return handle.read(limit)


# ----------------------------------------------------------------------- sniff


def sniff(path: str | os.PathLike[str]) -> SniffResult:
    """Inspect a file and propose how to read it. Loads nothing into the workspace."""
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise IngestError(f"There is no file at {file_path}")
    if file_path.is_dir():
        raise IngestError("That is a folder. Pick a file inside it.")

    kind = classify_path(file_path)
    size = file_path.stat().st_size
    result = SniffResult(path=str(file_path), kind=kind, size_bytes=size)

    if kind == "excel":
        _sniff_excel(file_path, result)
    elif kind == "csv":
        _sniff_csv(file_path, result)
    else:
        _sniff_via_duckdb(file_path, result)
    return result


#: Layouts where the same text means two different dates. If DuckDB picks one of
#: these while loading, the user has to be told which reading they got.
AMBIGUOUS_DATE_FORMATS = {
    "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y", "%d.%m.%Y", "%m.%d.%Y",
    "%d/%m/%y", "%m/%d/%y", "%d-%m-%y", "%m-%d-%y",
}


def detect_date_format(path: Path, delimiter: str | None = None) -> str | None:
    """Which date layout DuckDB's sniffer will use for this file, if any.

    Worth knowing before the fact: the sniffer settles on one silently, and for
    ``03/05/2024`` the choice between day-first and month-first changes the
    answer by two months.
    """
    con = duckdb.connect()
    try:
        cur = con.execute("SELECT * FROM sniff_csv(?)", [str(path)])
        names = [d[0] for d in (cur.description or [])]
        row = cur.fetchone()
        if not row:
            return None
        found = dict(zip(names, row))
        return found.get("DateFormat") or found.get("TimestampFormat") or None
    except duckdb.Error:
        return None
    finally:
        con.close()


def describe_date_format(fmt: str) -> str:
    """Explain a layout in terms of a worked example, not strptime codes."""
    if fmt in {"%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y"}:
        return "day first — 03/05/2024 is 3 May"
    if fmt in {"%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y", "%m/%d/%y", "%m-%d-%y"}:
        return "month first — 03/05/2024 is 5 March"
    return fmt


def _sniff_csv(path: Path, result: SniffResult) -> None:
    encoding = detect_encoding(path)
    head = _read_text_head(path, encoding)
    delimiter = sniff_delimiter(head)
    rows = list(csv.reader(io.StringIO(head), delimiter=delimiter))
    rows = [r for r in rows if r]
    header_row = rows[0] if rows else []
    second_row = rows[1] if len(rows) > 1 else None
    has_header = looks_like_header(header_row, second_row)

    result.encoding = encoding
    result.delimiter = delimiter
    result.has_header = has_header
    names = header_row if has_header else [f"column{i + 1}" for i in range(len(header_row))]
    result.columns = [{"name": n, "type": "VARCHAR", "kind": "text"} for n in names]
    body = rows[1:] if has_header else rows
    result.sample_rows = body[:20]

    result.date_format = detect_date_format(path, delimiter)
    if result.date_format in AMBIGUOUS_DATE_FORMATS:
        result.notes.append(
            f"Dates will be read {describe_date_format(result.date_format)}. "
            "Change this below if the file was written the other way round."
        )

    if encoding == "windows-1252":
        result.notes.append("This file is not UTF-8; reading it as Windows-1252.")
    if not has_header:
        result.notes.append("No header row detected — columns will be named column1, column2, …")
    if len(names) > EXCEL_MAX_COLS:
        result.notes.append(
            f"{len(names):,} columns — more than Excel's {EXCEL_MAX_COLS:,} limit. "
            "Excel export will need a column subset."
        )


def _sniff_excel(path: Path, result: SniffResult) -> None:
    try:
        from python_calamine import CalamineWorkbook
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise IngestError("Reading Excel files needs the 'python-calamine' package.") from exc

    try:
        workbook = CalamineWorkbook.from_path(str(path))
    except Exception as exc:
        raise IngestError(f"That workbook could not be opened: {exc}") from exc

    result.sheets = list(workbook.sheet_names)
    if not result.sheets:
        raise IngestError("That workbook has no sheets.")
    result.sheet = result.sheets[0]

    sheet = workbook.get_sheet_by_name(result.sheet)
    rows = sheet.to_python(skip_empty_area=True)
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        result.notes.append("That sheet is empty.")
        return
    header = [str(c) for c in rows[0]]
    result.has_header = looks_like_header(header, [str(c) for c in rows[1]] if len(rows) > 1 else None)
    names = header if result.has_header else [f"column{i + 1}" for i in range(len(header))]
    result.columns = [{"name": n, "type": "VARCHAR", "kind": "text"} for n in names]
    result.sample_rows = [[_jsonable(c) for c in r] for r in rows[1:21]]
    if len(result.sheets) > 1:
        result.notes.append(f"{len(result.sheets)} sheets in this workbook — pick one to open.")


def _sniff_via_duckdb(path: Path, result: SniffResult) -> None:
    """Let DuckDB describe Parquet and JSON; it already knows their schemas."""
    con = duckdb.connect()
    try:
        reader = "read_parquet(?)" if result.kind == "parquet" else "read_json_auto(?)"
        described = con.execute(f"DESCRIBE SELECT * FROM {reader}", [str(path)]).fetchall()
        result.columns = [{"name": r[0], "type": r[1], "kind": classify(r[1])} for r in described]
        sample = con.execute(f"SELECT * FROM {reader} LIMIT 20", [str(path)]).fetchall()
        result.sample_rows = [[_jsonable(c) for c in row] for row in sample]
    except duckdb.Error as exc:
        raise IngestError(f"That file could not be read: {exc}") from exc
    finally:
        con.close()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# ---------------------------------------------------------------------- ingest


def ingest(
    workspace: Workspace,
    path: str | os.PathLike[str],
    *,
    kind: str | None = None,
    delimiter: str | None = None,
    encoding: str | None = None,
    has_header: bool = True,
    sheet: str | None = None,
    all_varchar: bool = False,
    date_format: str | None = None,
    progress: Any = None,
) -> Dataset:
    """Read ``path`` into a new table and register it on the workspace.

    ``progress`` is an optional callable taking ``(phase, detail)`` so long loads
    can narrate themselves honestly rather than animating a fake percentage.
    """
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise IngestError(f"There is no file at {file_path}")

    kind = kind or classify_path(file_path)
    table = workspace.new_table_name()
    started = time.time()
    notes: list[str] = []

    def say(phase: str, detail: str = "") -> None:
        if progress:
            progress(phase, detail)

    say("reading", f"Reading {file_path.name}")

    if kind == "excel":
        _ingest_excel(workspace, file_path, table, sheet=sheet, has_header=has_header, notes=notes)
    else:
        _ingest_via_duckdb(
            workspace,
            file_path,
            table,
            kind=kind,
            delimiter=delimiter,
            encoding=encoding,
            has_header=has_header,
            all_varchar=all_varchar,
            date_format=date_format,
            notes=notes,
        )

    say("indexing", "Working out column types")
    columns = workspace.describe_table(table)
    row_count = workspace.count_rows(table)

    warnings: list[str] = []
    if kind == "csv" and any(c.kind == "date" for c in columns):
        used = date_format or detect_date_format(file_path, delimiter)
        if used in AMBIGUOUS_DATE_FORMATS:
            dated = ", ".join(c.name for c in columns if c.kind == "date")
            warnings.append(
                f"Dates in {dated} were read {describe_date_format(used)}. "
                "If that is the wrong way round, open the file again and set the "
                "date format."
            )

    if row_count > EXCEL_MAX_DATA_ROWS:
        parts = -(-row_count // EXCEL_MAX_DATA_ROWS)
        warnings.append(
            f"{row_count:,} rows is more than Excel can hold in one sheet "
            f"({EXCEL_MAX_DATA_ROWS:,}). An Excel export will need at least {parts} files."
        )
    if len(columns) > EXCEL_MAX_COLS:
        warnings.append(
            f"{len(columns):,} columns is more than Excel's {EXCEL_MAX_COLS:,} limit."
        )
    if row_count == 0:
        warnings.append("This file has no data rows.")

    dataset = Dataset(
        id=os.urandom(8).hex(),
        table=table,
        source_path=str(file_path),
        display_name=file_path.name,
        row_count=row_count,
        columns=columns,
        source_bytes=file_path.stat().st_size,
        ingest_seconds=time.time() - started,
        notes=notes,
        warnings=warnings,
    )
    say("done", f"{row_count:,} rows ready")
    return workspace.register(dataset)


def _ingest_via_duckdb(
    workspace: Workspace,
    path: Path,
    table: str,
    *,
    kind: str,
    delimiter: str | None,
    encoding: str | None,
    has_header: bool,
    all_varchar: bool,
    notes: list[str],
    date_format: str | None = None,
) -> None:
    quoted = ident(table)
    if kind == "parquet":
        sql = f"CREATE OR REPLACE TABLE {quoted} AS SELECT * FROM read_parquet(?)"
        params: list[Any] = [str(path)]
    elif kind == "json":
        sql = f"CREATE OR REPLACE TABLE {quoted} AS SELECT * FROM read_json_auto(?)"
        params = [str(path)]
    else:
        _ingest_csv(
            workspace, path, table,
            delimiter=delimiter, encoding=encoding,
            has_header=has_header, all_varchar=all_varchar, notes=notes,
            date_format=date_format,
        )
        return

    cur = workspace.cursor()
    try:
        cur.execute(sql, params)
    except duckdb.Error as exc:
        raise IngestError(_friendly_duckdb_error(exc)) from exc
    finally:
        try:
            cur.close()
        except duckdb.Error:
            pass


def _ingest_csv(
    workspace: Workspace,
    path: Path,
    table: str,
    *,
    delimiter: str | None,
    encoding: str | None,
    has_header: bool,
    all_varchar: bool,
    notes: list[str],
    date_format: str | None = None,
) -> None:
    """Load a delimited file, escalating through progressively safer readers.

    Real exports break in a few predictable ways and each remedy costs
    something, so each is applied only once it is needed:

    1. The fast path — DuckDB's parallel scanner, inferring types.
    2. Ragged rows — ``null_padding`` fills short rows, but DuckDB cannot
       combine it with the parallel scanner when cells contain quoted newlines,
       so this step also drops to a single-threaded read.
    3. Everything as text — for a column whose type changes far enough into the
       file that sampling missed it.

    Each escalation is recorded in ``notes``, so the user is told what happened
    instead of quietly receiving a different result.
    """
    encoding = encoding or detect_encoding(path)
    delimiter = delimiter or sniff_delimiter(_read_text_head(path, encoding, 131_072))

    base = ["header = ?", "delim = ?", "encoding = ?", "ignore_errors = true",
            "sample_size = 262144"]
    if date_format:
        base = base + [f"dateformat = {_literal(date_format)}",
                       f"timestampformat = {_literal(date_format)}"]
    ragged = base + ["null_padding = true", "parallel = false"]
    attempts: list[tuple[list[str], str | None]] = [
        (base, None),
        (ragged, "Some rows had fewer values than the header; the gaps were left empty."),
        (ragged + ["all_varchar = true"],
         "Some columns held a mix of types, so every column was read as text."),
    ]
    if all_varchar:
        attempts = [(base + ["all_varchar = true"], None), attempts[2]]

    params: list[Any] = [str(path), has_header, delimiter, encoding]
    last: Exception | None = None

    for options, note in attempts:
        cur = workspace.cursor()
        try:
            cur.execute(
                f"CREATE OR REPLACE TABLE {ident(table)} AS "
                f"SELECT * FROM read_csv(?, {', '.join(options)})",
                params,
            )
            if note:
                notes.append(note)
            return
        except duckdb.Error as exc:
            last = exc
        finally:
            try:
                cur.close()
            except duckdb.Error:
                pass

    raise IngestError(_friendly_duckdb_error(last or Exception("unknown")))


def _ingest_excel(
    workspace: Workspace,
    path: Path,
    table: str,
    *,
    sheet: str | None,
    has_header: bool,
    notes: list[str],
) -> None:
    from python_calamine import CalamineWorkbook

    workbook = CalamineWorkbook.from_path(str(path))
    sheet_name = sheet or (workbook.sheet_names[0] if workbook.sheet_names else None)
    if sheet_name is None:
        raise IngestError("That workbook has no sheets.")

    rows = workbook.get_sheet_by_name(sheet_name).to_python(skip_empty_area=True)
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        raise IngestError(f"Sheet “{sheet_name}” is empty.")

    if has_header:
        names = _unique_names([str(c).strip() or f"column{i + 1}" for i, c in enumerate(rows[0])])
        body = rows[1:]
    else:
        names = [f"column{i + 1}" for i in range(len(rows[0]))]
        body = rows

    width = len(names)
    # Excel sheets are ragged; pad and trim so every tuple matches the schema.
    padded = [tuple(list(r[:width]) + [None] * (width - len(r[:width]))) for r in body]

    cur = workspace.cursor()
    try:
        columns_sql = ", ".join(f"{ident(n)} VARCHAR" for n in names)
        cur.execute(f"CREATE OR REPLACE TABLE {ident(table)} ({columns_sql})")
        if padded:
            placeholders = ", ".join("?" for _ in names)
            cur.executemany(
                f"INSERT INTO {ident(table)} VALUES ({placeholders})",
                [tuple(_excel_cell(v) for v in row) for row in padded],
            )
        # Everything arrived as text; let DuckDB re-infer real types where it can.
        _retype_in_place(cur, table, names)
    finally:
        cur.close()

    notes.append(f"Loaded sheet “{sheet_name}”.")
    if len(workbook.sheet_names) > 1:
        notes.append(f"Other sheets available: {', '.join(n for n in workbook.sheet_names if n != sheet_name)}.")


def _excel_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, str)):
        return str(value)
    return str(value)


def _retype_in_place(cur: duckdb.DuckDBPyConnection, table: str, names: list[str]) -> None:
    """Promote all-text columns to numbers/dates when every value converts cleanly.

    Done per column so one messy column cannot force the whole sheet back to text.
    """
    quoted = ident(table)
    for name in names:
        col = ident(name)
        for target in ("BIGINT", "DOUBLE", "DATE", "TIMESTAMP"):
            try:
                row = cur.execute(
                    f"SELECT count(*) FROM {quoted} "
                    f"WHERE {col} IS NOT NULL AND TRY_CAST({col} AS {target}) IS NULL"
                ).fetchone()
            except duckdb.Error:
                break
            if row and row[0] == 0:
                nonnull = cur.execute(
                    f"SELECT count(*) FROM {quoted} WHERE {col} IS NOT NULL"
                ).fetchone()
                if not nonnull or nonnull[0] == 0:
                    break
                # Never promote a text column whose values carry a meaningful
                # leading zero — account numbers and ZIP codes must stay text.
                if target in ("BIGINT", "DOUBLE"):
                    padded = cur.execute(
                        f"SELECT count(*) FROM {quoted} "
                        f"WHERE {col} IS NOT NULL AND regexp_matches({col}, '^0[0-9]')"
                    ).fetchone()
                    if padded and padded[0] > 0:
                        break
                try:
                    cur.execute(f"ALTER TABLE {quoted} ALTER {col} TYPE {target}")
                except duckdb.Error:
                    pass
                break


def _unique_names(names: list[str]) -> list[str]:
    """Make header names unique the way a spreadsheet would: name, name_2, name_3."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen[name] = 1
            out.append(name)
        else:
            seen[name] += 1
            out.append(f"{name}_{seen[name]}")
    return out


def _friendly_duckdb_error(exc: Exception) -> str:
    text = str(exc)
    if "Could not convert" in text or "CSV Error" in text:
        return (
            "Some rows do not match the rest of the file. Try opening it again with "
            "“read every column as text”, then fix the columns once it is open."
        )
    if "No files found" in text:
        return "That path did not match any file."
    if "Permission denied" in text or "being used by another process" in text:
        return "That file is open in another program. Close it and try again."
    return f"That file could not be read: {text}"

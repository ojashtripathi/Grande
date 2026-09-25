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
import math
import os
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb

from .session import Column, Dataset, Workspace, classify
from .sql import glob_escape, ident

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


class IngestCancelled(Exception):
    """The person cancelled a load. Nothing was kept."""


#: How often, at most, a streaming Excel load reports progress.
EXCEL_PROGRESS_SECONDS = 0.25


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


#: Encoding names DuckDB's CSV reader accepts. It rejects the usual aliases —
#: "windows-1252" and "iso-8859-1" both fail — so the name has to be one of
#: these exactly, or no non-UTF-8 file opens at all.
DUCKDB_ENCODINGS = {"utf-8", "utf-16", "latin-1", "cp1252"}


def _raw_head(path: Path, probe: int) -> bytes:
    """The first bytes of a file's *content*, seeing through compression.

    Sniffing the container instead of the content is why .gz and .zip files
    were detected as cp1252 and then refused: compressed bytes are not valid
    UTF-8, so every compressed file looked like a legacy encoding.
    """
    suffixes = [s.lower() for s in path.suffixes]
    try:
        if suffixes and suffixes[-1] == ".zip":
            with zipfile.ZipFile(path) as archive:
                names = [n for n in archive.namelist() if not n.endswith("/")]
                if not names:
                    return b""
                with archive.open(names[0]) as member:
                    return member.read(probe)
        if suffixes and suffixes[-1] == ".gz":
            import gzip

            with gzip.open(path, "rb") as handle:
                return handle.read(probe)
    except (OSError, zipfile.BadZipFile, EOFError):
        return b""
    with open(path, "rb") as handle:
        return handle.read(probe)


def detect_encoding(path: Path, probe: int = 262_144) -> str:
    """Pick an encoding that will not throw. Deliberately conservative.

    We only distinguish the cases that actually occur in business exports: a
    UTF-8 BOM, valid UTF-8, a UTF-16 BOM, and "something else" (treated as
    cp1252, which is the usual source of stray £ and é in Windows CSVs).
    """
    head = _raw_head(path, probe)
    if not head:
        return "utf-8"
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
            # "cp1252", not "windows-1252": DuckDB rejects the latter outright,
            # so returning it meant no non-UTF-8 file could be opened at all.
            return "cp1252"


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
        cur = con.execute("SELECT * FROM sniff_csv(?)", [glob_escape(path)])
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

    # Only the header and twenty rows are shown, so read only those. Reading the
    # whole sheet into Python first cost seconds and gigabytes on a big sheet
    # before the dialog could appear.
    rows = _calamine(
        lambda: _first_rows(workbook.get_sheet_by_name(result.sheet), 21),
        "That workbook could not be read",
    )
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


def _calamine(action: Any, failure: str) -> Any:
    """Run a python-calamine call, turning every failure into an IngestError.

    Some broken or unusual files make python-calamine panic, which reaches
    Python as pyo3's PanicException: a BaseException, not an Exception, so it
    slipped past every handler — and in a background load it ended the worker
    silently, leaving the progress bar waiting for ever.
    """
    try:
        return action()
    except (KeyboardInterrupt, SystemExit, IngestError, IngestCancelled):
        raise
    except BaseException as exc:
        raise IngestError(f"{failure}: {exc}") from exc


def _is_blank(row: list[Any]) -> bool:
    return not any(str(c).strip() for c in row)


def _first_rows(sheet: Any, count: int) -> list[list[Any]]:
    """The first ``count`` non-blank rows of a sheet, reading no further."""
    # iter_rows panics on a sheet with no cells at all (height 0), and it also
    # yields the empty rows above where the data starts; blank rows are skipped
    # either way, which leaves exactly the rows to_python(skip_empty_area=True) had.
    if not sheet.height:
        return []
    rows: list[list[Any]] = []
    for row in sheet.iter_rows():
        if _is_blank(row):
            continue
        rows.append(row)
        if len(rows) == count:
            break
    return rows


def _sniff_via_duckdb(path: Path, result: SniffResult) -> None:
    """Let DuckDB describe Parquet and JSON; it already knows their schemas."""
    con = duckdb.connect()
    try:
        reader = "read_parquet(?)" if result.kind == "parquet" else "read_json_auto(?)"
        described = con.execute(f"DESCRIBE SELECT * FROM {reader}", [glob_escape(path)]).fetchall()
        result.columns = [{"name": r[0], "type": r[1], "kind": classify(r[1])} for r in described]
        sample = con.execute(f"SELECT * FROM {reader} LIMIT 20", [glob_escape(path)]).fetchall()
        result.sample_rows = [[_jsonable(c) for c in row] for row in sample]
    except duckdb.Error as exc:
        raise IngestError(f"That file could not be read: {exc}") from exc
    finally:
        con.close()


def _jsonable(value: Any) -> Any:
    # NaN and infinity have no JSON form: a preview row holding one made the
    # whole reply unreadable to the browser, so the file seemed not to open.
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
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
    cancel: Any = None,
) -> Dataset:
    """Read ``path`` into a new table and register it on the workspace.

    ``progress`` is an optional callable taking ``(phase, detail)`` so long loads
    can narrate themselves honestly rather than animating a fake percentage; a
    load that can measure itself (an Excel sheet) also passes a third argument,
    the percent done. ``cancel`` is an optional event: once it is set, an Excel
    load stops at its next check and raises :class:`IngestCancelled`.
    """
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise IngestError(f"There is no file at {file_path}")

    kind = kind or classify_path(file_path)
    table = workspace.new_table_name()
    started = time.time()
    notes: list[str] = []
    warnings: list[str] = []

    def say(phase: str, detail: str = "", percent: float | None = None) -> None:
        if not progress:
            return
        if percent is None:
            progress(phase, detail)
        else:
            progress(phase, detail, percent)

    say("reading", f"Reading {file_path.name}")

    if kind == "excel":
        _ingest_excel(
            workspace, file_path, table,
            sheet=sheet, has_header=has_header, all_varchar=all_varchar,
            notes=notes, say=say, cancel=cancel,
        )
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
            warnings=warnings,
        )

    say("indexing", "Working out column types")
    columns = workspace.describe_table(table)
    row_count = workspace.count_rows(table)

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
    warnings: list[str],
    date_format: str | None = None,
) -> None:
    quoted = ident(table)
    if kind == "parquet":
        sql = f"CREATE OR REPLACE TABLE {quoted} AS SELECT * FROM read_parquet(?)"
        params: list[Any] = [glob_escape(path)]
    elif kind == "json":
        sql = f"CREATE OR REPLACE TABLE {quoted} AS SELECT * FROM read_json_auto(?)"
        params = [glob_escape(path)]
    else:
        _ingest_csv(
            workspace, path, table,
            delimiter=delimiter, encoding=encoding,
            has_header=has_header, all_varchar=all_varchar, notes=notes,
            warnings=warnings, date_format=date_format,
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
    warnings: list[str],
    date_format: str | None = None,
) -> None:
    """Settle the encoding and the container, then hand off to the reader."""
    encoding = encoding or detect_encoding(path)
    delimiter = delimiter or sniff_delimiter(_read_text_head(path, encoding, 131_072))

    if encoding not in DUCKDB_ENCODINGS:
        notes.append(f"“{encoding}” is not a supported encoding; reading as Latin-1.")
        encoding = "latin-1"

    # DuckDB reads .gz and .zst itself but not .zip, so a zip is unpacked to the
    # workspace first. The extracted copy is removed once the table is built.
    extracted: Path | None = None
    if path.suffixes and path.suffixes[-1].lower() == ".zip":
        extracted = _extract_zip(workspace, path, notes)
        path = extracted

    try:
        _read_csv_with_escalation(
            workspace, path, table,
            delimiter=delimiter, encoding=encoding, has_header=has_header,
            all_varchar=all_varchar, notes=notes, warnings=warnings,
            date_format=date_format,
        )
    finally:
        if extracted is not None:
            try:
                extracted.unlink()
            except OSError:
                pass


def _extract_zip(workspace: Workspace, path: Path, notes: list[str]) -> Path:
    """Unpack the first data file from a .zip into the workspace."""
    try:
        with zipfile.ZipFile(path) as archive:
            members = [n for n in archive.namelist() if not n.endswith("/")]
            if not members:
                raise IngestError("That .zip archive is empty.")
            readable = [n for n in members if classify_path(Path(n)) != "excel"] or members
            chosen = readable[0]
            if len(members) > 1:
                notes.append(
                    f"The archive holds {len(members)} files; opened “{chosen}”."
                )
            # Unique per extraction: two opens running at once may unpack
            # members with the same name, and each deletes its copy when done.
            target = workspace.spill_dir / (
                f"unzipped-{os.getpid()}-{uuid.uuid4().hex[:8]}-{Path(chosen).name}"
            )
            with archive.open(chosen) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink, length=1024 * 1024)
            return target
    except zipfile.BadZipFile as exc:
        raise IngestError("That .zip archive could not be opened.") from exc


def _read_csv_with_escalation(
    workspace: Workspace,
    path: Path,
    table: str,
    *,
    delimiter: str,
    encoding: str,
    has_header: bool,
    all_varchar: bool,
    notes: list[str],
    warnings: list[str],
    date_format: str | None,
) -> None:
    """Read a delimited file, escalating through progressively safer readers.

    Real exports break in a few predictable ways and each remedy costs
    something, so each is applied only once it is needed:

    1. The fast path — DuckDB's parallel scanner, inferring types from a sample.
    2. Types from every row — for a column whose type changes far enough into
       the file that the sample missed it (an ``N/A`` at row 300,000). Only that
       column becomes text.
    3. Ragged rows — ``null_padding`` gives a short row empty cells and keeps a
       long row's extra values in added columns, but DuckDB cannot combine it
       with the parallel scanner when cells contain quoted newlines, so this
       step also drops to a single-threaded read.
    4. Everything as text.
    5. Last resort — load every row that parses, and say how many did not and
       on which lines.

    No step drops a row without saying so. The first attempt used to set
    ``ignore_errors``, which "succeeded" by skipping every row that did not fit
    — a 300,002-row file loaded as 300,000 with nothing said — so the remedies
    below it never ran.

    Each escalation is recorded in ``notes``, and any rows that could not be
    read in ``warnings``, so the user is told what happened instead of quietly
    receiving a different result.
    """
    base = ["header = ?", "delim = ?", "encoding = ?"]
    if date_format:
        base = base + [f"dateformat = {_literal(date_format)}",
                       f"timestampformat = {_literal(date_format)}"]
    sampled = base + ["sample_size = 262144"]
    every_row = base + ["sample_size = -1"]
    ragged = every_row + ["null_padding = true", "parallel = false"]
    as_text = ragged + ["all_varchar = true"]
    ragged_note = (
        "Some rows had a different number of values than the header. Missing "
        "values were left empty, and extra values were kept in added columns."
    )
    text_note = "Some columns held a mix of types, so every column was read as text."
    attempts: list[tuple[list[str], str | None]] = [
        (sampled, None),
        (every_row, "A column changed type far into the file, so column types were "
                    "worked out from every row rather than from the first ones."),
        (ragged, ragged_note),
        (as_text, text_note),
    ]
    if all_varchar:
        attempts = [(sampled + ["all_varchar = true"], None), (as_text, ragged_note)]

    params: list[Any] = [glob_escape(path), has_header, delimiter, encoding]
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

    # No reader took every row. Rather than refuse the whole file, keep what
    # parses and account for the rest by count and line number. The rejects
    # table is temporary and belongs to this cursor, so it is read before the
    # cursor closes.
    cur = workspace.cursor()
    try:
        cur.execute(
            f"CREATE OR REPLACE TABLE {ident(table)} AS "
            f"SELECT * FROM read_csv(?, {', '.join(as_text + ['store_rejects = true'])})",
            params,
        )
        skipped, lines = cur.execute(
            "SELECT count(*), list(line ORDER BY line)[1:5] "
            "FROM (SELECT DISTINCT line FROM reject_errors)"
        ).fetchone()
    except duckdb.Error as exc:
        raise IngestError(_friendly_duckdb_error(last or exc)) from exc
    finally:
        try:
            cur.close()
        except duckdb.Error:
            pass

    notes.append(text_note)
    if skipped:
        shown = ", ".join(f"{n:,}" for n in lines or []) + (", …" if skipped > len(lines or []) else "")
        warnings.append(
            f"{skipped:,} row{'s' if skipped != 1 else ''} could not be read and "
            f"{'were' if skipped != 1 else 'was'} left out (line {shown}). Everything "
            "else was loaded; check those lines in a text editor."
        )


def _ingest_excel(
    workspace: Workspace,
    path: Path,
    table: str,
    *,
    sheet: str | None,
    has_header: bool,
    all_varchar: bool = False,
    notes: list[str],
    say: Any = None,
    cancel: Any = None,
) -> None:
    """Load one sheet of a workbook as a table of text, then retype it.

    The sheet is streamed row by row into a temporary file that DuckDB reads in
    one bulk step. The first version bound every cell as a query parameter, and
    DuckDB 1.5 tries — and fails — to import pandas twice for each one: about a
    millisecond per cell, so a 10,000 x 10 sheet took 106 seconds and a full one
    hours, with the progress bar frozen and Cancel ignored. The names, text and
    types that come out are exactly those the cell-by-cell version produced; only
    the way the rows travel changed.
    """
    from python_calamine import CalamineWorkbook

    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise IngestCancelled()

    def report(detail: str, percent: float) -> None:
        if say:
            say("loading", detail, percent)

    workbook = _calamine(lambda: CalamineWorkbook.from_path(str(path)),
                         "That workbook could not be opened")
    sheet_names = list(workbook.sheet_names)
    sheet_name = sheet or (sheet_names[0] if sheet_names else None)
    if sheet_name is None:
        raise IngestError("That workbook has no sheets.")
    if sheet_name not in sheet_names:
        raise IngestError(f"There is no sheet called “{sheet_name}” in that workbook.")
    data = _calamine(lambda: workbook.get_sheet_by_name(sheet_name),
                     f"Sheet “{sheet_name}” could not be read")
    check()
    # iter_rows panics on a sheet with no cells at all, so answer that here.
    if not data.height:
        raise IngestError(f"Sheet “{sheet_name}” is empty.")

    # A value no cell can hold, standing for "no value": each field is written
    # quoted, and DuckDB reads a quoted "" back as an empty string (which is what
    # a blank cell always became) and this marker as NULL.
    null = f"grande-null-{uuid.uuid4().hex}"
    spill = workspace.spill_dir / f"excel-{os.getpid()}-{uuid.uuid4().hex[:8]}.csv"
    expected = max(1, data.start[0] + data.height)  # iter_rows starts at the sheet's top row
    names: list[str] | None = None
    width = written = seen = longest = 0
    cur = workspace.cursor()
    created = False
    try:
        with open(spill, "w", encoding="utf-8", errors="replace", newline="") as handle:
            out = csv.writer(handle, quoting=csv.QUOTE_ALL, lineterminator="\n")
            last_report = time.monotonic()

            def stream() -> None:
                nonlocal names, width, written, seen, longest, last_report
                for row in data.iter_rows():
                    seen += 1
                    if seen % 1000 == 0:
                        check()
                        now = time.monotonic()
                        if now - last_report >= EXCEL_PROGRESS_SECONDS:
                            last_report = now
                            report(f"Reading row {seen:,} of {expected:,}",
                                   12 + 58 * min(1.0, seen / expected))
                    if _is_blank(row):
                        continue
                    if names is None:
                        if has_header:
                            names = _unique_names(
                                [str(c).strip() or f"column{i + 1}" for i, c in enumerate(row)]
                            )
                            width = len(names)
                            continue
                        names = [f"column{i + 1}" for i in range(len(row))]
                        width = len(names)
                    # Trim and pad to the header's width, as before, and turn each
                    # cell into the same text _excel_cell always produced.
                    cells = [null if v is None else v if type(v) is str else _excel_cell(v)
                             for v in row[:width]]
                    if len(cells) < width:
                        cells.extend([null] * (width - len(cells)))
                    out.writerow(cells)
                    written += 1
                    size = sum(map(len, cells))
                    if size > longest:
                        longest = size

            _calamine(stream, f"Sheet “{sheet_name}” could not be read")

        if names is None:
            raise IngestError(f"Sheet “{sheet_name}” is empty.")
        check()

        columns_sql = ", ".join(f"{ident(n)} VARCHAR" for n in names)
        cur.execute(f"CREATE OR REPLACE TABLE {ident(table)} ({columns_sql})")
        created = True
        if written:
            report(f"Loading {written:,} rows", 72)
            spec = ", ".join(f"'c{i}': 'VARCHAR'" for i in range(width))
            options = [
                "header = false", "auto_detect = false", "delim = ','", "quote = '\"'",
                "escape = '\"'", "encoding = 'utf-8'", f"nullstr = {_literal(null)}",
                "allow_quoted_nulls = true", "strict_mode = true",
                # Cells may hold newlines; the single-threaded reader is the one
                # that is never confused by them.
                "parallel = false", f"columns = {{{spec}}}",
            ]
            # UTF-8 is at most four bytes a character; add the quotes and commas.
            line_bytes = longest * 4 + width * 3 + 16
            if line_bytes > 2_000_000:
                options.append(f"max_line_size = {line_bytes}")
                options.append(f"buffer_size = {line_bytes * 2}")
            cur.execute(
                f"INSERT INTO {ident(table)} SELECT * FROM read_csv(?, {', '.join(options)})",
                [glob_escape(spill)],
            )
            loaded = cur.execute(f"SELECT count(*) FROM {ident(table)}").fetchone()[0]
            if loaded != written:
                raise IngestError(
                    f"Only {loaded:,} of the sheet's {written:,} rows could be loaded, so "
                    "nothing was kept. Save the sheet as CSV and open that instead."
                )
        check()
        if not all_varchar:
            report("Working out column types", 74)
            # Everything arrived as text; let DuckDB re-infer real types where it can.
            _retype_in_place(cur, table, names, cancel=cancel)
    except BaseException as exc:
        if created:
            try:
                cur.execute(f"DROP TABLE IF EXISTS {ident(table)}")
            except duckdb.Error:
                pass
        if isinstance(exc, duckdb.Error):
            raise IngestError(_friendly_duckdb_error(exc)) from exc
        raise
    finally:
        cur.close()
        try:
            spill.unlink()
        except OSError:
            pass

    notes.append(f"Loaded sheet “{sheet_name}”.")
    if len(sheet_names) > 1:
        notes.append(f"Other sheets available: {', '.join(n for n in sheet_names if n != sheet_name)}.")


def _excel_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, str)):
        return str(value)
    return str(value)


#: A column may take a type only if no value changes on the way. DuckDB's casts
#: are more forgiving than that — TRY_CAST('1.25' AS BIGINT) is 1, and
#: TRY_CAST('2024-03-15 10:30' AS DATE) drops the time — so every decimal column
#: of every workbook was rounded to whole numbers, and every date-and-time column
#: lost its times. Each target carries the test for a value it would alter.
_LOSSY = {
    # Whole numbers only, written plainly: Excel's 5.0 yes, 1.25 no.
    "BIGINT": r"NOT regexp_matches({c}, '^\s*[+-]?[0-9]+(\.0*)?\s*$')",
    # A double holds 17 significant digits. A longer number is an identifier,
    # and making it a double would quietly change its last digits.
    "DOUBLE": r"length(ltrim(replace(regexp_extract({c}, '^\s*[+-]?([0-9]*\.?[0-9]*)', 1), '.', ''), '0')) > 17",
    # A date only when there is no time of day to lose.
    "DATE": "TRY_CAST({c} AS TIMESTAMP) IS DISTINCT FROM CAST(TRY_CAST({c} AS DATE) AS TIMESTAMP)",
    "TIMESTAMP": "FALSE",
}


def _retype_in_place(
    cur: duckdb.DuckDBPyConnection, table: str, names: list[str], *, cancel: Any = None
) -> None:
    """Promote all-text columns to numbers/dates when every value converts losslessly.

    Done per column so one messy column cannot force the whole sheet back to text.
    """
    quoted = ident(table)
    for name in names:
        if cancel is not None and cancel.is_set():
            raise IngestCancelled()
        col = ident(name)
        for target in ("BIGINT", "DOUBLE", "DATE", "TIMESTAMP"):
            lossy = _LOSSY[target].format(c=col)
            try:
                row = cur.execute(
                    f"SELECT count(*) FROM {quoted} WHERE {col} IS NOT NULL "
                    f"AND (TRY_CAST({col} AS {target}) IS NULL OR {lossy})"
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
    """Make header names unique the way a spreadsheet would: name, name_2, name_3.

    Compared without regard to case, because DuckDB column names are
    case-insensitive: headers "ID" and "Id" made CREATE TABLE fail, and so did
    "a", "a", "a_2", whose generated second name collided with the third.
    Names that were already distinct come out unchanged.
    """
    taken: set[str] = set()
    out: list[str] = []
    for name in names:
        candidate, n = name, 1
        while candidate.lower() in taken:
            n += 1
            candidate = f"{name}_{n}"
        taken.add(candidate.lower())
        out.append(candidate)
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

"""Opening the containers and encodings the README promises.

Every case here was broken while the README advertised it. The encoding one is
the worst: DuckDB's CSV reader rejects the name "windows-1252" outright, so
returning that from detection meant *no* non-UTF-8 file could be opened — and
a Windows export with £ or é in it is entirely ordinary.
"""

from __future__ import annotations

import csv
import datetime
import gzip
import threading
import time
import zipfile

import pytest
import xlsxwriter

from grande.engine import query as query_engine
from grande.engine.ingest import (
    DUCKDB_ENCODINGS,
    IngestCancelled,
    IngestError,
    detect_encoding,
    ingest,
    sniff,
)
from grande.engine.session import Workspace

ROWS = [[i, ["North", "South", "East"][i % 3], round(i * 1.5, 2)] for i in range(300)]


@pytest.fixture
def plain(tmp_path):
    path = tmp_path / "data.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["id", "region", "amount"])
        writer.writerows(ROWS)
    return path


@pytest.fixture
def workspace(tmp_path):
    ws = Workspace(tmp_path / "ws")
    yield ws
    ws.close()


def test_gzipped_csv(workspace, plain, tmp_path):
    path = tmp_path / "data.csv.gz"
    with open(plain, "rb") as source, gzip.open(path, "wb") as sink:
        sink.write(source.read())
    dataset = ingest(workspace, path)
    assert dataset.row_count == len(ROWS)
    assert [c.name for c in dataset.columns] == ["id", "region", "amount"]


def test_zipped_csv(workspace, plain, tmp_path):
    """DuckDB cannot read .zip at all, so it has to be unpacked first."""
    path = tmp_path / "data.csv.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(plain, "data.csv")
    dataset = ingest(workspace, path)
    assert dataset.row_count == len(ROWS)


def test_zip_with_several_members_says_which_it_opened(workspace, plain, tmp_path):
    path = tmp_path / "many.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(plain, "first.csv")
        archive.write(plain, "second.csv")
    dataset = ingest(workspace, path)
    assert dataset.row_count == len(ROWS)
    assert any("first.csv" in n for n in dataset.notes), dataset.notes


def test_empty_zip_is_refused_kindly(workspace, tmp_path):
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w"):
        pass
    with pytest.raises(IngestError):
        ingest(workspace, path)


def test_compression_is_seen_through_when_detecting_encoding(plain, tmp_path):
    """Sniffing the container rather than the content is what broke .gz/.zip.

    Compressed bytes are not valid UTF-8, so every compressed file was detected
    as a legacy encoding and then refused.
    """
    gz = tmp_path / "d.csv.gz"
    with open(plain, "rb") as source, gzip.open(gz, "wb") as sink:
        sink.write(source.read())
    zp = tmp_path / "d.csv.zip"
    with zipfile.ZipFile(zp, "w") as archive:
        archive.write(plain, "d.csv")

    assert detect_encoding(gz) == "utf-8"
    assert detect_encoding(zp) == "utf-8"


def test_windows_1252_file_opens_with_its_characters_intact(workspace, tmp_path):
    path = tmp_path / "legacy.csv"
    path.write_bytes(
        "id,name,price\n1,café crème,£2.50\n2,naïve,£1.00\n".encode("cp1252")
    )
    assert detect_encoding(path) == "cp1252"

    dataset = ingest(workspace, path)
    rows = query_engine.page(workspace, dataset, limit=5)["rows"]
    names = {r[1] for r in rows}
    prices = {r[2] for r in rows}
    assert "café crème" in names and "naïve" in names
    assert "£2.50" in prices


def test_detection_only_returns_names_duckdb_accepts(tmp_path):
    """The reader rejects 'windows-1252' and 'iso-8859-1'; only four names work."""
    samples = {
        "utf8.csv": "a,b\né,ü\n".encode("utf-8"),
        "bom.csv": b"\xef\xbb\xbf" + "a,b\n1,2\n".encode("utf-8"),
        "cp1252.csv": "a,b\ncafé,£1\n".encode("cp1252"),
        "utf16.csv": "a,b\n1,2\n".encode("utf-16"),
    }
    for name, data in samples.items():
        path = tmp_path / name
        path.write_bytes(data)
        assert detect_encoding(path) in DUCKDB_ENCODINGS, name


def test_sniff_reports_the_compressed_content(plain, tmp_path):
    path = tmp_path / "d.csv.gz"
    with open(plain, "rb") as source, gzip.open(path, "wb") as sink:
        sink.write(source.read())
    result = sniff(path)
    assert result.kind == "csv"
    assert [c["name"] for c in result.columns] == ["id", "region", "amount"]


# ------------------------------------------------- rows are never lost quietly


def _rows(workspace, dataset):
    return workspace.execute(f"SELECT * FROM {dataset.relation}")


def test_a_row_with_an_extra_value_is_kept(workspace, tmp_path):
    """Regression: ignore_errors on the first read skipped the row, silently."""
    path = tmp_path / "extra.csv"
    path.write_text("name,amount\nalpha,10\nbeta,20,EXTRA\ngamma,30\n", encoding="utf-8")
    dataset = ingest(workspace, path)
    assert dataset.row_count == 3
    assert ("beta", 20, "EXTRA") in _rows(workspace, dataset)
    assert any("different number of values" in n for n in dataset.notes), dataset.notes


def test_a_short_row_is_padded_not_dropped(workspace, tmp_path):
    path = tmp_path / "short.csv"
    path.write_text("a,b,c\n1,2,3\n4,5\n6,7,8\n", encoding="utf-8")
    dataset = ingest(workspace, path)
    assert sorted(_rows(workspace, dataset)) == [(1, 2, 3), (4, 5, None), (6, 7, 8)]


def test_a_late_type_change_keeps_every_row(workspace, tmp_path):
    """An N/A beyond the type sample used to cost its row; now only that column
    becomes text, and the other keeps its type."""
    path = tmp_path / "late.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        handle.write("id,amount\n")
        handle.writelines(f"{i},{i}\n" for i in range(270_000))
        handle.write("270000,N/A\n")
    dataset = ingest(workspace, path)
    assert dataset.row_count == 270_001
    kinds = {c.name: c.kind for c in dataset.columns}
    assert kinds == {"id": "number", "amount": "text"}


def test_unreadable_rows_are_counted_and_named(workspace, tmp_path):
    """When a row truly cannot be parsed, the rest loads and the loss is said."""
    path = tmp_path / "bad.csv"
    path.write_bytes(b"a,b\n1,ok\n2,bad \xff\xfe byte\n3,fine\n")
    dataset = ingest(workspace, path, encoding="utf-8")
    assert dataset.row_count == 2
    assert any("1 row could not be read" in w and "line 3" in w for w in dataset.warnings), \
        dataset.warnings


# ------------------------------------------------ names DuckDB reads as globs


def test_bracketed_file_name_opens_that_file_not_its_neighbours(workspace, tmp_path):
    """Regression: DuckDB globbed 'report[2024].csv' and read report2.csv — or
    report2.csv and report0.csv concatenated — whenever they were beside it."""
    real = tmp_path / "report[2024].csv"
    real.write_text("a,b\n1,100\n2,200\n", encoding="utf-8")
    (tmp_path / "report2.csv").write_text("a,b\n9,999\n", encoding="utf-8")
    (tmp_path / "report0.csv").write_text("a,b\n5,555\n", encoding="utf-8")
    dataset = ingest(workspace, real)
    assert sorted(_rows(workspace, dataset)) == [(1, 100), (2, 200)]


def _book(path, rows, sheet="Data", start_row=0, start_col=0):
    """Write rows to an .xlsx, typing each cell the way Excel would store it."""
    book = xlsxwriter.Workbook(str(path), {"strings_to_numbers": False, "strings_to_formulas": False,
                                           "strings_to_urls": False})
    ws = book.add_worksheet(sheet)
    stamp = book.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
    day = book.add_format({"num_format": "yyyy-mm-dd"})
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            at = (start_row + r, start_col + c)
            if value is None:
                continue
            if isinstance(value, datetime.datetime):
                ws.write_datetime(*at, value, stamp)
            elif isinstance(value, datetime.date):
                ws.write_datetime(*at, value, day)
            elif isinstance(value, str):
                ws.write_string(*at, value)
            else:
                ws.write(*at, value)
    book.close()
    return path


def test_excel_keeps_every_value_exactly(workspace, tmp_path):
    """Regression: TRY_CAST('1.25' AS BIGINT) is 1 and a timestamp cast to DATE
    drops its time, so every decimal column was rounded to whole numbers and
    every date-and-time column lost its times — silently, on open."""
    when = datetime.datetime(2024, 3, 15, 10, 30, 5)
    path = _book(tmp_path / "exact.xlsx", [
        ["price", "refund", "rate", "when", "day", "order_id", "account", "note", "qty"],
        [2500.0, -1500.5, 0.125, when, datetime.date(2024, 3, 15), "12345678901234567890", "007",
         'says "hi", twice\nthen stops', 3],
        [1000.25, -250.25, 1.5, when.replace(hour=23), datetime.date(1999, 12, 31), "98765432109876543210",
         "0042", "  padded  ", 4],
        [3.75, 0.0, 2.0, when.replace(second=0), datetime.date(2024, 2, 29), "11111111111111111111", "10",
         "café £ 😀", 5],
    ])
    ds = ingest(workspace, path)
    types = {c.name: c.type for c in ds.columns}
    assert types == {"price": "DOUBLE", "refund": "DOUBLE", "rate": "DOUBLE", "when": "TIMESTAMP",
                     "day": "DATE", "order_id": "VARCHAR", "account": "VARCHAR", "note": "VARCHAR",
                     "qty": "BIGINT"}
    rows = workspace.execute(f"SELECT * FROM {ds.relation} ORDER BY rowid")
    assert [r[0] for r in rows] == [2500.0, 1000.25, 3.75]
    assert [r[1] for r in rows] == [-1500.5, -250.25, 0.0]
    assert [r[2] for r in rows] == [0.125, 1.5, 2.0]
    assert [r[3] for r in rows] == [when, when.replace(hour=23), when.replace(second=0)]
    assert [r[5] for r in rows] == ["12345678901234567890", "98765432109876543210", "11111111111111111111"]
    assert [r[6] for r in rows] == ["007", "0042", "10"]
    assert [r[7] for r in rows] == ['says "hi", twice\nthen stops', "  padded  ", "café £ 😀"]


def test_excel_headers_that_differ_only_in_case(workspace, tmp_path):
    """DuckDB names are case-insensitive: "ID" and "Id" made CREATE TABLE fail."""
    path = _book(tmp_path / "case.xlsx", [["ID", "Id", "a", "a", "a_2"], [1, 2, 3, 4, 5]])
    ds = ingest(workspace, path)
    assert [c.name for c in ds.columns] == ["ID", "Id_2", "a", "a_2", "a_2_2"]
    assert workspace.execute(f"SELECT * FROM {ds.relation}") == [(1, 2, 3, 4, 5)]


def test_excel_read_every_column_as_text(workspace, tmp_path):
    """The option was ignored for workbooks."""
    path = _book(tmp_path / "text.xlsx", [["n", "d"], [1.5, datetime.date(2024, 1, 2)]])
    ds = ingest(workspace, path, all_varchar=True)
    assert {c.type for c in ds.columns} == {"VARCHAR"}


def test_excel_sheet_that_starts_part_way_down(workspace, tmp_path):
    path = _book(tmp_path / "offset.xlsx", [["id", "name"], [1, "one"], [None, None], [2, "two"]],
                 start_row=3, start_col=2)
    preview = sniff(path)
    assert [c["name"] for c in preview.columns] == ["id", "name"]
    assert preview.sample_rows == [[1.0, "one"], [2.0, "two"]]
    ds = ingest(workspace, path)
    assert workspace.execute(f"SELECT * FROM {ds.relation} ORDER BY rowid") == [(1, "one"), (2, "two")]


def test_excel_empty_sheets_say_so(workspace, tmp_path):
    book = xlsxwriter.Workbook(str(tmp_path / "empty.xlsx"))
    book.add_worksheet("Nothing")
    blank = book.add_worksheet("Blank")
    blank.write_string(0, 0, "   ")
    book.close()
    for sheet in ("Nothing", "Blank"):
        with pytest.raises(IngestError, match="is empty"):
            ingest(workspace, tmp_path / "empty.xlsx", sheet=sheet)
    with pytest.raises(IngestError, match="no sheet called"):
        ingest(workspace, tmp_path / "empty.xlsx", sheet="Missing")


def _big_book(path, rows=40_000):
    book = xlsxwriter.Workbook(str(path), {"constant_memory": True})
    ws = book.add_worksheet("Data")
    ws.write_row(0, 0, [f"col{c}" for c in range(10)])
    for r in range(1, rows + 1):
        ws.write_row(r, 0, [r, f"name {r}", r * 1.25, "North", r % 7, "x", r * 0.5, "y", r % 3, "z"])
    book.close()
    return path


def test_excel_load_is_quick_and_reports_progress(workspace, tmp_path):
    """Regression: every cell was bound as a query parameter, about a millisecond
    each, so this 400,000-cell sheet took over six minutes with no progress."""
    path = _big_book(tmp_path / "big.xlsx")
    events = []
    started = time.monotonic()
    ds = ingest(workspace, path, progress=lambda *event: events.append(event))
    assert time.monotonic() - started < 60
    assert ds.row_count == 40_000
    assert workspace.execute(f"SELECT sum(col2) FROM {ds.relation}") == [(sum(r * 1.25 for r in range(1, 40_001)),)]
    percents = [e[2] for e in events if len(e) == 3]
    assert percents and percents == sorted(percents)
    assert not list(workspace.spill_dir.glob("excel-*")), "temporary file left behind"


def test_excel_load_can_be_cancelled(workspace, tmp_path):
    path = _big_book(tmp_path / "big.xlsx")
    cancel = threading.Event()

    def progress(phase, detail, percent=None):
        if percent is not None:
            cancel.set()

    before = {r[0] for r in workspace.execute("SELECT table_name FROM duckdb_tables()")}
    with pytest.raises(IngestCancelled):
        ingest(workspace, path, progress=progress, cancel=cancel)
    after = {r[0] for r in workspace.execute("SELECT table_name FROM duckdb_tables()")}
    assert after == before, "a cancelled load left a table behind"
    assert workspace.list() == []
    assert not list(workspace.spill_dir.glob("excel-*")), "temporary file left behind"


def test_bracketed_parquet_name_opens_that_file(workspace, tmp_path):
    real = tmp_path / "data[1].parquet"
    decoy = tmp_path / "data1.parquet"
    for path, value in ((real, 1), (decoy, 999)):
        workspace.execute(
            f"COPY (SELECT {value} AS v) TO '{path.as_posix()}' (FORMAT parquet)"
        )
    assert real.exists() and decoy.exists()
    assert [c["name"] for c in sniff(real).columns] == ["v"]
    dataset = ingest(workspace, real)
    assert _rows(workspace, dataset) == [(1,)]

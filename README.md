# Grande

**Work with spreadsheets that are too big for Excel — on your own machine, offline.**

Excel stops at 1,048,576 rows. Plenty of everyday exports are bigger than that,
and when you open one Excel either refuses, or truncates it silently, or takes
the account number `07001` and turns it into `7001`.

Grande opens the file, lets you sort, filter, pivot and clean it at full size,
and writes it back out — including as a set of Excel workbooks split so each one
fits.

Nothing is uploaded. The server binds to `127.0.0.1`, no request leaves the
machine, and there is no account, no telemetry and no network call after install.

---

## Install and run

```bash
pip install grande
grande
```

Your browser opens on a local address. That is the whole setup.

To open a file straight away:

```bash
grande "C:\exports\transactions_2024.csv"
```

Requires Python 3.10 or newer. On first launch Grande will use DuckDB's Excel
writer if it is available, which is about three times faster; if it is not, it
falls back to a pure-Python writer and everything still works offline.

---

## What it does

**Opens what you actually have.** CSV, TSV, Excel (`.xlsx`, `.xlsm`, `.xls`,
`.ods`), Parquet, JSON and JSON Lines — plus `.gz` and `.zip`. It guesses the
separator, encoding and header row, shows you what it guessed, and lets you
correct it before loading.

**Browses the whole file.** The grid is windowed, so scrolling through forty
million rows is smooth. Sorting and filtering happen in the database over every
row — not over the first screenful.

**Filters like a spreadsheet.** Click a column header for a value checklist with
counts, or a range, or contains / starts with / is empty. Filters combine, show
as removable chips, and apply everywhere — the status bar, the pivot, the export.

**Shows you each column.** Every header carries a small distribution strip. Open
a column for its type, fill rate, distinct count, min, max, median and most
common values — computed across all rows.

**Pivots the way Excel does.** The PivotTable is built to the same model, with
the same vocabulary and the same gestures, so anyone who has used one already
knows how to drive it:

- A field list with checkboxes, and four areas — **Filters**, **Columns**,
  **Rows**, **Values** — laid out as Excel lays them out.
- Drag fields into an area, between areas, and to reorder within one. Every
  field also has a menu, so none of it depends on being able to drag.
- **Value Field Settings**, with Excel's "Summarize Values By" (Sum, Count,
  Average, Max, Min, Count Numbers, Distinct Count, Median, StdDev) and its
  "Show Values As" (% of Grand Total, % of Column Total, % of Row Total,
  Running Total, Rank).
- Several row and column fields, nested. Compact, Outline and Tabular layouts.
- Expand and collapse on every group, plus Expand All / Collapse All.
- Row Labels, Column Labels, subtotals and Grand Total in the usual places.
- Double-click any figure for **Show Details** — the rows behind it.
- **Grouping**: dates by year, quarter, month, week, day or hour, and numbers
  into bands. Drop a date into Rows and it groups by month straight away, so
  "sales by month" is one action rather than 1,095 daily rows.

Subtotals and grand totals are produced by `GROUP BY ROLLUP` from the underlying
rows, so they are right for every aggregation — an average subtotal is the
average of those rows, not the average of the averages above it.

**Cleans up.** Remove duplicates, find and replace, trim and re-case text, split
a column, change a type, rename, delete columns, fill blanks, or turn the current
filter into a permanent edit. Every step is listed and every step can be undone.

**Exports.** Excel (split automatically so no file exceeds the row limit), CSV,
TSV, Parquet or JSON Lines. Choose the rows per file and the naming style —
`report_a.xlsx, report_b.xlsx`, or `_1`, or `_part_1`, or `_001`. Before it runs,
it tells you how many files you are about to get and what they will be called.

**Has an escape hatch.** A read-only SQL console over your data, for when you
already know what you want to ask.

---

## Speed

Measured on a 14-core laptop, 1,000,000 rows × 12 columns (114 MB CSV), writing
multipart `.xlsx`:

| | Time | Rows/sec |
|---|---|---|
| The previous version of Grande | 86.9 s | 11,509 |
| `polars.write_excel` | 79.5 s | 12,581 |
| Grande 2 — portable writer, 8 processes | **19.0 s** | **53,565** |
| Grande 2 — DuckDB writer, 4 processes | **6.2 s** | **169,396** |

A multipart export is embarrassingly parallel — each workbook is independent —
so the current view is written once to a staging file and the parts are produced
in separate processes.

Reading is the same story: the file is parsed **once**, into a local DuckDB
table. Everything after that is a query against that table, so changing a pivot
dropdown is instant instead of re-reading the file.

---

## Your data comes back unchanged

This is the part Grande exists for, so it is worth being precise.

**Types are decided per column, not per cell.** A column DuckDB reads as a
number is written to Excel as a number in every row; a column read as text keeps
its leading zeros in every row.

This matters more than it sounds. The previous version checked each cell
individually and forced anything starting with `-` to text, because `-` can
begin a formula. The result: in a revenue column, `2500` was written as a number
and `-1500.00` was written as *text*, so `SUM` in Excel silently skipped every
refund. Deciding at the column level makes that class of bug impossible.

**Long identifiers stay exact.** Excel keeps 15 digits of precision, so an
18-digit order ID becomes `8.93123E+17` with the tail zeroed. Grande writes those
columns as text. The same applies on the way to your browser: integers beyond
JavaScript's exact range are sent as strings rather than being quietly rounded.

**Formulas do not run.** A cell containing `=cmd|'/c calc'!A0` is written as a
text cell, so Excel displays it instead of executing it.

**Limits are reported, not hidden.** A cell longer than Excel's 32,767-character
limit is truncated — and the export tells you how many were affected instead of
losing the text quietly.

---

## Safety

- The server listens on `127.0.0.1` only, so nothing on the network can reach it.
- Every API call needs a token generated fresh at startup and sent in a custom
  header. A page on another origin cannot send that header without a CORS
  preflight, and Grande answers no preflights — so a web page you happen to have
  open cannot drive Grande even though it knows the port.
- The `Host` header is checked, which closes DNS rebinding.
- Column names and filter values are quoted or bound as parameters, never pasted
  into SQL. A CSV whose header row contains a quote character is just a column
  with an awkward name.
- The SQL console runs read-only and cannot reach the filesystem.
- Unexpected errors are logged to the console Grande was launched from, and the
  browser gets a short reference instead of a stack trace.

---

## For developers

```bash
git clone <this repo>
cd grande
pip install -e ".[dev]"
pytest
grande --no-browser --port 8422
```

```
src/grande/
  __main__.py          entry point: free port, session token, threaded server
  engine/
    sql.py             identifier quoting, aggregation allowlist
    filters.py         spreadsheet filters compiled to parameterised SQL
    session.py         workspace and dataset registry over one DuckDB database
    ingest.py          sniffing and loading, per format
    query.py           paging, sorting, filtering, summaries, column profiles
    pivot.py           ROLLUP pivot, percentages, drill-down
    transform.py       cleaning operations, each with an undo level
    export.py          parallel multipart writer, plus the flat formats
    jobs.py            background jobs with progress and cancellation
  web/
    server.py          the local HTTP API
    static/            no build step, no CDN, no npm — plain ES modules
```

The front end is deliberately dependency-free: vanilla ES modules, one
stylesheet, an inline SVG sprite. There is nothing to compile and nothing to
fetch at runtime.

---

## Relationship to the original

This is a rewrite of [ojashtripathi/Grande](https://github.com/ojashtripathi/Grande),
which had the right idea. `BUILD-NOTES.md` records what the audit of that version
found, with file and line references and reproductions — including the negative
numbers described above, a grid that held 1,000 rows behind a badge promising ten
million, progress bars that were animations rather than measurements, and grand
totals that were arithmetically wrong for four of the six aggregations offered.

## License

MIT.

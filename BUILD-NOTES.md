# Grande 2 — build notes

Working record so this can be resumed after an interruption. Written for whoever
picks this up next, including a future session with no memory of the first.

## What this is

A from-scratch rewrite of <https://github.com/ojashtripathi/Grande> — a local,
offline tool for working with tables too big for Excel. Same goal as the
original (open huge CSVs, pivot them, export multipart .xlsx), rebuilt because
the original's core promises did not hold up under inspection.

The measurements below used a 1,000,000-row benchmark CSV (114.5 MB, 12
columns); generate any similar file to repeat them. The original repository can
be cloned from the link above for comparison.

## What the audit found in the original

Verified by reading the code and by running it, not taken from its README.

| # | Finding | Evidence |
|---|---|---|
| 1 | **Negative numbers are written to Excel as text.** The formula-injection check (`=`,`+`,`-`,`@`) runs *before* numeric conversion, so `-1500.00` becomes a string while `2500` becomes a number. `SUM` in Excel silently skips the refunds. | Reproduced: `splitter.py:145`; wrote a CSV and read the workbook back — `B2` is `s` (string), `B3` is `n` (number) |
| 2 | **The grid only ever holds 1,000 rows.** The row-limit dropdown maxes at 1000, and sort/filter/stats are JavaScript over that slice — under a badge reading "Supports >10M+ rows". | `index.html:120-126`, `app.js:279-368` |
| 3 | **Both progress bars are fake.** SSE is documented and implemented server-side but never used by the frontend; the bars are `setInterval` animations. | `app.js` split/parquet progress; `/api/split_stream` unreferenced |
| 4 | **Grand totals are arithmetically wrong for 4 of 6 aggregators.** Totals are sums of the displayed cells, so AVG/MIN/MAX/COUNT DISTINCT totals are meaningless. | `app.js:909-984` |
| 5 | **High-cardinality categories are silently dropped.** Beyond 50 distinct values the rest vanish, so row totals no longer match the data. No UI ever shows the `cardinality_notice` the backend returns. | `pivot_engine.py:160-171` |
| 6 | **Identifier quoting is `f'"{col}"'`** with no doubling — a `"` in a CSV header rewrites the query. The same file does it correctly for aliases 20 lines away. | `pivot_engine.py:100` |
| 7 | **The parity hash proves less than it claims.** The "source" digest is computed from the writer's own post-transformation tokens, so anything the writer does consistently is invisible — a 40,000-char cell truncated to 32,767 still certifies as identical. | `splitter.py:259-262` vs `verifier.py:68-89` |
| 8 | **Float parity fails on ordinary money.** `10.50` is written as `10.5`, so the hash mismatches and the auto-correction loop (which re-runs the same coercion) cannot fix it. | `splitter.py:154-157` |
| 9 | **The CSV is re-parsed on every request.** Every pivot, every dropdown change, re-reads the whole file. | `pivot_engine.py:48,98,244` |
| 10 | **`fetchdf()` needs pandas**, which is not in `requirements.txt`. | `pivot_engine.py` |
| 11 | No auth or Origin check on a loopback server with a GET side-effect endpoint and arbitrary SQL — reachable by any page the user visits. | `app.py:53,143` |
| 12 | Dark theme only; 39 emoji as icons, 21 font sizes, 20 spacing values, 12 radii; `--text-muted` on `--bg-card` is 3.75:1 (below WCAG AA). | `style.css` |

## Measured performance (this machine, 14 cores, 1M rows x 12 cols)

| Approach | Time | rows/s |
|---|---|---|
| Original (single-threaded, per-cell heuristics) | 86.9 s | 11,509 |
| Tuned xlsxwriter, single process | 69.3 s | 14,441 |
| polars `write_excel` | 79.5 s | 12,581 |
| DuckDB Excel extension, single process | 22.8 s | 43,832 |
| **xlsxwriter across 8 processes** (+0.34 s staging) | **19.0 s** | **53,565** |
| **DuckDB Excel writer across 4 processes** (+0.34 s staging) | **6.2 s** | **169,396** |
| CSV → Parquet (zstd) | 0.62 s | 1,622,518 |

So: 4.6x faster on the portable path, 14x on the fast path.

## Architecture decisions

1. **Ingest once.** A file is read into a DuckDB table on open; every later
   operation queries that table. This is the single biggest change.
2. **Column-level typing, never per-cell.** Fixes finding #1 by construction: a
   numeric column is numeric for every row, a text column keeps leading zeros
   for every row.
3. **Totals from `GROUP BY ROLLUP`,** computed from base rows, so every subtotal
   and grand total is correct for every aggregator (finding #4).
4. **"Other" bucket** instead of dropping categories (finding #5).
5. **Generated columns aliased `v0`, `v1`, …**; labels travel back as data, so no
   file value ever becomes SQL (finding #6).
6. **Stage to Parquet, then write parts in parallel processes.** Sorting happens
   once during staging, so each part is an offset range.
7. **Per-run token + Host check** on the loopback server (finding #11).
8. **Honest progress**: determinate where we control the chunking (export),
   indeterminate-with-elapsed where we do not (DuckDB's CSV read). Never fake.

## Layout

```
grande/
  pyproject.toml
  BUILD-NOTES.md            <- this file
  src/grande/
    __init__.py             DONE
    __main__.py             DONE   entry point, free port, token, waitress
    engine/
      sql.py                DONE   identifier/literal quoting, aggregator allowlist
      filters.py            DONE   Excel-style AutoFilter -> parameterised SQL
      session.py            DONE   Workspace + Dataset registry, DuckDB tuning
      ingest.py             DONE   sniff + load CSV/TSV/Parquet/JSON/Excel/gz/zip
      query.py              DONE   paging, sort, filter, summary, column profile
      pivot.py              DONE   ROLLUP pivot, % of total/row/column, drill-down
      export.py             DONE   parallel multipart xlsx + csv/parquet/json
      jobs.py               DONE   background jobs with progress + cancellation
      transform.py          DONE   dedupe, replace, clean, split, cast, undo
    web/
      server.py             DONE   Flask app, token auth, file browser, SSE
      static/               DONE   index.html, css, js (vanilla ES modules)
  tests/test_engine.py      DONE   35 tests
  run.bat / run.sh          DONE   one-click launchers
```

## Status — v1 complete and verified

- [x] Audit the original (11 of 14 agents; splitter audit and critic skipped,
      their findings independently reproduced)
- [x] Benchmark the write path and validate the parallel architecture
- [x] `engine/` — sql, filters, session, ingest, query, pivot, export, jobs, transform
- [x] `web/server.py` — token-authenticated API, built-in file browser, SSE
- [x] Frontend — windowed grid, filters, pivot builder, clean, export, SQL, themes
- [x] 35 tests, including regressions for every defect listed above
- [x] README, launchers

### Measured end to end on the 1M-row benchmark

| Operation | Time |
|---|---|
| Open the 114 MB CSV | 1.46 s |
| Sorted page at row 500,000 (whole-file sort) | 0.12 s |
| Pivot, region x category, SUM revenue | 0.03 s |
| Column summary over all rows | 0.03 s |
| Export 1,000,000 rows to 4 .xlsx files | 4.2 s (238k rows/s) |

The pivot grand total (24,997,928,337.46) equals the independently computed
column sum exactly, and each row total equals the sum of its own cells.

## Defects found and fixed *in this rewrite* while testing it

Recorded because they are the same classes of bug the original had, and two of
them were introduced by my own optimisations.

1. **Excel precision loss, self-inflicted.** DuckDB types an 18-digit id as
   BIGINT and the fast writer emitted it numerically, so Excel would show
   `8.93123E+17`. Now any integer column whose values exceed 15 digits is
   written as text, checked against real values rather than the declared type,
   and reported. (`export.precision_risk_columns`)
2. **`preserve_insertion_order = false`, self-inflicted.** A genuine speed win
   that let DuckDB return rows in an order unrelated to the file — so "row 1"
   was not the first row, and "keep the first duplicate" kept an arbitrary one.
   Removed, and pinned by `test_unsorted_means_file_order`.
3. **Stack traces to the browser.** The catch-all handler returned the
   exception text, and job failures shipped a formatted traceback in the API
   response — the same CWE-209 pattern the original has in nine handlers.
   Internal faults are now logged locally and answered with a reference.
4. **SSE 500.** A `Connection: keep-alive` response header is hop-by-hop and
   forbidden by PEP 3333; waitress rejected every event-stream request.
5. **404s reported as 500s.** The catch-all swallowed werkzeug `HTTPException`s.
6. **CSV loads failing on quoted newlines.** `null_padding` cannot be combined
   with the parallel scanner when cells contain quoted newlines. Replaced with a
   three-step escalation that reports which step was needed.
7. **Refresh signed you out.** The token was stripped from the URL and not kept.
8. **Pivot fields reachable only by dragging** — no keyboard or touch path, and
   no way at all to reach the Columns shelf.
9. **`[hidden]` beaten by class `display` rules**, so the toolbar showed with no
   file open.

## Known gaps / next

From the competitive research, ranked, none of them started:

1. Join two files (the VLOOKUP replacement) — with a match-rate and
   duplicate-key warning, which no competing tool surfaces.
2. Append many files or a folder into one dataset (`union_by_name`).
3. Calculated columns — via a whitelisted expression grammar, never raw SQL.
4. ~~Pivot date grouping and numeric bins.~~ **Done.** Row and column fields
   now carry a grouping spec; dates group by year/quarter/month/week/day/hour
   and numbers into bands. Pinned by `test_group_dates_by_month`. The PivotTable
   UI was rebuilt at the same time to match Excel's: four areas, drag between
   them, Value Field Settings, compact/outline/tabular, expand and collapse,
   Show Details, and several nested row and column fields.
5. Multi-sheet workbook export and split-by-column-value.
6. Ingest: skip preamble rows / choose the header row.
7. Recent files, a project file, and `grande run project.json` for batch replay.
8. A data-health report on open (`SUMMARIZE`).

## To resume

```bash
cd grande
python -m venv .venv            # then activate it
python -m pip install -e ".[dev]"
python -m pytest
python -m grande
```

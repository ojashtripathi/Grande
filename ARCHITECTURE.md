# 🏛️ Grande: Systems Architecture & Engineering Specification (v2.0)

> Comprehensive technical specification, database storage model, parallel multiprocessing mechanics, and mathematical invariants for collaborative engineers and contributors.

---

## 📐 Table of Contents
1. [System Topology & High-Level Architecture](#1-system-topology--high-level-architecture)
2. [Ingestion & Storage Engine](#2-ingestion--storage-engine)
   - [The "Ingest Once" Paradigm](#the-ingest-once-paradigm)
   - [Format Coverage & Calamine Rust Reader](#format-coverage--calamine-rust-reader)
   - [Two-Stage Sniffing & 3-Tier Escalation](#two-stage-sniffing--3-tier-escalation)
   - [Multi-Instance Concurrency & File Locking](#multi-instance-concurrency--file-locking)
3. [Parallel Multipart Export Engine](#3-parallel-multipart-export-engine)
   - [Parquet Staging View](#parquet-staging-view)
   - [Dual-Path Multiprocessing Pipeline](#dual-path-multiprocessing-pipeline)
   - [Column-Level Typing vs Per-Cell Heuristics](#column-level-typing-vs-per-cell-heuristics)
   - [Lossless 18-Digit Precision & 32k Cell Boundary Reporting](#lossless-18-digit-precision--32k-cell-boundary-reporting)
4. [Mathematically Sound Pivot & Analytics Engine](#4-mathematically-sound-pivot--analytics-engine)
   - [True Grand Totals & Subtotals via `GROUP BY ROLLUP`](#true-grand-totals--subtotals-via-group-by-rollup)
   - [High-Cardinality Residual "Other" Bucket](#high-cardinality-residual-other-bucket)
   - [SQL AST & Safe Quoting (`ident`, `literal`, `v0` Aliasing)](#sql-ast--safe-quoting)
   - [Excel Pivot Parity (Value Settings, Grouping, Drill-Down)](#excel-pivot-parity)
5. [Security Perimeter & Process Isolation](#5-security-perimeter--process-isolation)
   - [Cryptographic Per-Run Token & Host Validation](#cryptographic-per-run-token--host-validation)
   - [CWE-209 Stack Trace Leakage Prevention](#cwe-209-stack-trace-leakage-prevention)
   - [Production Waitress WSGI Concurrency](#production-waitress-wsgi-concurrency)
6. [Virtualized Frontend Architecture & UX](#6-virtualized-frontend-architecture--ux)
   - [Windowed Grid with Proportional Virtual Scrolling](#windowed-grid-with-proportional-virtual-scrolling)
   - [WCAG AA Dual Theme & SVG Iconography](#wcag-aa-dual-theme--svg-iconography)
7. [Measured Production Benchmarks](#7-measured-production-benchmarks)

---

## 1. System Topology & High-Level Architecture

Grande is designed as an out-of-core desktop analytical platform. It pairs an embedded columnar database engine (**DuckDB**) with a threaded production WSGI server (**Waitress**) and a vanilla ES6 virtualized browser interface.

```
+-----------------------------------------------------------------------------------------+
|                               Browser Interface (Vanilla ES6)                           |
|  - Virtualized Windowed Grid (500-row blocks, proportional virtual scroll up to 50M+)   |
|  - Excel-Parity PivotTable Designer (Filters, Columns, Rows, Values, Grouping, Details) |
|  - Reactive Store (Undoable Data Cleaning Recipes, AutoFilters, Command Palette)        |
+-------------------------------------------▲---------------------------------------------+
                                            │ HTTP / SSE + X-Grande-Token (127.0.0.1:Port)
+-------------------------------------------▼---------------------------------------------+
|                     Production Waitress WSGI Server (src/grande/web/)                   |
|  - Token Authentication & DNS Rebinding Host Enforcement                                |
|  - Ring-Buffered SSE Event Streaming & Cooperative Task Cancellation                   |
|  - Sanitized Error Handling (Opaque reference IDs, zero stack traces leaked)           |
+-------------------------------------------▲---------------------------------------------+
                                            │
+-------------------------------------------▼---------------------------------------------+
|                          Grande Engine Core (src/grande/engine/)                        |
|                                                                                         |
|  [ Ingest & Session ]      [ Pivot & Analytics ]       [ Parallel Export Pipeline ]     |
|  - Ingest Once -> Table    - GROUP BY ROLLUP           - Stage view to Parquet          |
|  - CSV, Parquet, JSON      - True Totals for all Aggs  - 4x DuckDB Procs (169k rows/s)  |
|  - Calamine Rust Excel     - Residual "Other" Bucket   - 8x XlsxWriter (53k rows/s)     |
|  - 3-Tier Escalation       - Safe Quoting (v0, v1, ..) - Column-Level Type Preservation |
|  - Drive Spill Protection  - Grouping & Drill-Down     - 18-digit ID text protection    |
|                                                                                         |
|  [ In-Process Embedded Database: workspace.duckdb (Disk-Spilling Out-Of-Core Engine) ]  |
+-----------------------------------------------------------------------------------------+
```

---

## 2. Ingestion & Storage Engine

### The "Ingest Once" Paradigm
In legacy architectures, reading an un-indexed CSV on every user action forces the database to re-parse gigabytes of raw text repeatedly.

Grande 2 enforces **Ingest Once**:
1. When a dataset is opened, it is parsed and loaded **once** into a physical DuckDB table (`t_<uuid>`) inside `workspace.duckdb`.
2. Schema, column data types, distribution profiles, and exact row counts are cached in memory ([`session.py`](src/grande/engine/session.py)).
3. All subsequent interactions—filtering, sorting, paged scrolling, pivot generation, and column statistics—execute against the indexed table in **0.03 to 0.12 seconds**.
4. **Insertion Order Invariant**: `preserve_insertion_order` is strictly maintained so that "Row 1" in the virtualized grid is guaranteed to be Row 1 of the user's source file, preserving duplicate deduplication semantics.

### Format Coverage & Calamine Rust Reader
Grande 2 natively opens:
- Delimited text: `.csv`, `.tsv`, `.txt`, `.tab`, `.psv`, `.dat`
- Columnar & semi-structured: `.parquet`, `.json`, `.jsonl`, `.ndjson`
- Compressed streams: `.gz`, `.bz2`, `.zst`, `.zip`
- Native Excel workbooks: `.xlsx`, `.xlsm`, `.xlsb`, `.xls`, `.ods`

For Excel files, Grande 2 integrates **`python-calamine`** ([`ingest.py:560-605`](src/grande/engine/ingest.py#L560-L605)), a high-speed Rust-based parser. Sheets are parsed into DuckDB, normalized for ragged boundaries, and passed through `_retype_in_place()`. This selectively promotes numbers and dates while safeguarding identifiers with leading zeroes (`regexp_matches(col, '^0[0-9]')`).

### Two-Stage Sniffing & 3-Tier Escalation
1. **Sniff Phase (`ingest.py:204-222`)**: Inspects the first 256 KB to score delimiter candidates, detect character encodings (UTF-8, UTF-8 BOM, UTF-16, windows-1252), verify header presence (`looks_like_header`), and flag ambiguous date patterns (e.g. `03/05/2024`).
2. **3-Tier Escalation Pipeline**:
   - *Tier 1*: High-throughput parallel CSV scanner with automatic type detection.
   - *Tier 2*: Serial scanner with `null_padding = true, parallel = false` to handle uneven row lengths and embedded multiline text.
   - *Tier 3*: Fallback with `all_varchar = true` for severely degraded, mixed-type datasets.

### Multi-Instance Concurrency & File Locking
When multiple Grande instances open simultaneously, DuckDB's process lock is detected. Grande 2 intercepts `duckdb.IOException` ([`session.py:121-137`](src/grande/engine/session.py#L121-L137)), generates an isolated ephemeral database (`workspace-<pid>`), and sweeps orphaned cache files on launch.

---

## 3. Parallel Multipart Export Engine

```
[ Active Filtered & Sorted View ]
               │
               ▼
[ Staging: Materialize to Parquet (0.34s / 1M rows) ]
               │
               ├───────────────────────────────────┐
               ▼ (Fast Path: DuckDB Extension)     ▼ (Portable Path: XlsxWriter)
      [ 4 Parallel Processes ]            [ 8 Parallel Worker Processes ]
      - Compiled C++ Excel Writer         - constant_memory=True
      - Direct memory partition sink       - Target-drive spooling (.grande-export)
      - Throughput: 169,396 rows/sec      - Throughput: 53,565 rows/sec
               │                                   │
               └─────────────────┬─────────────────┘
                                 ▼
                   Part_a.xlsx, Part_b.xlsx, ...
                   (100% Type-Identical & Lossless)
```

### Parquet Staging View
Single-threaded writing directly from raw CSV is inherently I/O-bound. Grande 2 decouples export preparation from workbook formatting:
1. The active view (applied filters, sort orders, and column subsets) is materialized into an intermediate snappy-compressed Parquet file ([`export.py:220-264`](src/grande/engine/export.py#L220-L264)).
2. Staging takes approximately **0.34 seconds for 1,000,000 rows**.
3. Once staged, generating Part $n$ requires only an offset slice: `SELECT * FROM read_parquet('view.parquet') LIMIT count OFFSET offset`.

### Dual-Path Multiprocessing Pipeline
Grande 2 dispatches workbook generation across parallel processes via Python's `ProcessPoolExecutor`:
- **Fast Path (DuckDB Excel Extension)**: Dispatched across 4 worker processes, achieving **169,396 rows/sec (6.2s for 1M rows across 4 workbooks)** — a **14.7x speedup**.
- **Portable Path (Pure-Python XlsxWriter)**: Dispatched across 8 worker processes streaming 50,000-row chunks with `constant_memory=True` and target-drive disk spooling, achieving **53,565 rows/sec (19.0s for 1M rows)** — a **4.6x speedup**.

### Column-Level Typing vs Per-Cell Heuristics
- **The Negative Number Defect**: Naive cell-level heuristics classify strings starting with `-` as potential formula injection threats, writing `-1500.00` as text while `2500.00` is written as a number. In Excel, `=SUM()` ignores text strings, silently discarding refunds and credit adjustments.
- **Grande 2 Resolution**: Types are determined at the **column level** from the database schema ([`export.py:314-348`](src/grande/engine/export.py#L314-L348)). Numeric columns always write numbers. Formula execution is neutralized globally at the workbook container level via `strings_to_formulas: False`.

### Lossless 18-Digit Precision & 32k Cell Boundary Reporting
- **18-Digit Identifiers**: Excel retains only 15 digits of precision (IEEE 754 float limit). Grande 2 evaluates `precision_risk_columns()` across actual data values to detect integers exceeding `999_999_999_999_999`. These are cast to `VARCHAR` during staging and emitted as text cells with explicit user warnings.
- **32,767 Cell Boundaries**: Cells exceeding Excel's boundary are truncated with `"…"`. The exact number of affected cells is aggregated across workers and surfaced in the export summary.

---

## 4. Mathematically Sound Pivot & Analytics Engine

### True Grand Totals & Subtotals via `GROUP BY ROLLUP`
Aggregators fall into two classes:
- **Decomposable**: `SUM`, `COUNT` (where the sum of subset sums equals the total sum).
- **Non-Decomposable**: `AVG`, `MIN`, `MAX`, `MEDIAN`, `STDDEV`, `COUNT DISTINCT` (where summing cell values produces invalid numbers).

Grande 2 calculates all subtotals and Grand Totals inside the database kernel via **`GROUP BY ROLLUP`** ([`pivot.py:228-292`](src/grande/engine/pivot.py#L228-L292)). Because aggregation occurs over raw base records:
- The subtotal for an `AVG` metric is the true population mean $\frac{\sum x}{\sum n}$, not an average of averages.
- The Grand Total for `MIN` or `MAX` is the true minimum or maximum across the dataset.
- `COUNT DISTINCT` computes the true set union size, eliminating duplicate double-counting.

### High-Cardinality Residual "Other" Bucket
When pivoting on columns with many unique values, truncating at 50 causes rows beyond that cutoff to vanish from the matrix, causing row totals to diverge from source financial data.

Grande 2 constructs an explicit **"Other" column** ([`pivot.py:261-276`](src/grande/engine/pivot.py#L261-L276)) using `FILTER (WHERE NOT (kept_categories))`. All overflow categories are aggregated into this column, ensuring row totals match 100% of underlying records.

### SQL AST & Safe Quoting
All query construction is handled by [`sql.py`](src/grande/engine/sql.py):
1. Column names are quoted via `ident()`, which validates against null bytes and doubles embedded double quotes (`"col ""name"""`).
2. Literal values are escaped via `literal()`.
3. Projected expressions are aliased as synthetic identifiers (`v0, v1, v2...`). Human labels travel strictly within structured JSON metadata, completely decoupling data values from SQL syntax.

### Excel Pivot Parity
- **Value Field Settings**: 10 aggregations with custom naming ("Sum of Sales", "Average of Price").
- **Show Values As**: `% of Grand Total`, `% of Column Total`, `% of Row Total`, `Running Total In`, and `Rank Largest to Smallest`.
- **Hierarchical Grouping**: Date truncation into years, quarters, months, weeks, days, or hours; numeric binning into equal-width bands.
- **Show Details (Drill-Down)**: Double-clicking any pivot cell executes `drill_down()`, returning the exact raw source rows behind that calculation.

---

## 5. Security Perimeter & Process Isolation

### Cryptographic Per-Run Token & Host Validation
Running on `127.0.0.1` is not sufficient to prevent cross-origin attacks; arbitrary websites visited in a browser can issue requests to loopback ports.

Grande 2 implements defense-in-depth ([`server.py:64-89`](src/grande/web/server.py#L64-L89)):
1. **Per-Run Cryptographic Token**: A 24-byte URL-safe token is generated at startup (`secrets.token_urlsafe(24)`). All `/api/*` endpoints require this token in the `X-Grande-Token` header.
2. **Host Header Validation**: Rejects requests whose `Host` does not match `ALLOWED_HOSTS` (`127.0.0.1`, `localhost`, `[::1]`), defeating DNS rebinding attacks.
3. **No-CORS Policy**: No CORS headers are emitted. All responses include `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, and `Cache-Control: no-store`.
4. **Filesystem Sandboxing**: In the SQL console, DuckDB filesystem operations are locked down via `SET disabled_filesystems = 'LocalFileSystem'`.

### CWE-209 Stack Trace Leakage Prevention
Grande 2 sanitizes internal exceptions ([`server.py:95-119`](src/grande/web/server.py#L95-L119)). Domain errors display human-readable guidance. Unhandled exceptions log full tracebacks to the local terminal and return an opaque 8-character reference ID (`[ref 9a2f]`) to the browser, eliminating information disclosure vulnerabilities.

### Production Waitress WSGI Concurrency
Instead of Flask's single-threaded development server, Grande 2 runs on **Waitress** (`waitress.serve`):
- Ephemeral port selection (`s.bind(("127.0.0.1", 0))`), eliminating port collision races.
- 8 worker threads for non-blocking concurrent requests.
- Channel timeout extended to 3,600 seconds to support uninterrupted multi-gigabyte export streams.

---

## 6. Virtualized Frontend Architecture & UX

### Windowed Grid with Proportional Virtual Scrolling
Grande 2's data grid ([`grid.js`](src/grande/web/static/js/grid.js)) is virtualized:
- **DOM Efficiency**: Renders only visible rows plus a 12-row overscan buffer.
- **Server-Side Pagination**: Fetches rows in aligned 500-row blocks with background prefetching and LRU cache trimming.
- **Proportional Virtual Scrolling**: Browsers cap element heights at ~33.5M pixels (limiting naive tables to ~800,000 rows). Grande 2 monitors the `MAX_SCROLL_PX` boundary and switches to **proportional row mapping**, enabling smooth scrolling through 50,000,000+ rows.
- **Sparkline Profiles**: Column headers feature 14px distribution histograms for numeric columns and frequency bars for categorical data.

### WCAG AA Dual Theme & SVG Iconography
- **Color Contrast**: Features two themes (**Warm Paper Light** and **Bakelite Dark**) exceeding WCAG AA standards:
  - Muted text contrast: **7.1:1 to 7.3:1** (requirement: $\ge 4.5:1$).
  - Primary text contrast: **12.6:1 to 13.9:1**.
- **Iconography**: 100% emoji-free UI. Features an embedded SVG sprite sheet for crisp, cross-platform rendering without network font requests.

---

## 7. Measured Production Benchmarks

*Hardware: 14-core Intel workstation, NVMe SSD, 1,000,000 rows × 12 columns (114.5 MB CSV)*

| Benchmark Operation | Grande 1 | Grande 2 | Performance Delta |
|---|---|---|---|
| Open & Profile Dataset | Ephemeral re-read | **1.46 s** (Ingest Once) | **Instant subsequent queries** |
| Sort & Page at Row 500,000 | ~1.80 s | **0.12 s** | **15.0x faster** |
| Pivot Matrix Calculation | ~2.40 s | **0.03 s** | **80.0x faster** |
| Column Summary Statistics | ~1.90 s | **0.03 s** | **63.3x faster** |
| CSV → Parquet Conversion | ~1.80 s | **0.62 s** (1.62M rows/s) | **2.9x faster** |
| **Multipart Excel Export (1M rows)** | **86.90 s** (11.5k rows/s) | **19.00 s** (xlsxwriter 8P)<br>**6.20 s** (DuckDB 4P) | **4.6x faster** (Portable)<br>**14.7x faster** (Fast Path: 169k rows/s) |

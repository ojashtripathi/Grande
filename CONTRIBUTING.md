# 🤝 Contributing to Grande

Thank you for contributing to **Grande**! Grande is dedicated to high-performance, mathematically sound, lossless data processing for tables that exceed Microsoft Excel's limits.

---

## 🛠️ Development Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/ojashtripathi/Grande.git
   cd Grande
   ```

2. **Create a Python 3.10+ virtual environment**:
   ```bash
   python -m venv venv
   # On Windows:
   venv\Scripts\activate
   # On Linux/macOS:
   source venv/bin/activate
   ```

3. **Install Grande in editable mode with development dependencies**:
   ```bash
   pip install -e ".[dev]"
   # Or install dependencies from requirements.txt:
   pip install -r requirements.txt
   ```

4. **Launch the local development server**:
   ```bash
   python -m grande
   ```

---

## 🧪 Testing & Verification Protocol

Before submitting a pull request, **all unit and regression tests must pass**:

### Run the Engine Test Suite
```bash
python -m pytest tests/test_engine.py -v
```
The test suite covers:
- Ingestion formats (CSV, TSV, Parquet, JSON, Excel, compressed archives).
- Two-stage sniffing and 3-tier escalation fallbacks.
- SQL quoting, AST safety, and identifier escaping (`ident()`, `literal()`).
- High-cardinality residual `"Other"` bucket aggregation.
- Subtotals and Grand Totals correctness via `GROUP BY ROLLUP`.
- Value Field Settings and Excel "Show Values As" calculations (% of total, row, column, running totals, ranks).
- Lossless parallel multipart export (including 18-digit precision preservation and negative number numeric types).
- Multi-instance process locking and workspace isolation.

---

## 📐 Core Engineering Invariants

When contributing code, adhere to these architectural invariants:

1. **Ingest Once**:
   Never re-parse source files inside request handlers. Ingest files into resident database tables (`session.py`, `ingest.py`) and execute all queries against that table.
2. **Column-Level Typing**:
   Never apply per-cell heuristic type overrides that can bifurcate numeric columns (e.g., classifying `-1500.00` as text while `2500.00` is numeric). Typing is established at the column schema level.
3. **True Mathematical Totals**:
   Never compute Grand Totals by summing client-side cells for non-decomposable aggregators (`AVG`, `MIN`, `MAX`, `MEDIAN`, `COUNT_DISTINCT`). All subtotals and Grand Totals must be generated via database `GROUP BY ROLLUP` over base records.
4. **SQL Injection & AST Isolation**:
   Never interpolate raw user input or data values into SQL queries. Column names must pass through `ident()`, and dynamic value columns must be aliased as synthetic identifiers (`v0, v1, ...`) with human labels passed in JSON metadata.
5. **Security & Sandboxing**:
   All API routes must enforce `X-Grande-Token` validation and `Host` header checks. Internal server exceptions must never leak stack traces or filesystem paths to client browsers.
6. **Zero External CDNs**:
   The frontend interface must operate 100% offline. Do not add external script tags, Google Fonts, or CDN stylesheets.

---

## 📝 Commit Convention

We follow conventional commit format:
- `feat:` New feature or engine capability
- `fix:` Bug fix or regression correction
- `perf:` Performance optimization
- `test:` Adding or updating test suites
- `docs:` Documentation improvements
- `refactor:` Code refactoring with no behavioral change

---

## 📄 License
By contributing to Grande, you agree that your contributions will be licensed under the project's [MIT License](LICENSE).

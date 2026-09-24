"""Workspace and dataset registry.

The central design change from the original Grande: a file is read **once**, into
a DuckDB table, and every later question — preview, sort, filter, profile, pivot,
export — is answered from that table.

The original called ``read_csv_auto(...)`` inside every request, so opening a
pivot on a 10 GB CSV re-parsed 10 GB of text each time the user changed a
dropdown. Ingesting once turns that into a single up-front cost and makes
everything afterwards interactive.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import duckdb

from .sql import ident

#: DuckDB type names grouped into the four kinds the UI cares about. The UI uses
#: the kind to pick an alignment, a filter widget and a default aggregation.
_NUMERIC = re.compile(
    r"^(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER|UBIGINT|"
    r"UHUGEINT|FLOAT|DOUBLE|DECIMAL|REAL|NUMERIC)"
)
_TEMPORAL = re.compile(r"^(DATE|TIME|TIMESTAMP|INTERVAL)")
_BOOLEAN = re.compile(r"^BOOLEAN")


def classify(duckdb_type: str) -> str:
    """Map a DuckDB type name to 'number', 'date', 'boolean' or 'text'."""
    t = (duckdb_type or "").upper()
    if _NUMERIC.match(t):
        return "number"
    if _TEMPORAL.match(t):
        return "date"
    if _BOOLEAN.match(t):
        return "boolean"
    return "text"


class WorkspaceBusy(RuntimeError):
    """The chosen workspace is already open in another process."""


@dataclass
class Column:
    name: str
    type: str
    kind: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "kind": self.kind}


@dataclass
class Dataset:
    """One loaded table plus the provenance needed to explain it to the user."""

    id: str
    table: str
    source_path: str
    display_name: str
    row_count: int
    columns: list[Column]
    source_bytes: int = 0
    loaded_at: float = field(default_factory=time.time)
    ingest_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)
    #: Set when the source had more columns than Excel can hold, etc.
    warnings: list[str] = field(default_factory=list)

    @property
    def relation(self) -> str:
        """The quoted table name, safe to paste into generated SQL."""
        return ident(self.table)

    def column(self, name: str) -> Column | None:
        for col in self.columns:
            if col.name == name:
                return col
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "source_path": self.source_path,
            "row_count": self.row_count,
            "column_count": len(self.columns),
            "columns": [c.as_dict() for c in self.columns],
            "source_bytes": self.source_bytes,
            "loaded_at": self.loaded_at,
            "ingest_seconds": round(self.ingest_seconds, 3),
            "notes": self.notes,
            "warnings": self.warnings,
        }


class Workspace:
    """Owns the DuckDB database and the set of loaded datasets.

    DuckDB connections are not safe to use from several threads at once, but
    cursors taken from one connection are. Every caller therefore goes through
    :meth:`cursor`, which hands out a short-lived cursor over the shared database.
    """

    def __init__(self, directory: str | os.PathLike[str] | None = None, *, memory_limit: str | None = None):
        chosen = Path(directory) if directory else _default_cache_dir()
        self._temporary = False

        try:
            self._open(chosen)
        except duckdb.IOException:
            # DuckDB allows one writer per database file, so a second copy of
            # Grande cannot share the default cache. Rather than failing — which
            # is what a second double-click would otherwise do — this run gets a
            # cache of its own. The workspace is only a cache, so nothing is lost.
            if directory is not None:
                raise WorkspaceBusy(
                    f"Another program is using {chosen / 'workspace.duckdb'}. "
                    "Close it, or start Grande with a different --workspace."
                ) from None
            fallback = chosen.parent / f"{chosen.name}-{os.getpid()}"
            _sweep_stale_workspaces(chosen.parent, keep=chosen.name)
            self._open(fallback)
            self._temporary = True

        self._configure(memory_limit)

    def _open(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.dir = directory
        self.db_path = directory / "workspace.duckdb"
        self.spill_dir = directory / "spill"
        self.spill_dir.mkdir(exist_ok=True)

        self._lock = threading.RLock()
        self._datasets: dict[str, Dataset] = {}
        self._con = duckdb.connect(str(self.db_path))

    # ---------------------------------------------------------------- setup

    def _configure(self, memory_limit: str | None) -> None:
        """Tune DuckDB for a desktop machine.

        The original pinned ``memory_limit='2GB'`` and ``threads=4`` regardless of
        the machine, which throttles a 32-core workstation and still over-commits
        a 4 GB laptop. DuckDB's own defaults (80% of RAM, one thread per core) are
        better; we only redirect spill files onto the workspace volume so a large
        sort cannot fill the system drive.

        Note what is deliberately *not* set here: ``preserve_insertion_order``.
        Turning it off is a real speed win, but it lets DuckDB return rows in an
        order unrelated to the file. In a tool that stands in for a spreadsheet,
        "row 1" must be the first row of the user's file — unsorted really means
        file order — and "keep the first duplicate" must mean the first one in
        the file. Correctness wins over the throughput here.
        """
        settings: list[tuple[str, Any]] = [
            ("temp_directory", str(self.spill_dir)),
            ("enable_progress_bar", False),
        ]
        if memory_limit:
            settings.append(("memory_limit", memory_limit))
        for key, value in settings:
            try:
                self._con.execute(f"SET {key} = ?", [value])
            except duckdb.Error:
                # An unknown setting on an older DuckDB must not stop the app.
                pass

    # ------------------------------------------------------------- querying

    def cursor(self) -> duckdb.DuckDBPyConnection:
        """A thread-safe cursor over the shared database."""
        return self._con.cursor()

    def execute(self, sql: str, params: list[Any] | None = None) -> list[tuple]:
        cur = self.cursor()
        try:
            return cur.execute(sql, params or []).fetchall()
        finally:
            cur.close()

    def execute_one(self, sql: str, params: list[Any] | None = None) -> tuple | None:
        cur = self.cursor()
        try:
            return cur.execute(sql, params or []).fetchone()
        finally:
            cur.close()

    # ------------------------------------------------------------- datasets

    def new_table_name(self) -> str:
        return "t_" + uuid.uuid4().hex[:12]

    def register(self, dataset: Dataset) -> Dataset:
        with self._lock:
            self._datasets[dataset.id] = dataset
        return dataset

    def get(self, dataset_id: str) -> Dataset:
        with self._lock:
            dataset = self._datasets.get(dataset_id)
        if dataset is None:
            raise KeyError(f"No dataset {dataset_id!r} is open.")
        return dataset

    def list(self) -> list[Dataset]:
        with self._lock:
            return sorted(self._datasets.values(), key=lambda d: d.loaded_at, reverse=True)

    def drop(self, dataset_id: str) -> None:
        with self._lock:
            dataset = self._datasets.pop(dataset_id, None)
        if dataset is None:
            return
        try:
            self.execute(f"DROP TABLE IF EXISTS {dataset.relation}")
        except duckdb.Error:
            pass

    def describe_table(self, table: str) -> list[Column]:
        rows = self.execute(f"DESCRIBE {ident(table)}")
        return [Column(name=r[0], type=r[1], kind=classify(r[1])) for r in rows]

    def count_rows(self, table: str) -> int:
        row = self.execute_one(f"SELECT count(*) FROM {ident(table)}")
        return int(row[0]) if row else 0

    # ------------------------------------------------------------- lifecycle

    def close(self) -> None:
        with self._lock:
            self._datasets.clear()
        try:
            self._con.close()
        except duckdb.Error:
            pass
        # A per-process cache exists only for this run; leaving it behind would
        # accumulate a copy per launch.
        if getattr(self, "_temporary", False):
            shutil.rmtree(self.dir, ignore_errors=True)

    def reset(self) -> None:
        """Drop every table and reclaim the file. Used by 'Start over'."""
        for dataset in self.list():
            self.drop(dataset.id)
        try:
            self._con.execute("CHECKPOINT")
        except duckdb.Error:
            pass

    def purge_spill(self) -> None:
        shutil.rmtree(self.spill_dir, ignore_errors=True)
        self.spill_dir.mkdir(exist_ok=True)

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _sweep_stale_workspaces(parent: Path, *, keep: str) -> None:
    """Delete per-process caches left behind by runs that are no longer alive."""
    try:
        candidates = [p for p in parent.iterdir() if p.is_dir() and p.name.startswith(f"{keep}-")]
    except OSError:
        return
    for path in candidates:
        suffix = path.name.rsplit("-", 1)[-1]
        if not suffix.isdigit():
            continue
        if _process_alive(int(suffix)):
            continue
        shutil.rmtree(path, ignore_errors=True)


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except (OSError, AttributeError, ValueError):
        return False


def _default_cache_dir() -> Path:
    """Per-user cache directory, following each platform's convention."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return Path(base) / "Grande" / "cache"
    if os.sys.platform == "darwin":  # type: ignore[attr-defined]
        return Path(os.path.expanduser("~/Library/Caches/Grande"))
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "grande"


def iter_batches(cursor: duckdb.DuckDBPyConnection, size: int = 50_000) -> Iterator[list[tuple]]:
    """Yield rows from an executed cursor in batches, never materialising it all."""
    while True:
        batch = cursor.fetchmany(size)
        if not batch:
            return
        yield batch

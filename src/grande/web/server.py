"""The local HTTP API.

Security posture, which the original lacked entirely: the server binds to
127.0.0.1, every ``/api`` call must carry a per-run token, and the ``Host``
header is checked. Without those, any web page the user happened to visit could
reach a predictable loopback port and drive the app — reading files, running
SQL, writing workbooks — because a plain GET needs no CORS preflight.
"""

from __future__ import annotations

import math
import secrets
import os
import platform
import time
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, jsonify, request, send_from_directory
from flask.json.provider import DefaultJSONProvider
from werkzeug.exceptions import HTTPException

from .. import APP_NAME, __version__
from ..engine import export as export_engine
from ..engine import ingest as ingest_engine
from ..engine import pivot as pivot_engine
from ..engine import query as query_engine
from ..engine import transform as transform_engine
from ..engine.filters import describe_filter
from ..engine.jobs import JobRunner
from ..engine.session import Workspace
from ..engine.sql import SqlError, check_read_only

STATIC_DIR = Path(__file__).parent / "static"

#: Hosts we will answer to. Anything else is a rebinding attempt.
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


class ApiError(Exception):
    """An error with a message meant for the person using the app."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class FiniteJSONProvider(DefaultJSONProvider):
    """JSON a browser can always read.

    Python writes a float NaN or infinity as a bare ``NaN`` / ``Infinity``,
    which is not JSON: the browser's JSON.parse rejects the whole reply, and a
    page waiting on it simply stops. Values are made finite where they leave
    the engine; this is the backstop for anything that slips past, and costs
    nothing when the data is clean.
    """

    def dumps(self, obj: Any, **kwargs: Any) -> str:
        kwargs.setdefault("allow_nan", False)
        try:
            return super().dumps(obj, **kwargs)
        except ValueError:
            return super().dumps(_finite(obj), **kwargs)


def _finite(value: Any) -> Any:
    """``value`` with every NaN and infinity replaced by None (JSON null)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def create_app(
    *,
    workspace: str | None = None,
    token: str = "",
    initial_file: str | None = None,
) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.json = FiniteJSONProvider(app)
    app.config["JSON_SORT_KEYS"] = False

    ws = Workspace(workspace)
    jobs = JobRunner()
    state: dict[str, Any] = {"initial_file": initial_file, "token": token}

    # ------------------------------------------------------------ guardrails

    @app.before_request
    def _guard() -> Response | None:
        host = (request.host or "").rsplit(":", 1)[0]
        if host not in ALLOWED_HOSTS:
            return _problem("This app only answers on 127.0.0.1.", 403)
        if not request.path.startswith("/api/"):
            return None
        if not token:
            return None
        supplied = (
            request.headers.get("X-Grande-Token")
            or request.args.get("t")
            or ""
        )
        if supplied != token:
            return _problem("This request is missing the session token.", 401)
        return None

    @app.after_request
    def _headers(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # No CORS headers at all: other origins get nothing.
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.errorhandler(ApiError)
    def _api_error(exc: ApiError) -> Response:
        return _problem(exc.message, exc.status)

    @app.errorhandler(Exception)
    def _unexpected(exc: Exception) -> Response:
        # Errors we raise on purpose carry a message written for a human and are
        # safe to show. Everything else is an internal fault whose text can name
        # filesystem paths, generated SQL or library internals, so it is logged
        # to the console the user launched Grande from and replaced with a
        # reference. (The original returned str(e) from nine handlers.)
        # Routing and method errors are already meaningful; do not upgrade a 404
        # into a 500 just because it arrived here as an exception.
        if isinstance(exc, HTTPException):
            return _problem(exc.description or exc.name, exc.code or 500)
        for kind in (SqlError, ingest_engine.IngestError, pivot_engine.PivotError,
                     transform_engine.TransformError, export_engine.ExportError):
            if isinstance(exc, kind):
                return _problem(str(exc), 400)
        if isinstance(exc, KeyError):
            return _problem("That dataset is no longer open. Open the file again.", 404)

        reference = secrets.token_hex(4)
        app.logger.error("Unhandled error [ref %s] on %s", reference, request.path, exc_info=exc)
        return _problem(
            "Something went wrong inside Grande. The details are in the Grande "
            f"window (reference {reference}).",
            500,
        )

    # ----------------------------------------------------------------- shell

    @app.route("/")
    def index() -> Response:
        return send_from_directory(STATIC_DIR, "index.html")

    @app.route("/static/<path:relative>")
    def static_files(relative: str) -> Response:
        return send_from_directory(STATIC_DIR, relative)

    @app.route("/api/hello")
    def hello() -> Response:
        return jsonify({
            "app": APP_NAME,
            "version": __version__,
            "platform": platform.system(),
            "home": str(Path.home()),
            "initial_file": state.get("initial_file"),
            "fast_excel_writer": export_engine.excel_writer_available(),
            "cpu_count": os.cpu_count(),
            "datasets": [d.as_dict() for d in ws.list()],
        })

    # --------------------------------------------------------- file browsing

    @app.route("/api/browse")
    def browse() -> Response:
        """List a folder. Replaces the original's server-side Tkinter dialog,
        which blocked a request thread and only worked on Windows."""
        raw = request.args.get("path") or str(Path.home())
        target = Path(raw).expanduser()
        if not target.exists():
            target = Path.home()
        if target.is_file():
            target = target.parent

        entries: list[dict[str, Any]] = []
        try:
            for item in sorted(
                target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())
            ):
                if item.name.startswith(".") or _hidden(item):
                    continue
                try:
                    is_dir = item.is_dir()
                    size = 0 if is_dir else item.stat().st_size
                except OSError:
                    continue
                if not is_dir and not _openable(item):
                    continue
                entries.append({
                    "name": item.name,
                    "path": str(item),
                    "is_dir": is_dir,
                    "size": size,
                    "kind": None if is_dir else ingest_engine.classify_path(item),
                })
        except PermissionError:
            raise ApiError(f"No permission to read {target}.", 403)

        return jsonify({
            "path": str(target),
            "parent": str(target.parent) if target.parent != target else None,
            "entries": entries[:2000],
            "truncated": len(entries) > 2000,
            "places": _places(),
        })

    # -------------------------------------------------------------- datasets

    @app.route("/api/sniff", methods=["POST"])
    def sniff() -> Response:
        body = _body()
        path = _require(body, "path")
        return jsonify(ingest_engine.sniff(path).as_dict())

    @app.route("/api/open", methods=["POST"])
    def open_file() -> Response:
        body = _body()
        path = _require(body, "path")
        title = Path(path).name

        def work(job: Any) -> dict[str, Any] | None:
            def report(phase: str, detail: str, percent: float | None = None) -> None:
                if percent is None:
                    percent = {"reading": 10, "indexing": 75, "done": 100}.get(phase, 40)
                job.emit(percent=percent, message=detail, phase=phase)

            try:
                dataset = ingest_engine.ingest(
                    ws, path,
                    kind=body.get("kind"),
                    delimiter=body.get("delimiter"),
                    encoding=body.get("encoding"),
                    has_header=bool(body.get("has_header", True)),
                    sheet=body.get("sheet"),
                    all_varchar=bool(body.get("all_varchar", False)),
                    date_format=body.get("date_format") or None,
                    progress=report,
                    cancel=job.cancel_token,
                )
            except ingest_engine.IngestCancelled:
                # Nothing was kept; the runner sees the cancel and says so.
                return None
            return dataset.as_dict()

        job = jobs.submit("open", f"Opening {title}", work)
        return jsonify(job.as_dict())

    @app.route("/api/dataset/<dataset_id>")
    def dataset_info(dataset_id: str) -> Response:
        dataset = ws.get(dataset_id)
        return jsonify({
            **dataset.as_dict(),
            "can_undo": transform_engine.can_undo(dataset),
            "recipe": transform_engine.recipe(dataset),
        })

    @app.route("/api/dataset/<dataset_id>", methods=["DELETE"])
    def close_dataset(dataset_id: str) -> Response:
        ws.drop(dataset_id)
        return jsonify({"ok": True})

    @app.route("/api/dataset/<dataset_id>/rows", methods=["POST"])
    def rows(dataset_id: str) -> Response:
        dataset = ws.get(dataset_id)
        body = _body()
        return jsonify(query_engine.page(
            ws, dataset,
            offset=int(body.get("offset", 0)),
            limit=int(body.get("limit", query_engine.DEFAULT_PAGE)),
            filters=body.get("filters"),
            sort=body.get("sort"),
            columns=body.get("columns"),
            with_count=bool(body.get("with_count", True)),
        ))

    @app.route("/api/dataset/<dataset_id>/distinct", methods=["POST"])
    def distinct(dataset_id: str) -> Response:
        dataset = ws.get(dataset_id)
        body = _body()
        return jsonify(query_engine.distinct_values(
            ws, dataset, _require(body, "column"),
            search=body.get("search"),
            limit=int(body.get("limit", 200)),
            filters=body.get("filters"),
        ))

    @app.route("/api/dataset/<dataset_id>/summary", methods=["POST"])
    def summary(dataset_id: str) -> Response:
        dataset = ws.get(dataset_id)
        body = _body()
        return jsonify(query_engine.summary(
            ws, dataset, _require(body, "column"), filters=body.get("filters")
        ))

    @app.route("/api/dataset/<dataset_id>/profile", methods=["POST"])
    def profile(dataset_id: str) -> Response:
        dataset = ws.get(dataset_id)
        body = _body()
        return jsonify(query_engine.profile(
            ws, dataset, _require(body, "column"), filters=body.get("filters")
        ))

    @app.route("/api/dataset/<dataset_id>/describe-filters", methods=["POST"])
    def describe_filters(dataset_id: str) -> Response:
        body = _body()
        return jsonify({
            "chips": [describe_filter(f) for f in (body.get("filters") or [])]
        })

    # ----------------------------------------------------------------- pivot

    @app.route("/api/dataset/<dataset_id>/pivot", methods=["POST"])
    def pivot(dataset_id: str) -> Response:
        dataset = ws.get(dataset_id)
        body = _body()
        return jsonify(pivot_engine.compute(
            ws, dataset,
            rows=body.get("rows"),
            columns=body.get("columns"),
            column=body.get("column"),
            values=body.get("values"),
            filters=body.get("filters"),
            max_columns=int(body.get("max_columns", pivot_engine.DEFAULT_MAX_COLUMNS)),
            limit=int(body.get("limit", pivot_engine.DEFAULT_ROW_LIMIT)),
            subtotals=bool(body.get("subtotals", True)),
            sort=body.get("sort"),
        ))

    @app.route("/api/dataset/<dataset_id>/drill", methods=["POST"])
    def drill(dataset_id: str) -> Response:
        dataset = ws.get(dataset_id)
        body = _body()
        return jsonify(pivot_engine.drill_down(
            ws, dataset,
            row_fields=body.get("row_fields") or [],
            keys=body.get("keys") or [],
            columns=body.get("column_fields"),
            categories=body.get("categories"),
            column=body.get("column"),
            category=body.get("category"),
            filters=body.get("filters"),
            limit=int(body.get("limit", 500)),
        ))

    # ------------------------------------------------------------ transforms

    TRANSFORMS: dict[str, Callable[..., Any]] = {
        "remove_duplicates": transform_engine.remove_duplicates,
        "find_replace": transform_engine.find_replace,
        "clean_text": transform_engine.clean_text,
        "split_column": transform_engine.split_column,
        "change_type": transform_engine.change_type,
        "rename_column": transform_engine.rename_column,
        "remove_columns": transform_engine.remove_columns,
        "fill_blanks": transform_engine.fill_blanks,
        "keep_filtered": transform_engine.keep_filtered,
    }

    @app.route("/api/dataset/<dataset_id>/transform", methods=["POST"])
    def transform(dataset_id: str) -> Response:
        dataset = ws.get(dataset_id)
        body = _body()
        op = _require(body, "op")
        handler = TRANSFORMS.get(op)
        if handler is None:
            raise ApiError(f"Unknown operation: {op}")
        params = {k: v for k, v in (body.get("params") or {}).items()}
        try:
            dataset = handler(ws, dataset, **params)
        except TypeError as exc:
            # A belt-and-braces guard: the UI omits fields the user left empty,
            # so an operation whose argument is required would otherwise reach
            # the user as an internal error rather than as "choose a column".
            if "argument" not in str(exc):
                raise
            app.logger.warning("transform %s called with %s: %s", op, sorted(params), exc)
            raise ApiError("Some required choices are missing for that operation.")
        return jsonify({
            **dataset.as_dict(),
            "can_undo": transform_engine.can_undo(dataset),
            "recipe": transform_engine.recipe(dataset),
        })

    @app.route("/api/dataset/<dataset_id>/transform/preview", methods=["POST"])
    def transform_preview(dataset_id: str) -> Response:
        """What an operation would do, before it does it."""
        dataset = ws.get(dataset_id)
        body = _body()
        op = _require(body, "op")
        params = dict(body.get("params") or {})
        if op != "change_type":
            raise ApiError(f"No preview available for {op}.")
        return jsonify(transform_engine.preview_change_type(ws, dataset, **params))

    @app.route("/api/dataset/<dataset_id>/undo", methods=["POST"])
    def undo(dataset_id: str) -> Response:
        dataset = transform_engine.undo(ws, ws.get(dataset_id))
        return jsonify({
            **dataset.as_dict(),
            "can_undo": transform_engine.can_undo(dataset),
            "recipe": transform_engine.recipe(dataset),
        })

    # ---------------------------------------------------------------- export

    @app.route("/api/dataset/<dataset_id>/export/plan", methods=["POST"])
    def export_plan(dataset_id: str) -> Response:
        dataset = ws.get(dataset_id)
        body = _body()
        fmt = (body.get("format") or "xlsx").lower()
        extension = {"xlsx": ".xlsx", "csv": ".csv", "tsv": ".tsv",
                     "parquet": ".parquet", "json": ".jsonl"}.get(fmt, ".xlsx")
        plan = export_engine.plan_export(
            ws, dataset,
            directory=body.get("directory") or str(Path.home()),
            base_name=body.get("base_name"),
            rows_per_file=int(body.get("rows_per_file") or ingest_engine.EXCEL_MAX_DATA_ROWS),
            suffix_style=body.get("suffix_style") or "alpha",
            filters=body.get("filters"),
            columns=body.get("columns"),
            extension=extension,
        )
        return jsonify(plan.as_dict())

    @app.route("/api/dataset/<dataset_id>/export", methods=["POST"])
    def export(dataset_id: str) -> Response:
        dataset = ws.get(dataset_id)
        body = _body()
        fmt = (body.get("format") or "xlsx").lower()
        directory = body.get("directory") or str(Path.home())
        # Existing files are replaced only when the person has confirmed it.
        overwrite = body.get("overwrite") is True

        def work(job: Any) -> dict[str, Any]:
            def report(payload: dict[str, Any]) -> None:
                job.emit(**payload)

            if fmt == "xlsx":
                return export_engine.export_excel(
                    ws, dataset,
                    directory=directory,
                    base_name=body.get("base_name"),
                    rows_per_file=int(body.get("rows_per_file") or ingest_engine.EXCEL_MAX_DATA_ROWS),
                    suffix_style=body.get("suffix_style") or "alpha",
                    filters=body.get("filters"),
                    sort=body.get("sort"),
                    columns=body.get("columns"),
                    progress=report,
                    cancel=job.cancel_token,
                    overwrite=overwrite,
                )
            return export_engine.export_flat(
                ws, dataset,
                directory=directory,
                base_name=body.get("base_name"),
                fmt=fmt,
                filters=body.get("filters"),
                sort=body.get("sort"),
                columns=body.get("columns"),
                rows_per_file=body.get("rows_per_file"),
                suffix_style=body.get("suffix_style") or "alpha",
                compression=body.get("compression") or "zstd",
                progress=report,
                overwrite=overwrite,
            )

        job = jobs.submit("export", f"Exporting {dataset.display_name}", work)
        return jsonify(job.as_dict())

    @app.route("/api/reveal", methods=["POST"])
    def reveal() -> Response:
        """Open the containing folder in the OS file manager.

        Only ever a folder. ``os.startfile``, ``open`` and ``xdg-open`` *run* a
        file they are given, and the parent of a path that passes through a
        program (``C:\\tools\\app.exe\\x``) is that program, so the folder is
        checked to be a directory first. On macOS it is revealed with
        ``open -R``, because an ``.app`` bundle is a directory that plain
        ``open`` would launch.
        """
        target = Path(_require(_body(), "path")).expanduser()
        folder = target if target.is_dir() else target.parent
        if not folder.is_dir():
            raise ApiError("That folder does not exist any more.", 404)
        folder = folder.resolve()
        try:
            if platform.system() == "Windows":
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif platform.system() == "Darwin":
                import subprocess

                subprocess.Popen(["open", "-R", str(folder)])
            else:
                import subprocess

                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            raise ApiError(f"Could not open that folder: {exc}")
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ jobs

    @app.route("/api/job/<job_id>")
    def job_status(job_id: str) -> Response:
        return jsonify(jobs.get(job_id).as_dict())

    @app.route("/api/job/<job_id>/cancel", methods=["POST"])
    def job_cancel(job_id: str) -> Response:
        return jsonify(jobs.cancel(job_id).as_dict())

    @app.route("/api/job/<job_id>/stream")
    def job_stream(job_id: str) -> Response:
        job = jobs.get(job_id)
        after = int(request.args.get("after", 0))

        def generate():
            yield ": open\n\n"
            for event in job.stream(after=after):
                if event.get("heartbeat"):
                    yield ": ping\n\n"
                    continue
                yield f"data: {app.json.dumps(event)}\n\n"
                if event.get("final"):
                    return

        # No "Connection" header here: it is hop-by-hop, and PEP 3333 forbids a
        # WSGI application from setting one (waitress raises on it).
        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ------------------------------------------------------------------- sql

    @app.route("/api/dataset/<dataset_id>/sql", methods=["POST"])
    def sql(dataset_id: str) -> Response:
        dataset = ws.get(dataset_id)
        body = _body()
        # Checked by reading the statement, not by disabling the filesystem:
        # `SET disabled_filesystems` is global to the DuckDB instance, so doing
        # that here left every later file open failing with a permission error
        # until Grande was restarted.
        statement = check_read_only(body.get("query") or "")
        limit = max(1, min(int(body.get("limit", 1000)), 10_000))

        cur = ws.cursor()
        try:
            cur.execute(f"CREATE OR REPLACE TEMP VIEW data AS SELECT * FROM {dataset.relation}")
            started = time.time()
            result = cur.execute(f"SELECT * FROM ({statement}) LIMIT {limit}")
            columns = [d[0] for d in (result.description or [])]
            records = result.fetchall()
            elapsed = time.time() - started
        except Exception as exc:
            raise ApiError(_sql_message(exc))
        finally:
            cur.close()

        return jsonify({
            "columns": columns,
            "rows": [[query_engine._cell(v) for v in row] for row in records],
            "row_count": len(records),
            "truncated": len(records) >= limit,
            "seconds": round(elapsed, 3),
        })

    @app.route("/api/shutdown", methods=["POST"])
    def shutdown() -> Response:
        ws.purge_spill()
        return jsonify({"ok": True})

    app.workspace = ws  # type: ignore[attr-defined]
    app.jobs = jobs  # type: ignore[attr-defined]
    return app


# --------------------------------------------------------------------- helpers


def _body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _require(body: dict[str, Any], key: str) -> Any:
    value = body.get(key)
    if value in (None, ""):
        raise ApiError(f"“{key}” is required.")
    return value


def _problem(message: str, status: int) -> Response:
    response = jsonify({"error": message})
    response.status_code = status
    return response


def _hidden(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        import stat

        return bool(path.stat().st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN)  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        return False


def _openable(path: Path) -> bool:
    known = (
        ingest_engine.CSV_SUFFIXES | ingest_engine.PARQUET_SUFFIXES
        | ingest_engine.JSON_SUFFIXES | ingest_engine.EXCEL_SUFFIXES
    )
    suffixes = [s.lower() for s in path.suffixes]
    if not suffixes:
        return False
    if suffixes[-1] in ingest_engine.COMPRESSED_SUFFIXES:
        return len(suffixes) > 1 and suffixes[-2] in known
    return suffixes[-1] in known


def _places() -> list[dict[str, str]]:
    """Shortcuts for the file browser sidebar."""
    home = Path.home()
    candidates = [
        ("Home", home),
        ("Desktop", home / "Desktop"),
        ("Documents", home / "Documents"),
        ("Downloads", home / "Downloads"),
    ]
    places = [
        {"name": name, "path": str(p)} for name, p in candidates if p.exists()
    ]
    if os.name == "nt":
        import string

        for letter in string.ascii_uppercase:
            drive = Path(f"{letter}:\\")
            if drive.exists():
                places.append({"name": f"{letter}:", "path": str(drive)})
    return places


def _sql_message(exc: Exception) -> str:
    text = str(exc)
    if "Catalog Error" in text and "does not exist" in text:
        return f"{text.splitlines()[0]}  (your data is the table “data”)"
    if "Parser Error" in text or "syntax error" in text.lower():
        return f"That query has a syntax error: {text.splitlines()[0]}"
    if "Permission" in text or "disabled" in text.lower():
        return "Only read-only queries are allowed here."
    return text.splitlines()[0] if text else "That query failed."

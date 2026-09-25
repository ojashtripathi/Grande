"""The local HTTP API's own guards."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from werkzeug.test import Client

from grande.engine.ingest import ingest
from grande.web.server import create_app


def browser_parse(text: str):
    """Parse the way a browser's JSON.parse does: bare NaN/Infinity are errors."""
    def refuse(token):
        raise ValueError(f"not JSON: {token}")
    return json.loads(text, parse_constant=refuse)


@pytest.fixture
def app(tmp_path):
    application = create_app(workspace=str(tmp_path / "ws"), token="")
    try:
        yield application
    finally:
        application.workspace.close()


@pytest.fixture
def odd_floats(app, tmp_path):
    """A Parquet file whose first rows hold NaN and infinities, as computed
    columns (0/0, a growth rate against zero) often do."""
    path = tmp_path / "ratios.parquet"
    app.workspace.execute(f"""
        COPY (SELECT * FROM (VALUES
            ('North', CAST('NaN' AS DOUBLE)),
            ('South', CAST('Infinity' AS DOUBLE)),
            ('East',  CAST('-Infinity' AS DOUBLE)),
            ('West',  2.5)) t(region, growth))
        TO '{path.as_posix()}' (FORMAT parquet)""")
    return path


def test_preview_with_nan_and_infinity_is_readable(app, odd_floats):
    """Regression: the reply said NaN / Infinity, which is not JSON, so the
    browser could not read it and the file seemed not to open at all."""
    response = Client(app).post("/api/sniff", json={"path": str(odd_floats)})
    assert response.status_code == 200
    preview = browser_parse(response.get_data(as_text=True))
    assert [row[1] for row in preview["sample_rows"]] == [None, None, None, 2.5]


def test_pivot_over_nan_and_infinity_is_readable(app, odd_floats):
    dataset = ingest(app.workspace, odd_floats)
    response = Client(app).post(f"/api/dataset/{dataset.id}/pivot", json={
        "rows": [{"column": "region"}],
        "values": [{"column": "growth", "agg": "avg"}],
    })
    assert response.status_code == 200
    browser_parse(response.get_data(as_text=True))


def test_cancelling_an_excel_open_stops_it(app, tmp_path):
    """Regression: Cancel did nothing while a workbook loaded; the load ran on
    and the dataset was registered anyway."""
    import time

    import xlsxwriter

    path = tmp_path / "big.xlsx"
    book = xlsxwriter.Workbook(str(path), {"constant_memory": True})
    sheet = book.add_worksheet("Data")
    sheet.write_row(0, 0, [f"c{i}" for i in range(10)])
    for r in range(1, 80_001):
        sheet.write_row(r, 0, [r, "text", r * 0.5, "more", r % 7, "x", r, "y", r, "z"])
    book.close()

    client = Client(app)
    job = client.post("/api/open", json={"path": str(path)}).get_json()

    def status():
        return client.get(f"/api/job/{job['id']}").get_json()

    deadline = time.monotonic() + 60
    while status()["detail"].get("phase") != "loading":
        assert time.monotonic() < deadline, "never reported loading progress"
        time.sleep(0.02)
    client.post(f"/api/job/{job['id']}/cancel", json={})
    while not status()["finished"]:
        assert time.monotonic() < deadline, "cancel did not stop the load"
        time.sleep(0.05)
    assert status()["status"] == "cancelled"
    assert app.workspace.list() == []


def test_no_reply_can_carry_nan(app):
    """The backstop: whatever slips past the engine still becomes JSON null."""
    text = app.json.dumps({"a": float("nan"), "b": [1.5, float("inf")], "c": {"d": float("-inf")}})
    assert browser_parse(text) == {"a": None, "b": [1.5, None], "c": {"d": None}}


@pytest.fixture
def client(tmp_path):
    # werkzeug's client directly: Flask's test_client() also needs click.testing.
    app = create_app(workspace=str(tmp_path / "ws"), token="")
    try:
        yield Client(app)
    finally:
        app.workspace.close()


@pytest.fixture
def launched(monkeypatch):
    """Record what /api/reveal would open, and open nothing."""
    calls: list[str] = []
    monkeypatch.setattr(os, "startfile", lambda path: calls.append(str(path)), raising=False)
    monkeypatch.setattr(subprocess, "Popen", lambda args, **_: calls.append(str(args[-1])))
    return calls


def test_reveal_opens_a_folder(client, launched, tmp_path):
    folder = tmp_path / "exports"
    folder.mkdir()
    (folder / "book.xlsx").write_bytes(b"")

    assert client.post("/api/reveal", json={"path": str(folder)}).status_code == 200
    assert client.post("/api/reveal", json={"path": str(folder / "book.xlsx")}).status_code == 200
    assert [Path(p) for p in launched] == [folder.resolve(), folder.resolve()]


def test_reveal_never_runs_a_file(client, launched, tmp_path):
    """Regression: the parent of C:\\tools\\app.exe\\x is app.exe, and
    os.startfile runs what it is given — so reveal launched the program."""
    program = tmp_path / "tool.exe"
    program.write_bytes(b"MZ")

    response = client.post("/api/reveal", json={"path": str(program / "anything")})
    assert response.status_code == 404
    assert launched == []

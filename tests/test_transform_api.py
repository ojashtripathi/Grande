"""Every clean-up operation, called the way the browser calls it.

The UI drops fields the user left empty, so an operation reaches the engine with
a subset of its arguments. A keyword-only argument with no default then raises
``TypeError`` and surfaces as an internal error with a reference code, which is
how "Find and replace" shipped broken: leaving the column list blank — its
documented way of saying "every text column" — crashed it.

These call each operation with the *minimum* the UI would send, and then with
nothing at all, and require either a sensible result or a message written for a
person. Never a TypeError.
"""

from __future__ import annotations

import csv
import inspect

import pytest

from grande.engine import transform as transform_engine
from grande.engine.ingest import ingest
from grande.engine.session import Workspace
from grande.web.server import create_app

#: Name -> the smallest parameters the UI would send for a normal use.
MINIMAL = {
    "remove_duplicates": {},
    "find_replace": {"find": "alpha", "replace": "omega"},
    "clean_text": {"columns": ["name"]},
    "split_column": {"column": "name", "delimiter": "-"},
    "change_type": {"column": "qty", "to": "integer"},
    "rename_column": {"column": "name", "to": "label"},
    "remove_columns": {"columns": ["note"]},
    "fill_blanks": {"columns": ["note"], "value": "none"},
    "keep_filtered": {"filters": [{"column": "qty", "op": "gte", "value": 2}]},
}


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path / "t.csv"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "qty", "note"])
        writer.writerows([
            ["alpha-one", "1", "keep"],
            ["alpha-two", "2", ""],
            ["beta-three", "3", "keep"],
            ["beta-three", "3", ""],
        ])
    ws = Workspace(tmp_path / "ws")
    try:
        yield ws, ingest(ws, path)
    finally:
        ws.close()


def test_every_operation_is_reachable_from_the_ui():
    """The dispatch table and the engine must not drift apart."""
    app = create_app(token="")
    handlers = {
        rule.endpoint for rule in app.url_map.iter_rules()
    }
    assert "transform" in handlers
    for name in MINIMAL:
        assert hasattr(transform_engine, name), f"{name} is offered but does not exist"


@pytest.mark.parametrize("op", sorted(MINIMAL))
def test_minimal_parameters_work(dataset, op):
    ws, ds = dataset
    handler = getattr(transform_engine, op)
    result = handler(ws, ds, **MINIMAL[op])
    assert result.row_count >= 0
    assert result.columns


@pytest.mark.parametrize("op", sorted(MINIMAL))
def test_no_parameters_gives_a_message_not_a_crash(dataset, op):
    """Called with nothing, an operation must explain itself."""
    ws, ds = dataset
    handler = getattr(transform_engine, op)
    try:
        handler(ws, ds)
    except transform_engine.TransformError as exc:
        assert str(exc), "the message must say something"
    except TypeError as exc:                       # the bug this file exists for
        pytest.fail(f"{op} has a required keyword with no default: {exc}")


@pytest.mark.parametrize("op", sorted(MINIMAL))
def test_all_arguments_have_defaults(op):
    """Checked directly, so a new operation cannot reintroduce the problem."""
    signature = inspect.signature(getattr(transform_engine, op))
    required = [
        name for name, p in signature.parameters.items()
        if p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
    ]
    assert not required, f"{op} requires {required}; the UI omits empty fields"

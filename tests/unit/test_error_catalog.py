"""Unit tests for :func:`lauren.reflect.error_catalog` and the catalog/docs sync (F05)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import lauren
from lauren.exceptions import (
    GuardConfigError,
    HTTPError,
    LaurenError,
    LifecycleViolationError,
    NotFoundError,
    RouteNotFoundError,
)
from lauren.reflect import ReflectedError, error_catalog
from lauren.socketio import SocketIOError
from lauren.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[2]
FRAMEWORK = error_catalog(include_user=False)


class _AppNotFound(NotFoundError):
    """An application error used by the catalog tests."""

    code = "app_thing_not_found"
    problem_type = "https://errors.example.com/app-thing"


def _entry(cls: type) -> ReflectedError:
    return next(e for e in error_catalog() if e.cls is cls)


def test_framework_catalog_lists_each_class_once() -> None:
    classes = [entry.cls for entry in FRAMEWORK]

    assert len(classes) == len(set(classes))
    assert classes[0] is LaurenError


def test_framework_catalog_covers_every_exported_error() -> None:
    exported = {
        getattr(lauren, name)
        for name in lauren.__all__
        if isinstance(getattr(lauren, name), type) and issubclass(getattr(lauren, name), LaurenError)
    }

    assert exported <= {entry.cls for entry in FRAMEWORK}


def test_framework_catalog_excludes_application_errors() -> None:
    assert _AppNotFound not in {entry.cls for entry in FRAMEWORK}


def test_catalog_includes_application_errors_by_default() -> None:
    entry = _entry(_AppNotFound)

    assert (entry.code, entry.status_code, entry.category) == ("app_thing_not_found", 404, "http")
    assert entry.problem_type == "https://errors.example.com/app-thing"
    assert entry.summary == "An application error used by the catalog tests."


def test_catalog_walks_subclasses_depth_first() -> None:
    classes = [entry.cls for entry in FRAMEWORK]

    assert classes.index(HTTPError) < classes.index(NotFoundError) < classes.index(RouteNotFoundError)


@pytest.mark.parametrize(
    "cls,category,status",
    [
        (LaurenError, "other", None),
        (GuardConfigError, "startup", None),
        (RouteNotFoundError, "http", 404),
        (LifecycleViolationError, "lifecycle", None),
        (WebSocketDisconnect, "websocket", None),
        (SocketIOError, "websocket", None),
    ],
    ids=lambda v: getattr(v, "__name__", str(v)),
)
def test_catalog_categories_and_statuses(cls: type, category: str, status: int | None) -> None:
    entry = next(e for e in FRAMEWORK if e.cls is cls)

    assert (entry.category, entry.status_code) == (category, status)


def test_framework_codes_are_unique() -> None:
    codes = [entry.code for entry in FRAMEWORK]

    assert len(codes) == len(set(codes)), sorted(c for c in codes if codes.count(c) > 1)


def test_summary_is_the_first_docstring_paragraph_on_one_line() -> None:
    entry = next(e for e in FRAMEWORK if e.cls.__name__ == "SessionConfigError")

    assert "\n" not in entry.summary
    assert entry.summary.endswith("did not enable sessions.")


def test_class_without_its_own_docstring_has_an_empty_summary() -> None:
    class _Bare(HTTPError):
        code = "bare"

    assert _entry(_Bare).summary == ""


def test_reflected_error_is_frozen() -> None:
    with pytest.raises(AttributeError):
        FRAMEWORK[0].code = "x"  # type: ignore[misc]


@pytest.mark.parametrize("doc", ["docs/reference/errors.md", "lauren/llms-full.txt"])
@pytest.mark.parametrize("entry", FRAMEWORK, ids=lambda e: e.cls.__name__)
def test_every_framework_error_is_documented(doc: str, entry: ReflectedError) -> None:
    text = (ROOT / doc).read_text(encoding="utf-8")

    assert re.search(rf"`{entry.cls.__name__}[`(]", text), f"{entry.cls.__name__} is not documented in {doc}"


def test_diamond_subclasses_are_listed_once() -> None:
    from lauren.exceptions import ConflictError

    class _Both(NotFoundError, ConflictError):
        code = "both"

    assert [e.cls for e in error_catalog()].count(_Both) == 1

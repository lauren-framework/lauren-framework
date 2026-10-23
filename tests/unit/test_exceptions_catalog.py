"""The error catalog is exported consistently (D02 item 18c).

Every exception class defined in ``lauren.exceptions`` must be listed in that
module's ``__all__`` and re-exported from ``lauren``; HTTP-mapped classes need a
stable ``code`` and a status.
"""

from __future__ import annotations

import inspect

import pytest

import lauren
import lauren.exceptions as exceptions

CATALOG = [
    obj
    for obj in vars(exceptions).values()
    if inspect.isclass(obj) and issubclass(obj, BaseException) and obj.__module__ == exceptions.__name__
]


def test_catalog_is_non_empty_and_rooted_at_lauren_error() -> None:
    assert CATALOG
    assert all(issubclass(cls, exceptions.LaurenError) for cls in CATALOG)


@pytest.mark.parametrize("cls", CATALOG, ids=lambda cls: cls.__name__)
def test_every_class_is_in_exceptions_all(cls: type) -> None:
    assert cls.__name__ in exceptions.__all__


@pytest.mark.parametrize("cls", CATALOG, ids=lambda cls: cls.__name__)
def test_every_class_is_re_exported_from_lauren(cls: type) -> None:
    assert cls.__name__ in lauren.__all__
    assert getattr(lauren, cls.__name__) is cls


def test_exceptions_all_names_only_catalog_classes() -> None:
    assert sorted(exceptions.__all__) == sorted(cls.__name__ for cls in CATALOG)


@pytest.mark.parametrize(
    "cls",
    [cls for cls in CATALOG if issubclass(cls, exceptions.HTTPError) and cls is not exceptions.HTTPError],
    ids=lambda cls: cls.__name__,
)
def test_http_errors_have_a_status_and_their_own_code(cls: type) -> None:
    assert isinstance(cls.status_code, int) and 400 <= cls.status_code < 600
    assert cls.code and cls.code != exceptions.HTTPError.code

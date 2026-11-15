"""Multipart resource configuration fails during app compilation."""

from __future__ import annotations

import pytest

from lauren import ContentConfig, LaurenFactory, MultipartConfig, StartupError, module
from lauren._forms._config import resolve_multipart


@module()
class Root:
    pass


@pytest.mark.parametrize(
    "field",
    ["max_parts", "max_fields", "max_files", "max_field_size", "max_part_header_bytes", "spool_threshold"],
)
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "10"])
def test_multipart_budgets_require_positive_integers(field, value) -> None:
    config = MultipartConfig(**{field: value})
    with pytest.raises(StartupError) as caught:
        LaurenFactory.create(Root, content=ContentConfig(multipart=config))
    assert caught.value.detail["field"] == field


@pytest.mark.parametrize("value", ["unknown", None, True])
def test_multipart_undeclared_policy_is_validated(value) -> None:
    with pytest.raises(StartupError):
        resolve_multipart(MultipartConfig(undeclared_parts=value))


def test_multipart_requires_a_config_object() -> None:
    with pytest.raises(StartupError):
        resolve_multipart({})


def test_temp_directory_must_exist(tmp_path) -> None:
    with pytest.raises(StartupError) as caught:
        resolve_multipart(MultipartConfig(temp_dir=tmp_path / "missing"))
    assert caught.value.detail["field"] == "temp_dir"


def test_temp_directory_is_frozen_as_an_absolute_path(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    child = tmp_path / "uploads"
    child.mkdir()
    resolved = resolve_multipart(MultipartConfig(temp_dir="uploads"))
    assert resolved.temp_dir == str(child)

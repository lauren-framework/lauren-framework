"""Uncompiled host extractors honor defaults and scalar validation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lauren.exceptions import ExtractorFieldError
from lauren.extractors import Extraction, extract_parameter


def _extraction(*, has_default: bool = False) -> Extraction:
    return Extraction("tenant", "host_param", int, None, 42, has_default)


async def test_host_parameter_missing_without_default_reports_field() -> None:
    with pytest.raises(ExtractorFieldError) as caught:
        await extract_parameter(SimpleNamespace(host_params={}), _extraction())

    assert caught.value.detail["field"] == "tenant"


async def test_host_parameter_missing_with_default_uses_it() -> None:
    value = await extract_parameter(SimpleNamespace(host_params={}), _extraction(has_default=True))

    assert value == 42


async def test_host_parameter_value_is_coerced_to_declared_type() -> None:
    value = await extract_parameter(SimpleNamespace(host_params={"tenant": "7"}), _extraction())

    assert value == 7

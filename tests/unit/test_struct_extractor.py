"""Unit tests for struct-type extraction helpers.

Tests for:
- _is_msgspec_struct_type
- _is_dataclass_type
- _is_struct_type
- _convert_struct
"""

from __future__ import annotations

import dataclasses
from typing import Optional
import importlib

import pytest

from lauren.exceptions import ExtractorError
from lauren.extractors import (
    _convert_struct,
    _is_dataclass_type,
    _is_msgspec_struct_type,
    _is_struct_type,
)

HAS_MSGSPEC = importlib.util.find_spec("msgspec") is not None
if HAS_MSGSPEC:
    import msgspec

HAS_PYDANTIC = importlib.util.find_spec("pydantic") is not None


# ---------------------------------------------------------------------------
# Shared fixture types
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PageDC:
    page: int
    size: int = 20


# ---------------------------------------------------------------------------
# _is_msgspec_struct_type
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_MSGSPEC, reason="msgspec not installed")
class TestIsMsgspecStructType:
    def test_struct_subclass_returns_true(self) -> None:
        class MyStruct(msgspec.Struct):
            x: int

        assert _is_msgspec_struct_type(MyStruct) is True

    def test_optional_struct_returns_true(self) -> None:
        class MyStruct(msgspec.Struct):
            x: int

        assert _is_msgspec_struct_type(Optional[MyStruct]) is True

    def test_plain_class_returns_false(self) -> None:
        class Plain:
            x: int

        assert _is_msgspec_struct_type(Plain) is False

    def test_dataclass_returns_false(self) -> None:
        assert _is_msgspec_struct_type(PageDC) is False

    def test_int_returns_false(self) -> None:
        assert _is_msgspec_struct_type(int) is False

    def test_none_annotation_returns_false(self) -> None:
        assert _is_msgspec_struct_type(type(None)) is False

    @pytest.mark.skipif(not HAS_PYDANTIC, reason="pydantic not installed")
    def test_pydantic_model_returns_false(self) -> None:
        from pydantic import BaseModel

        class M(BaseModel):
            x: int

        assert _is_msgspec_struct_type(M) is False


# ---------------------------------------------------------------------------
# _is_dataclass_type
# ---------------------------------------------------------------------------


class TestIsDataclassType:
    def test_dataclass_returns_true(self) -> None:
        assert _is_dataclass_type(PageDC) is True

    def test_optional_dataclass_returns_true(self) -> None:
        assert _is_dataclass_type(Optional[PageDC]) is True

    def test_plain_class_returns_false(self) -> None:
        class Plain:
            x: int

        assert _is_dataclass_type(Plain) is False

    def test_int_returns_false(self) -> None:
        assert _is_dataclass_type(int) is False

    @pytest.mark.skipif(not HAS_PYDANTIC, reason="pydantic not installed")
    def test_pydantic_model_returns_false(self) -> None:
        from pydantic import BaseModel

        class M(BaseModel):
            x: int

        assert _is_dataclass_type(M) is False

    @pytest.mark.skipif(not HAS_MSGSPEC, reason="msgspec not installed")
    def test_msgspec_struct_returns_false(self) -> None:
        class S(msgspec.Struct):
            x: int

        assert _is_dataclass_type(S) is False


# ---------------------------------------------------------------------------
# _is_struct_type (union)
# ---------------------------------------------------------------------------


class TestIsStructType:
    def test_dataclass_returns_true(self) -> None:
        assert _is_struct_type(PageDC) is True

    @pytest.mark.skipif(not HAS_MSGSPEC, reason="msgspec not installed")
    def test_msgspec_struct_returns_true(self) -> None:
        class S(msgspec.Struct):
            x: int

        assert _is_struct_type(S) is True

    def test_plain_class_returns_false(self) -> None:
        class Plain:
            pass

        assert _is_struct_type(Plain) is False

    def test_int_returns_false(self) -> None:
        assert _is_struct_type(int) is False

    @pytest.mark.skipif(not HAS_PYDANTIC, reason="pydantic not installed")
    def test_pydantic_model_returns_false(self) -> None:
        from pydantic import BaseModel

        class M(BaseModel):
            x: int

        assert _is_struct_type(M) is False


# ---------------------------------------------------------------------------
# _convert_struct — msgspec.Struct
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_MSGSPEC, reason="msgspec not installed")
class TestConvertStructMsgspec:
    def test_coerces_string_to_int(self) -> None:
        class Params(msgspec.Struct):
            page: int
            size: int

        result = _convert_struct({"page": "3", "size": "10"}, Params, "params")
        assert isinstance(result, Params)
        assert result.page == 3
        assert result.size == 10

    def test_uses_struct_defaults_for_missing_fields(self) -> None:
        class Params(msgspec.Struct):
            page: int
            size: int = 20

        result = _convert_struct({"page": "1"}, Params, "params")
        assert isinstance(result, Params)
        assert result.page == 1
        assert result.size == 20

    def test_raises_extractor_error_on_invalid_type(self) -> None:
        class Params(msgspec.Struct):
            page: int

        with pytest.raises(ExtractorError):
            _convert_struct({"page": "not-a-number"}, Params, "params")

    def test_handles_float_field(self) -> None:
        class Params(msgspec.Struct):
            ratio: float

        result = _convert_struct({"ratio": "3.14"}, Params, "params")
        assert result.ratio == pytest.approx(3.14)

    def test_handles_bool_field(self) -> None:
        class Params(msgspec.Struct):
            active: bool

        result = _convert_struct({"active": "true"}, Params, "params")
        assert result.active is True


# ---------------------------------------------------------------------------
# _convert_struct — Python dataclass
# ---------------------------------------------------------------------------


class TestConvertStructDataclass:
    def test_coerces_string_to_int(self) -> None:
        result = _convert_struct({"page": "5", "size": "30"}, PageDC, "params")
        assert isinstance(result, PageDC)
        assert result.page == 5
        assert result.size == 30

    def test_uses_dataclass_defaults_for_missing_fields(self) -> None:
        result = _convert_struct({"page": "2"}, PageDC, "params")
        assert isinstance(result, PageDC)
        assert result.page == 2
        assert result.size == 20  # default

    def test_raises_extractor_error_on_invalid_type(self) -> None:
        with pytest.raises(ExtractorError):
            _convert_struct({"page": "not-a-number"}, PageDC, "params")

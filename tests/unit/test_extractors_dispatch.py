"""Unit tests for extractors.py changes in Phase 3."""

from __future__ import annotations

import dataclasses
from typing import TypedDict

import pytest


@dataclasses.dataclass
class Widget:
    name: str
    weight: float = 0.0


class WidgetTD(TypedDict):
    name: str


class TestValidateJsonDispatch:
    def test_dispatches_to_dataclass(self):
        from lauren.extractors import _validate_json

        result = _validate_json({"name": "cog"}, Widget, "body")
        assert isinstance(result, Widget)

    def test_dispatches_to_typeddict(self):
        from lauren.extractors import _validate_json

        result = _validate_json({"name": "cog"}, WidgetTD, "body")
        assert result["name"] == "cog"

    def test_pydantic_model_still_dispatches_when_installed(self):
        pytest.importorskip("pydantic")
        from pydantic import BaseModel

        from lauren.extractors import _validate_json

        class M(BaseModel):
            name: str

        result = _validate_json({"name": "test"}, M, "body")
        assert result.name == "test"

    def test_validate_pydantic_function_deleted(self):
        import lauren.extractors as mod

        assert not hasattr(mod, "_validate_pydantic"), "_validate_pydantic() must be deleted in Phase 3"

    def test_pydantic_available_flag_exists(self):
        import lauren.extractors as mod

        assert hasattr(mod, "_PYDANTIC_AVAILABLE")

    def test_scalar_passthrough(self):
        from lauren.extractors import _validate_json

        assert _validate_json(42, int, "body") == 42
        assert _validate_json("hello", str, "body") == "hello"

    def test_missing_required_dataclass_raises_extractor_error(self):
        from lauren.exceptions import ExtractorError
        from lauren.extractors import _validate_json

        with pytest.raises(ExtractorError):
            _validate_json({}, Widget, "body")


class TestIsPydanticModelType:
    def test_returns_false_for_dataclass(self):
        from lauren.extractors import _is_pydantic_model_type

        assert _is_pydantic_model_type(Widget) is False

    def test_returns_true_for_basemodel(self):
        pytest.importorskip("pydantic")
        from pydantic import BaseModel

        from lauren.extractors import _is_pydantic_model_type

        class M(BaseModel):
            x: int

        assert _is_pydantic_model_type(M) is True


class TestInvokeAdapter:
    def test_invoke_adapter_maps_errors_to_extractor_error(self):
        pytest.importorskip("pydantic")
        from pydantic import BaseModel, TypeAdapter

        from lauren.exceptions import ExtractorError
        from lauren.extractors import _invoke_adapter

        class M(BaseModel):
            count: int

        ta = TypeAdapter(M)

        class _Adapter:
            def validate_python(self, data):
                return ta.validate_python(data)

        with pytest.raises(ExtractorError):
            _invoke_adapter(_Adapter(), {"count": "bad"}, "field")

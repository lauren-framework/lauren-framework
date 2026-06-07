"""Verify lauren imports succeed when pydantic and msgspec are absent."""

import sys
import pytest


def _nuke_module(monkeypatch, *names: str) -> None:
    """Remove modules from sys.modules and block future imports."""
    for name in names:
        to_remove = [k for k in sys.modules if k == name or k.startswith(f"{name}.")]
        for k in to_remove:
            monkeypatch.delitem(sys.modules, k, raising=False)
        monkeypatch.setitem(sys.modules, name, None)


@pytest.fixture(autouse=True)
def fresh_lauren(monkeypatch):
    """Force re-import of lauren after nuking pydantic/msgspec."""
    _nuke_module(monkeypatch, "pydantic", "pydantic_core", "msgspec")
    lauren_keys = [k for k in sys.modules if k.startswith("lauren")]
    for k in lauren_keys:
        monkeypatch.delitem(sys.modules, k, raising=False)


class TestFrameworkImportability:
    def test_lauren_importable(self):
        import importlib

        mod = importlib.import_module("lauren")
        assert mod is not None

    def test_extractors_importable(self):
        import importlib

        importlib.import_module("lauren.extractors")

    def test_streaming_importable(self):
        import importlib

        importlib.import_module("lauren.streaming")

    def test_serialization_importable(self):
        import importlib

        importlib.import_module("lauren.serialization")

    def test_asgi_importable(self):
        import importlib

        importlib.import_module("lauren._asgi")

    def test_openapi_importable(self):
        import importlib

        importlib.import_module("lauren._asgi._openapi")

    def test_ws_runtime_importable(self):
        import importlib

        importlib.import_module("lauren._ws_runtime")

    def test_validation_module_importable(self):
        import importlib

        importlib.import_module("lauren._validation")

    def test_discriminated_module_importable(self):
        import importlib

        importlib.import_module("lauren._discriminated")


class TestPydanticUnavailableFlags:
    def test_pydantic_available_false(self):
        import lauren.extractors

        assert lauren.extractors._PYDANTIC_AVAILABLE is False

    def test_is_pydantic_model_returns_false(self):
        from lauren._validation import is_pydantic_model
        import dataclasses

        @dataclasses.dataclass
        class DC:
            x: int

        assert is_pydantic_model(DC) is False


class TestDiscriminatedImportable:
    def test_discriminated_importable_without_pydantic(self):
        import importlib

        m = importlib.import_module("lauren")
        assert hasattr(m, "Discriminated")

    def test_can_create_discriminated_type_without_pydantic(self):
        from lauren import Discriminated
        import dataclasses
        from typing import Literal

        @dataclasses.dataclass
        class A:
            kind: Literal["a"] = "a"
            x: int = 0

        @dataclasses.dataclass
        class B:
            kind: Literal["b"] = "b"
            y: int = 0

        DType = Discriminated[A | B, "kind"]
        assert DType is not None

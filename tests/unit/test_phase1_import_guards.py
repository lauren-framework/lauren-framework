"""Phase 1 unit tests: lauren can be imported without pydantic installed."""

from __future__ import annotations

import importlib
import sys
import types


def _strip_pydantic(monkeypatch: object) -> None:
    """Make pydantic appear unimportable for the duration of a test."""
    import unittest.mock as mock

    class _Blocker(types.ModuleType):
        def __init__(self) -> None:
            super().__init__("pydantic")

        def __getattr__(self, name: str) -> object:
            raise ImportError("pydantic intentionally blocked")

    # Patch sys.modules so that `import pydantic` raises ImportError
    blocker = mock.MagicMock(side_effect=ImportError("pydantic intentionally blocked"))
    return blocker


def test_lauren_imports_without_pydantic(monkeypatch):
    """Core lauren namespace must be importable even when pydantic is absent."""
    import lauren  # noqa: F401  — just importing must not raise

    assert hasattr(lauren, "LaurenFactory")
    assert hasattr(lauren, "controller")


def test_validation_module_imports_without_pydantic(monkeypatch):
    """_validation module must be importable without pydantic present."""
    # Reload is safe here because the module does no top-level pydantic import
    if "lauren._validation" in sys.modules:
        del sys.modules["lauren._validation"]

    mod = importlib.import_module("lauren._validation")
    assert hasattr(mod, "is_pydantic_model")
    assert hasattr(mod, "validate_as")
    assert hasattr(mod, "json_schema_for")


def test_is_pydantic_model_returns_false_without_pydantic(monkeypatch):
    """is_pydantic_model must return False, not raise, when pydantic is missing."""
    monkeypatch.setitem(sys.modules, "pydantic", None)  # type: ignore[arg-type]

    # Re-import after patching
    if "lauren._validation" in sys.modules:
        del sys.modules["lauren._validation"]
    from lauren._validation import is_pydantic_model

    class Dummy:
        pass

    # Should not raise ImportError
    assert is_pydantic_model(Dummy) is False


def test_validation_public_api_surface():
    """All documented public symbols are exported from lauren._validation."""
    from lauren import _validation

    expected = {
        "is_pydantic_model",
        "is_msgspec_struct",
        "is_dataclass",
        "is_typeddict",
        "is_json_body_type",
        "validate_as",
        "json_schema_for",
    }
    assert expected.issubset(set(_validation.__all__))

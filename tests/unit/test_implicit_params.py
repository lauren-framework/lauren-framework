"""Unit tests for implicit (auto-detected) parameter extraction.

Covers the two new promotion rules added to
``_compile_handler_signature``:

1. **Bare Pydantic BaseModel** (possibly ``Optional[Model]``) →
   automatically extracted from the JSON request body (``source="json"``).
2. **Bare scalar annotation** (``int``, ``str``, ``float``, ``bool``,
   ``bytes``, ``complex``) — possibly ``Optional[scalar]`` or
   ``list[scalar]`` — automatically extracted as a query-string param
   (``source="query"``).

These tests operate at the unit level: they call
``_compile_handler_signature`` directly (or indirectly via
``LaurenFactory.create``) without issuing real HTTP requests so that
the *compilation phase* can be verified cheaply and in isolation.
"""

from __future__ import annotations

import inspect
from typing import Optional

import pytest
from pydantic import BaseModel

from lauren import (
    LaurenFactory,
    controller,
    get,
    module,
    post,
)
from lauren.exceptions import UnresolvableParameterError
from lauren.extractors import (
    _is_implicit_query_type,
    _is_pydantic_model_type,
    _SCALAR_TYPES,
)


# ---------------------------------------------------------------------------
# Helper — build a minimal LaurenApp for a single controller without
# starting it, then inspect the compiled handler's extractions.
# ---------------------------------------------------------------------------


def _extractions_for(ctrl_cls: type, method_name: str) -> dict:
    """Return a {param_name: extraction} dict for one compiled handler."""

    @module(controllers=[ctrl_cls])
    class M:
        pass

    app = LaurenFactory.create(M)
    # _handlers is a dict[tuple[method, path_template], CompiledHandler]
    for compiled in app._handlers.values():
        if compiled.handler_fn.__name__ == method_name:
            return {e.name: e for e in compiled.extractions}
    raise AssertionError(f"Handler {method_name!r} not found in compiled routes")


# ---------------------------------------------------------------------------
# Tests for the helper predicates in extractors.py
# ---------------------------------------------------------------------------


class TestIsImplicitQueryType:
    """Unit tests for ``_is_implicit_query_type``."""

    @pytest.mark.parametrize("typ", list(_SCALAR_TYPES))
    def test_scalar_types_detected(self, typ):
        assert _is_implicit_query_type(typ) is True

    def test_optional_int(self):
        assert _is_implicit_query_type(Optional[int]) is True

    def test_optional_str(self):
        assert _is_implicit_query_type(Optional[str]) is True

    def test_optional_bool(self):
        assert _is_implicit_query_type(Optional[bool]) is True

    def test_list_int(self):
        assert _is_implicit_query_type(list[int]) is True

    def test_list_str(self):
        assert _is_implicit_query_type(list[str]) is True

    def test_list_float(self):
        assert _is_implicit_query_type(list[float]) is True

    def test_tuple_int(self):
        assert _is_implicit_query_type(tuple[int, ...]) is True

    def test_empty_annotation_returns_false(self):
        # Bare, unannotated params should NOT be auto-promoted.
        assert _is_implicit_query_type(inspect.Parameter.empty) is False

    def test_pydantic_model_not_scalar(self):
        class M(BaseModel):
            x: int

        assert _is_implicit_query_type(M) is False

    def test_arbitrary_class_not_scalar(self):
        class Svc:
            pass

        assert _is_implicit_query_type(Svc) is False

    def test_list_of_model_not_scalar(self):
        class M(BaseModel):
            x: int

        assert _is_implicit_query_type(list[M]) is False

    def test_dict_not_scalar(self):
        assert _is_implicit_query_type(dict[str, int]) is False


class TestIsPydanticModelType:
    """Unit tests for ``_is_pydantic_model_type``."""

    def test_plain_model(self):
        class M(BaseModel):
            x: int

        assert _is_pydantic_model_type(M) is True

    def test_optional_model(self):
        class M(BaseModel):
            x: int

        assert _is_pydantic_model_type(Optional[M]) is True

    def test_union_model_none(self):
        class M(BaseModel):
            x: int

        assert _is_pydantic_model_type(M | None) is True

    def test_int_not_model(self):
        assert _is_pydantic_model_type(int) is False

    def test_arbitrary_class_not_model(self):
        class C:
            pass

        assert _is_pydantic_model_type(C) is False

    def test_none_type_not_model(self):
        assert _is_pydantic_model_type(type(None)) is False


# ---------------------------------------------------------------------------
# Compilation-phase tests — verify that _compile_handler_signature promotes
# params to the right source at app-build time.
# ---------------------------------------------------------------------------


class TestImplicitQueryParamCompilation:
    """Verify that scalar params compile to source='query'."""

    def test_int_compiles_to_query(self):
        @controller("/x")
        class C:
            @get("/")
            async def h(self, q: int) -> dict:
                return {}

        exts = _extractions_for(C, "h")
        assert exts["q"].source == "query"
        assert exts["q"].inner_type is int

    def test_str_compiles_to_query(self):
        @controller("/x")
        class C:
            @get("/")
            async def h(self, name: str) -> dict:
                return {}

        exts = _extractions_for(C, "h")
        assert exts["name"].source == "query"

    def test_float_compiles_to_query(self):
        @controller("/x")
        class C:
            @get("/")
            async def h(self, amount: float) -> dict:
                return {}

        exts = _extractions_for(C, "h")
        assert exts["amount"].source == "query"

    def test_bool_compiles_to_query(self):
        @controller("/x")
        class C:
            @get("/")
            async def h(self, flag: bool) -> dict:
                return {}

        exts = _extractions_for(C, "h")
        assert exts["flag"].source == "query"

    def test_optional_int_compiles_to_query(self):
        @controller("/x")
        class C:
            @get("/")
            async def h(self, q: Optional[int] = None) -> dict:
                return {}

        exts = _extractions_for(C, "h")
        assert exts["q"].source == "query"

    def test_list_int_compiles_to_query(self):
        @controller("/x")
        class C:
            @get("/")
            async def h(self, ids: list[int]) -> dict:
                return {}

        exts = _extractions_for(C, "h")
        assert exts["ids"].source == "query"

    def test_list_str_compiles_to_query(self):
        @controller("/x")
        class C:
            @get("/")
            async def h(self, tags: list[str]) -> dict:
                return {}

        exts = _extractions_for(C, "h")
        assert exts["tags"].source == "query"


class TestImplicitBodyParamCompilation:
    """Verify that Pydantic model params compile to source='json'."""

    def test_model_compiles_to_json(self):
        class Payload(BaseModel):
            name: str
            value: int

        @controller("/x")
        class C:
            @post("/")
            async def h(self, body: Payload) -> dict:
                return {}

        exts = _extractions_for(C, "h")
        assert exts["body"].source == "json"
        assert exts["body"].reads_body is True

    def test_optional_model_compiles_to_json(self):
        class Payload(BaseModel):
            name: str

        @controller("/x")
        class C:
            @post("/")
            async def h(self, body: Optional[Payload] = None) -> dict:
                return {}

        exts = _extractions_for(C, "h")
        assert exts["body"].source == "json"
        assert exts["body"].reads_body is True

    def test_model_union_none_compiles_to_json(self):
        class Payload(BaseModel):
            name: str

        @controller("/x")
        class C:
            @post("/")
            async def h(self, body: Payload | None = None) -> dict:
                return {}

        exts = _extractions_for(C, "h")
        assert exts["body"].source == "json"


class TestMixedImplicitAndExplicit:
    """Verify mixed handler signatures compile correctly."""

    def test_path_and_query_and_body(self):
        class Payload(BaseModel):
            title: str

        @controller("/items")
        class C:
            @post("/{item_id}")
            async def h(self, item_id: int, q: str, body: Payload) -> dict:
                return {}

        exts = _extractions_for(C, "h")
        assert exts["item_id"].source == "path"
        assert exts["q"].source == "query"
        assert exts["body"].source == "json"

    def test_multiple_query_params(self):
        @controller("/")
        class C:
            @get("/")
            async def h(self, page: int, page_size: int, search: str) -> dict:
                return {}

        exts = _extractions_for(C, "h")
        assert exts["page"].source == "query"
        assert exts["page_size"].source == "query"
        assert exts["search"].source == "query"


class TestNonScalarNonModelRaisesAtStartup:
    """Verify that non-scalar, non-model, non-DI params still raise."""

    def test_arbitrary_class_raises(self):
        class NotAModel:
            pass

        @controller("/x")
        class C:
            @get("/")
            async def h(self, svc: NotAModel) -> dict:
                return {}

        @module(controllers=[C])
        class M:
            pass

        with pytest.raises(UnresolvableParameterError):
            LaurenFactory.create(M)

    def test_unannotated_param_raises(self):
        @controller("/x")
        class C:
            @get("/")
            async def h(self, mystery) -> dict:
                return {}

        @module(controllers=[C])
        class M:
            pass

        with pytest.raises(UnresolvableParameterError):
            LaurenFactory.create(M)

    def test_list_of_non_scalar_class_raises(self):
        class Plugin:
            pass

        @controller("/x")
        class C:
            @get("/")
            async def h(self, plugins: list[Plugin]) -> dict:
                return {}

        @module(controllers=[C])
        class M:
            pass

        with pytest.raises(Exception):
            LaurenFactory.create(M)

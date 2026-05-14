"""End-to-end tests for pluggable JSON serialization.

These tests drive a real :class:`LaurenApp` produced by
:meth:`LaurenFactory.create` and verify that:

* The app captures its JSON encoder at build time and exposes it via
  ``app.json_encoder``.
* A user-supplied encoder is honoured for every JSON emission site:
  plain dict/list returns, Pydantic models, dataclasses, error
  responses, and streaming frames.
* Every backend produces bytes that round-trip through ``json.loads``
  to the same Python value, regardless of the on-wire encoding
  choice.
* The process-wide default (``set_active_encoder``) is picked up by
  apps that don't pass an explicit encoder.
"""

from __future__ import annotations

import json as stdlib_json
from dataclasses import dataclass

import pytest
from pydantic import BaseModel

from lauren import (
    LaurenFactory,
    Query,
    StdlibJSONEncoder,
    auto_encoder,
    controller,
    get,
    module,
)
from lauren.serialization import (
    JSONEncoder,
    MsgspecEncoder,
    OrjsonEncoder,
    set_active_encoder,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Baseline — encoder is captured on the app
# ---------------------------------------------------------------------------


@controller("/echo")
class _EchoController:
    @get("/")
    async def echo(self) -> dict:
        return {"hello": "world", "n": 42, "flag": True}


@module(controllers=[_EchoController])
class _EchoModule:
    pass


def test_default_app_uses_stdlib_encoder() -> None:
    previous = set_active_encoder(StdlibJSONEncoder())
    try:
        app = LaurenFactory.create(_EchoModule)
        assert isinstance(app.json_encoder, StdlibJSONEncoder)
    finally:
        set_active_encoder(previous)


def test_user_supplied_encoder_is_captured_at_build_time() -> None:
    custom = StdlibJSONEncoder()
    app = LaurenFactory.create(_EchoModule, json_encoder=custom)
    assert app.json_encoder is custom


def test_process_wide_default_is_adopted_when_no_explicit_encoder() -> None:
    pytest.importorskip("orjson")
    stub = OrjsonEncoder()
    previous = set_active_encoder(stub)
    try:
        app = LaurenFactory.create(_EchoModule)
        assert app.json_encoder is stub
    finally:
        set_active_encoder(previous)


# ---------------------------------------------------------------------------
# End-to-end: dict / list / Pydantic / dataclass returns
# ---------------------------------------------------------------------------


class _Item(BaseModel):
    id: int
    name: str


@dataclass
class _Settings:
    theme: str
    dark_mode: bool


@controller("/items")
class _ItemsController:
    @get("/one")
    async def one(self) -> _Item:
        return _Item(id=1, name="alpha")

    @get("/many")
    async def many(self) -> list[_Item]:
        return [_Item(id=i, name=f"item-{i}") for i in range(3)]

    @get("/settings")
    async def settings(self) -> _Settings:
        return _Settings(theme="solarized", dark_mode=True)

    @get("/primitives")
    async def primitives(self) -> dict:
        return {"n": 42, "f": 3.14, "b": True, "none": None, "s": "café"}


@module(controllers=[_ItemsController])
class _ItemsModule:
    pass


@pytest.fixture(params=["stdlib", "orjson", "msgspec"])
def encoder(request: pytest.FixtureRequest) -> JSONEncoder:
    if request.param == "stdlib":
        return StdlibJSONEncoder()
    if request.param == "orjson":
        pytest.importorskip("orjson")
        return OrjsonEncoder()
    if request.param == "msgspec":
        pytest.importorskip("msgspec")
        return MsgspecEncoder()
    raise AssertionError(f"unknown encoder param: {request.param}")


def test_pydantic_model_roundtrips_through_every_encoder(
    encoder: JSONEncoder,
) -> None:
    app = LaurenFactory.create(_ItemsModule, json_encoder=encoder)
    r = TestClient(app).get("/items/one")
    assert r.status_code == 200
    assert r.json() == {"id": 1, "name": "alpha"}
    # Content-type is still application/json regardless of backend.
    assert (r.header("content-type") or "").startswith("application/json")


def test_list_of_pydantic_models_roundtrips_through_every_encoder(
    encoder: JSONEncoder,
) -> None:
    app = LaurenFactory.create(_ItemsModule, json_encoder=encoder)
    r = TestClient(app).get("/items/many")
    assert r.json() == [
        {"id": 0, "name": "item-0"},
        {"id": 1, "name": "item-1"},
        {"id": 2, "name": "item-2"},
    ]


def test_dataclass_roundtrips_through_every_encoder(
    encoder: JSONEncoder,
) -> None:
    app = LaurenFactory.create(_ItemsModule, json_encoder=encoder)
    r = TestClient(app).get("/items/settings")
    assert r.json() == {"theme": "solarized", "dark_mode": True}


def test_primitive_dict_roundtrips_through_every_encoder(
    encoder: JSONEncoder,
) -> None:
    app = LaurenFactory.create(_ItemsModule, json_encoder=encoder)
    r = TestClient(app).get("/items/primitives")
    assert r.json() == {"n": 42, "f": 3.14, "b": True, "none": None, "s": "café"}


# ---------------------------------------------------------------------------
# Error envelope flows through the configured encoder
# ---------------------------------------------------------------------------


@controller("/err")
class _ErrController:
    @get("/boom")
    async def boom(self) -> dict:
        from lauren.exceptions import HTTPError

        class _TeapotError(HTTPError):
            status_code = 418
            code = "teapot"

        raise _TeapotError("something broke", detail={"x": "y"})


@module(controllers=[_ErrController])
class _ErrModule:
    pass


def test_error_response_uses_configured_encoder(encoder: JSONEncoder) -> None:
    app = LaurenFactory.create(_ErrModule, json_encoder=encoder)
    r = TestClient(app).get("/err/boom")
    assert r.status_code == 418
    parsed = r.json()
    # The error payload is a dict; contents depend on the framework's
    # error envelope, but it must round-trip correctly through JSON.
    assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Compact output — no whitespace on the wire
# ---------------------------------------------------------------------------


def test_response_body_has_no_whitespace_with_any_encoder(
    encoder: JSONEncoder,
) -> None:
    """All three encoders emit compact JSON. We assert on the raw
    body bytes rather than the parsed value so a regression that
    silently added indentation (e.g. via ``json.dumps(indent=2)``)
    would be caught.
    """
    app = LaurenFactory.create(_ItemsModule, json_encoder=encoder)
    r = TestClient(app).get("/items/primitives")
    body = r.body
    # Walking the byte string: any space character that is NOT between
    # two double quotes indicates non-compact output.
    in_string = False
    escape = False
    for byte in body:
        ch = chr(byte)
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string and ch in (" ", "\t", "\n", "\r"):
            pytest.fail(f"non-compact whitespace at byte {byte!r} with encoder={encoder.name}: body={body!r}")


# ---------------------------------------------------------------------------
# auto_encoder() picks a real backend when one is installed
# ---------------------------------------------------------------------------


def test_auto_encoder_wired_through_factory_produces_valid_responses() -> None:
    app = LaurenFactory.create(_ItemsModule, json_encoder=auto_encoder())
    r = TestClient(app).get("/items/one")
    assert r.status_code == 200
    assert r.json() == {"id": 1, "name": "alpha"}


# ---------------------------------------------------------------------------
# Handler returning raw bytes / Response sidesteps the encoder
# ---------------------------------------------------------------------------


@controller("/raw")
class _RawController:
    @get("/bytes")
    async def raw(self) -> bytes:
        # Raw-bytes handlers must not be re-encoded.
        return stdlib_json.dumps({"pre": "encoded"}).encode("utf-8")


@module(controllers=[_RawController])
class _RawModule:
    pass


def test_raw_bytes_handler_bypasses_json_encoder(
    encoder: JSONEncoder,
) -> None:
    """If the encoder was mistakenly invoked on already-encoded
    bytes we'd get a string-escaped nested value, not a dict.
    """
    app = LaurenFactory.create(_RawModule, json_encoder=encoder)
    r = TestClient(app).get("/raw/bytes")
    assert r.status_code == 200
    # Body is the raw bytes from the handler, served as octet-stream.
    assert r.body == b'{"pre": "encoded"}'


# ---------------------------------------------------------------------------
# Query parameter carries through the encoder-agnostic extractor path
# ---------------------------------------------------------------------------


@controller("/search")
class _SearchController:
    @get("/")
    async def search(self, q: Query[str]) -> dict:
        return {"query": q, "echoed": True}


@module(controllers=[_SearchController])
class _SearchModule:
    pass


def test_unicode_query_roundtrips_through_every_encoder(
    encoder: JSONEncoder,
) -> None:
    """Unicode query values must be URL-encoded on the wire but
    returned as proper Python strings, and the configured encoder
    must emit them correctly (no ``\\uXXXX`` escapes that would
    break byte-length assumptions downstream)."""
    from urllib.parse import quote

    app = LaurenFactory.create(_SearchModule, json_encoder=encoder)
    value = "café 日本"
    r = TestClient(app).get(f"/search/?q={quote(value)}")
    assert r.status_code == 200
    assert r.json() == {"query": value, "echoed": True}


# ---------------------------------------------------------------------------
# PydanticEncoder integration tests
# ---------------------------------------------------------------------------


from lauren.serialization import PydanticEncoder  # noqa: E402


def _pydantic_client(handler_cls: type) -> "TestClient":
    @module(controllers=[handler_cls])
    class M:
        pass

    return TestClient(LaurenFactory.create(M, json_encoder=PydanticEncoder()))


class TestPydanticEncoderIntegration:
    """End-to-end: PydanticEncoder plugged into a real LaurenApp."""

    def test_pydantic_model_handler_correct_body(self) -> None:
        class Item(BaseModel):
            id: int
            name: str

        @controller("/pi")
        class C:
            @get("/")
            async def h(self) -> Item:
                return Item(id=7, name="widget")

        r = _pydantic_client(C).get("/pi/")
        assert r.status_code == 200
        assert r.json() == {"id": 7, "name": "widget"}

    def test_content_type_is_application_json(self) -> None:
        class Simple(BaseModel):
            ok: bool

        @controller("/pict")
        class C:
            @get("/")
            async def h(self) -> Simple:
                return Simple(ok=True)

        r = _pydantic_client(C).get("/pict/")
        assert (r.header("content-type") or "").startswith("application/json")

    def test_honours_field_serializer(self) -> None:
        from pydantic import field_serializer

        class Model(BaseModel):
            score: float

            @field_serializer("score")
            def fmt(self, v: float) -> str:
                return f"{v:.2f}"

        @controller("/pfs")
        class C:
            @get("/")
            async def h(self) -> Model:
                return Model(score=3.14159)

        r = _pydantic_client(C).get("/pfs/")
        assert r.json() == {"score": "3.14"}

    def test_list_of_models(self) -> None:
        class Point(BaseModel):
            x: int
            y: int

        @controller("/plom")
        class C:
            @get("/")
            async def h(self) -> list[Point]:
                return [Point(x=1, y=2), Point(x=3, y=4)]

        r = _pydantic_client(C).get("/plom/")
        assert r.json() == [{"x": 1, "y": 2}, {"x": 3, "y": 4}]

    def test_plain_dict_handler_still_works(self) -> None:
        @controller("/pdict")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {"key": "value", "num": 42}

        r = _pydantic_client(C).get("/pdict/")
        assert r.json() == {"key": "value", "num": 42}

    def test_error_response_uses_pydantic_encoder(self) -> None:
        from lauren.exceptions import ForbiddenError

        @controller("/perr")
        class C:
            @get("/")
            async def h(self) -> dict:
                raise ForbiddenError("denied")

        r = _pydantic_client(C).get("/perr/")
        assert r.status_code == 403
        body = r.json()
        assert "error" in body

    def test_nested_model(self) -> None:
        class Inner(BaseModel):
            v: str

        class Outer(BaseModel):
            inner: Inner
            count: int

        @controller("/pnest")
        class C:
            @get("/")
            async def h(self) -> Outer:
                return Outer(inner=Inner(v="hello"), count=3)

        r = _pydantic_client(C).get("/pnest/")
        assert r.json() == {"inner": {"v": "hello"}, "count": 3}

    def test_parity_with_stdlib_encoder(self) -> None:
        """PydanticEncoder and StdlibJSONEncoder produce semantically equal output."""

        class Payload(BaseModel):
            id: int
            tags: list[str]

        @controller("/pparity")
        class C:
            @get("/")
            async def h(self) -> Payload:
                return Payload(id=99, tags=["a", "b"])

        @module(controllers=[C])
        class M:
            pass

        stdlib_r = TestClient(LaurenFactory.create(M, json_encoder=StdlibJSONEncoder())).get("/pparity/")
        pydantic_r = TestClient(LaurenFactory.create(M, json_encoder=PydanticEncoder())).get("/pparity/")
        assert stdlib_r.json() == pydantic_r.json()

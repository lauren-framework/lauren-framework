"""End-to-end: the content-type CSRF backstop and multi-backend codecs (F03 §10.7).

A cookie-session app that opted into ``same_site="none"`` (a cross-site SPA)
receives what a browser sends for a forged HTML form with
``enctype="text/plain"``: no CORS preflight, the session cookie attached and
a body that happens to be valid JSON. Lenient content handling processes it
(the documented hazard); ``strict_content_type=True`` answers 415 before the
body is read. The second half round-trips JSON — and MessagePack, when
msgspec is installed — through pydantic, msgspec, dataclass and TypedDict
bodies in one app.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypedDict

import pytest

from lauren import (
    ContentConfig,
    Json,
    LaurenFactory,
    Session,
    SessionConfig,
    controller,
    get,
    module,
    post,
)
from lauren.testing import TestClient

#: The body a forged ``<form enctype="text/plain">`` produces with an input
#: named ``{"amount": 100, "to": "mallory", "pad": "`` and the value ``"}``.
FORGED = b'{"amount": 100, "to": "mallory", "pad": "="}'


@controller("/bank")
class Bank:
    @get("/login")
    async def login(self, session: Session) -> dict:
        session["user"] = "alice"
        return {"user": "alice"}

    @post("/transfer")
    async def transfer(self, session: Session, body: Json[dict]) -> dict:
        if session.get("user") is None:
            return {"error": "anonymous"}
        return {"sent": body["amount"], "to": body["to"], "by": session["user"]}


@module(controllers=[Bank])
class BankModule:
    pass


@contextmanager
def _bank(strict: Any) -> Iterator[TestClient]:
    app = LaurenFactory.create(
        BankModule,
        sessions=SessionConfig(secret="s" * 32, same_site="none", secure=True),
        content=ContentConfig(strict_content_type=strict),
    )
    with TestClient(app, base_url="https://bank.example") as client:
        assert client.get("/bank/login").status_code == 200  # the victim's session cookie
        yield client


def test_lenient_content_handling_processes_a_forged_form_post() -> None:
    with _bank(False) as client:
        response = client.post("/bank/transfer", content=FORGED, headers={"content-type": "text/plain"})

    assert response.status_code == 200
    assert response.json() == {"sent": 100, "to": "mallory", "by": "alice"}


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", None])
def test_strict_content_type_rejects_the_forged_post(content_type: str | None) -> None:
    headers = {"content-type": content_type} if content_type is not None else {}
    with _bank(True) as client:
        response = client.post("/bank/transfer", content=FORGED, headers=headers)

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


def test_strict_content_type_still_serves_the_real_client() -> None:
    with _bank(True) as client:
        response = client.post("/bank/transfer", json={"amount": 5, "to": "bob"})

    assert response.json() == {"sent": 5, "to": "bob", "by": "alice"}


# --- one app, every validator backend, JSON and MessagePack --------------------


@dataclass
class DataItem:
    sku: str
    qty: int


class DictItem(TypedDict):
    sku: str
    qty: int


def _multi_backend_app(codecs: list[Any]) -> Any:
    pydantic = pytest.importorskip("pydantic")

    class PydItem(pydantic.BaseModel):
        sku: str
        qty: int

    media = [c.media_types[0] for c in codecs]
    both = ["application/json", *media]

    @controller("/items")
    class Items:
        @post("/pydantic", consumes=both, produces=both)
        async def pyd(self, body: Json[PydItem]) -> PydItem:
            return body

        @post("/dataclass", consumes=both, produces=both)
        async def data(self, body: Json[DataItem]) -> DataItem:
            return body

        @post("/typeddict", consumes=both, produces=both)
        async def typed(self, body: Json[DictItem]) -> DictItem:
            return body

        @post("/msgspec", consumes=both, produces=both)
        async def struct(self, body: Json[dict]) -> dict:
            return body

    routes = ["/items/pydantic", "/items/dataclass", "/items/typeddict", "/items/msgspec"]
    try:
        import msgspec

        class StructItem(msgspec.Struct):
            sku: str
            qty: int

        @controller("/structs")
        class Structs:
            @post("/", consumes=both, produces=both)
            async def struct(self, body: Json[StructItem]) -> StructItem:
                return body

        controllers: list[type] = [Items, Structs]
        routes.append("/structs/")
    except ImportError:
        controllers = [Items]

    @module(controllers=controllers)
    class ShopModule:
        pass

    return LaurenFactory.create(ShopModule, content=ContentConfig(codecs=codecs)), routes


def test_every_backend_round_trips_json() -> None:
    app, routes = _multi_backend_app([])
    client = TestClient(app)

    for route in routes:
        ok = client.post(route, json={"sku": "a-1", "qty": 2})
        bad = client.post(
            route, content=b'{"sku": "a-1", "qty": ', headers={"content-type": "application/json"}
        )

        assert ok.status_code == 200, route
        assert ok.json() == {"sku": "a-1", "qty": 2}, route
        assert bad.status_code == 422 and bad.json()["error"]["detail"]["reason"] == "malformed_body", route


def test_every_backend_round_trips_msgpack() -> None:
    pytest.importorskip("msgspec")
    from lauren import MsgpackCodec

    codec = MsgpackCodec()
    app, routes = _multi_backend_app([codec])
    client = TestClient(app)
    headers = {"content-type": "application/msgpack", "accept": "application/msgpack"}

    for route in routes:
        response = client.post(route, content=codec.encode({"sku": "b-2", "qty": 3}), headers=headers)

        assert response.status_code == 200, route
        assert response.headers["content-type"] == "application/msgpack", route
        assert codec.decode(response.content) == {"sku": "b-2", "qty": 3}, route


def test_struct_bodies_report_msgpack_validation_errors_as_issues() -> None:
    pytest.importorskip("msgspec")
    from lauren import MsgpackCodec

    codec = MsgpackCodec()
    app, _ = _multi_backend_app([codec])

    response = TestClient(app).post(
        "/structs/",
        content=codec.encode({"sku": "c", "qty": "many"}),
        headers={"content-type": "application/msgpack"},
    )

    issues = response.json()["error"]["detail"]["issues"]
    assert response.status_code == 422
    assert issues[0]["loc"] == ["body", "qty"]

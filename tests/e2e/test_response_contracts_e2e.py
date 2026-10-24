"""E2E: response contracts across every model backend in one app (filter and strict)."""

from __future__ import annotations

import dataclasses
import datetime
import importlib.util
import sys
from typing import Literal, NamedTuple, TypedDict

import pytest

from lauren import (
    Created,
    Discriminated,
    LaurenFactory,
    NoContent,
    Ok,
    controller,
    delete,
    get,
    module,
    post,
    put,
)
from lauren.testing import TestClient


@dataclasses.dataclass
class Address:
    city: str


@dataclasses.dataclass
class Account:
    id: int
    owner: str
    address: Address
    opened: datetime.date


class AccountRow:
    """What a repository hands back: more attributes than the API declares."""

    def __init__(self, id: int) -> None:
        self.id = id
        self.owner = f"owner-{id}"
        self.address = {"city": "Lyon", "geo": "45.7,4.8"}
        self.opened = datetime.date(2024, 1, 2)
        self.iban = "FR76-SECRET"


class Summary(TypedDict):
    total: int
    currency: str


class Pair(NamedTuple):
    left: int
    right: int


@dataclasses.dataclass
class Card:
    kind: Literal["card"]
    last4: str


@dataclasses.dataclass
class Wire:
    kind: Literal["wire"]
    bank: str


Payment = Discriminated[Card | Wire, "kind"]  # noqa: F821


@controller("/bank")
class BankController:
    @get("/accounts/{id}")
    async def account(self, id: int) -> Account:
        return AccountRow(id)  # type: ignore[return-value]

    @get("/accounts")
    async def accounts(self) -> list[Account]:
        return [AccountRow(1), AccountRow(2)]  # type: ignore[list-item]

    @get("/summary")
    async def summary(self) -> Summary:
        return {"total": 3, "currency": "EUR", "debug": "internal"}  # type: ignore[typeddict-unknown-key]

    @get("/pair")
    async def pair(self) -> Pair:
        return Pair(1, 2)

    @get("/payment")
    async def payment(self) -> Payment:
        return {"kind": "card", "last4": "4242", "pan": "4242424242424242"}  # type: ignore[return-value]

    @post("/accounts")
    async def open_account(self) -> Created[Account]:
        return Created(AccountRow(9), location="/bank/accounts/9")  # type: ignore[arg-type]

    @put("/accounts/{id}")
    async def upsert(self, id: int) -> Ok[Account] | Created[Account]:
        row = AccountRow(id)
        return Ok(row) if id < 5 else Created(row, location=f"/bank/accounts/{id}")  # type: ignore[arg-type]

    @delete("/accounts/{id}")
    async def close(self, id: int) -> NoContent:
        return NoContent()


EXPECTED_ACCOUNT = {"id": 1, "owner": "owner-1", "address": {"city": "Lyon"}, "opened": "2024-01-02"}


def _client(mode: str = "filter", extra: list[type] | None = None) -> TestClient:
    @module(controllers=[BankController, *(extra or [])])
    class BankModule:
        pass

    return TestClient(LaurenFactory.create(BankModule, response_validation=mode))


def _stdlib_checks(client: TestClient) -> None:
    assert client.get("/bank/accounts/1").json() == EXPECTED_ACCOUNT
    assert [a["id"] for a in client.get("/bank/accounts").json()] == [1, 2]
    assert "iban" not in client.get("/bank/accounts").text
    assert client.get("/bank/summary").json() == {"total": 3, "currency": "EUR"}
    assert client.get("/bank/pair").json() == {"left": 1, "right": 2}
    assert client.get("/bank/payment").json() == {"kind": "card", "last4": "4242"}
    created = client.post("/bank/accounts")
    assert (created.status_code, created.header("location"), "iban" in created.text) == (
        201,
        "/bank/accounts/9",
        False,
    )
    assert client.put("/bank/accounts/1").status_code == 200
    assert client.put("/bank/accounts/7").status_code == 201
    assert client.delete("/bank/accounts/1").status_code == 204


@pytest.mark.parametrize("mode", ["filter", "strict"])
def test_stdlib_models_are_projected(mode: str) -> None:
    _stdlib_checks(_client(mode))


def test_stdlib_models_are_projected_without_pydantic(
    monkeypatch: pytest.MonkeyPatch, _preload_lauren: None
) -> None:
    import lauren.streaming

    monkeypatch.setitem(sys.modules, "pydantic", None)  # type: ignore[arg-type]
    monkeypatch.setattr(lauren.streaming, "_PYDANTIC_AVAILABLE", False)

    _stdlib_checks(_client("strict"))


def test_off_mode_still_leaks_for_comparison() -> None:
    assert "FR76-SECRET" in _client("off").get("/bank/accounts").text


@pytest.mark.skipif(importlib.util.find_spec("pydantic") is None, reason="pydantic not installed")
@pytest.mark.parametrize("mode", ["filter", "strict"])
def test_pydantic_outputs_share_the_contract(mode: str) -> None:
    import pydantic

    class PublicProfile(pydantic.BaseModel):
        handle: str
        joined: datetime.date

    class StoredProfile(PublicProfile):
        email: str

    @controller("/profiles")
    class ProfileController:
        @get("/me")
        async def me(self) -> PublicProfile:
            return StoredProfile(handle="ada", joined=datetime.date(2020, 5, 1), email="a@x.io")

        @get("/row")
        async def row(self) -> list[PublicProfile]:
            return [AccountRow(1), {"handle": "bo", "joined": "2021-01-01", "email": "b@x.io"}]  # type: ignore[list-item]

    client = _client(mode, extra=[ProfileController])

    assert client.get("/profiles/me").json() == {"handle": "ada", "joined": "2020-05-01"}
    assert client.get("/profiles/row").status_code == 500  # AccountRow has no ``handle``
    _stdlib_checks(client)


@pytest.mark.skipif(importlib.util.find_spec("msgspec") is None, reason="msgspec not installed")
def test_msgspec_outputs_share_the_contract() -> None:
    import msgspec

    class Token(msgspec.Struct):
        name: str

    class StoredToken(Token):
        secret: str = "s3cr3t"

    @controller("/tokens")
    class TokenController:
        @get("/")
        async def tokens(self) -> list[Token]:
            return [StoredToken(name="a"), {"name": "b", "secret": "x"}]  # type: ignore[list-item]

    client = _client("filter", extra=[TokenController])

    assert client.get("/tokens/").json() == [{"name": "a"}, {"name": "b"}]

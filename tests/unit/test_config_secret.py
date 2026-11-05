"""Unit tests for ``Secret[T]`` and secret redaction."""

from __future__ import annotations

import copy
import dataclasses
import json
import pickle

import pytest

from lauren import Secret
from lauren._config._secret import REDACTED, is_secret, is_secret_type
from lauren._sessions._signing import normalize_secrets
from lauren.serialization import StdlibJSONEncoder
from lauren.types import _json_default


def test_repr_and_str_never_contain_the_value() -> None:
    secret = Secret("hunter2")

    assert "hunter2" not in repr(secret)
    assert "hunter2" not in str(secret)
    assert "hunter2" not in f"{secret}"
    assert repr(secret) == f"Secret('{REDACTED}')"


def test_get_secret_value_round_trips() -> None:
    assert Secret("hunter2").get_secret_value() == "hunter2"
    assert Secret(b"raw").get_secret_value() == b"raw"


def test_equality_compares_underlying_values() -> None:
    assert Secret("a") == Secret("a")
    assert Secret("a") != Secret("b")
    assert Secret("a") != "a"


def test_hash_follows_the_underlying_value() -> None:
    assert hash(Secret("a")) == hash(Secret("a"))
    assert len({Secret("a"), Secret("a"), Secret("b")}) == 2


@pytest.mark.parametrize(("value", "expected"), [("x", True), ("", False), (0, False), (3, True)])
def test_truthiness_follows_the_underlying_value(value: object, expected: bool) -> None:
    assert bool(Secret(value)) is expected


def test_secret_is_a_valid_frozen_dataclass_default() -> None:
    @dataclasses.dataclass(frozen=True)
    class Settings:
        key: Secret[str] = Secret("")

    assert Settings().key == Secret("")


def test_secret_survives_copy_and_pickle() -> None:
    secret = Secret("v")

    assert copy.deepcopy(secret).get_secret_value() == "v"
    assert pickle.loads(pickle.dumps(secret)).get_secret_value() == "v"


def test_json_default_returns_the_mask() -> None:
    assert _json_default(Secret("hunter2")) == REDACTED


def test_dataclass_with_secret_encodes_without_the_value() -> None:
    @dataclasses.dataclass(frozen=True)
    class Settings:
        token: Secret[str]
        name: str = "app"

    body = StdlibJSONEncoder().encode({"settings": Settings(Secret("hunter2"))})

    assert b"hunter2" not in body
    assert json.loads(body) == {"settings": {"token": REDACTED, "name": "app"}}


def test_is_secret_recognises_secret_values_and_types() -> None:
    assert is_secret(Secret("x"))
    assert not is_secret("x")
    assert is_secret_type(Secret[str])
    assert is_secret_type(Secret)
    assert not is_secret_type(str)
    assert not is_secret_type(list[str])


@pytest.mark.pydantic
def test_pydantic_secret_types_are_recognised_and_redacted() -> None:
    pydantic = pytest.importorskip("pydantic")

    value = pydantic.SecretStr("hunter2")

    assert is_secret(value)
    assert is_secret_type(pydantic.SecretStr)
    assert _json_default(value) == REDACTED


@pytest.mark.pydantic
def test_secret_field_in_a_pydantic_model() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Model(pydantic.BaseModel):
        token: Secret[int]

    model = Model.model_validate({"token": "42"})

    assert model.token.get_secret_value() == 42
    assert Model.model_validate({"token": Secret(7)}).token.get_secret_value() == 7
    assert json.loads(model.model_dump_json()) == {"token": REDACTED}
    assert isinstance(model.model_dump()["token"], Secret)


@pytest.mark.pydantic
def test_bare_secret_field_in_a_pydantic_model_accepts_anything() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Model(pydantic.BaseModel):
        token: Secret  # type: ignore[type-arg]

    assert Model(token=[1]).token.get_secret_value() == [1]


@pytest.mark.parametrize(
    ("secret", "expected"),
    [
        (Secret("abc"), (b"abc",)),
        (Secret(b"abc"), (b"abc",)),
        ([Secret("new"), "old"], (b"new", b"old")),
        (Secret(""), ()),
    ],
)
def test_session_signing_secrets_accept_secret(secret: object, expected: tuple[bytes, ...]) -> None:
    assert normalize_secrets(secret) == expected

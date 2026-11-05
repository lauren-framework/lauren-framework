"""Integration: the ``json_fallback`` policy for objects no encoder rule covers (PRD F01 §1.3)."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from lauren import (
    Lauren,
    LaurenFactory,
    StartupError,
    StdlibJSONEncoder,
    controller,
    get,
    module,
    use_encoder,
)
from lauren.logging import InMemoryLogger, LogLevel
from lauren.serialization import JSON_FALLBACK_POLICIES, OrjsonEncoder
from lauren.testing import TestClient
from lauren.types import Response


class Secretive:
    """An object with public attributes and no serializer rule."""

    def __init__(self) -> None:
        self.public_name = "x"
        self.api_key = "sk-live-123"
        self._private = "hidden"


class AlsoSecretive:
    def __init__(self) -> None:
        self.token = "t"


@controller("/o")
class ObjectController:
    @get("/obj")
    async def obj(self) -> Any:
        return Secretive()

    @get("/other")
    async def other(self) -> Any:
        return AlsoSecretive()

    @get("/pinned-dict")
    @use_encoder(StdlibJSONEncoder(fallback="dict"))
    async def pinned_dict(self) -> Any:
        return Secretive()

    @get("/pinned-error")
    @use_encoder(StdlibJSONEncoder(fallback="error"))
    async def pinned_error(self) -> Any:
        return Secretive()


def _client(**kwargs: Any) -> TestClient:
    @module(controllers=[ObjectController])
    class ObjectModule:
        pass

    return TestClient(LaurenFactory.create(ObjectModule, **kwargs))


def test_policies_are_dict_warn_error() -> None:
    assert JSON_FALLBACK_POLICIES == ("dict", "warn", "error")


@pytest.mark.parametrize("policy", ["dict", "warn"])
def test_dict_and_warn_send_public_attributes(policy: str) -> None:
    body = _client(json_fallback=policy).get("/o/obj").json()

    assert body == {"public_name": "x", "api_key": "sk-live-123"}


def test_error_policy_turns_the_object_into_a_500() -> None:
    response = _client(json_fallback="error").get("/o/obj")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert b"sk-live" not in response.body


def test_default_policy_is_warn() -> None:
    @module(controllers=[ObjectController])
    class ObjectModule:
        pass

    assert LaurenFactory.create(ObjectModule).json_fallback == "warn"


def test_warn_logs_once_per_type() -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)
    client = _client(logger=log)

    for _ in range(3):
        client.get("/o/obj")
    client.get("/o/other")

    warned = [r.extra["type"] for r in log.records if r.context == "Serialization"]
    assert warned == ["Secretive", "AlsoSecretive"]


def test_warn_also_reaches_the_stdlib_logger(caplog: pytest.LogCaptureFixture) -> None:
    client = _client()

    with caplog.at_level(logging.WARNING, logger="lauren"):
        client.get("/o/obj")

    assert any("__dict__ fallback" in record.getMessage() for record in caplog.records)


def test_a_broken_logger_does_not_fail_the_response() -> None:
    class BrokenLogger(InMemoryLogger):
        def warn(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("logger down")

    assert _client(logger=BrokenLogger()).get("/o/obj").status_code == 200


def test_an_encoder_default_callback_still_wins() -> None:
    assert (
        StdlibJSONEncoder(default=lambda obj: "custom").encode_compact({"x": Secretive()})
        == b'{"x":"custom"}'
    )


def test_dict_policy_does_not_log() -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)

    _client(json_fallback="dict", logger=log).get("/o/obj")

    assert not [r for r in log.records if r.context == "Serialization"]


def test_each_app_keeps_its_own_warn_memory() -> None:
    first, second = InMemoryLogger(), InMemoryLogger()

    _client(logger=first).get("/o/obj")
    _client(logger=second).get("/o/obj")

    assert [len([r for r in log.records if r.context == "Serialization"]) for log in (first, second)] == [
        1,
        1,
    ]


def test_pinned_encoder_policy_wins_over_the_app() -> None:
    client = _client(json_fallback="error")

    assert client.get("/o/pinned-dict").json() == {"public_name": "x", "api_key": "sk-live-123"}


def test_pinned_error_policy_applies_under_a_lenient_app() -> None:
    assert _client(json_fallback="dict").get("/o/pinned-error").status_code == 500


def test_outside_an_app_the_fallback_dumps_attributes() -> None:
    assert Response.json(Secretive()).body == b'{"public_name":"x","api_key":"sk-live-123"}'


def test_unknown_policy_fails_at_startup() -> None:
    @module()
    class Empty:
        pass

    with pytest.raises(StartupError) as info:
        LaurenFactory.create(Empty, json_fallback="ignore")

    assert info.value.detail["reason"] == "invalid_json_fallback"


@pytest.mark.parametrize("bad", ["ignore", 1])
def test_unknown_encoder_policy_is_rejected(bad: Any) -> None:
    with pytest.raises(ValueError, match="json_fallback"):
        StdlibJSONEncoder(fallback=bad)


def test_default_and_fallback_cannot_be_combined() -> None:
    with pytest.raises(ValueError, match="not both"):
        StdlibJSONEncoder(default=str, fallback="error")


def test_orjson_encoder_honours_a_pinned_error_policy() -> None:
    pytest.importorskip("orjson")
    encoder = OrjsonEncoder(fallback="error")

    with pytest.raises(TypeError, match="json_fallback='error'"):
        encoder.encode({"x": Secretive()})


def test_imperative_app_accepts_json_fallback() -> None:
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None, json_fallback="error")

    @app.get("/obj")
    async def obj() -> Any:
        return Secretive()

    assert TestClient(app).get("/obj").status_code == 500


def test_response_contract_never_reaches_the_fallback() -> None:
    import dataclasses

    @dataclasses.dataclass
    class Public:
        public_name: str

    @controller("/c")
    class ContractController:
        @get("/")
        async def obj(self) -> Public:
            return Secretive()  # type: ignore[return-value]

    @module(controllers=[ContractController])
    class ContractModule:
        pass

    client = TestClient(
        LaurenFactory.create(ContractModule, response_validation="filter", json_fallback="error")
    )

    assert client.get("/c/").json() == {"public_name": "x"}

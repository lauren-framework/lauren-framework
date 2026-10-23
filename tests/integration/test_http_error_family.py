"""Full-stack tests for the HTTP error family, error headers, RFC 9457 and guard ``deny_error`` (F05)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, ClassVar

import pytest

from lauren import (
    BadRequestError,
    ConflictError,
    GoneError,
    HTTPError,
    LaurenFactory,
    MethodNotAllowedError,
    NotAcceptableError,
    NotFoundError,
    PreconditionFailedError,
    Request,
    RequestTimeoutError,
    Response,
    ServiceUnavailableError,
    StdlibJSONEncoder,
    TooManyRequestsError,
    UnauthorizedError,
    UnsupportedMediaTypeError,
    WebSocket,
    controller,
    exception_handler,
    get,
    injectable,
    middleware,
    module,
    on_connect,
    on_message,
    use_encoder,
    use_exception_handlers,
    use_guards,
    ws_controller,
)
from lauren.exceptions import ForbiddenError, GuardConfigError
from lauren.testing import TestClient, WsTestClient

_RAISED: dict[str, HTTPError] = {
    "bad_request": BadRequestError("malformed cursor"),
    "not_found": NotFoundError("no such thing"),
    "not_acceptable": NotAcceptableError("cannot produce text/csv"),
    "conflict": ConflictError("email taken"),
    "gone": GoneError("deleted"),
    "precondition_failed": PreconditionFailedError("stale etag"),
    "unsupported_media_type": UnsupportedMediaTypeError("send JSON"),
    "too_many_requests": TooManyRequestsError("slow down", retry_after=30),
    "service_unavailable": ServiceUnavailableError(
        "draining", retry_after=datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc)
    ),
    "request_timeout": RequestTimeoutError("upstream deadline"),
    "unauthorized": UnauthorizedError("who are you", challenge='Bearer realm="api"'),
    "method_not_allowed": MethodNotAllowedError("use PUT", allow=["PUT"]),
}

_EXPECTED: dict[str, tuple[int, dict[str, str]]] = {
    "bad_request": (400, {}),
    "not_found": (404, {}),
    "not_acceptable": (406, {}),
    "conflict": (409, {}),
    "gone": (410, {}),
    "precondition_failed": (412, {}),
    "unsupported_media_type": (415, {}),
    "too_many_requests": (429, {"retry-after": "30"}),
    "service_unavailable": (503, {"retry-after": "Fri, 23 Oct 2026 18:00:00 GMT"}),
    "request_timeout": (504, {}),
    "unauthorized": (401, {"www-authenticate": 'Bearer realm="api"'}),
    "method_not_allowed": (405, {"allow": "PUT"}),
}


class UserNotFound(NotFoundError):
    code = "user_not_found"


@controller("/raise")
class RaiseController:
    @get("/{name}")
    async def raise_it(self, name: str) -> dict:
        raise _RAISED[name]

    @get("/user/{user_id}")
    async def user(self, user_id: int) -> dict:
        raise UserNotFound(
            "user does not exist", detail={"id": user_id}, headers={"cache-control": "no-store"}
        )


@module(controllers=[RaiseController])
class RaiseModule:
    pass


def _client(root: type = RaiseModule, **kwargs: Any) -> TestClient:
    return TestClient(LaurenFactory.create(root, **kwargs))


# ---------------------------------------------------------------------------
# Status, code and headers on the wire
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(_RAISED), ids=list(_RAISED))
def test_raised_error_renders_status_code_and_headers(name: str) -> None:
    status, headers = _EXPECTED[name]

    r = _client().get(f"/raise/{name}")

    assert r.status_code == status
    assert r.json()["error"]["code"] == _RAISED[name].code
    for header, value in headers.items():
        assert r.header(header) == value


def test_allow_header_is_sent_when_a_handler_raises_405() -> None:
    r = _client().get("/raise/method_not_allowed")

    assert r.headers_all("allow") == ["PUT"]


def test_router_405_sends_a_single_allow_header() -> None:
    r = _client().post("/raise/x")

    assert r.status_code == 405
    assert r.headers_all("allow") == ["GET"]


def test_domain_subclass_keeps_the_family_status_with_its_own_code() -> None:
    r = _client().get("/raise/user/7")

    assert r.status_code == 404
    assert r.json() == {
        "error": {"code": "user_not_found", "message": "user does not exist", "detail": {"id": 7}}
    }
    assert r.header("cache-control") == "no-store"


def test_error_headers_reach_the_client_from_a_global_middleware() -> None:
    @middleware()
    class RateLimit:
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            raise TooManyRequestsError("quota", retry_after=5)

    r = _client(global_middlewares=[RateLimit]).get("/raise/not_found")

    assert r.status_code == 429
    assert r.header("retry-after") == "5"


def test_header_and_detail_survive_an_unencodable_detail_fallback() -> None:
    @controller("/odd")
    class Odd:
        @get("/")
        async def odd(self) -> dict:
            raise UnauthorizedError("no", challenge="Bearer", detail={"blob": object()})

    @module(controllers=[Odd])
    class OddModule:
        pass

    r = _client(OddModule, json_encoder=StdlibJSONEncoder()).get("/odd/")

    assert r.status_code == 401
    assert r.header("www-authenticate") == "Bearer"
    assert r.json()["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------------------
# Exception handlers own their responses
# ---------------------------------------------------------------------------


@exception_handler(NotFoundError)
def _not_found_to_teapot(exc: NotFoundError, request: Request) -> Response:
    return Response.json({"handled": exc.code, "headers": [list(h) for h in exc.headers]}, status=418)


@controller("/handled")
@use_exception_handlers(_not_found_to_teapot)
class HandledController:
    @get("/")
    async def show(self) -> dict:
        raise UserNotFound("gone", headers={"x-trace": "abc"})


@module(controllers=[HandledController])
class HandledModule:
    pass


def test_handler_for_a_family_class_claims_its_subclasses() -> None:
    r = _client(HandledModule).get("/handled/")

    assert r.status_code == 418
    assert r.json()["handled"] == "user_not_found"


def test_handler_response_does_not_get_the_error_headers_merged() -> None:
    r = _client(HandledModule).get("/handled/")

    assert r.header("x-trace") is None
    assert r.json()["headers"] == [["x-trace", "abc"]]


def test_per_route_encoder_renders_error_bodies() -> None:
    calls: list[Any] = []

    class TracingEncoder(StdlibJSONEncoder):
        def encode_compact(self, data: Any) -> bytes:
            calls.append(data)
            return super().encode_compact(data)

    @controller("/enc")
    class EncController:
        @get("/")
        @use_encoder(TracingEncoder())
        async def show(self) -> dict:
            raise ConflictError("dup")

    @module(controllers=[EncController])
    class EncModule:
        pass

    r = _client(EncModule).get("/enc/")

    assert r.status_code == 409
    assert calls and calls[-1]["error"]["code"] == "conflict"


# ---------------------------------------------------------------------------
# error_format="rfc9457"
# ---------------------------------------------------------------------------


def test_rfc9457_app_end_to_end() -> None:
    client = _client(error_format="rfc9457", problem_type_base="https://errors.example.com/")

    r = client.get("/raise/user/7")

    assert r.status_code == 404
    assert r.header("content-type") == "application/problem+json"
    assert r.json() == {
        "type": "https://errors.example.com/user_not_found",
        "title": "Not Found",
        "status": 404,
        "detail": "user does not exist",
        "instance": "/raise/user/7",
        "code": "user_not_found",
        "context": {"id": 7},
    }


def test_rfc9457_routing_miss_has_instance_and_about_blank() -> None:
    r = _client(error_format="rfc9457").get("/nowhere")

    assert r.json()["type"] == "about:blank"
    assert r.json()["instance"] == "/nowhere"


def test_rfc9457_unhandled_error_is_a_problem_document() -> None:
    @controller("/crash")
    class Crash:
        @get("/")
        async def crash(self) -> dict:
            raise KeyError("k")

    @module(controllers=[Crash])
    class CrashModule:
        pass

    r = _client(CrashModule, error_format="rfc9457").get("/crash/")

    assert r.status_code == 500
    assert r.json()["code"] == "internal_error"
    assert r.json()["context"] == {"type": "KeyError"}


def test_error_format_property_reports_rfc9457() -> None:
    assert LaurenFactory.create(RaiseModule, error_format="rfc9457").error_format == "rfc9457"


def test_unknown_error_format_warning_mentions_the_2_0_change(caplog: pytest.LogCaptureFixture) -> None:
    from lauren.logging import InMemoryLogger

    log = InMemoryLogger()
    app = LaurenFactory.create(RaiseModule, error_format="problem+json", logger=log)

    assert app.error_format == "default"
    assert any("2.0" in record.message and "rfc9457" in record.message for record in log.records)


@pytest.mark.parametrize("base", ["", 7], ids=["empty", "non-str"])
def test_invalid_problem_type_base_fails_startup(base: object) -> None:
    from lauren.exceptions import StartupError

    with pytest.raises(StartupError) as info:
        LaurenFactory.create(RaiseModule, problem_type_base=base)  # type: ignore[arg-type]

    assert info.value.detail["reason"] == "invalid_problem_type_base"


def test_problem_type_base_with_the_default_format_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.WARNING, logger="lauren"):
        LaurenFactory.create(RaiseModule, problem_type_base="https://errors.example.com/")

    assert any("problem_type_base has no effect" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Guard deny_error
# ---------------------------------------------------------------------------


class ApiKeyGuard:
    deny_error: ClassVar[type[HTTPError]] = UnauthorizedError

    async def can_activate(self, ctx: Any) -> bool:
        return ctx.request.headers.get("x-api-key") == "secret"


class PlainDeny:
    async def can_activate(self, ctx: Any) -> bool:
        return False


class ChallengeGuard:
    @staticmethod
    def deny_error() -> HTTPError:
        return UnauthorizedError("login required", challenge='Basic realm="admin"')

    async def can_activate(self, ctx: Any) -> bool:
        return False


@injectable()  # the guard *role* is not inherited (strict inheritance)...
class InheritedApiKeyGuard(ApiKeyGuard):
    pass  # ...but ``deny_error`` is plain class configuration and is


def _guarded(*guards: type, **kwargs: Any) -> TestClient:
    @controller("/g")
    class Guarded:
        @get("/")
        @use_guards(*guards)
        async def show(self) -> dict:
            return {"ok": True}

    @module(controllers=[Guarded])
    class GuardedModule:
        pass

    return _client(GuardedModule, **kwargs)


def test_guard_deny_error_turns_a_denial_into_401() -> None:
    r = _guarded(ApiKeyGuard).get("/g/")

    assert r.status_code == 401
    assert r.json() == {
        "error": {
            "code": "unauthorized",
            "message": "guard ApiKeyGuard denied the request",
            "detail": {"guard": "ApiKeyGuard"},
        }
    }


def test_guard_with_deny_error_still_admits_valid_requests() -> None:
    assert _guarded(ApiKeyGuard).get("/g/", headers={"x-api-key": "secret"}).status_code == 200


def test_guard_without_deny_error_still_answers_403() -> None:
    r = _guarded(PlainDeny).get("/g/")

    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"


def test_guard_deny_error_factory_can_add_a_challenge() -> None:
    r = _guarded(ChallengeGuard).get("/g/")

    assert r.status_code == 401
    assert r.header("www-authenticate") == 'Basic realm="admin"'
    assert r.json()["error"]["message"] == "login required"


def test_guard_subclass_inherits_deny_error() -> None:
    assert _guarded(InheritedApiKeyGuard).get("/g/").status_code == 401


def test_global_guard_deny_error_is_honoured() -> None:
    r = _client(global_guards=[ApiKeyGuard]).get("/raise/not_found")

    assert r.status_code == 401


def test_denied_request_can_still_be_reshaped_by_an_exception_handler() -> None:
    @exception_handler(UnauthorizedError)
    def to_login(exc: UnauthorizedError, request: Request) -> Response:
        return Response.json({"login": True}, status=401)

    r = _guarded(ApiKeyGuard, global_exception_handlers=[to_login]).get("/g/")

    assert r.json() == {"login": True}


@pytest.mark.parametrize(
    "deny_error,reason_part",
    [
        (ValueError, "not an HTTPError subclass"),
        (UnauthorizedError("x"), "exception instance"),
        ("unauthorized", "neither an HTTPError subclass"),
    ],
    ids=["non-http-class", "instance", "string"],
)
def test_invalid_deny_error_fails_startup(deny_error: object, reason_part: str) -> None:
    BadGuard = type("BadGuard", (), {"deny_error": deny_error, "can_activate": PlainDeny.can_activate})

    with pytest.raises(GuardConfigError) as info:
        _guarded(BadGuard)

    assert reason_part in str(info.value)
    assert info.value.detail["guard"] == "BadGuard"
    assert info.value.detail["reason"] == "invalid_deny_error"


def test_deny_error_factory_needing_arguments_fails_startup() -> None:
    class NeedsSelf:
        def deny_error(self) -> HTTPError:
            return UnauthorizedError("x")

        async def can_activate(self, ctx: Any) -> bool:
            return False

    with pytest.raises(GuardConfigError) as info:
        _guarded(NeedsSelf)

    assert "no arguments" in str(info.value)


def test_invalid_global_guard_deny_error_fails_startup() -> None:
    BadGuard = type("BadGlobal", (), {"deny_error": KeyError, "can_activate": PlainDeny.can_activate})

    with pytest.raises(GuardConfigError):
        _client(global_guards=[BadGuard])


def test_deny_error_factory_returning_a_non_http_error_is_a_500() -> None:
    class Broken:
        deny_error = staticmethod(lambda: ValueError("nope"))

        async def can_activate(self, ctx: Any) -> bool:
            return False

    r = _guarded(Broken).get("/g/")

    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"


def test_deny_error_factory_without_an_introspectable_signature_is_trusted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import lauren._asgi as asgi

    def no_signature(obj: Any) -> Any:
        raise ValueError("no signature found")

    def factory() -> HTTPError:
        return UnauthorizedError("from a C-level callable")

    monkeypatch.setattr(asgi.inspect, "signature", no_signature)
    Guard = type("CGuard", (), {"deny_error": staticmethod(factory), "can_activate": PlainDeny.can_activate})

    assert asgi._compile_guard_denial(Guard) is factory


def test_guard_added_after_build_is_compiled_on_first_denial() -> None:
    app = LaurenFactory.create(RaiseModule)
    app._global_guards.append(ApiKeyGuard)
    app.container.register(ApiKeyGuard)

    assert app._guard_denial(ApiKeyGuard, message="m", detail={}).status_code == 401
    assert app._guard_denials[ApiKeyGuard] is UnauthorizedError


# ---------------------------------------------------------------------------
# WebSocket guards: deny_error maps to the error frame / close reason
# ---------------------------------------------------------------------------


class WsKeyGuard:
    deny_error = UnauthorizedError

    async def can_activate(self, ctx: Any) -> bool:
        return ctx.request.headers.get("x-api-key") == "secret"


def _ws_app(gateway: type, **kwargs: Any) -> Any:
    @module(controllers=[gateway])
    class WsModule:
        pass

    return LaurenFactory.create(WsModule, **kwargs)


async def test_ws_message_guard_denial_uses_deny_error_in_the_error_frame() -> None:
    @ws_controller("/chat")
    class Chat:
        @on_message("wipe")
        @use_guards(WsKeyGuard)
        async def wipe(self, ws: WebSocket) -> None:
            await ws.send_json({"event": "wiped"})

    async with WsTestClient(_ws_app(Chat)).connect("/chat") as ws:
        await ws.send_json({"event": "wipe", "data": {}})
        reply = await asyncio.wait_for(ws.receive_json(), timeout=2)

    assert reply["error"]["code"] == "unauthorized"
    assert reply["error"]["detail"] == {"guard": "WsKeyGuard", "event": "wipe"}


async def test_ws_connect_denial_closes_1008_with_the_deny_error_code_as_reason() -> None:
    @use_guards(WsKeyGuard)
    @ws_controller("/secure")
    class Secure:
        @on_connect
        async def joined(self, ws: WebSocket) -> None:
            await ws.accept()

    async with WsTestClient(_ws_app(Secure)).connect("/secure") as rejected:
        assert (rejected.close_code, rejected.close_reason) == (1008, "unauthorized")


async def test_ws_connect_denial_reason_defaults_to_forbidden() -> None:
    @use_guards(PlainDeny)
    @ws_controller("/closed")
    class Closed:
        @on_connect
        async def joined(self, ws: WebSocket) -> None:
            await ws.accept()

    async with WsTestClient(_ws_app(Closed)).connect("/closed") as rejected:
        assert (rejected.close_code, rejected.close_reason) == (1008, "forbidden")


async def test_ws_broken_deny_error_factory_still_closes_the_connection() -> None:
    class BrokenWs:
        deny_error = staticmethod(lambda: 1 / 0)

        async def can_activate(self, ctx: Any) -> bool:
            return False

    @use_guards(BrokenWs)
    @ws_controller("/broken")
    class BrokenGateway:
        @on_connect
        async def joined(self, ws: WebSocket) -> None:
            await ws.accept()

    async with WsTestClient(_ws_app(BrokenGateway)).connect("/broken") as rejected:
        assert (rejected.close_code, rejected.close_reason) == (1008, "")


async def test_ws_guard_that_raises_closes_without_a_reason() -> None:
    class Exploding:
        deny_error = UnauthorizedError

        async def can_activate(self, ctx: Any) -> bool:
            raise RuntimeError("guard bug")

    @use_guards(Exploding)
    @ws_controller("/explode")
    class ExplodeGateway:
        @on_connect
        async def joined(self, ws: WebSocket) -> None:
            await ws.accept()

    async with WsTestClient(_ws_app(ExplodeGateway)).connect("/explode") as rejected:
        assert (rejected.close_code, rejected.close_reason) == (1008, "")


def test_invalid_ws_guard_deny_error_fails_startup() -> None:
    BadWsGuard = type(
        "BadWsGuard", (), {"deny_error": ForbiddenError("x"), "can_activate": PlainDeny.can_activate}
    )

    @use_guards(BadWsGuard)
    @ws_controller("/bad")
    class BadGateway:
        @on_connect
        async def joined(self, ws: WebSocket) -> None:
            await ws.accept()

    with pytest.raises(GuardConfigError):
        _ws_app(BadGateway)

"""Integration: ``@raises`` declarations — reflect, OpenAPI and the debug-mode drift warning."""

from __future__ import annotations

from typing import Any

import pytest

from lauren import (
    ConflictError,
    ForbiddenError,
    LaurenFactory,
    NotFoundError,
    UnauthorizedError,
    controller,
    get,
    module,
    post,
    raises,
    use_guards,
)
from lauren.logging import InMemoryLogger, LogLevel
from lauren.reflect import get_route_metadata, reflect_routes
from lauren.testing import TestClient


class DuplicateEmail(ConflictError):
    """The e-mail address is already registered."""

    code = "duplicate_email"


@controller("/users")
@raises(UnauthorizedError)
class UserController:
    @get("/{id}")
    @raises(NotFoundError, description="No user with that id")
    async def show(self, id: int) -> dict:
        if id == 0:
            raise NotFoundError("no such user")
        if id == 1:
            raise ConflictError("undeclared on purpose")
        return {"id": id}

    @post("/", status_code=201)
    @raises(ConflictError)
    @raises(DuplicateEmail)
    async def create(self) -> dict:
        return {"id": 1}


@controller("/admins")
class AdminController(UserController):
    pass


def _app(**kwargs: Any) -> Any:
    @module(controllers=[UserController, AdminController])
    class UsersModule:
        pass

    return LaurenFactory.create(UsersModule, **kwargs)


def test_method_and_class_declarations_merge() -> None:
    app = _app()
    TestClient(app)

    route = get_route_metadata(app, "GET", "/users/{id}")

    assert route is not None
    assert route.raises == (NotFoundError, UnauthorizedError)


def test_stacked_declarations_accumulate() -> None:
    app = _app()
    TestClient(app)

    route = get_route_metadata(app, "POST", "/users")

    assert route is not None
    assert route.raises == (ConflictError, DuplicateEmail, UnauthorizedError)


def test_subclass_controller_does_not_inherit_class_level_raises() -> None:
    app = _app()
    TestClient(app)

    route = get_route_metadata(app, "GET", "/admins/{id}")

    assert route is not None
    assert route.raises == (NotFoundError,)


def test_reflect_lists_raises_as_typed_responses() -> None:
    (show,) = [r for r in reflect_routes(UserController) if r.handler.__name__ == "show"]

    assert [(r.status, r.body_type, r.description, r.source) for r in show.responses_typed] == [
        (200, dict, None, "declared"),
        (404, NotFoundError, "No user with that id", "raises"),
        (401, UnauthorizedError, None, "raises"),
    ]


def test_reflect_lists_manual_responses() -> None:
    @controller("/m")
    class ManualController:
        @get("/", responses={404: {"description": "Missing"}, 410: "Gone for good"})
        async def index(self) -> dict:
            return {}

    (route,) = reflect_routes(ManualController)

    assert [(r.status, r.description, r.source) for r in route.responses_typed][1:] == [
        (404, "Missing", "manual"),
        (410, "Gone for good", "manual"),
    ]


def test_static_reflect_tolerates_a_declaration_the_app_would_reject() -> None:
    @controller("/bad")
    class BadController:
        @get("/", status_code=204)
        async def index(self) -> dict:
            return {}

    (route,) = reflect_routes(BadController)

    assert (route.status_code, route.responses_typed) == (204, ())


def test_openapi_documents_one_response_per_error_status() -> None:
    responses = _app().openapi()["paths"]["/users/{id}"]["get"]["responses"]

    assert sorted(responses) == ["200", "401", "404"]
    assert responses["404"]["description"] == "No user with that id"
    assert responses["404"]["x-lauren-error-codes"] == ["not_found"]
    assert responses["404"]["content"] == {
        "application/json": {"schema": {"$ref": "#/components/schemas/LaurenErrorEnvelope"}}
    }


def test_openapi_merges_errors_that_share_a_status() -> None:
    responses = _app().openapi()["paths"]["/users"]["post"]["responses"]

    assert responses["409"]["x-lauren-error-codes"] == ["conflict", "duplicate_email"]
    assert "already registered" in responses["409"]["description"]


def test_openapi_problem_details_use_the_problem_media_type() -> None:
    doc = _app(error_format="rfc9457").openapi()

    content = doc["paths"]["/users/{id}"]["get"]["responses"]["404"]["content"]
    assert content == {
        "application/problem+json": {"schema": {"$ref": "#/components/schemas/LaurenProblemDetails"}}
    }
    assert "LaurenProblemDetails" in doc["components"]["schemas"]


def test_routes_without_raises_add_no_error_component() -> None:
    @controller("/plain")
    class PlainController:
        @get("/")
        async def index(self) -> dict:
            return {}

    @module(controllers=[PlainController])
    class PlainModule:
        pass

    assert "LaurenErrorEnvelope" not in LaurenFactory.create(PlainModule).openapi()["components"]["schemas"]


def test_imperative_routes_keep_their_raises() -> None:
    from lauren import Lauren

    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/thing")
    @raises(NotFoundError)
    async def thing() -> dict:
        return {}

    client = TestClient(app)
    client.get("/thing")

    assert "404" in app.openapi()["paths"]["/thing"]["get"]["responses"]


# -- debug-mode drift warning -------------------------------------------------


def _drift_messages(log: InMemoryLogger) -> list[str]:
    return [r.message for r in log.records if r.context == "ResponseContract" and r.level == LogLevel.WARN]


def test_debug_warns_once_about_an_undeclared_error() -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)
    client = TestClient(_app(debug=True, logger=log))

    client.get("/users/1")
    client.get("/users/1")

    (message,) = _drift_messages(log)
    assert "ConflictError" in message and "GET /users/{id}" in message


def test_debug_stays_quiet_for_declared_errors() -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)

    TestClient(_app(debug=True, logger=log)).get("/users/0")

    assert _drift_messages(log) == []


def test_drift_warning_needs_debug_pages() -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)

    TestClient(_app(logger=log)).get("/users/1")

    assert _drift_messages(log) == []


def test_drift_warning_ignores_errors_raised_outside_the_handler() -> None:
    class Deny:
        async def can_activate(self, ctx: Any) -> bool:
            return False

    @use_guards(Deny)
    @controller("/guarded")
    class GuardedController:
        @get("/")
        @raises(NotFoundError)
        async def index(self) -> dict:
            return {}

    @module(controllers=[GuardedController])
    class GuardedModule:
        pass

    log = InMemoryLogger(level=LogLevel.DEBUG)
    response = TestClient(LaurenFactory.create(GuardedModule, debug=True, logger=log)).get("/guarded/")

    assert response.status_code == ForbiddenError.status_code
    assert _drift_messages(log) == []


@pytest.mark.parametrize("path", ["/users/0", "/users/5"])
def test_declared_raises_do_not_change_runtime_behaviour(path: str) -> None:
    response = TestClient(_app()).get(path)

    assert response.status_code in (200, 404)

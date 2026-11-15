"""Decoration-time checks for route templates, ``host=``, ``@route`` and ``@any_method``."""

from __future__ import annotations

from typing import Any

import pytest

from lauren import Lauren, any_method, controller, get, module, route
from lauren.decorators import CONTROLLER_META, MODULE_META, ROUTE_META
from lauren.exceptions import DecoratorUsageError
from lauren.websockets import WS_CONTROLLER_META, ws_controller


def _handler() -> None:
    return None


def test_route_appends_one_route_meta_per_method() -> None:
    def fn() -> None:
        return None

    decorated = route("/x", methods=["post", "PUT", "POST"], summary="s")(fn)

    assert decorated is fn
    metas = getattr(fn, ROUTE_META)
    assert [m.method for m in metas] == ["POST", "PUT"]
    assert {m.summary for m in metas} == {"s"}


def test_route_stacks_with_verb_decorators() -> None:
    fn = get("/x")(route("/x", methods=["DELETE"])(_handler))

    assert [m.method for m in getattr(fn, ROUTE_META)] == ["DELETE", "GET"]
    delattr(fn, ROUTE_META)


@pytest.mark.parametrize("methods", [["BREW"], [], "GET", [1], None])
def test_route_rejects_invalid_methods(methods: Any) -> None:
    with pytest.raises(DecoratorUsageError) as exc_info:
        route("/x", methods=methods)

    assert exc_info.value.detail["reason"] == "invalid_method"


def test_route_rejects_bare_usage() -> None:
    with pytest.raises(DecoratorUsageError):
        route(_handler, methods=["GET"])  # type: ignore[arg-type]


def test_any_method_records_the_wildcard_method() -> None:
    def fn() -> None:
        return None

    any_method("/proxy/{*rest}", include_in_schema=False)(fn)

    (meta,) = getattr(fn, ROUTE_META)
    assert meta.method == "*"
    assert meta.include_in_schema is False


def test_any_method_rejects_bare_usage() -> None:
    with pytest.raises(DecoratorUsageError) as exc_info:
        any_method(_handler)  # type: ignore[arg-type]

    assert exc_info.value.detail["decorator"] == "any_method"


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("/x/{id:int(min='a')}", "bad_converter_args"),
        ("/x/{id:re([)}", "bad_pattern"),
        ("/x/{*rest}/tail", "wildcard_not_last"),
        ("/x/{a}/{a}", "duplicate_param"),
    ],
)
def test_bad_templates_fail_at_decoration(path: str, reason: str) -> None:
    with pytest.raises(DecoratorUsageError) as exc_info:
        get(path)(_handler)

    assert exc_info.value.detail == {
        "decorator": "get",
        "target": _handler.__qualname__,
        "option": "path",
        "reason": reason,
    }


def test_non_string_path_fails_at_decoration() -> None:
    with pytest.raises(DecoratorUsageError) as exc_info:
        get(42)(_handler)  # type: ignore[arg-type]

    assert exc_info.value.detail["reason"] == "invalid_path"


def test_unknown_converter_names_wait_for_startup() -> None:
    def fn() -> None:
        return None

    get("/b/{isbn:isbn}")(fn)  # resolved against RoutingConfig.converters at startup

    assert getattr(fn, ROUTE_META)[0].path == "/b/{isbn:isbn}"


def test_controller_prefix_template_fails_at_decoration() -> None:
    with pytest.raises(DecoratorUsageError) as exc_info:

        @controller("/x/{id:int(max=-)}")
        class _Bad:
            pass

    assert exc_info.value.detail["decorator"] == "controller"


def test_controller_and_module_record_host() -> None:
    @controller("/", host="{tenant}.example.com")
    class Tenant:
        pass

    @module(controllers=[Tenant], host="api.example.com")
    class TenantModule:
        pass

    assert getattr(Tenant, CONTROLLER_META).host == "{tenant}.example.com"
    assert getattr(TenantModule, MODULE_META).host == "api.example.com"


@pytest.mark.parametrize(
    "decorate",
    [
        lambda: controller("/", host="example.com:8080"),
        lambda: module(host="https://example.com"),
        lambda: ws_controller("/ws", host="{*sub}.example.com"),
    ],
)
def test_malformed_host_fails_at_decoration(decorate: Any) -> None:
    with pytest.raises(DecoratorUsageError) as exc_info:
        decorate()

    assert exc_info.value.detail["option"] == "host"


def test_ws_controller_records_host() -> None:
    @ws_controller("/ws", host="{tenant}.example.com")
    class Gateway:
        pass

    assert getattr(Gateway, WS_CONTROLLER_META).host == "{tenant}.example.com"


def test_lauren_route_helpers_validate_at_decoration() -> None:
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)

    with pytest.raises(DecoratorUsageError):
        app.get("/x/{id:int(min=x)}")(_handler)
    with pytest.raises(DecoratorUsageError):
        app.route("/x", methods=["BREW"])
    with pytest.raises(DecoratorUsageError):
        app.route("/x", methods=None)
    with pytest.raises(DecoratorUsageError):
        app.any_method(_handler)  # type: ignore[arg-type]


def test_route_methods_use_the_shared_route_option_validator() -> None:
    from lauren.decorators import _ROUTE_OPTION_VALIDATORS, _validate_route_options

    assert "methods" in _ROUTE_OPTION_VALIDATORS
    assert _validate_route_options("route", _handler, {"methods": ["post", "POST"]}) == {"methods": ("POST",)}

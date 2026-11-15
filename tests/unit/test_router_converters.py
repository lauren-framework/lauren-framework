"""Typed path converters: matching, precedence, formatting and template parsing."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from lauren import PathConverter, RoutingConfig
from lauren._routing import Router
from lauren._routing._converters import (
    BUILTIN_CONVERTERS,
    TemplateError,
    build_converter,
    check_template,
    normalize_host,
    parse_host,
    parse_segment,
    parse_template,
)
from lauren.exceptions import (
    DecoratorUsageError,
    RouteNotFoundError,
    RouterConflictError,
    StartupError,
)


def _noop() -> None:
    return None


class IsbnConverter(PathConverter):
    name = "isbn"

    def matches(self, segment: str) -> bool:
        return len(segment) == 13 and segment.isdigit()

    def openapi_schema(self) -> dict[str, Any]:
        return {"type": "string", "pattern": "^[0-9]{13}$"}


# ---------------------------------------------------------------------------
# Built-in converters accept / reject
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "segment", "expected"),
    [
        ("str", "anything", True),
        ("str", "", False),
        ("int", "42", True),
        ("int", "007", True),
        ("int", "-1", False),
        ("int", "4.2", False),
        ("int", "²", False),
        ("int(min=1)", "0", False),
        ("int(min=1, max=10)", "10", True),
        ("int(min=1, max=10)", "11", False),
        ("sint", "-12", True),
        ("sint", "12", True),
        ("sint", "--1", False),
        ("float", "1.5", True),
        ("float", "-2", True),
        ("float", "1e5", False),
        ("float(max=1.0)", "1.5", False),
        ("uuid", "6f1c2a3e-1b2c-4d5e-8f90-1234567890ab", True),
        ("uuid", "6F1C2A3E-1B2C-4D5E-8F90-1234567890AB", True),
        ("uuid", "6f1c2a3e1b2c4d5e8f901234567890ab", False),
        ("slug", "blue-mug", True),
        ("slug", "Blue-Mug", False),
        ("slug", "blue--mug", False),
        ("re([A-Z]{3})", "ABC", True),
        ("re([A-Z]{3})", "ABCD", False),
    ],
)
def test_builtin_converter_matches_whole_segment(spec: str, segment: str, expected: bool) -> None:
    converter = build_converter(spec, None)

    assert converter is not None
    assert converter.matches(segment) is expected


def test_int_bounds_reject_values_int_cannot_parse() -> None:
    converter = build_converter("int(max=5)", None)

    assert converter is not None
    assert converter.matches("9" * 5000) is False


@pytest.mark.parametrize(
    ("spec", "segment", "value"),
    [
        ("int", "42", 42),
        ("sint", "-3", -3),
        ("float", "2.5", 2.5),
        ("uuid", "6f1c2a3e-1b2c-4d5e-8f90-1234567890ab", uuid.UUID("6f1c2a3e-1b2c-4d5e-8f90-1234567890ab")),
        ("slug", "a-b", "a-b"),
        ("str", "x", "x"),
    ],
)
def test_to_python_converts_matched_segment(spec: str, segment: str, value: object) -> None:
    converter = build_converter(spec, None)

    assert converter is not None
    assert converter.to_python(segment) == value


@pytest.mark.parametrize(
    ("spec", "value", "text"),
    [
        ("int", 42, "42"),
        ("sint", -42, "-42"),
        ("float", 1e-20, "0.00000000000000000001"),
        ("float", 3, "3.0"),
        ("uuid", "6F1C2A3E-1B2C-4D5E-8F90-1234567890AB", "6f1c2a3e-1b2c-4d5e-8f90-1234567890ab"),
        ("slug", "a-b", "a-b"),
        ("re([a-z]+)", "abc", "abc"),
    ],
)
def test_to_url_formats_a_matching_segment(spec: str, value: object, text: str) -> None:
    converter = build_converter(spec, None)

    assert converter is not None
    assert converter.to_url(value) == text
    assert converter.matches(text)


@pytest.mark.parametrize(
    ("spec", "value"),
    [
        ("int", -1),
        ("int", True),
        ("int", "1"),
        ("int(max=3)", 4),
        ("float", float("inf")),
        ("float", "1"),
        ("slug", "Not A Slug"),
        ("re([a-z]+)", "123"),
    ],
)
def test_to_url_rejects_values_the_converter_cannot_match(spec: str, value: object) -> None:
    converter = build_converter(spec, None)

    assert converter is not None
    with pytest.raises(ValueError):
        converter.to_url(value)


@pytest.mark.parametrize(
    ("spec", "schema"),
    [
        ("int", {"type": "integer", "minimum": 0}),
        ("int(min=1, max=9)", {"type": "integer", "minimum": 1, "maximum": 9}),
        ("sint", {"type": "integer"}),
        ("float(min=0)", {"type": "number", "minimum": 0}),
        ("uuid", {"type": "string", "format": "uuid"}),
        ("slug", {"type": "string", "pattern": "^(?:[a-z0-9]+(?:-[a-z0-9]+)*)$"}),
        ("re([A-Z]{3})", {"type": "string", "pattern": "^(?:[A-Z]{3})$"}),
        ("str", {"type": "string"}),
    ],
)
def test_openapi_schema_describes_the_converter(spec: str, schema: dict[str, Any]) -> None:
    converter = build_converter(spec, None)

    assert converter is not None
    assert converter.openapi_schema() == schema


def test_path_converter_base_class_defaults() -> None:
    converter = PathConverter()

    assert converter.matches("x") and not converter.matches("")
    assert converter.to_python("x") == "x"
    assert converter.to_url(5) == "5"
    assert converter.openapi_schema() == {"type": "string"}
    assert "PathConverter" in repr(converter)


# ---------------------------------------------------------------------------
# Template parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("segment", "kind", "template"),
    [
        ("items", "static", "items"),
        ("{id}", "param", "{id}"),
        ("{id:int}", "param", "{id:int}"),
        ("{id:int( min = 1 )}", "param", "{id:int(min=1)}"),
        ("{rest:path}", "wildcard", "{*rest}"),
        ("{*rest}", "wildcard", "{*rest}"),
        ("{code:re([A-Z]{3})}", "param", "{code:re([A-Z]{3})}"),
    ],
)
def test_parse_segment_canonical_template(segment: str, kind: str, template: str) -> None:
    parsed = parse_segment(segment)

    assert parsed.kind == kind
    assert parsed.template() == template


@pytest.mark.parametrize(
    ("template", "reason"),
    [
        ("/x/{id:nope}", "unknown_converter"),
        ("/x/{id:int(min='a')}", "bad_converter_args"),
        ("/x/{id:int(1)}", "bad_converter_args"),
        ("/x/{id:int(min=5, max=1)}", "bad_converter_args"),
        ("/x/{id:int(step=2)}", "bad_converter_args"),
        ("/x/{id:int(min=)}", "bad_converter_args"),
        ("/x/{id:int(**k)}", "bad_converter_args"),
        ("/x/{id:int(min=x)}", "bad_converter_args"),
        ("/x/{id:slug(a=1)}", "bad_converter_args"),
        ("/x/{id:re()}", "bad_converter_args"),
        ("/x/{id:re([)}", "bad_pattern"),
        ("/x/{id:int(min=1}", "bad_converter_args"),
        ("/x/{id:}", "bad_template"),
        ("/x/{}", "bad_template"),
        ("/x/{*}", "bad_template"),
        ("/x/{first-name}", "bad_template"),
        ("/x/{incomplete", "bad_template"),
        ("/x/a}tail", "bad_template"),
        ("/x/{id:9bad}", "bad_converter_name"),
        ("/x/{*rest:int}", "bad_template"),
        ("/x/{a}/{a}", "duplicate_param"),
        ("/x/{*rest}/more", "wildcard_not_last"),
    ],
)
def test_parse_template_rejects_malformed_templates(template: str, reason: str) -> None:
    with pytest.raises(TemplateError) as exc_info:
        parse_template(template)

    assert exc_info.value.reason == reason


def test_check_template_defers_unknown_converter_names() -> None:
    check_template("/books/{isbn:isbn}")  # a custom converter registered at startup


def test_custom_converter_takes_no_arguments() -> None:
    with pytest.raises(TemplateError) as exc_info:
        parse_segment("{b:isbn(x=1)}", {"isbn": IsbnConverter()})

    assert exc_info.value.reason == "bad_converter_args"


def test_custom_converter_spec_is_its_registered_name() -> None:
    parsed = parse_segment("{b:isbn}", {"isbn": IsbnConverter()})

    assert parsed.template() == "{b:isbn}"
    assert isinstance(parsed.converter, IsbnConverter)


def test_builtin_converter_names_are_listed() -> None:
    assert set(BUILTIN_CONVERTERS) == {"str", "int", "sint", "float", "uuid", "slug", "re"}


# ---------------------------------------------------------------------------
# Router: precedence, backtracking, names
# ---------------------------------------------------------------------------


def _items_router(**config: Any) -> Router:
    router = Router(RoutingConfig(**config))
    router.add_route("GET", "/items/{id:int}", _noop)
    router.add_route("GET", "/items/special", _noop)
    router.add_route("GET", "/items/{slug:slug}", _noop)
    router.add_route("GET", "/items/{code:re([A-Z]{3})}", _noop)
    router.add_route("GET", "/items/{key:uuid}", _noop)
    router.add_route("GET", "/items/{other}", _noop)
    router.freeze()
    return router


@pytest.mark.parametrize(
    ("path", "template", "params"),
    [
        ("/items/42", "/items/{id:int}", {"id": "42"}),
        ("/items/special", "/items/special", {}),
        ("/items/blue-mug", "/items/{slug:slug}", {"slug": "blue-mug"}),
        ("/items/ABC", "/items/{code:re([A-Z]{3})}", {"code": "ABC"}),
        (
            "/items/6f1c2a3e-1b2c-4d5e-8f90-1234567890ab",
            "/items/{key:uuid}",
            {"key": "6f1c2a3e-1b2c-4d5e-8f90-1234567890ab"},
        ),
        ("/items/Hello World", "/items/{other}", {"other": "Hello World"}),
    ],
)
def test_typed_siblings_match_by_specificity(path: str, template: str, params: dict[str, str]) -> None:
    router = _items_router()

    entry, found = router.find("GET", path)

    assert entry.path_template == template
    assert found == params


def test_converter_mismatch_is_a_routing_miss() -> None:
    router = Router()
    router.add_route("GET", "/items/{id:int}", _noop)
    router.freeze()

    with pytest.raises(RouteNotFoundError):
        router.find("GET", "/items/abc")


def test_walk_backtracks_out_of_a_typed_branch() -> None:
    router = Router()
    router.add_route("GET", "/a/{n:int}/x", _noop)
    router.add_route("GET", "/a/{s}/y", _noop)
    router.freeze()

    entry, params = router.find("GET", "/a/5/y")

    assert entry.path_template == "/a/{s}/y"
    assert params == {"s": "5"}


def test_ties_keep_declaration_order() -> None:
    router = Router()
    router.add_route("GET", "/n/{a:int(max=10)}", _noop)
    router.add_route("GET", "/n/{b:int}", _noop)
    router.freeze()

    assert router.find("GET", "/n/5")[0].path_template == "/n/{a:int(max=10)}"
    assert router.find("GET", "/n/50")[0].path_template == "/n/{b:int}"


def test_custom_converters_are_tried_before_builtins() -> None:
    router = Router(RoutingConfig(converters={"isbn": IsbnConverter()}))
    router.add_route("GET", "/b/{n:int}", _noop)
    router.add_route("GET", "/b/{isbn:isbn}", _noop)
    router.freeze()

    assert router.find("GET", "/b/9780306406157")[0].path_template == "/b/{isbn:isbn}"
    assert router.find("GET", "/b/42")[0].path_template == "/b/{n:int}"


def test_unknown_converter_at_registration_is_a_decorator_usage_error() -> None:
    router = Router()

    with pytest.raises(DecoratorUsageError) as exc_info:
        router.add_route("GET", "/b/{isbn:isbn}", _noop)

    assert exc_info.value.detail["reason"] == "unknown_converter"


def test_untyped_and_explicit_str_share_a_position() -> None:
    router = Router()
    router.add_route("GET", "/u/{a}", _noop)

    with pytest.raises(RouterConflictError):
        router.add_route("GET", "/u/{b:str}", _noop)


def test_path_converter_is_a_wildcard_synonym() -> None:
    router = Router()
    entry = router.add_route("GET", "/files/{rest:path}", _noop)

    assert entry.path_template == "/files/{*rest}"
    assert router.find("GET", "/files/a/b")[1] == {"rest": "a/b"}


def test_untyped_param_does_not_match_an_empty_segment() -> None:
    router = Router()
    router.add_route("GET", "/a/{x}/b", _noop)

    with pytest.raises(RouteNotFoundError):
        router.find("GET", "/a//b")


def test_entry_exposes_converters_and_path_format() -> None:
    router = Router()

    entry = router.add_route("GET", "/items/{id:int}/{*rest}", _noop)

    assert entry.path_format == "/items/{id}/{rest}"
    assert entry.converters["id"].python_type is int
    assert entry.converters["rest"].name == "path"


# ---------------------------------------------------------------------------
# RouteEntry.build_path — the URL-generation seam
# ---------------------------------------------------------------------------


def test_build_path_formats_and_encodes_each_segment() -> None:
    router = Router()
    entry = router.add_route("GET", "/files/{owner}/{id:int}/{*rest}", _noop)

    assert entry.build_path(owner="a b/c", id=7, rest="x/y z") == "/files/a%20b%2Fc/7/x/y%20z"


def test_build_path_of_a_static_route() -> None:
    entry = Router().add_route("GET", "/café", _noop)

    assert entry.build_path() == "/caf%C3%A9"


def test_build_path_accepts_a_mapping() -> None:
    entry = Router().add_route("GET", "/u/{id:uuid}", _noop)
    value = uuid.UUID("6f1c2a3e-1b2c-4d5e-8f90-1234567890ab")

    assert entry.build_path({"id": value}) == f"/u/{value}"


def test_build_path_rejects_missing_and_mismatching_values() -> None:
    entry = Router().add_route("GET", "/n/{id:int}/{name}", _noop)

    with pytest.raises(KeyError):
        entry.build_path(id=1)
    with pytest.raises(ValueError):
        entry.build_path(id=1, name="")


def test_build_path_checks_custom_converter_matches() -> None:
    router = Router(RoutingConfig(converters={"isbn": IsbnConverter()}))
    entry = router.add_route("GET", "/b/{isbn:isbn}", _noop)

    with pytest.raises(ValueError):
        entry.build_path(isbn="123")


def test_build_path_without_parsed_segments_quotes_the_template() -> None:
    from lauren._routing import RouteEntry

    entry = RouteEntry(method="GET", path_template="/a b", handler=_noop)

    assert entry.build_path() == "/a%20b"
    assert entry.build_host() is None


# ---------------------------------------------------------------------------
# RoutingConfig validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"auto_head": "yes"},
        {"trailing_slash": "sometimes"},
        {"converters": ["isbn"]},
        {"converters": {"int": IsbnConverter()}},
        {"converters": {"path": IsbnConverter()}},
        {"converters": {"bad name": IsbnConverter()}},
        {"converters": {"isbn": IsbnConverter}},
        {"converters": {"isbn": object()}},
    ],
)
def test_invalid_routing_config_fails_with_reason(kwargs: dict[str, Any]) -> None:
    with pytest.raises(StartupError) as exc_info:
        RoutingConfig(**kwargs)

    assert exc_info.value.detail["reason"] == "invalid_routing_config"


def test_routing_config_converters_are_read_only() -> None:
    config = RoutingConfig(converters={"isbn": IsbnConverter()})

    with pytest.raises(TypeError):
        config.converters["x"] = IsbnConverter()  # type: ignore[index]


def test_duck_typed_converter_is_accepted() -> None:
    class Upper:
        python_type = str

        def matches(self, segment: str) -> bool:
            return segment.isupper()

        def to_python(self, segment: str) -> str:
            return segment

        def to_url(self, value: object) -> str:
            return str(value)

        def openapi_schema(self) -> dict[str, Any]:
            return {"type": "string"}

    router = Router(RoutingConfig(converters={"upper": Upper()}))
    router.add_route("GET", "/c/{code:upper}", _noop)

    assert router.find("GET", "/c/ABC")[1] == {"code": "ABC"}


# ---------------------------------------------------------------------------
# Host patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "host", "params"),
    [
        ("{tenant}.example.com", "acme.example.com", {"tenant": "acme"}),
        ("{tenant:slug}.example.com", "acme-co.example.com", {"tenant": "acme-co"}),
        ("{tenant:slug}.example.com", "a_b.example.com", None),
        ("admin.example.com", "admin.example.com", {}),
        ("admin.example.com", "api.example.com", None),
        ("{a}.{b}.example.com", "x.example.com", None),
    ],
)
def test_host_pattern_match(pattern: str, host: str, params: dict[str, str] | None) -> None:
    parsed = parse_host(pattern)

    assert parsed.match(host.split(".")) == params


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "example.com:8080",
        "https://example.com",
        "a..b",
        "{*sub}.example.com",
        "x{sub}.example.com",
        "{}.example.com",
        "{a}.{a}.com",
        123,
    ],
)
def test_malformed_host_patterns_are_rejected(pattern: Any) -> None:
    with pytest.raises(TemplateError) as exc_info:
        parse_host(pattern)

    assert exc_info.value.reason in {"bad_host", "bad_template"}


def test_host_pattern_with_a_regex_label_keeps_its_dots() -> None:
    parsed = parse_host("{v:re(v[0-9.]+)}.api.example.com")

    assert parsed.match(["v1", "api", "example", "com"]) == {"v": "v1"}
    assert parsed.canonical == "{v:re(v[0-9.]+)}.api.example.com"


def test_host_pattern_build_and_openapi_url() -> None:
    parsed = parse_host("{tenant:slug}.Example.com")

    assert parsed.build({"tenant": "acme"}) == "acme.example.com"
    assert parsed.openapi_url() == "//{tenant}.example.com"
    with pytest.raises(KeyError):
        parsed.build({})
    with pytest.raises(ValueError):
        parsed.build({"tenant": "Not Valid"})


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Example.COM", "example.com"),
        ("example.com:8443", "example.com"),
        ("example.com.", "example.com"),
        ("[::1]:8000", "[::1]"),
        ("[::1", "[::1"),
    ],
)
def test_normalize_host(value: str, expected: str) -> None:
    assert normalize_host(value) == expected


@pytest.mark.parametrize("spec", ["int", "sint", "int(max=5)"])
def test_numeric_converter_never_matches_unparseable_giant_integer(spec: str) -> None:
    converter = build_converter(spec, None)

    assert converter is not None
    assert not converter.matches("9" * 5000)


def test_float_converter_rejects_non_finite_literal_and_bounds() -> None:
    converter = build_converter("float", None)

    assert converter is not None
    assert not converter.matches("9" * 1000)
    with pytest.raises(TemplateError) as exc_info:
        build_converter("float(max=1e309)", None)
    assert exc_info.value.reason == "bad_converter_args"

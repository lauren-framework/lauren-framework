"""Pagination parameters: defaults, maxima, renamed parameters and config resolution (F29).

The plans are compiled exactly as ``LaurenFactory.create`` compiles them and
driven with hand-built requests; startup failures go through the real factory.
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest

from lauren import (
    CursorPagination,
    LaurenFactory,
    OffsetPagination,
    PageNumberPagination,
    PaginationConfig,
    Request,
    controller,
    get,
    module,
)
from lauren._pagination import build_plan, is_paging_annotation, resolve_app
from lauren._pagination._config import BUILTIN_DEFAULTS, resolve
from lauren.config import Secret
from lauren.exceptions import ExtractorFieldError, StartupError, UnresolvableParameterError
from lauren.extractors import QueryField
from lauren.validation import TypeCompiler

_WHERE = {"class": "C", "handler": "h", "param": "paging", "route": "GET /items"}


def _plan(annotation: Any, app: PaginationConfig | None = None) -> Any:
    return build_plan(annotation, resolve_app(app), TypeCompiler(), include_input=False, where=_WHERE)


async def _extract(annotation: Any, query: str = "", app: PaginationConfig | None = None) -> Any:
    request = Request(method="GET", path="/items", raw_query_string=query.encode("latin-1"))
    return await _plan(annotation, app).extract(request)


async def _issues(annotation: Any, query: str, app: PaginationConfig | None = None) -> list[dict[str, Any]]:
    with pytest.raises(ExtractorFieldError) as info:
        await _extract(annotation, query, app)
    return info.value.detail["issues"]


# ---------------------------------------------------------------------------
# Offset / limit
# ---------------------------------------------------------------------------


async def test_offset_defaults_when_query_is_empty() -> None:
    paging = await _extract(OffsetPagination)

    assert (paging.limit, paging.offset) == (20, 0)


async def test_offset_reads_limit_and_offset() -> None:
    paging = await _extract(OffsetPagination, "limit=5&offset=15")

    assert (paging.limit, paging.offset) == (5, 15)


async def test_offset_uses_first_value_of_repeated_parameter() -> None:
    paging = await _extract(OffsetPagination, "limit=5&limit=7")

    assert paging.limit == 5


@pytest.mark.parametrize(
    ("query", "loc", "issue_type"),
    [
        ("limit=101", ["query", "limit"], "less_than_equal"),
        ("limit=0", ["query", "limit"], "greater_than_equal"),
        ("limit=-3", ["query", "limit"], "greater_than_equal"),
        ("limit=abc", ["query", "limit"], "int_parsing"),
        ("limit=1.5", ["query", "limit"], "int_parsing"),
        ("limit=", ["query", "limit"], "int_parsing"),
        ("offset=-1", ["query", "offset"], "greater_than_equal"),
        ("offset=x", ["query", "offset"], "int_parsing"),
    ],
)
async def test_offset_rejects_bad_values_with_query_loc(query: str, loc: list[str], issue_type: str) -> None:
    issues = await _issues(OffsetPagination, query)

    assert [(i["loc"], i["type"]) for i in issues] == [(loc, issue_type)]


async def test_offset_reports_every_bad_parameter_at_once() -> None:
    issues = await _issues(OffsetPagination, "limit=500&offset=-1")

    assert [i["loc"] for i in issues] == [["query", "limit"], ["query", "offset"]]


async def test_offset_max_limit_carries_constraint_context() -> None:
    issues = await _issues(OffsetPagination, "limit=101")

    assert issues[0]["ctx"] == {"le": 100}


async def test_offset_max_offset_bounds_offset() -> None:
    issues = await _issues(OffsetPagination, "offset=1001", PaginationConfig(max_offset=1000))

    assert issues[0]["loc"] == ["query", "offset"]
    assert issues[0]["ctx"] == {"le": 1000}


async def test_offset_renamed_parameters_are_read() -> None:
    annotation = Annotated[OffsetPagination, PaginationConfig(limit_param="pageSize", offset_param="skip")]

    paging = await _extract(annotation, "pageSize=7&skip=14&limit=99")

    assert (paging.limit, paging.offset) == (7, 14)


async def test_offset_renamed_parameter_errors_use_wire_name() -> None:
    annotation = Annotated[OffsetPagination, PaginationConfig(limit_param="pageSize")]

    issues = await _issues(annotation, "pageSize=0")

    assert issues[0]["loc"] == ["query", "pageSize"]


async def test_annotation_config_overrides_app_defaults() -> None:
    annotation = Annotated[OffsetPagination, PaginationConfig(default_limit=50, max_limit=200)]

    paging = await _extract(annotation, "", PaginationConfig(default_limit=10, max_limit=20))

    assert paging.limit == 50


async def test_app_config_applies_without_annotation() -> None:
    paging = await _extract(OffsetPagination, "", PaginationConfig(default_limit=10))

    assert paging.limit == 10


async def test_annotation_inherits_unset_fields_from_app() -> None:
    annotation = Annotated[OffsetPagination, PaginationConfig(max_limit=500)]

    paging = await _extract(annotation, "", PaginationConfig(default_limit=30, limit_param="size"))

    assert paging.limit == 30
    assert _plan(annotation, PaginationConfig(limit_param="size")).config.limit_param == "size"


async def test_nested_annotated_layers_merge_left_to_right() -> None:
    annotation = Annotated[
        Annotated[OffsetPagination, PaginationConfig(default_limit=5)], PaginationConfig(default_limit=6)
    ]

    paging = await _extract(annotation)

    assert paging.limit == 6


# ---------------------------------------------------------------------------
# Page number
# ---------------------------------------------------------------------------


async def test_page_number_defaults() -> None:
    paging = await _extract(PageNumberPagination)

    assert (paging.page, paging.per_page, paging.limit, paging.offset) == (1, 20, 20, 0)


@pytest.mark.parametrize(("page", "per_page", "offset"), [(1, 10, 0), (2, 10, 10), (5, 3, 12)])
async def test_page_number_maps_to_offset(page: int, per_page: int, offset: int) -> None:
    paging = await _extract(PageNumberPagination, f"page={page}&per_page={per_page}")

    assert (paging.limit, paging.offset) == (per_page, offset)


@pytest.mark.parametrize(
    ("query", "loc"),
    [("page=0", ["query", "page"]), ("per_page=101", ["query", "per_page"]), ("page=a", ["query", "page"])],
)
async def test_page_number_rejects_bad_values(query: str, loc: list[str]) -> None:
    issues = await _issues(PageNumberPagination, query)

    assert issues[0]["loc"] == loc


async def test_page_number_beyond_max_offset_is_rejected_on_page() -> None:
    issues = await _issues(PageNumberPagination, "page=12&per_page=10", PaginationConfig(max_offset=100))

    assert issues[0]["loc"] == ["query", "page"]
    assert issues[0]["type"] == "less_than_equal"
    assert issues[0]["ctx"] == {"le": 11}


async def test_page_number_renamed_parameters() -> None:
    annotation = Annotated[PageNumberPagination, PaginationConfig(page_param="p", per_page_param="size")]

    paging = await _extract(annotation, "p=3&size=4")

    assert (paging.page, paging.per_page) == (3, 4)


# ---------------------------------------------------------------------------
# Cursor parameter plan
# ---------------------------------------------------------------------------


async def test_cursor_first_page_has_no_cursor() -> None:
    paging = await _extract(CursorPagination[int], "limit=3", PaginationConfig(cursor_secret="k"))

    assert (paging.cursor, paging.direction, paging.limit) == (None, "next", 3)


async def test_cursor_empty_value_is_first_page() -> None:
    paging = await _extract(CursorPagination[int], "cursor=", PaginationConfig(cursor_secret="k"))

    assert paging.cursor is None


async def test_cursor_limit_is_validated() -> None:
    issues = await _issues(CursorPagination[int], "limit=0", PaginationConfig(cursor_secret="k"))

    assert issues[0]["loc"] == ["query", "limit"]


def test_cursor_plan_without_secret_fails() -> None:
    with pytest.raises(StartupError) as info:
        _plan(CursorPagination[int])

    assert info.value.detail["reason"] == "pagination_secret_missing"
    assert info.value.detail["route"] == "GET /items"


def test_cursor_plan_with_unsupported_type_fails() -> None:
    class Opaque:
        pass

    with pytest.raises(UnresolvableParameterError) as info:
        _plan(CursorPagination[Opaque], PaginationConfig(cursor_secret="k"))

    assert info.value.detail["reason"] == "unsupported_cursor_type"
    assert info.value.detail["param"] == "paging"


def test_bare_cursor_pagination_accepts_any_keyset() -> None:
    plan = _plan(CursorPagination, PaginationConfig(cursor_secret="k"))

    assert plan.kind == "cursor"


def test_cursor_secret_accepts_config_secret() -> None:
    plan = _plan(CursorPagination[int], PaginationConfig(cursor_secret=Secret("k")))

    assert plan.config.secrets == (b"k",)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (OffsetPagination, True),
        (PageNumberPagination, True),
        (CursorPagination, True),
        (CursorPagination[int], True),
        (Annotated[OffsetPagination, PaginationConfig()], True),
        (int, False),
        (Annotated[int, PaginationConfig()], False),
    ],
)
def test_paging_annotation_detection(annotation: Any, expected: bool) -> None:
    assert is_paging_annotation(annotation) is expected


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_builtin_defaults() -> None:
    cfg = BUILTIN_DEFAULTS

    assert (cfg.default_limit, cfg.max_limit, cfg.max_offset) == (20, 100, None)
    assert (cfg.envelope, cfg.meta_keys, cfg.total_header, cfg.base_url) == ("items", "snake", False, "")


@pytest.mark.parametrize(("envelope", "expected"), [("bare", True), ("items", False), ("data", False)])
def test_total_header_defaults_follow_envelope(envelope: str, expected: bool) -> None:
    assert resolve((PaginationConfig(envelope=envelope),)).total_header is expected  # type: ignore[arg-type]


def test_inherited_default_limit_follows_lower_max_limit() -> None:
    cfg = resolve((PaginationConfig(default_limit=50), PaginationConfig(max_limit=10)))

    assert (cfg.default_limit, cfg.max_limit) == (10, 10)


def test_builtin_default_limit_follows_lower_app_max_limit() -> None:
    cfg = resolve((PaginationConfig(max_limit=5),))

    assert cfg.default_limit == 5


def test_base_url_trailing_slash_is_dropped() -> None:
    assert (
        resolve((PaginationConfig(base_url="https://api.example.com/"),)).base_url
        == "https://api.example.com"
    )


def test_secret_never_appears_in_repr() -> None:
    assert "hunter2" not in repr(PaginationConfig(cursor_secret="hunter2"))
    assert "hunter2" not in repr(resolve((PaginationConfig(cursor_secret="hunter2"),)))


@pytest.mark.parametrize(
    ("config", "field"),
    [
        (PaginationConfig(default_limit=150, max_limit=100), "default_limit"),
        (PaginationConfig(default_limit=150), "default_limit"),
        (PaginationConfig(default_limit=0), "default_limit"),
        (PaginationConfig(max_limit=True), "max_limit"),  # type: ignore[arg-type]
        (PaginationConfig(max_offset=-1), "max_offset"),
        (PaginationConfig(limit_param=""), "limit_param"),
        (PaginationConfig(limit_param=" limit"), "limit_param"),
        (PaginationConfig(offset_param="limit"), "offset_param"),
        (PaginationConfig(per_page_param="page"), "per_page_param"),
        (PaginationConfig(cursor_param="limit"), "limit_param"),
        (PaginationConfig(envelope="json"), "envelope"),  # type: ignore[arg-type]
        (PaginationConfig(meta_keys="kebab"), "meta_keys"),  # type: ignore[arg-type]
        (PaginationConfig(total_header="yes"), "total_header"),  # type: ignore[arg-type]
        (PaginationConfig(base_url="api.example.com"), "base_url"),
        (PaginationConfig(base_url="https://x.test/?a=1"), "base_url"),
        (PaginationConfig(cursor_secret=42), "cursor_secret"),  # type: ignore[arg-type]
        (PaginationConfig(cursor_max_age=0), "cursor_max_age"),
        (PaginationConfig(cursor_max_age=float("inf")), "cursor_max_age"),
        (PaginationConfig(cursor_max_age=True), "cursor_max_age"),
    ],
)
def test_invalid_app_config_fails_at_create(config: PaginationConfig, field: str) -> None:
    with pytest.raises(StartupError) as info:
        LaurenFactory.create(_EmptyModule, pagination=config)

    assert info.value.detail["reason"] == "invalid_pagination_config"
    assert info.value.detail["field"] == field


def test_invalid_secret_error_does_not_echo_the_value() -> None:
    with pytest.raises(StartupError) as info:
        resolve((PaginationConfig(cursor_secret=42),))  # type: ignore[arg-type]

    assert "42" not in info.value.message


def test_pagination_argument_must_be_a_config() -> None:
    with pytest.raises(StartupError) as info:
        LaurenFactory.create(_EmptyModule, pagination={"max_limit": 10})  # type: ignore[arg-type]

    assert info.value.detail == {"reason": "invalid_pagination_config", "got": "dict"}


@pytest.mark.parametrize("field", ["envelope", "meta_keys"])
def test_response_shape_is_app_wide_only(field: str) -> None:
    value = {"envelope": "bare", "meta_keys": "camel"}[field]
    annotation = Annotated[OffsetPagination, PaginationConfig(**{field: value})]

    with pytest.raises(StartupError) as info:
        _plan(annotation)

    assert info.value.detail["reason"] == "invalid_pagination_config"
    assert info.value.detail["field"] == field
    assert info.value.detail["param"] == "paging"


def test_route_config_error_names_the_route() -> None:
    @controller("/r")
    class _Routes:
        @get("/")
        async def index(self, paging: Annotated[OffsetPagination, PaginationConfig(max_limit=0)]) -> dict:
            return {}

    @module(controllers=[_Routes])
    class _Mod:
        pass

    with pytest.raises(StartupError) as info:
        LaurenFactory.create(_Mod)

    assert info.value.detail["field"] == "max_limit"
    assert info.value.detail["route"] == "GET /r"
    assert info.value.detail["handler"] == "index"


def test_field_descriptor_on_paging_parameter_is_rejected() -> None:
    @controller("/r")
    class _Routes:
        @get("/")
        async def index(self, paging: OffsetPagination = QueryField(description="x")) -> dict:  # type: ignore[assignment]
            return {}

    @module(controllers=[_Routes])
    class _Mod:
        pass

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(_Mod)

    assert info.value.detail["reason"] == "pagination_field_descriptor"


# ---------------------------------------------------------------------------
# Value types built outside a request
# ---------------------------------------------------------------------------


def test_offset_pagination_value_semantics() -> None:
    paging = OffsetPagination(limit=10, offset=30)

    assert (paging.limit, paging.offset) == (10, 30)
    assert paging == OffsetPagination(limit=10, offset=30)
    assert hash(paging) == hash(OffsetPagination(limit=10, offset=30))
    assert paging != OffsetPagination(limit=10, offset=0)
    assert repr(paging) == "OffsetPagination(limit=10, offset=30)"


def test_page_number_pagination_value_semantics() -> None:
    paging = PageNumberPagination(page=3, per_page=10)

    assert (paging.limit, paging.offset) == (10, 20)
    assert paging == PageNumberPagination(page=3, per_page=10)
    assert hash(paging) == hash(PageNumberPagination(page=3, per_page=10))
    assert paging != OffsetPagination(limit=10, offset=20)
    assert repr(paging) == "PageNumberPagination(page=3, per_page=10)"


def test_cursor_pagination_value_semantics() -> None:
    paging = CursorPagination(cursor=5, limit=10, direction="prev")

    assert (paging.cursor, paging.direction, paging.limit) == (5, "prev", 10)
    assert repr(paging) == "CursorPagination(cursor=5, direction='prev', limit=10)"


@pytest.mark.parametrize(
    "build",
    [
        lambda: OffsetPagination(limit=0),
        lambda: OffsetPagination(offset=-1),
        lambda: OffsetPagination(limit=True),
        lambda: PageNumberPagination(page=0),
        lambda: PageNumberPagination(per_page=0),
        lambda: CursorPagination(limit=0),
        lambda: CursorPagination(direction="sideways"),
    ],
)
def test_value_types_reject_invalid_arguments(build: Any) -> None:
    with pytest.raises(ValueError):
        build()


def test_cursor_pagination_without_secret_cannot_sign() -> None:
    with pytest.raises(ValueError, match="secret"):
        CursorPagination(limit=2).encode_cursor(1)


def test_cursor_pagination_with_secret_signs() -> None:
    token = CursorPagination(limit=2, secret="k").encode_cursor(1)

    assert token.count(".") == 1


@module()
class _EmptyModule:
    pass

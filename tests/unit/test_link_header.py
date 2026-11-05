"""RFC 8288 ``Link`` URLs: query preservation, re-encoding and relations (F29)."""

from __future__ import annotations

from typing import Any

import pytest

from lauren import CursorPage, CursorPagination, OffsetPagination, Page, PageNumberPagination, Request
from lauren._pagination._config import resolve
from lauren._pagination._links import ROOT_PATH_ATTR, RequestSnapshot, build_url, link_header, snapshot
from lauren._pagination._page import page_links
from lauren._pagination._params import _Bound
from lauren.pagination import PaginationConfig

_MANAGED = ("limit", "offset")


def _url(query: str, values: dict[str, str | None], *, path: str = "/items", base_url: str = "") -> str:
    return build_url(RequestSnapshot(path, query.encode("latin-1")), _MANAGED, values, base_url=base_url)


# ---------------------------------------------------------------------------
# build_url
# ---------------------------------------------------------------------------


def test_unrelated_parameters_keep_their_order() -> None:
    url = _url("q=a&limit=10&sort=name&offset=20&tag=x", {"limit": "10", "offset": "30"})

    assert url == "/items?q=a&limit=10&sort=name&offset=30&tag=x"


def test_missing_pagination_parameters_are_appended_in_order() -> None:
    url = _url("q=a", {"limit": "10", "offset": "10"})

    assert url == "/items?q=a&limit=10&offset=10"


def test_parameter_mapped_to_none_is_removed() -> None:
    url = _url("q=a&offset=10&limit=5", {"offset": None, "limit": "5"})

    assert url == "/items?q=a&limit=5"


def test_parameter_absent_from_values_is_kept() -> None:
    url = _url("offset=10&limit=5", {"offset": "20"})

    assert url == "/items?offset=20&limit=5"


def test_repeated_pagination_parameter_is_collapsed() -> None:
    url = _url("offset=1&q=x&offset=2", {"offset": "3", "limit": "5"})

    assert url == "/items?offset=3&q=x&limit=5"


def test_empty_query_produces_bare_path_when_nothing_is_set() -> None:
    assert _url("", {"offset": None, "limit": None}) == "/items"


def test_empty_segments_are_dropped() -> None:
    assert _url("a=1&&b=2&", {}) == "/items?a=1&b=2"


def test_flag_parameter_without_value_is_kept_as_flag() -> None:
    assert _url("debug&a=", {}) == "/items?debug&a="


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("q=hello+world", "q=hello%20world"),
        ("q=hello%20world", "q=hello%20world"),
        ("q=a%26b%3Dc", "q=a%26b%3Dc"),
        ("q=caf%C3%A9", "q=caf%C3%A9"),
        ("q=caf\xc3\xa9", "q=caf%C3%A9"),
        ("q=%2F%3F%23", "q=%2F%3F%23"),
        ("k%20ey=v", "k%20ey=v"),
        ("q=%E6%97%A5%E6%9C%AC", "q=%E6%97%A5%E6%9C%AC"),
    ],
)
def test_values_are_re_encoded_faithfully(query: str, expected: str) -> None:
    assert _url(query, {}) == f"/items?{expected}"


def test_non_ascii_parameter_name_is_matched_and_encoded() -> None:
    url = build_url(RequestSnapshot("/items", b"p%C3%A1gina=2"), ("página",), {"página": "3"})

    assert url == "/items?p%C3%A1gina=3"


def test_path_is_percent_encoded() -> None:
    assert _url("", {"limit": "5"}, path="/files/a b/é") == "/files/a%20b/%C3%A9?limit=5"


def test_base_url_makes_links_absolute() -> None:
    url = _url("q=1", {"limit": "5"}, base_url="https://api.example.com")

    assert url == "https://api.example.com/items?q=1&limit=5"


def test_snapshot_includes_root_path() -> None:
    request = Request(method="GET", path="/items", raw_query_string=b"q=1")
    setattr(request, ROOT_PATH_ATTR, "/api/")

    snap = snapshot(request)

    assert (snap.path, snap.query) == ("/api/items", b"q=1")


def test_snapshot_survives_request_reuse() -> None:
    request = Request(method="GET", path="/items", raw_query_string=b"q=1")
    snap = snapshot(request)

    request.reset(
        method="GET",
        path="/other",
        raw_query_string=b"z=9",
        headers=request.headers,
        client=request.client,
        server=request.server,
        receive=request._receive,
        app_state=request.app_state,
        max_body_size=1024,
    )

    assert (snap.path, snap.query) == ("/items", b"q=1")


def test_link_header_format() -> None:
    header = link_header([("next", "/a?offset=10"), ("last", "/a?offset=90")])

    assert header == '</a?offset=10>; rel="next", </a?offset=90>; rel="last"'


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


def _bound(query: str = "", **config: Any) -> _Bound:
    return _Bound(resolve((PaginationConfig(**config),)), RequestSnapshot("/items", query.encode()))


def _rels(page: Any, paging: Any, bound: _Bound) -> dict[str, str]:
    return dict(page_links(page, paging, bound))


def _offset_page(limit: int, offset: int, total: int | None, has_next: bool | None = None) -> tuple[Any, Any]:
    paging = OffsetPagination(limit=limit, offset=offset)
    items = list(range(max(0, min(limit, (total or limit) - offset))))
    return Page.of(items, paging, total=total, has_next=has_next), paging


def test_first_page_has_no_prev() -> None:
    page, paging = _offset_page(10, 0, 35)

    rels = _rels(page, paging, _bound())

    assert set(rels) == {"first", "next", "last"}
    assert rels["next"] == "/items?limit=10&offset=10"
    assert rels["last"] == "/items?limit=10&offset=30"


def test_middle_page_has_all_relations() -> None:
    page, paging = _offset_page(10, 10, 35)

    rels = _rels(page, paging, _bound())

    assert rels == {
        "first": "/items?limit=10&offset=0",
        "prev": "/items?limit=10&offset=0",
        "next": "/items?limit=10&offset=20",
        "last": "/items?limit=10&offset=30",
    }


def test_last_page_has_no_next() -> None:
    page, paging = _offset_page(10, 30, 35)

    assert "next" not in _rels(page, paging, _bound())


def test_last_requires_total() -> None:
    page, paging = _offset_page(10, 10, None, has_next=True)

    assert set(_rels(page, paging, _bound())) == {"first", "prev", "next"}


def test_empty_collection_last_is_first_page() -> None:
    page, paging = _offset_page(10, 0, 0)

    assert _rels(page, paging, _bound())["last"] == "/items?limit=10&offset=0"


def test_prev_of_out_of_range_page_points_at_last_page() -> None:
    page, paging = _offset_page(10, 100, 35)

    assert _rels(page, paging, _bound())["prev"] == "/items?limit=10&offset=30"


def test_unaligned_prev_never_goes_negative() -> None:
    page, paging = _offset_page(10, 5, 35)

    assert _rels(page, paging, _bound())["prev"] == "/items?limit=10&offset=0"


def test_links_beyond_max_offset_are_omitted() -> None:
    page, paging = _offset_page(10, 20, 100)

    rels = _rels(page, paging, _bound(max_offset=25))

    assert "next" not in rels
    assert "last" not in rels


def test_links_keep_unrelated_query_parameters() -> None:
    page, paging = _offset_page(10, 0, 35)

    rels = _rels(page, paging, _bound("q=a+b&offset=0&limit=10"))

    assert rels["next"] == "/items?q=a%20b&offset=10&limit=10"


def test_absolute_links_with_base_url() -> None:
    page, paging = _offset_page(10, 0, 35)

    rels = _rels(page, paging, _bound(base_url="https://api.example.com"))

    assert rels["next"] == "https://api.example.com/items?limit=10&offset=10"


def test_renamed_parameters_in_links() -> None:
    page, paging = _offset_page(10, 0, 35)

    rels = _rels(page, paging, _bound(limit_param="size", offset_param="skip"))

    assert rels["next"] == "/items?size=10&skip=10"


def test_page_number_relations() -> None:
    paging = PageNumberPagination(page=2, per_page=10)
    page = Page.of(list(range(10)), paging, total=35)

    rels = _rels(page, paging, _bound())

    assert rels == {
        "first": "/items?page=1&per_page=10",
        "prev": "/items?page=1&per_page=10",
        "next": "/items?page=3&per_page=10",
        "last": "/items?page=4&per_page=10",
    }


def test_page_number_prev_of_out_of_range_page_points_at_last_page() -> None:
    paging = PageNumberPagination(page=9, per_page=10)
    page = Page.of([], paging, total=35)

    assert _rels(page, paging, _bound())["prev"] == "/items?page=4&per_page=10"


def test_page_number_links_beyond_max_offset_are_omitted() -> None:
    paging = PageNumberPagination(page=2, per_page=10)
    page = Page.of(list(range(10)), paging, total=100)

    rels = _rels(page, paging, _bound(max_offset=15))

    assert set(rels) == {"first", "prev"}


def test_cursor_relations_never_include_last() -> None:
    paging = CursorPagination(cursor=3, limit=2, secret="k")
    page = CursorPage.from_rows([4, 5, 6], paging, key=lambda r: r)

    rels = _rels(page, paging, _bound("cursor=old&limit=2&q=x"))

    assert set(rels) == {"first", "prev", "next"}
    assert rels["first"] == "/items?limit=2&q=x"
    assert rels["next"] == f"/items?cursor={page.next_cursor}&limit=2&q=x"


def test_self_link_is_the_request_url() -> None:
    page, paging = _offset_page(10, 0, 35)

    links = page_links(page, paging, _bound("q=1&offset=0"), include_self=True)

    assert links[0] == ("self", "/items?q=1&offset=0")

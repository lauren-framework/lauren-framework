"""``Page[T]`` / ``CursorPage[T]`` helpers, ``paginate_sequence`` and page rendering (F29)."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from lauren import (
    CursorPage,
    CursorPagination,
    OffsetPagination,
    Page,
    PageNumberPagination,
    PaginationConfig,
    Request,
    paginate_sequence,
)
from lauren._pagination._config import resolve
from lauren._pagination._page import render, schema_name
from lauren._pagination._params import build_plan
from lauren.validation import TypeCompiler

ITEMS = list(range(1, 36))


# ---------------------------------------------------------------------------
# Page.of / Page.from_rows
# ---------------------------------------------------------------------------


def test_page_of_derives_has_next_from_total() -> None:
    page = Page.of([1, 2], OffsetPagination(limit=2, offset=0), total=3)

    assert (page.items, page.limit, page.offset, page.total, page.has_next) == ([1, 2], 2, 0, 3, True)


def test_page_of_last_page_has_no_next() -> None:
    page = Page.of([3], OffsetPagination(limit=2, offset=2), total=3)

    assert page.has_next is False


def test_page_of_explicit_has_next_wins() -> None:
    page = Page.of([1, 2], OffsetPagination(limit=2), has_next=False)

    assert (page.total, page.has_next) == (None, False)


def test_page_of_never_guesses_has_next() -> None:
    with pytest.raises(ValueError, match="never"):
        Page.of([1, 2], OffsetPagination(limit=2))


def test_page_of_rejects_more_items_than_limit() -> None:
    with pytest.raises(ValueError, match="from_rows"):
        Page.of([1, 2, 3], OffsetPagination(limit=2), total=10)


@pytest.mark.parametrize("total", [-1, True, "3"])
def test_page_of_rejects_invalid_total(total: Any) -> None:
    with pytest.raises(ValueError):
        Page.of([], OffsetPagination(limit=2), total=total)


def test_page_of_rejects_cursor_paging() -> None:
    with pytest.raises(TypeError, match="CursorPage.from_rows"):
        Page.of([], CursorPagination(limit=2), total=0)  # type: ignore[arg-type]


def test_page_of_accepts_page_number_paging() -> None:
    page = Page.of([4, 5], PageNumberPagination(page=2, per_page=2), total=5)

    assert (page.limit, page.offset, page.has_next) == (2, 2, True)


def test_page_of_materialises_iterables() -> None:
    page = Page.of(iter([1, 2]), OffsetPagination(limit=2), total=2)

    assert page.items == [1, 2]


@pytest.mark.parametrize(("rows", "items", "has_next"), [([1, 2, 3], [1, 2], True), ([1, 2], [1, 2], False)])
def test_from_rows_trims_the_probe_row(rows: list[int], items: list[int], has_next: bool) -> None:
    page = Page.from_rows(rows, OffsetPagination(limit=2))

    assert (page.items, page.has_next) == (items, has_next)


def test_from_rows_keeps_total() -> None:
    assert Page.from_rows([1], OffsetPagination(limit=2), total=1).total == 1


def test_page_is_a_frozen_dataclass() -> None:
    page = Page(items=[1], limit=1, offset=0, total=1, has_next=False)

    with pytest.raises(dataclasses.FrozenInstanceError):
        page.limit = 5  # type: ignore[misc]
    assert dataclasses.asdict(page) == {"items": [1], "limit": 1, "offset": 0, "total": 1, "has_next": False}


def test_pages_compare_by_fields_only() -> None:
    built = Page.of([1], OffsetPagination(limit=1), total=1)

    assert built == Page(items=[1], limit=1, offset=0, total=1, has_next=False)


# ---------------------------------------------------------------------------
# CursorPage.from_rows
# ---------------------------------------------------------------------------


def _decode(paging: CursorPagination[Any], token: str | None) -> Any:
    assert token is not None
    assert paging._codec is not None
    return paging._codec.decode(token)


def test_cursor_first_page() -> None:
    paging: CursorPagination[int] = CursorPagination(limit=2, secret="k")

    page = CursorPage.from_rows([1, 2, 3], paging, key=lambda r: r)

    assert (page.items, page.has_next, page.prev_cursor) == ([1, 2], True, None)
    assert _decode(paging, page.next_cursor) == (2, "next")


def test_cursor_last_page_has_no_next_cursor() -> None:
    paging: CursorPagination[int] = CursorPagination(cursor=2, limit=2, secret="k")

    page = CursorPage.from_rows([3], paging, key=lambda r: r)

    assert (page.items, page.has_next, page.next_cursor) == ([3], False, None)
    assert _decode(paging, page.prev_cursor) == (3, "prev")


def test_cursor_prev_direction_returns_ascending_items() -> None:
    paging: CursorPagination[int] = CursorPagination(cursor=5, limit=2, direction="prev", secret="k")

    page = CursorPage.from_rows([4, 3, 2], paging, key=lambda r: r)

    assert (page.items, page.has_next) == ([3, 4], True)
    assert _decode(paging, page.next_cursor) == (4, "next")
    assert _decode(paging, page.prev_cursor) == (3, "prev")


def test_cursor_prev_direction_reaching_the_start_has_no_prev() -> None:
    paging: CursorPagination[int] = CursorPagination(cursor=3, limit=2, direction="prev", secret="k")

    page = CursorPage.from_rows([2, 1], paging, key=lambda r: r)

    assert (page.items, page.prev_cursor) == ([1, 2], None)


def test_cursor_empty_prev_page() -> None:
    paging: CursorPagination[int] = CursorPagination(cursor=1, limit=2, direction="prev", secret="k")

    page = CursorPage.from_rows([], paging, key=lambda r: r)

    assert (page.items, page.has_next, page.next_cursor, page.prev_cursor) == ([], False, None, None)


def test_cursor_from_rows_requires_cursor_paging() -> None:
    with pytest.raises(TypeError):
        CursorPage.from_rows([], OffsetPagination(), key=lambda r: r)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# paginate_sequence
# ---------------------------------------------------------------------------


def test_paginate_sequence_offset() -> None:
    page = paginate_sequence(ITEMS, OffsetPagination(limit=10, offset=30))

    assert (page.items, page.total, page.has_next) == ([31, 32, 33, 34, 35], 35, False)


def test_paginate_sequence_page_number() -> None:
    page = paginate_sequence(ITEMS, PageNumberPagination(page=2, per_page=10))

    assert page.items == list(range(11, 21))


def test_paginate_sequence_accepts_iterables() -> None:
    page = paginate_sequence((i for i in ITEMS), OffsetPagination(limit=3))

    assert page.items == [1, 2, 3]


def test_paginate_sequence_cursor_walks_forward_and_back() -> None:
    first = paginate_sequence(ITEMS, CursorPagination(limit=10, secret="k"))
    cursor, _ = _decode(CursorPagination(secret="k"), first.next_cursor)
    second = paginate_sequence(ITEMS, CursorPagination(cursor=cursor, limit=10, secret="k"))
    back_cursor, direction = _decode(CursorPagination(secret="k"), second.prev_cursor)
    back = paginate_sequence(
        ITEMS, CursorPagination(cursor=back_cursor, limit=10, direction="prev", secret="k")
    )

    assert first.items == ITEMS[:10]
    assert second.items == ITEMS[10:20]
    assert (direction, back.items, back.prev_cursor) == ("prev", ITEMS[:10], None)


def test_paginate_sequence_cursor_last_page() -> None:
    page = paginate_sequence(ITEMS, CursorPagination(cursor=30, limit=10, secret="k"))

    assert (page.items, page.has_next, page.next_cursor) == (ITEMS[30:], False, None)


def test_paginate_sequence_rejects_typed_cursors() -> None:
    with pytest.raises(TypeError, match="positional"):
        paginate_sequence(ITEMS, CursorPagination(cursor={"id": 1}, limit=10, secret="k"))


def test_paginate_sequence_rejects_other_values() -> None:
    with pytest.raises(TypeError):
        paginate_sequence(ITEMS, object())


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _in_request(query: str = "", **config: Any) -> OffsetPagination:
    plan = build_plan(
        OffsetPagination,
        resolve((PaginationConfig(**config),)),
        TypeCompiler(),
        include_input=False,
        where={},
    )
    import asyncio

    request = Request(method="GET", path="/items", raw_query_string=query.encode())
    return asyncio.run(plan.extract(request))  # type: ignore[no-any-return]


def _body(page: Any, **config: Any) -> Any:
    return json.loads(render(page, None, resolve((PaginationConfig(**config),))).body)


def test_render_items_envelope() -> None:
    page = Page.of([1, 2], OffsetPagination(limit=2), total=5)

    assert _body(page) == {"items": [1, 2], "meta": {"limit": 2, "offset": 0, "total": 5, "has_next": True}}


def test_render_camel_meta_keys() -> None:
    page = Page.of([1], PageNumberPagination(page=1, per_page=1), total=3)

    assert _body(page, meta_keys="camel")["meta"] == {
        "page": 1,
        "perPage": 1,
        "total": 3,
        "totalPages": 3,
        "hasNext": True,
    }


def test_render_unknown_total_keeps_meta_shape() -> None:
    page = Page.of([1], PageNumberPagination(per_page=1), has_next=True)

    assert _body(page)["meta"] == {
        "page": 1,
        "per_page": 1,
        "total": None,
        "total_pages": None,
        "has_next": True,
    }


def test_render_data_envelope_without_request_has_null_links() -> None:
    page = Page.of([1], OffsetPagination(limit=1), total=1)

    body = _body(page, envelope="data")

    assert body["data"] == [1]
    assert body["links"] == {"self": None, "first": None, "prev": None, "next": None, "last": None}


def test_render_bare_envelope_sends_total_header() -> None:
    page = Page.of([1], OffsetPagination(limit=1), total=7)

    response = render(page, None, resolve((PaginationConfig(envelope="bare"),)))

    assert json.loads(response.body) == [1]
    assert response.headers.get("x-total-count") == "7"
    assert response.headers.get("link") is None


def test_render_cursor_meta() -> None:
    page = CursorPage(items=[1], limit=1, has_next=False)

    assert _body(page)["meta"] == {"limit": 1, "has_next": False, "next_cursor": None, "prev_cursor": None}


def test_render_in_request_page_uses_its_own_config_and_links() -> None:
    paging = _in_request("q=1", envelope="data", total_header=True)
    page = Page.of([1, 2], paging, total=50)

    response = render(page, None, None)

    body = json.loads(response.body)
    assert body["links"]["self"] == "/items?q=1"
    assert body["links"]["next"] == "/items?q=1&limit=20&offset=20"
    assert response.headers.get("x-total-count") == "50"
    assert 'rel="self"' not in (response.headers.get("link") or "")


def test_render_without_defaults_uses_builtin_shape() -> None:
    page = Page.of([1], OffsetPagination(limit=1), total=1)

    assert "items" in json.loads(render(page, None, None).body)


# ---------------------------------------------------------------------------
# Schema names
# ---------------------------------------------------------------------------


class UserOut:
    pass


@pytest.mark.parametrize(
    ("item", "suffix", "expected"),
    [
        (UserOut, "Page", "UserOutPage"),
        (int, "CursorPage", "IntCursorPage"),
        (None, "Page", "Page"),
        (Any, "Page", "Page"),
        (list[int], "Page", "ListPage"),
    ],
)
def test_schema_names_are_valid_component_keys(item: Any, suffix: str, expected: str) -> None:
    assert schema_name(item, suffix) == expected


def test_schema_name_hook_on_page_types() -> None:
    assert Page.__lauren_schema_name__((UserOut,)) == "UserOutPage"
    assert CursorPage.__lauren_schema_name__((UserOut,)) == "UserOutCursorPage"

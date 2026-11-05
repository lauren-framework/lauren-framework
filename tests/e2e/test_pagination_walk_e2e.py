"""Walking paginated collections by following links, end to end (F29).

A client that only follows ``rel="next"`` (header) or ``links.next`` (body)
sees every item exactly once, in offset, page-number and cursor modes; with
rows inserted between requests, cursor mode still yields every pre-existing
item exactly once (keyset pagination is stable where offsets shift).
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from lauren import (
    CursorPage,
    CursorPagination,
    LaurenFactory,
    OffsetPagination,
    Page,
    PageNumberPagination,
    PaginationConfig,
    Scope,
    controller,
    get,
    injectable,
    module,
)
from lauren.testing import TestClient, TestResponse


@dataclasses.dataclass
class Item:
    id: int
    title: str


@dataclasses.dataclass(frozen=True)
class ItemKey:
    id: int


@injectable(scope=Scope.SINGLETON)
class ItemStore:
    def __init__(self) -> None:
        self.rows: list[Item] = [Item(id=i * 10, title=f"item {i}") for i in range(1, 48)]

    def ordered(self) -> list[Item]:
        return sorted(self.rows, key=lambda r: r.id)

    def insert(self, item_id: int) -> None:
        self.rows.append(Item(id=item_id, title=f"inserted {item_id}"))

    def window(self, limit: int, offset: int) -> tuple[list[Item], int]:
        rows = self.ordered()
        return rows[offset : offset + limit], len(rows)

    def keyset(self, key: ItemKey | None, direction: str, limit: int) -> list[Item]:
        rows = self.ordered()
        if direction == "prev":
            assert key is not None
            return [r for r in reversed(rows) if r.id < key.id][:limit]
        return [r for r in rows if key is None or r.id > key.id][:limit]


@controller("/items")
class ItemController:
    def __init__(self, store: ItemStore) -> None:
        self.store = store

    @get("/offset")
    async def by_offset(self, paging: OffsetPagination, tag: str = "") -> Page[Item]:
        rows, total = self.store.window(paging.limit, paging.offset)
        return Page.of(rows, paging, total=total)

    @get("/pages")
    async def by_page(self, paging: PageNumberPagination) -> Page[Item]:
        rows, total = self.store.window(paging.limit, paging.offset)
        return Page.of(rows, paging, total=total)

    @get("/cursor")
    async def by_cursor(self, paging: CursorPagination[ItemKey], tag: str = "") -> CursorPage[Item]:
        rows = self.store.keyset(paging.cursor, paging.direction, paging.limit + 1)
        return CursorPage.from_rows(rows, paging, key=lambda r: ItemKey(id=r.id))


@module(controllers=[ItemController], providers=[ItemStore])
class WalkModule:
    pass


def _client(**config: Any) -> TestClient:
    return TestClient(
        LaurenFactory.create(WalkModule, pagination=PaginationConfig(cursor_secret="walk-secret", **config))
    )


def _rel(response: TestResponse, rel: str) -> str | None:
    header = response.headers.get("link") or ""
    for part in header.split(", ") if header else ():
        url, rel_part = part.split("; ")
        if rel_part == f'rel="{rel}"':
            return url.strip("<>")
    return None


def _ids(response: TestResponse) -> list[int]:
    body = response.json()
    items = body if isinstance(body, list) else body.get("items", body.get("data"))
    return [item["id"] for item in items]


def _walk(client: TestClient, start: str, rel: str = "next", between: Any = None) -> list[int]:
    seen: list[int] = []
    url: str | None = start
    for _ in range(200):
        if url is None:
            return seen
        response = client.get(url)
        assert response.status_code == 200, response.text
        seen.extend(_ids(response))
        if between is not None:
            between()
        url = _rel(response, rel)
    raise AssertionError("pagination never ended")


EXPECTED = [i * 10 for i in range(1, 48)]


@pytest.mark.parametrize("limit", [1, 7, 10, 47, 100])
def test_offset_walk_sees_every_item_once(limit: int) -> None:
    with _client() as client:
        assert _walk(client, f"/items/offset?limit={limit}&tag=a%20b") == EXPECTED


@pytest.mark.parametrize("per_page", [3, 10, 50])
def test_page_number_walk_sees_every_item_once(per_page: int) -> None:
    with _client() as client:
        assert _walk(client, f"/items/pages?per_page={per_page}") == EXPECTED


@pytest.mark.parametrize("limit", [1, 6, 47, 60])
def test_cursor_walk_sees_every_item_once(limit: int) -> None:
    with _client() as client:
        assert _walk(client, f"/items/cursor?limit={limit}&tag=x") == EXPECTED


def test_cursor_walk_backwards_from_the_last_page() -> None:
    with _client() as client:
        forward_urls: list[str] = []
        url: str | None = "/items/cursor?limit=5"
        last: TestResponse | None = None
        while url is not None:
            forward_urls.append(url)
            last = client.get(url)
            url = _rel(last, "next")
        assert last is not None
        prev_url = _rel(last, "prev")
        assert prev_url is not None

        seen_backwards = _ids(last)
        for _ in range(50):
            if prev_url is None:
                break
            response = client.get(prev_url)
            seen_backwards = _ids(response) + seen_backwards
            prev_url = _rel(response, "prev")

    assert seen_backwards == EXPECTED


def test_cursor_walk_is_stable_under_concurrent_inserts() -> None:
    with _client() as client:
        store = client.resolve(ItemStore)
        inserted = iter(range(1, 200))

        def insert_rows() -> None:
            n = next(inserted)
            store.insert(-n)  # before every cursor: shifts every offset
            store.insert(5 + n * 10)  # between existing rows, ahead of the reader

        seen = _walk(client, "/items/cursor?limit=5", between=insert_rows)

    pre_existing = [i for i in seen if i % 10 == 0 and i > 0]
    assert pre_existing == EXPECTED


def test_offset_walk_follows_body_links_in_data_envelope() -> None:
    with _client(envelope="data") as client:
        seen: list[int] = []
        url: str | None = "/items/offset?limit=10"
        while url is not None:
            body = client.get(url).json()
            seen.extend(item["id"] for item in body["data"])
            url = body["links"]["next"]

    assert seen == EXPECTED


def test_bare_envelope_walk_with_total_count() -> None:
    with _client(envelope="bare") as client:
        first = client.get("/items/offset?limit=10")

        assert first.headers.get("x-total-count") == str(len(EXPECTED))
        assert _walk(client, "/items/offset?limit=10") == EXPECTED

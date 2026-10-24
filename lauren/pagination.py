"""Pagination: validated paging parameters, page responses, links and signed cursors.

Declare a paging parameter and return a page::

    from lauren import OffsetPagination, Page, controller, get

    @controller("/users")
    class UserController:
        def __init__(self, repo: UserRepo) -> None:
            self.repo = repo

        @get("/")
        async def list_users(self, paging: OffsetPagination) -> Page[UserOut]:
            rows, total = await self.repo.list(limit=paging.limit, offset=paging.offset)
            return Page.of(rows, paging, total=total)

``?limit=`` / ``?offset=`` are validated at the edge (422 with
``loc: ["query", "limit"]`` above ``max_limit``), the body is the app's
envelope (``{"items": [...], "meta": {...}}`` by default) and the response
carries RFC 8288 ``Link`` headers built from the request URL.
``PageNumberPagination`` reads ``?page=&per_page=``; ``CursorPagination[C]``
decodes an opaque, HMAC-signed ``?cursor=`` into a typed keyset ``C`` and
pairs with ``CursorPage.from_rows(rows, paging, key=...)``.

Configure app-wide with ``LaurenFactory.create(..., pagination=PaginationConfig(...))``
and per parameter with ``Annotated[OffsetPagination, PaginationConfig(max_limit=500)]``.
"""

from __future__ import annotations

from ._pagination import (
    CursorPage,
    CursorPagination,
    OffsetPagination,
    Page,
    PageNumberPagination,
    PaginationConfig,
    paginate_sequence,
)

__all__ = [
    "CursorPage",
    "CursorPagination",
    "OffsetPagination",
    "Page",
    "PageNumberPagination",
    "PaginationConfig",
    "paginate_sequence",
]

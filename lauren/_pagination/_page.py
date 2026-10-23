"""Page response types, the row helpers and page rendering (private).

``Page[T]`` and ``CursorPage[T]`` are frozen stdlib dataclasses. A page built
with ``Page.of`` / ``Page.from_rows`` / ``CursorPage.from_rows`` /
``paginate_sequence`` remembers the paging value it came from, so returning
it from a handler renders the app's envelope plus RFC 8288 ``Link`` (and
``X-Total-Count``) headers generated from the request URL. The dispatcher
recognises pages with one ``isinstance`` check against :class:`PageResponse`
in ``_coerce_to_response`` (until F01's response-conversion protocol lands).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

from ..types import Headers, Response
from ._config import BUILTIN_DEFAULTS, ResolvedPagination
from ._links import build_url, link_header
from ._params import CursorPagination, OffsetPagination, PageNumberPagination, _Bound

T = TypeVar("T")

#: Characters allowed in an OpenAPI component key (``^[a-zA-Z0-9.\-_]+$``).
_COMPONENT_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

_CAMEL: dict[str, str] = {
    "has_next": "hasNext",
    "per_page": "perPage",
    "total_pages": "totalPages",
    "next_cursor": "nextCursor",
    "prev_cursor": "prevCursor",
}

#: Link relations of the ``"data"`` envelope's ``links`` object, in order.
LINK_RELATIONS: tuple[str, ...] = ("self", "first", "prev", "next", "last")


def schema_name(item_type: Any, suffix: str) -> str:
    """``UserOut`` + ``"Page"`` → ``"UserOutPage"`` (a valid OpenAPI component key)."""
    if item_type is None or item_type is Any:
        base = ""
    else:
        base = getattr(item_type, "__name__", None) or repr(item_type)
        base = _COMPONENT_UNSAFE.sub("_", base).strip("_")
        if base:
            base = base[0].upper() + base[1:]
    return f"{base}{suffix}"


class PageResponse:
    """Private base of the values the dispatcher renders as a page response."""

    __slots__ = ()

    #: Suffix of the OpenAPI component name (``UserOut`` + suffix).
    _schema_suffix: ClassVar[str] = "Page"

    @classmethod
    def __lauren_schema_name__(cls, args: tuple[Any, ...]) -> str:
        """Deterministic OpenAPI component name for ``cls[*args]`` (``UserOutPage``)."""
        return schema_name(args[0] if args else None, cls._schema_suffix)

    def _lauren_response(self, encoder: Any, defaults: ResolvedPagination | None) -> Response:
        return render(self, encoder, defaults)


@dataclass(frozen=True, kw_only=True)
class Page(PageResponse, Generic[T]):
    """One page of an offset / page-number paginated collection.

    ``items`` holds at most ``limit`` items starting at ``offset``; ``total``
    is the size of the whole collection when the handler knows it (``None``
    otherwise — counting is optional, as with Spring's ``Slice``) and
    ``has_next`` says whether another page follows. Build pages with
    :meth:`of`, :meth:`from_rows` or ``paginate_sequence`` so the response
    carries ``Link`` headers; a page constructed directly renders without them.
    """

    items: list[T]
    limit: int
    offset: int = 0
    total: int | None = None
    has_next: bool

    _schema_suffix: ClassVar[str] = "Page"

    @classmethod
    def of(
        cls,
        items: Iterable[T],
        paging: OffsetPagination | PageNumberPagination,
        *,
        total: int | None = None,
        has_next: bool | None = None,
    ) -> Page[T]:
        """Build the page for ``paging`` from at most ``paging.limit`` items.

        ``has_next`` is ``offset + len(items) < total`` when ``total`` is
        given; otherwise pass ``has_next=`` (or fetch ``limit + 1`` rows and
        use :meth:`from_rows`). The framework never guesses it from
        ``len(items) == limit``.
        """
        _check_offset_paging(paging, "Page.of")
        rows = list(items)
        if len(rows) > paging.limit:
            raise ValueError(
                f"Page.of() got {len(rows)} items for limit {paging.limit}; "
                "fetch limit + 1 rows and use Page.from_rows() to trim the probe row"
            )
        if total is not None:
            _check_total(total)
        if has_next is None:
            if total is None:
                raise ValueError(
                    "Page.of() needs total= or has_next=: whether another page exists is never "
                    "guessed; fetch limit + 1 rows and use Page.from_rows() when counting is too costly"
                )
            has_next = paging.offset + len(rows) < total
        page = cls(items=rows, limit=paging.limit, offset=paging.offset, total=total, has_next=bool(has_next))
        object.__setattr__(page, "_paging", paging)
        return page

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[T],
        paging: OffsetPagination | PageNumberPagination,
        *,
        total: int | None = None,
    ) -> Page[T]:
        """Build the page from up to ``paging.limit + 1`` rows.

        The extra (probe) row only proves a next page exists: it is trimmed
        and ``has_next`` is ``True`` when it was present.
        """
        _check_offset_paging(paging, "Page.from_rows")
        fetched = list(rows)
        more = len(fetched) > paging.limit
        return cls.of(fetched[: paging.limit], paging, total=total, has_next=more)


@dataclass(frozen=True, kw_only=True)
class CursorPage(PageResponse, Generic[T]):
    """One page of a cursor (keyset) paginated collection.

    ``next_cursor`` / ``prev_cursor`` are opaque signed cursors (``None`` when
    there is no such page) that the client sends back as ``?cursor=``;
    ``has_next`` says whether another page follows. Build it with
    :meth:`from_rows`.
    """

    items: list[T]
    limit: int
    has_next: bool
    next_cursor: str | None = None
    prev_cursor: str | None = None

    _schema_suffix: ClassVar[str] = "CursorPage"

    @classmethod
    def from_rows(
        cls,
        rows: Iterable[T],
        paging: CursorPagination[Any],
        *,
        key: Callable[[T], Any],
    ) -> CursorPage[T]:
        """Build the page from up to ``paging.limit + 1`` rows in the direction of travel.

        For ``paging.direction == "next"`` pass the rows after the cursor in
        ascending order; for ``"prev"`` the rows before it in descending order
        (the page is returned ascending). The probe row is trimmed, and
        ``key(row)`` of the boundary rows is signed into ``next_cursor`` /
        ``prev_cursor``.
        """
        if not isinstance(paging, CursorPagination):
            raise TypeError(f"CursorPage.from_rows() needs a CursorPagination, got {type(paging).__name__}")
        fetched = list(rows)
        limit = paging.limit
        more = len(fetched) > limit
        items = fetched[:limit]
        next_cursor: str | None = None
        prev_cursor: str | None = None
        if paging.direction == "prev":
            items.reverse()
            has_next = bool(items)
            if items:
                next_cursor = paging.encode_cursor(key(items[-1]), direction="next")
                if more:
                    prev_cursor = paging.encode_cursor(key(items[0]), direction="prev")
        else:
            has_next = more
            if more:
                next_cursor = paging.encode_cursor(key(items[-1]), direction="next")
            if paging.cursor is not None and items:
                prev_cursor = paging.encode_cursor(key(items[0]), direction="prev")
        page = cls(
            items=items, limit=limit, has_next=has_next, next_cursor=next_cursor, prev_cursor=prev_cursor
        )
        object.__setattr__(page, "_paging", paging)
        return page


def _check_offset_paging(paging: Any, where: str) -> None:
    if not isinstance(paging, (OffsetPagination, PageNumberPagination)):
        raise TypeError(
            f"{where}() needs an OffsetPagination or PageNumberPagination, got {type(paging).__name__}"
            + (
                "; use CursorPage.from_rows() for cursor pagination"
                if isinstance(paging, CursorPagination)
                else ""
            )
        )


def _check_total(total: Any) -> None:
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError(f"total must be an int >= 0 or None, got {total!r}")


def paginate_sequence(seq: Iterable[T], paging: Any) -> Any:
    """Paginate an in-memory sequence (or any iterable) — for small lists, tests and demos.

    With :class:`OffsetPagination` / :class:`PageNumberPagination` it returns a
    ``Page[T]`` with ``total=len(seq)``. With :class:`CursorPagination` it
    returns a ``CursorPage[T]`` whose cursors are item positions, so declare
    the parameter as ``CursorPagination`` or ``CursorPagination[int]``.
    """
    items: Sequence[T] = seq if isinstance(seq, Sequence) else list(seq)
    if isinstance(paging, (OffsetPagination, PageNumberPagination)):
        start = paging.offset
        return Page.of(items[start : start + paging.limit], paging, total=len(items))
    if not isinstance(paging, CursorPagination):
        raise TypeError(
            "paginate_sequence() needs an OffsetPagination, PageNumberPagination or CursorPagination, "
            f"got {type(paging).__name__}"
        )
    position = paging.cursor
    if position is not None and (isinstance(position, bool) or not isinstance(position, int) or position < 0):
        raise TypeError(
            "paginate_sequence() uses positional cursors; declare the parameter as CursorPagination "
            f"or CursorPagination[int] (got a {type(position).__name__} cursor)"
        )
    size = len(items)
    limit = paging.limit
    if paging.direction == "prev":
        end = min(position if position is not None else size, size)
        start = max(0, end - limit)
    else:
        start = min(position or 0, size)
        end = min(start + limit, size)
    has_next = end < size
    page: CursorPage[T] = CursorPage(
        items=list(items[start:end]),
        limit=limit,
        has_next=has_next,
        next_cursor=paging.encode_cursor(end, direction="next") if has_next else None,
        prev_cursor=paging.encode_cursor(start, direction="prev") if start > 0 else None,
    )
    object.__setattr__(page, "_paging", paging)
    return page


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def page_kind(page: PageResponse, paging: Any) -> str:
    """``"cursor"``, ``"page"`` (page-number) or ``"offset"``."""
    if isinstance(page, CursorPage):
        return "cursor"
    return "page" if isinstance(paging, PageNumberPagination) else "offset"


def _key(name: str, style: str) -> str:
    return _CAMEL.get(name, name) if style == "camel" else name


def _meta(page: Any, kind: str, paging: Any, style: str) -> dict[str, Any]:
    if kind == "cursor":
        pairs: list[tuple[str, Any]] = [
            ("limit", page.limit),
            ("has_next", page.has_next),
            ("next_cursor", page.next_cursor),
            ("prev_cursor", page.prev_cursor),
        ]
    elif kind == "page":
        total = page.total
        pairs = [
            ("page", paging.page),
            ("per_page", paging.per_page),
            ("total", total),
            ("total_pages", math.ceil(total / page.limit) if total is not None else None),
            ("has_next", page.has_next),
        ]
    else:
        pairs = [
            ("limit", page.limit),
            ("offset", page.offset),
            ("total", page.total),
            ("has_next", page.has_next),
        ]
    return {_key(name, style): value for name, value in pairs}


def _reachable(offset: int, cfg: ResolvedPagination) -> bool:
    return cfg.max_offset is None or offset <= cfg.max_offset


def page_links(page: Any, paging: Any, bound: _Bound, *, include_self: bool = False) -> list[tuple[str, str]]:
    """The navigation links of ``page`` as ``[(rel, url), ...]`` (first, prev, next, last)."""
    cfg = bound.config
    snap = bound.snap
    kind = page_kind(page, paging)
    if kind == "cursor":
        managed: tuple[str, ...] = (cfg.limit_param, cfg.cursor_param)
    elif kind == "page":
        managed = (cfg.page_param, cfg.per_page_param)
    else:
        managed = (cfg.limit_param, cfg.offset_param)

    def url(values: dict[str, str | None]) -> str:
        return build_url(snap, managed, values, base_url=cfg.base_url)

    links: list[tuple[str, str]] = []
    if include_self:
        links.append(("self", url({})))
    limit = page.limit
    if kind == "cursor":
        links.append(("first", url({cfg.limit_param: str(limit), cfg.cursor_param: None})))
        if page.prev_cursor is not None:
            links.append(("prev", url({cfg.limit_param: str(limit), cfg.cursor_param: page.prev_cursor})))
        if page.next_cursor is not None:
            links.append(("next", url({cfg.limit_param: str(limit), cfg.cursor_param: page.next_cursor})))
        return links
    total = page.total
    offset = page.offset
    last_offset = ((total - 1) // limit) * limit if total else 0
    if kind == "page":
        number = offset // limit + 1
        last_number = last_offset // limit + 1
        size = {cfg.per_page_param: str(limit)}
        links.append(("first", url({cfg.page_param: "1", **size})))
        if number > 1:
            prev_number = number - 1 if total is None else min(number - 1, last_number)
            links.append(("prev", url({cfg.page_param: str(prev_number), **size})))
        if page.has_next and _reachable(offset + limit, cfg):
            links.append(("next", url({cfg.page_param: str(number + 1), **size})))
        if total is not None and _reachable(last_offset, cfg):
            links.append(("last", url({cfg.page_param: str(last_number), **size})))
        return links
    size = {cfg.limit_param: str(limit)}
    links.append(("first", url({cfg.offset_param: "0", **size})))
    if offset > 0:
        prev_offset = max(0, offset - limit)
        if total is not None:
            prev_offset = min(prev_offset, last_offset)
        links.append(("prev", url({cfg.offset_param: str(prev_offset), **size})))
    if page.has_next and _reachable(offset + limit, cfg):
        links.append(("next", url({cfg.offset_param: str(offset + limit), **size})))
    if total is not None and _reachable(last_offset, cfg):
        links.append(("last", url({cfg.offset_param: str(last_offset), **size})))
    return links


def render(page: PageResponse, encoder: Any, defaults: ResolvedPagination | None) -> Response:
    """Convert a page into its JSON :class:`Response` (envelope + ``Link`` / ``X-Total-Count``)."""
    paging = page.__dict__.get("_paging")
    bound: _Bound | None = getattr(paging, "_bound", None) if paging is not None else None
    cfg = bound.config if bound is not None else (defaults or BUILTIN_DEFAULTS)
    kind = page_kind(page, paging)
    items = list(getattr(page, "items"))
    links = page_links(page, paging, bound, include_self=cfg.envelope == "data") if bound is not None else []
    header_links = [(rel, href) for rel, href in links if rel != "self"]
    raw_headers: list[tuple[str, str]] = []
    if header_links:
        raw_headers.append(("link", link_header(header_links)))
    total = getattr(page, "total", None)
    if total is not None and cfg.total_header:
        raw_headers.append(("x-total-count", str(total)))
    body: Any
    if cfg.envelope == "bare":
        body = items
    else:
        meta = _meta(page, kind, paging, cfg.meta_keys)
        if cfg.envelope == "data":
            by_rel = dict(links)
            body = {"data": items, "meta": meta, "links": {rel: by_rel.get(rel) for rel in LINK_RELATIONS}}
        else:
            body = {"items": items, "meta": meta}
    return Response.json(body, headers=Headers(raw_headers) if raw_headers else None, encoder=encoder)


__all__ = [
    "CursorPage",
    "LINK_RELATIONS",
    "Page",
    "PageResponse",
    "page_kind",
    "page_links",
    "paginate_sequence",
    "render",
    "schema_name",
]

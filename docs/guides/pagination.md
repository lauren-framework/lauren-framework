# Pagination

> Every list endpoint needs the same decisions: parameter names, defaults, a hard maximum, offset or cursor, how to say "there is more", and how to document it. Lauren makes them once per app: declare `paging: OffsetPagination` (or `PageNumberPagination`, or `CursorPagination[C]`), return a `Page[T]` (or `CursorPage[T]`), and the framework validates the parameters, renders one consistent envelope, emits RFC 8288 `Link` headers and documents it all in OpenAPI.

## Quick start

<!-- docs-contract: run -->
```python
from dataclasses import dataclass

from lauren import LaurenFactory, OffsetPagination, Page, controller, get, injectable, module
from lauren.testing import TestClient


@dataclass
class UserOut:
    id: int
    name: str


@injectable()
class UserRepo:
    def __init__(self) -> None:
        self.rows = [UserOut(id=i, name=f"user {i}") for i in range(1, 136)]

    async def list(self, *, limit: int, offset: int) -> tuple[list[UserOut], int]:
        return self.rows[offset : offset + limit], len(self.rows)


@controller("/users")
class UserController:
    def __init__(self, repo: UserRepo) -> None:
        self.repo = repo

    @get("/")
    async def list_users(self, paging: OffsetPagination) -> Page[UserOut]:
        rows, total = await self.repo.list(limit=paging.limit, offset=paging.offset)
        return Page.of(rows, paging, total=total)


@module(controllers=[UserController], providers=[UserRepo])
class AppModule:
    pass


client = TestClient(LaurenFactory.create(AppModule))

response = client.get("/users/?limit=20&offset=40&q=ada")
assert response.json()["meta"] == {"limit": 20, "offset": 40, "total": 135, "has_next": True}
assert response.json()["items"][0] == {"id": 41, "name": "user 41"}
assert response.headers["link"] == (
    '</users/?limit=20&offset=0&q=ada>; rel="first", '
    '</users/?limit=20&offset=20&q=ada>; rel="prev", '
    '</users/?limit=20&offset=60&q=ada>; rel="next", '
    '</users/?limit=20&offset=120&q=ada>; rel="last"'
)

bad = client.get("/users/?limit=500&offset=-1")
assert bad.status_code == 422
assert [issue["loc"] for issue in bad.json()["error"]["detail"]["issues"]] == [
    ["query", "limit"],
    ["query", "offset"],
]
```

One parameter and one return type give you validated parameters with a hard maximum, a consistent envelope and navigation links. Unrelated query parameters (`q=ada`) are kept in every link, in their original order.

## Paging parameters

A handler parameter annotated with a paging type is a built-in extraction source, compiled once at startup like `Query[int]` — no `Depends`, no marker.

| Type | Query parameters | Exposes |
|---|---|---|
| `OffsetPagination` | `?limit=&offset=` | `.limit`, `.offset` |
| `PageNumberPagination` | `?page=&per_page=` (1-based) | `.page`, `.per_page`, plus `.limit` / `.offset` (`(page - 1) * per_page`) |
| `CursorPagination[C]` | `?cursor=&limit=` | `.cursor` (a `C`, or `None` on the first page), `.direction`, `.limit` |

The integers are validated by the native validation engine with `Ge` / `Le` markers built from the configuration: `limit` (and `per_page`) between 1 and `max_limit` (default 20, maximum 100), `offset` from 0 up to `max_offset` (unbounded unless configured), `page` from 1. A bad value is a 422 in the usual shape — `loc: ["query", "limit"]`, `type: "less_than_equal"`, `ctx: {"le": 100}` — and is reported together with every other failing parameter of the request. A missing parameter takes its default.

`PageNumberPagination` and `OffsetPagination` expose the same `.limit` / `.offset`, so the repository call and `Page.of(...)` are identical for both. A page beyond `max_offset` is a 422 on `page`.

## Configuration

`PaginationConfig` is a partial declaration — every field defaults to `None`, meaning "inherit". Set it app-wide, then override per parameter with `Annotated` metadata:

<!-- docs-contract: fragment -->
```python
from typing import Annotated

from lauren import LaurenFactory, OffsetPagination, PaginationConfig

app = LaurenFactory.create(
    AppModule,
    pagination=PaginationConfig(default_limit=25, max_limit=200, max_offset=10_000),
)

# One route allows bigger pages and names the parameter like its frontend does.
Export = Annotated[OffsetPagination, PaginationConfig(max_limit=1000, limit_param="pageSize")]


@get("/export")
async def export(self, paging: Export) -> Page[OrderOut]: ...
```

Precedence is **parameter annotation → app → built-in default**:

| Field | Built-in default | Meaning |
|---|---|---|
| `default_limit` | `20` | Page size when the client sends none |
| `max_limit` | `100` | Largest accepted `limit` / `per_page` |
| `max_offset` | unbounded | Largest accepted `offset` (deep offsets are a database trap) |
| `limit_param`, `offset_param`, `page_param`, `per_page_param`, `cursor_param` | `limit`, `offset`, `page`, `per_page`, `cursor` | Query parameter names |
| `envelope` | `"items"` | Response body shape (app-wide only, see below) |
| `meta_keys` | `"snake"` | `"camel"` turns `has_next` into `hasNext` (app-wide only) |
| `total_header` | on for `"bare"`, off otherwise | Send `X-Total-Count` when the total is known |
| `base_url` | relative links | A trusted origin (`"https://api.example.com"`) that makes `Link` URLs absolute |
| `cursor_secret` | none | Key(s) that sign cursors; required by any `CursorPagination` route |
| `cursor_max_age` | never expire | Cursor lifetime in seconds |

Everything is validated by `LaurenFactory.create`: a `default_limit` above the effective `max_limit`, non-positive limits, duplicate parameter names, an unknown envelope or a malformed `base_url` raise `StartupError` with `detail["reason"] == "invalid_pagination_config"` and `detail["field"]`. The one exception: when a more specific level (a parameter's `Annotated` config, or the app over the built-in default) lowers `max_limit` below an inherited `default_limit`, the default follows the maximum down. `envelope` and `meta_keys` are rejected in a per-parameter config — one response shape per app keeps clients and generated types consistent.

## Building pages

| Helper | Use it when |
|---|---|
| `Page.of(items, paging, total=...)` | you know the total (a `COUNT(*)` is cheap enough) |
| `Page.of(items, paging, has_next=...)` | you know whether more rows exist some other way |
| `Page.from_rows(rows, paging)` | you fetched `paging.limit + 1` rows: the probe row is trimmed and proves `has_next` |
| `CursorPage.from_rows(rows, paging, key=...)` | cursor pagination (below) |
| `paginate_sequence(seq, paging)` | an in-memory list or iterable (small collections, tests, demos) |

The framework never guesses `has_next` from `len(items) == limit`: `Page.of` without `total=` or `has_next=` raises `ValueError`, and so does passing more items than `paging.limit` (you forgot to trim the probe row — use `from_rows`). Counting is optional, as with Spring Data's `Slice`; skip `total` on large tables.

<!-- docs-contract: fragment -->
```python
@get("/orders")
async def list_orders(self, paging: OffsetPagination) -> Page[OrderOut]:
    rows = await self.repo.list(limit=paging.limit + 1, offset=paging.offset)  # one probe row
    return Page.from_rows(rows, paging)  # total stays None: no COUNT(*)
```

`Page[T]` and `CursorPage[T]` are frozen stdlib dataclasses, so items may be pydantic models, msgspec structs, dataclasses or `TypedDict`s — they serialise through the app's JSON encoder exactly like a plain list. Build pages with the helpers above: a page constructed directly (`Page(items=..., limit=..., offset=..., has_next=...)`) renders with the app's envelope but without links, because it does not know the request URL.

## Response shape

The envelope is chosen once per app with `PaginationConfig(envelope=...)`:

| `envelope=` | Body | Headers |
|---|---|---|
| `"items"` (default) | `{"items": [...], "meta": {"limit": 20, "offset": 40, "total": 135, "has_next": true}}` | `Link` (+ `X-Total-Count` with `total_header=True`) |
| `"data"` | `{"data": [...], "meta": {...}, "links": {"self", "first", "prev", "next", "last"}}` (JSON:API style; unavailable links are `null`) | `Link` |
| `"bare"` | `[...]` (GitHub style) | `Link`, `X-Total-Count` |

`meta` depends on the paging type:

| Paging | `meta` keys |
|---|---|
| `OffsetPagination` | `limit`, `offset`, `total`, `has_next` |
| `PageNumberPagination` | `page`, `per_page`, `total`, `total_pages`, `has_next` |
| `CursorPagination` | `limit`, `has_next`, `next_cursor`, `prev_cursor` |

`total` (and `total_pages`) is `null` when the handler did not supply it; the shape never changes.

### `Link` headers

Links follow RFC 8288 and are built from a copy of the request's path and query taken by the extractor: only the pagination parameters are replaced, every other parameter keeps its position and is re-encoded faithfully (spaces as `%20`, `&` / `=` inside values stay escaped, non-ASCII bytes stay percent-encoded).

| Relation | When |
|---|---|
| `first` | always |
| `prev` | not on the first page (offset 0 / no cursor) |
| `next` | only when `has_next` |
| `last` | only when `total` is known — never for cursor pages |

A link whose offset would exceed `max_offset` is omitted, since following it would answer 422.

Links are path-absolute references (`/users/?limit=20&offset=60`) that include the app's root path (`LaurenFactory.create(root_path=...)` or the server's ASGI `root_path`); clients resolve them against the request URL as RFC 8288 specifies. Set `PaginationConfig(base_url="https://api.example.com")` for absolute URLs — a configured, trusted origin, never the request's `Host` header.

## Cursor pagination

Offsets shift when rows are inserted or deleted between requests, and deep offsets are slow. Keyset (cursor) pagination fixes both: the client sends back an opaque cursor that encodes "the last row I saw", and the next query continues strictly after it.

<!-- docs-contract: run -->
```python
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from lauren import (
    CursorPage,
    CursorPagination,
    LaurenFactory,
    PaginationConfig,
    controller,
    get,
    module,
)
from lauren.testing import TestClient

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


@dataclass
class EventOut:
    id: int
    created_at: datetime


@dataclass(frozen=True)
class EventCursor:  # the keyset, validated when a cursor is decoded
    created_at: datetime
    id: int


EVENTS = [EventOut(id=i, created_at=START + timedelta(minutes=i)) for i in range(1, 26)]


def sort_key(event: EventOut) -> tuple[datetime, int]:
    return (event.created_at, event.id)


@controller("/events")
class EventController:
    @get("/")
    async def events(self, paging: CursorPagination[EventCursor]) -> CursorPage[EventOut]:
        bound = (paging.cursor.created_at, paging.cursor.id) if paging.cursor else None
        if paging.direction == "next":  # rows after the cursor, ascending
            rows = [e for e in EVENTS if bound is None or sort_key(e) > bound]
        else:  # "prev": rows before the cursor, descending
            rows = [e for e in reversed(EVENTS) if sort_key(e) < bound]
        return CursorPage.from_rows(
            rows[: paging.limit + 1],
            paging,
            key=lambda e: EventCursor(created_at=e.created_at, id=e.id),
        )


@module(controllers=[EventController])
class EventsModule:
    pass


app = LaurenFactory.create(
    EventsModule,
    pagination=PaginationConfig(cursor_secret="a-dedicated-random-key"),  # never the session secret
)
client = TestClient(app)

first = client.get("/events/?limit=10").json()
assert [e["id"] for e in first["items"]] == list(range(1, 11))
assert first["meta"]["prev_cursor"] is None

second = client.get("/events/", params={"limit": 10, "cursor": first["meta"]["next_cursor"]}).json()
assert [e["id"] for e in second["items"]] == list(range(11, 21))

back = client.get("/events/", params={"limit": 10, "cursor": second["meta"]["prev_cursor"]}).json()
assert [e["id"] for e in back["items"]] == list(range(1, 11))

tampered = client.get("/events/", params={"cursor": first["meta"]["next_cursor"][:-1] + "0"})
assert tampered.status_code == 400
assert tampered.json()["error"]["code"] == "invalid_cursor"
```

How it works:

* **Direction.** A cursor carries a direction. For `"next"` fetch the rows strictly after the keyset in ascending order; for `"prev"` the rows strictly before it in descending order. Either way fetch `paging.limit + 1` rows: `CursorPage.from_rows` trims the probe row, returns the page in ascending order, sets `has_next` and signs `next_cursor` / `prev_cursor` from `key(row)` of the boundary rows. `prev_cursor` is `None` on the first page.
* **The keyset type.** `C` is any type the validation engine compiles — a dataclass, `TypedDict`, `NamedTuple`, pydantic model, msgspec struct or scalar (`CursorPagination[int]` for an id). The keyset is projected to JSON when signed and validated back into a `C` when decoded, so `paging.cursor.created_at` is a real `datetime`. A type the engine cannot compile raises `UnresolvableParameterError` (`reason="unsupported_cursor_type"`) at startup; bare `CursorPagination` accepts any JSON keyset.
* **Signed and versioned.** The wire format is `base64url(json({"v": 1, "k": keyset, "d": direction, "x": expiry?}))` plus an HMAC-SHA256 signature. A tampered, truncated, malformed, expired or wrong-version cursor is a 400 `BadRequestError` with `code="invalid_cursor"` and `detail["reason"]` — never a 500 and never a silently empty page. The cursor is signed, not encrypted: never put data the client must not read into a keyset.
* **Keys.** `PaginationConfig(cursor_secret=...)` takes a string, bytes, a `lauren.config.Secret` (so it can come from your typed settings) or a sequence of them for rotation: the first key signs, every key verifies. Rotate by prepending a new key and dropping the old one once its cursors have aged out. A `CursorPagination` route without a secret fails `LaurenFactory.create` with `StartupError(detail={"reason": "pagination_secret_missing", "route": ...})`. Use a dedicated key — cursors are signed in their own namespace, so even a reused session secret could not turn a session cookie into a cursor, but one key per purpose limits the damage of a leak.
* **Expiry.** `PaginationConfig(cursor_max_age=3600)` stamps cursors with an expiry; older cursors (and cursors issued before expiry was configured) are rejected with `reason="expired"`.

`paginate_sequence(seq, paging)` also accepts a bare `CursorPagination` (or `CursorPagination[int]`): it uses item positions as cursors, which is handy for demos and tests but not stable under inserts.

## OpenAPI

The generated document describes paginated routes completely:

* **Parameters** — `limit` / `offset` (or `page` / `per_page`, or `cursor` / `limit`) under their configured names, with `minimum`, `maximum` and `default` from the effective configuration; `cursor` is an opaque string.
* **Bodies** — a named component per item type and paging style: `UserOutPage` (offset), `UserOutNumberedPage` (page number), `EventOutCursorPage` (cursor), with the app's envelope and `meta` keys; item types never collide. With `envelope="bare"` the body is a plain array.
* **Headers** — the 200 response documents `Link`, and `X-Total-Count` when the configuration sends it.

## Testing services that take a paging value

Paging values are plain objects you can build in a unit test; built outside a request they render without links:

<!-- docs-contract: run -->
```python
from lauren import CursorPage, CursorPagination, OffsetPagination, Page, paginate_sequence

page = paginate_sequence(range(1, 8), OffsetPagination(limit=3, offset=3))
assert (page.items, page.total, page.has_next) == ([4, 5, 6], 7, True)

probe = Page.from_rows([1, 2, 3, 4], OffsetPagination(limit=3))
assert (probe.items, probe.has_next, probe.total) == ([1, 2, 3], True, None)

paging = CursorPagination(limit=2, secret="test-key")  # secret= lets it sign cursors
cursor_page = CursorPage.from_rows([10, 20, 30], paging, key=lambda row: row)
assert cursor_page.items == [10, 20] and cursor_page.next_cursor is not None
```

## Database queries

Core computes `limit`, `offset` and cursor keysets; it does not build queries. Pass `paging.limit + 1` / `paging.offset` (or the keyset) to your repository and finish with `Page.from_rows` / `CursorPage.from_rows`. An ORM companion (the planned `lauren-sqlalchemy` `paginate(select, paging)`) builds on exactly these primitives.

## Errors

| Condition | Error |
|---|---|
| `limit` / `offset` / `page` / `per_page` out of range or not an integer | 422 `ExtractorFieldError`, `issues[].loc == ["query", <name>]` |
| Tampered, malformed, expired or outdated cursor | 400 `BadRequestError`, `code="invalid_cursor"`, `detail["reason"]` |
| `CursorPagination` route without `cursor_secret` | `StartupError`, `detail["reason"] == "pagination_secret_missing"` |
| Invalid `PaginationConfig` | `StartupError`, `detail["reason"] == "invalid_pagination_config"`, `detail["field"]` |
| Cursor type the engine cannot compile | `UnresolvableParameterError`, `detail["reason"] == "unsupported_cursor_type"` |

## Pitfalls

* Exposing raw database ids or offsets as cursors — clients start constructing them, and you can never change the ordering again. Use `CursorPagination`: its cursors are opaque and signed.
* Reusing the session secret as `cursor_secret`. Keep one key per purpose.
* Returning a page built without `Page.of` / `from_rows` / `paginate_sequence` and expecting `Link` headers — only pages that know their paging value (and through it the request URL) can link.
* Counting rows on every request of a huge table. `total` is optional: fetch `limit + 1` rows with `Page.from_rows`, or switch to cursors.

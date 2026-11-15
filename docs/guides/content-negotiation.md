# Content Negotiation & Body Limits

> What a route accepts (`consumes=`), what it answers with (`produces=`), how
> large a body may be (`max_body_size=`) and which codecs turn bytes into data
> (`ContentConfig(codecs=...)`). Every rule is compiled in
> `LaurenFactory.create`; routes that keep the defaults pay nothing per request.

---

## Quick start

<!-- docs-contract: run -->
```python
from dataclasses import dataclass

from lauren import Bytes, ContentConfig, Json, LaurenFactory, controller, get, module, patch, post
from lauren.testing import TestClient

MiB = 1024 * 1024


@dataclass
class Item:
    name: str


@controller("/items")
class ItemController:
    @post("/")                                              # accepts JSON (inferred)
    async def create(self, body: Json[Item]) -> Item:
        return body

    @patch("/{id}", consumes=["application/merge-patch+json"])
    async def merge(self, id: int, body: Json[Item]) -> dict:
        return {"id": id, "name": body.name}

    @post("/import", consumes="text/csv", max_body_size=20 * MiB)
    async def import_csv(self, body: Bytes) -> dict:
        return {"rows": body.count(b"\n") + 1}

    @get("/{id}", produces=["application/json", "application/vnd.items+json"])
    async def show(self, id: int) -> Item:
        return Item(name=f"item-{id}")


@module(controllers=[ItemController])
class AppModule:
    pass


app = LaurenFactory.create(AppModule, content=ContentConfig(strict_content_type=True))
client = TestClient(app)

# JSON routes reject what an HTML form can send cross-site.
assert client.post("/items/", json={"name": "a"}).status_code == 200
forged = client.post("/items/", content=b'{"name": "a"}', headers={"content-type": "text/plain"})
assert forged.status_code == 415
assert forged.headers["accept-post"] == "application/json, application/*+json"

# Declared consumes are always enforced.
assert client.post("/items/import", content=b"a,b\nc,d", headers={"content-type": "text/csv"}).json() == {"rows": 2}
assert client.post("/items/import", json=[]).status_code == 415

# One route may take 20 MiB while the others keep the 1 MiB default.
assert client.post("/items/", content=b" " * (2 * MiB), headers={"content-type": "application/json"}).status_code == 413

# Accept picks the representation; the response varies on it.
shown = client.get("/items/1", headers={"accept": "application/vnd.items+json"})
assert shown.headers["content-type"] == "application/vnd.items+json"
assert shown.headers["vary"] == "Accept"
```

---

## Why content types matter: the CSRF backstop

Browsers send `text/plain`, `application/x-www-form-urlencoded` and
`multipart/form-data` cross-origin **without a CORS preflight**. CORS only stops
the attacker from *reading* the response — the request still runs. A plain HTML
form with `enctype="text/plain"` and an input named `{"amount": 100, "pad": "`
with the value `"}` produces a body that is valid JSON; a `fetch()` with a
typeless `Blob` sends no `Content-Type` at all. If a JSON endpoint is
authenticated by something the browser attaches on its own — a cookie session
with `same_site="none"`, a sibling-subdomain attacker (`SameSite` is
site-scoped), cached HTTP Basic credentials, a client certificate, or the
network position of an intranet app — that request is a CSRF.

`ContentConfig(strict_content_type=True)` closes it: a JSON body parameter only
accepts `application/json` and `application/*+json` (UTF-8), and a non-empty
body **without** a `Content-Type` is a 415 too. The check runs after guards
(401/403 still win) and before a single body byte is read. It is a backstop —
lauren-guards' `csrf()` remains the complete defence for
form-encoded routes.

| `strict_content_type` | JSON / form bodies |
|---|---|
| `True` | 415 `UnsupportedMediaTypeError` for anything else (including multipart bodies on a `Form[T]` route until F04) |
| `"warn"` | accepted; each new media type is logged once per route (context `ContentPolicy`) |
| `False` | not checked |
| `None` (default) | `"warn"` with `sessions=`, else `False` — 2.0 makes `True` the default |

Use `"warn"` to measure which clients would break before switching to `True`.

---

## What a route accepts: `consumes=`

Without a declaration, a route accepts what its body parameters understand:

| Body parameter | Accepted | Checked |
|---|---|---|
| `Json[T]`, an implicit model, `Discriminated[...]` | `application/json`, `application/*+json` | under `strict_content_type` |
| `Form[T]` | `application/x-www-form-urlencoded` | under `strict_content_type` (F04 adds typed multipart form decoding) |
| `UploadFile` | `multipart/form-data` | always |
| `Bytes`, `ByteStream`, `Stream[T]` | anything | only with `consumes=` |

`consumes=` (a media type or a list; ranges such as `text/*` and
`application/*+json` work) replaces the inferred list and is **always**
enforced. On a controller it is the default of every route that reads a body;
a route's own `consumes=` wins. A 415 lists what is supported in
`detail.supported` and in `Accept-Post` / `Accept-Patch` (RFC 5789).

A `Json[T]` parameter decodes the body with the codec of its `Content-Type`, so
`consumes=["application/merge-patch+json"]` just works (every `+json` type uses
the JSON codec) and `consumes=["application/msgpack"]` needs the MessagePack
codec. A media type no codec serves fails at startup
(`StartupError`, `reason="codec_not_registered"`) — raw `Bytes` routes can
name any type.

---

## Per-route body limits

`max_body_size=` on a route or a controller sets that route's limit — larger or
smaller than the app's `LaurenFactory.create(max_body_size=...)`:

<!-- docs-contract: fragment -->
```python
@controller("/media", max_body_size=10 * MiB)               # controller default
class MediaController:
    @post("/avatar", max_body_size=2 * MiB)                  # tighter than the controller
    async def avatar(self, file: UploadFile) -> dict: ...

    @post("/video", max_body_size=500 * MiB)                 # larger than the app default
    async def video(self, body: ByteStream) -> dict: ...
```

* Precedence: route → controller → app. Values must be positive `int`s
  (`DecoratorUsageError` at decoration otherwise; there is no `"10MB"` syntax —
  write `10 * MiB`).
* The limit applies right after routing. Global middleware that reads the body
  *before* routing is bounded by the app limit, which is why the app default
  should stay small; a body it already buffered is re-checked against the
  route's limit.
* A `Content-Length` above the limit is rejected before the first byte is read;
  without one, the running byte count decides.
* Every oversize body — `Json[T]`, `Bytes`, `ByteStream`, `Form`, `UploadFile` —
  is a 413 `request_body_too_large` with `detail.max_body_size`.

---

## Malformed bodies

A body that cannot be decoded (`{"name": ` — a syntax error, not a schema
error) is a 422 `extractor_error` with `detail.reason == "malformed_body"` and
a `json_invalid` issue. RFC 9110 reserves 422 for well-formed content, so
`ContentConfig(malformed_body_status=400)` answers 400 `bad_request` instead,
with `detail.line` / `detail.col` when the parser reports them. Schema errors
stay 422. 2.0 makes 400 the default.

---

## What a route answers with: `produces=`

<!-- docs-contract: fragment -->
```python
from lauren import ContentConfig, LaurenFactory, MsgpackCodec, get

@get("/users/{id}", produces=["application/json", "application/msgpack"])
async def show(self, id: int) -> UserOut: ...

app = LaurenFactory.create(AppModule, content=ContentConfig(codecs=[MsgpackCodec()]))
```

* Effective list: route → controller → `ContentConfig.default_produces`
  (`("application/json",)`, today's behaviour). The first entry is the default.
* `Accept` is parsed per RFC 9110: an entry's quality is the `q` of the most
  specific matching range, `q=0` excludes it, ties go to the `produces` order and
  a missing `Accept` selects the first entry.
* Only values the framework serializes are negotiated — dicts, lists, models,
  dataclasses, Structs, typed results, `(body, status)` tuples, pages. A returned
  `Response`, `str` or `bytes` goes out as it is.
* A route with several entries adds `Vary: Accept`; `@cache_response` stores
  one entry per negotiated representation.
* JSON representations are written by the route's encoder, so `@use_encoder`
  and `json_encoder=` keep working; `+json` types are labelled as negotiated.
* When nothing in `Accept` is available the first entry is served;
  `ContentConfig(strict_accept=True)` answers 406 `not_acceptable` with
  `detail.available` instead — before the handler runs when the route declares
  `produces=` or a structured return type.
* Error envelopes are always JSON.

`StreamingResponse[T]` keeps its own SSE / NDJSON / JSON Lines negotiation; it
uses the same parser, so `Accept: text/event-stream;q=0, */*` no longer selects
SSE.

---

## Codecs

A codec has `media_types` (concrete types; the first is its canonical response
type), `decode(bytes) -> Any` and `encode(value) -> bytes`:

<!-- docs-contract: fragment -->
```python
import csv, io

class CsvCodec:
    media_types = ("text/csv",)

    def decode(self, data: bytes) -> list[dict]:
        return list(csv.DictReader(io.StringIO(data.decode("utf-8"))))

    def encode(self, value) -> bytes:
        rows = value if isinstance(value, list) else [value]
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        writer.writerows(rows)
        return out.getvalue().encode("utf-8")

app = LaurenFactory.create(AppModule, content=ContentConfig(codecs=[CsvCodec()]))
```

Raise `ValueError` (or a subclass) from `decode` for a malformed body;
other exceptions propagate as 500, rather than masking a codec bug as a
client error. Two codecs claiming one media type fail at startup
(`reason="codec_conflict"`).

**`JsonCodec`** is always registered. `backend="auto"` uses msgspec when it is
installed, else the standard library; a body the fast parser rejects is
re-parsed by `json`, which decides — so what is accepted (`NaN`, huge
integers, UTF-16) and every error message are the same whatever is installed.
With a fast backend, pydantic bodies validate in one `model_validate_json` pass
and `msgspec.Struct` bodies in one `msgspec.json.decode`; a failure is reported
by the generic path, so 422 bodies are identical. `JsonCodec(backend="stdlib")`
turns the fast paths off; `backend="orjson"` is opt-in because orjson decodes
integers beyond 64 bits as floats.

**`MsgpackCodec`** (needs `msgspec`) serves `application/msgpack`,
`application/x-msgpack` and `application/vnd.msgpack`. Struct bodies decode with
`msgspec.msgpack.decode(type=T)`; other targets decode, then validate like JSON.

---

## Testing

Send a codec's bytes with `content=` and decode the response the same way;
`lauren.testing.TestClient` and `AsyncTestClient` also accept `msgpack=`
(requires msgspec), and `TestResponse.msgpack()` decodes the response:

<!-- docs-contract: fragment -->
```python
codec = MsgpackCodec()
response = client.post(
    "/events",
    msgpack={"kind": "click"},
    headers={"accept": "application/msgpack"},
)
assert response.msgpack() == {"accepted": True}
```

---

## OpenAPI and reflection

Declared `consumes` become the request body's content types and `produces` the
success responses' content types in the generated document.
`get_all_routes(app)` reports the effective `consumes`, `produces` and
`max_body_size` of every route (`reflect_routes(cls)` the declared ones).

## Migrating to the 2.0 defaults

2.0 turns on `strict_content_type=True` and `malformed_body_status=400`. Set
`strict_content_type="warn"` today, watch the `ContentPolicy` warnings, fix the
clients (or declare `consumes=` for the types you mean to accept), then switch
to `True`.

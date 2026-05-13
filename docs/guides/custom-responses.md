# Custom Responses

Lauren's `Response` class is designed to be subclassed.  Any `Response` subclass
returned from a handler is passed through the dispatch pipeline unchanged — no
JSON coercion, no content negotiation, no extra wrapping.

## Why subclass?

Subclassing `Response` lets you:

- **Encapsulate encoding and headers** in one reusable class — every handler that
  produces the same format just returns `MyResponse(data)`.
- **Add domain-specific factory methods** (e.g. `JsonApiResponse.resource(...)`,
  `HalResponse.from_model(...)`) without polluting handler code.
- **Add computed properties** (e.g. `size`, `etag`) and carry extra metadata that
  interceptors or middleware can read after the handler returns.

## Minimal example

```python title="app/responses.py"
from lauren import Response


class JsonApiResponse(Response):
    """JSON:API-compliant response (application/vnd.api+json)."""

    @classmethod
    def resource(
        cls,
        data: dict,
        *,
        status: int = 200,
        meta: dict | None = None,
    ) -> "JsonApiResponse":
        import json

        payload: dict = {"data": data}
        if meta:
            payload["meta"] = meta
        body = json.dumps(payload, separators=(",", ":")).encode()
        return cls(body, status=status, media_type="application/vnd.api+json")

    @classmethod
    def error(cls, title: str, status: int = 400) -> "JsonApiResponse":
        import json

        body = json.dumps({"errors": [{"title": title}]}, separators=(",", ":")).encode()
        return cls(body, status=status, media_type="application/vnd.api+json")
```

```python title="app/users.py"
from lauren import controller, get, post
from .responses import JsonApiResponse


@controller("/users")
class UserController:
    @get("/{id}")
    async def get_user(self, id: int) -> JsonApiResponse:
        user = {"type": "user", "id": id, "attributes": {"name": "Alice"}}
        return JsonApiResponse.resource(user)

    @post("/")
    async def create_user(self) -> JsonApiResponse:
        # …
        return JsonApiResponse.resource(
            {"type": "user", "id": 99},
            status=201,
        )
```

## Builder methods preserve the subclass type

All `with_*` builder methods return an instance of the **same subclass**, never a
plain `Response`.  This means you can safely chain builders on a custom response:

```python
resp = JsonApiResponse.resource(data)
resp = resp.with_header("x-request-id", request_id)  # still JsonApiResponse
resp = resp.with_cookie("session", token)             # still JsonApiResponse
```

## Adding new attributes

Because `Response` has no `__slots__`, subclasses can freely add instance
attributes:

```python
class TracedResponse(Response):
    def __init__(self, *args, trace_id: str = "", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.trace_id = trace_id   # plain instance attribute — no __slots__ needed
```

Read the attribute in an interceptor or middleware:

```python
from lauren import injectable
from lauren.types import ExecutionContext, CallHandler


@injectable()
class TraceInterceptor:
    async def intercept(
        self,
        ctx: ExecutionContext,
        next_handler: CallHandler,
    ):
        result = await next_handler.handle()
        if isinstance(result, TracedResponse):
            print(f"trace_id={result.trace_id}")
        return result
```

## Carrying a streaming body

Custom responses can also stream content by passing an async iterable to `stream=`:

```python
import asyncio
from typing import AsyncIterator
from lauren import Response


class CsvResponse(Response):
    @classmethod
    def from_rows(cls, rows: list[list[str]]) -> "CsvResponse":
        async def _gen() -> AsyncIterator[bytes]:
            for row in rows:
                yield (",".join(row) + "\n").encode()
            await asyncio.sleep(0)  # yield control once

        return cls(
            stream=_gen(),
            media_type="text/csv",
            headers=Headers([("content-disposition", 'attachment; filename="export.csv"')]),
        )
```

## What the dispatch pipeline does with a custom response

```
handler() → CustomResponse instance
    │
    ▼
_coerce_to_response()
    │  isinstance(value, Response) → True
    │  return value unchanged
    ▼
_send_response()
    reads: .status, .headers.raw(), .body / .stream_body
    (all inherited from Response — no customisation needed)
```

The pipeline never inspects the subclass name or its extra attributes.  It only
reads the five inherited properties (`status`, `headers`, `body`, `stream_body`,
`media_type`) that `_send_response` needs to write the ASGI frames.

## Interceptors and the custom response type

Interceptors receive the return value of the handler *before* the dispatch pipeline
sends it.  Use `isinstance` to branch on your custom type:

```python
@injectable()
class AuditInterceptor:
    async def intercept(self, ctx: ExecutionContext, next_handler: CallHandler):
        result = await next_handler.handle()
        if isinstance(result, JsonApiResponse):
            # log JSON:API-specific info
            ...
        return result
```

## Choosing between subclassing and a factory classmethod on Response

| Pattern | When to use |
|---|---|
| `Response.xml(data)` / `Response.file(path)` | One-off, stateless format conversion |
| Custom `Response` subclass | Reusable type with domain methods, extra properties, or middleware/interceptor hooks |

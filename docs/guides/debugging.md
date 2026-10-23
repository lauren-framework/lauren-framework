# Debug Error Pages

> In development, an unhandled exception should tell you *what* broke and
> *where* without a trip to the server console. `LaurenFactory.create(...,
> debug=True)` turns the opaque `500 internal_error` into a read-only report:
> exception chain, traceback with source, the matched route and its middleware
> → guard → interceptor chain, the extraction plan and a redacted request
> summary.

## Turning it on

<!-- docs-contract: run -->
```python
from lauren import LaurenFactory, controller, get, module
from lauren.testing import TestClient


@controller("/orders")
class OrderController:
    @get("/{order_id}")
    async def show(self, order_id: int) -> dict:
        totals = {}
        return {"total": totals[order_id]}  # KeyError — a bug


@module(controllers=[OrderController])
class AppModule:
    pass


app = LaurenFactory.create(AppModule, debug=True)  # development only

client = TestClient(app)  # TestClient connects from 127.0.0.1 (loopback)
page = client.get("/orders/7", headers={"accept": "text/html"})
assert page.status_code == 500
assert page.header("content-type") == "text/html; charset=utf-8"
assert "KeyError" in page.text and "OrderController.show" in page.text

report = client.get("/orders/7", headers={"accept": "application/json"}).json()
assert report["error"]["code"] == "internal_error"           # the normal envelope…
assert report["debug"]["route"]["path_template"] == "/orders/{order_id}"  # …plus "debug"
```

`Lauren(debug=True)` forwards the same setting when the app compiles.

What the client gets depends on its `Accept` header:

* **`text/html` preferred** (a browser) — a single self-contained HTML page:
  inline CSS, no JavaScript, every dynamic string HTML-escaped.
* **anything else** (`*/*`, `application/json`, …) — the normal error envelope
  (default or RFC 9457, per `error_format`) plus a top-level `debug` member with
  the same report as JSON: `exception`, `chain`, `frames`, `route`, `request`
  and `environment` (Lauren and Python versions, `error_format`).

Both carry `Cache-Control: no-store`. The status stays 500, and the error is
still logged with its traceback exactly as without debug.

## `DebugConfig`

`debug=True` is shorthand for `debug=DebugConfig()`:

```python
from lauren import DebugConfig, LaurenFactory

app = LaurenFactory.create(
    AppModule,
    debug=DebugConfig(
        include_4xx=False,          # also attach debug.raised_at to HTTPError responses
        allow_remote=False,         # loopback clients only (127.0.0.0/8, ::1)
        redact_headers=("authorization", "cookie", "set-cookie",
                        "proxy-authorization", "x-api-key"),
        redact_query_keys=("token", "password", "secret", "api_key"),
        source_context=5,           # source lines around each frame
        max_frames=40,              # innermost frames kept
        include_locals=False,       # off: locals routinely hold secrets
    ),
)
```

| Field | Default | Effect |
|---|---|---|
| `include_4xx` | `False` | Rendered `HTTPError` responses (any status) gain `debug.raised_at` — where the error was raised — and `debug.traceback`, the "why did I get 422?" case. |
| `allow_remote` | `False` | Serve debug output to every client instead of loopback only. |
| `redact_headers` | see above | Header values replaced by `[redacted]`; a header matches when its name **contains** an entry (case-insensitive). |
| `redact_query_keys` | see above | Query values replaced by `[redacted]` (substring match, so `access_token` is covered); also used to redact frame locals. |
| `source_context` | `5` | Lines of source shown before and after each frame's line. |
| `max_frames` | `40` | Innermost traceback frames kept. |
| `include_locals` | `False` | Show each frame's local variables (`repr`, truncated). |

An invalid value (`max_frames=0`, a non-bool flag, a string where a tuple is
expected, `debug="yes"`) raises `StartupError` with
`detail["reason"] == "invalid_debug_config"` at `create()`.

## Safety rules

Debug pages expose source code and request data, so they are hard to enable by
accident:

1. **Never inferred.** There is no environment-variable switch; debug is on only
   when you pass `debug=`.
2. **Loud.** `create()` logs a `WARN` — "Debug error pages enabled — never use
   in production" — every time.
3. **Refused in production.** When the typed configuration module
   (`lauren.config`) is installed and its active `Profile` is a production
   profile, `debug=` raises `StartupError(detail={"reason":
   "debug_in_production", "profile": ...})` at `create()`.
4. **Loopback only by default.** Other clients get the plain 500. This is
   **not** a security boundary behind a reverse proxy on the same host (every
   request then looks like loopback) — rules 2 and 3 are the real protection.
5. **Fails safe.** If rendering the report itself fails, the plain 500 envelope
   is sent and the secondary error is logged (`origin="debug_renderer"`) —
   never a 500 inside a 500.

Debug pages are read-only: there is no interactive console, and nothing in the
page executes code. When debug is off, the renderer module is not even
imported, so production apps pay nothing for the feature.

## What is in the report

| Section | Contents |
|---|---|
| Exception | Qualified type and message, plus the `__cause__` / `__context__` chain (with the location each was raised at). |
| Traceback | Innermost `max_frames` frames, each with file, line, function and `source_context` lines of source; framework frames are dimmed. |
| Route | Method and template, handler, controller, owning module, the effective middleware → guard → interceptor chain, exception handlers, and every parameter with its source (`path`, `query`, `json`, …). "Did not match a route" when the error happened before routing. |
| Request | Method, path, query (redacted), headers (redacted), client address. |
| Environment | Lauren and Python versions, `error_format`. |

## See also

* [Error Catalog](../reference/errors.md) — the envelope, RFC 9457 problem
  details and the deprecation notice for `detail.type`.
* [Custom Exception Handlers](custom-exception-handlers.md) — claimed errors
  never reach the debug page; it only replaces the unhandled-error 500.
* [Logging in Production](logging.md) — what to rely on once debug is off.

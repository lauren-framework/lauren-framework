# Reference

Quick-lookup material — error codes, cheat sheets, and one-page recipes.

<div class="grid cards" markdown>

-   :material-alert-octagon: [__Error Catalog__](errors.md)

    ---
    All 28 error classes, their HTTP status codes (where applicable), and what each one means.

-   :material-clipboard-list: [__Cheat Sheet__](cheat-sheet.md)

    ---
    Single-page reference of every common pattern — declaring an injectable, defining a route, raising an HTTP error, and so on.

</div>

## API surface — public exports

Every public name lives at the top of the `lauren` package and is re-exported from `lauren/__init__.py`:

```python
from lauren import (
    # Application
    Lauren, LaurenApp, LaurenFactory,
    # Decorators
    controller, module, injectable, middleware, exception_handler,
    get, post, put, patch, delete, head, options,
    use_guards, use_middlewares, use_exception_handlers, set_metadata,
    post_construct, pre_destruct,
    # DI
    Scope, DIContainer, Inject, OptionalDep, Token,
    use_value, use_class, use_factory, use_existing,
    # Extractors
    Path, Query, Header, Cookie, Json, Form, Bytes, State, Depends,
    QueryField, HeaderField, CookieField, PathField,
    # Types
    Request, Response, Headers, ExecutionContext,
    # Routing
    Router, RouteEntry,
    # Streaming / SSE / WS
    EventStream, ServerSentEvent,
    # Errors (selection)
    LaurenError, HTTPError, UnauthorizedError, ForbiddenError,
    ExtractorError, RouteNotFoundError, MethodNotAllowedError,
    # ... and the rest of the catalog
)
```

For programmatic doc consumption (`llms.txt` / `llms-full.txt`):

```python
from lauren import docs
print(docs.llms_txt())          # short overview (~2 KB)
print(docs.llms_full_txt())     # complete reference (~25 KB)
```

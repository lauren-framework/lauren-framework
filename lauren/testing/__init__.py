"""In-process testing toolkit for Lauren apps.

* :class:`TestClient` — synchronous ASGI client with one persistent event loop
  per client, lifespan as a context manager, a browser-like cookie jar,
  ``data=`` / ``files=`` / streamed bodies, ``client.stream(...)``,
  WebSockets, simulated client disconnects and provider-free DI access.
* :class:`AsyncTestClient` — the same surface on the caller's event loop.
* :class:`Test` — ``Test.create_testing_module(AppModule).override_provider(X)
  .use_value(fake).compile()``: startup-validated provider, guard,
  interceptor, middleware, exception-handler and module overrides.
* :func:`lifespan` — startup / shutdown around a third-party client.
* :class:`WsTestClient` — async WebSocket sessions on the caller's loop.
* ``lauren.testing.pytest_plugin`` — opt-in fixtures
  (``pytest_plugins = ["lauren.testing.pytest_plugin"]``).
"""

from __future__ import annotations

from ._client import AsyncTestClient, TestClient, WebSocketSession, lifespan
from ._cookies import CookieJar, StoredCookie
from ._encoding import _flatten_params  # noqa: F401 — 1.x import path kept for compatibility
from ._module import Test, TestingModuleBuilder
from ._response import HTTPStatusAssertionError, ResponseHeaders, StreamingTestResponse, TestResponse
from ._ws import WebSocketTestSession, WsTestClient

__all__ = [
    "AsyncTestClient",
    "CookieJar",
    "HTTPStatusAssertionError",
    "ResponseHeaders",
    "StoredCookie",
    "StreamingTestResponse",
    "Test",
    "TestClient",
    "TestResponse",
    "TestingModuleBuilder",
    "WebSocketSession",
    "WebSocketTestSession",
    "WsTestClient",
    "lifespan",
]

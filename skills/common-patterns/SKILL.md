---
name: common-patterns
description: Provides copy-paste complete Lauren framework patterns for the most common production scenarios. Covers authenticated CRUD endpoints, health check module, background job with lifecycle hooks, and typed SSE streaming. Use when scaffolding a new feature or when a complete working example is needed rather than API reference.
---

# Common Lauren Patterns

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep.

Complete, copy-pasteable patterns. Each is a working snippet you can drop into a project with minimal changes.

## Patterns

- **Authenticated CRUD**: controller with JWT guard, Pydantic models, DI service → [auth-protected-crud.md](auth-protected-crud.md)
- **Health check module**: minimal `GET /health` + `GET /ready` endpoint → [health-check.md](health-check.md)
- **Background job**: fire-and-forget task with `@post_construct`/`@pre_destruct` + shutdown signal → [background-job.md](background-job.md)
- **Typed SSE stream**: `StreamingResponse[T]` with Pydantic model, content-negotiation → [typed-sse-stream.md](typed-sse-stream.md)
- **Module composition**: feature modules with shared database, cross-cutting concerns in root → see below
- **CORS middleware**: global middleware with OPTIONS preflight handling → see below
- **Timing interceptor**: global interceptor adding `X-Response-Time` header → see below

## Quick reference

### Minimal module + controller

```python
from lauren import module, controller, get, injectable, Scope

@injectable(scope=Scope.SINGLETON)
class GreetService:
    def greet(self, name: str) -> str:
        return f"Hello, {name}!"

@controller("/greet")
class GreetController:
    def __init__(self, svc: GreetService) -> None:
        self._svc = svc

    @get("/{name}")
    async def greet(self, name: str) -> dict:
        return {"message": self._svc.greet(name)}

@module(controllers=[GreetController], providers=[GreetService])
class GreetModule: ...
```

### Request-scoped provider with cleanup

```python
from lauren import injectable, Scope, post_construct, pre_destruct

@injectable(scope=Scope.REQUEST)
class DbSession:
    @post_construct
    async def open(self) -> None:
        self._conn = await pool.acquire()

    @pre_destruct
    async def close(self) -> None:
        await pool.release(self._conn)
```

### Custom provider token

```python
from lauren import Token, use_value, module

Settings = Token[dict]("Settings")

@module(providers=[use_value(provide=Settings, value={"env": "prod"})])
class AppModule: ...

# Inject anywhere:
class MyService:
    def __init__(self, settings: Settings) -> None: ...
```

## Real-world patterns from examples

### Feature module composition (from lauren-eats)

Split a large app into independently testable feature modules with a shared database module:

```python
from lauren import module

@module(providers=[DatabaseService], exports=[DatabaseService])
class DatabaseModule:
    """Owns the DatabaseService singleton. Other modules import it for DB access."""

@module(imports=[DatabaseModule], controllers=[MenuController], providers=[MenuService], exports=[MenuService])
class MenuModule:
    """Menu browsing — imports DatabaseModule, exports MenuService for cross-module use."""

@module(imports=[DatabaseModule, MenuModule], controllers=[ChatController], providers=[ChatService])
class ChatModule:
    """Chat feature — depends on both database and menu services."""

@module(imports=[DatabaseModule, MenuModule, OrderModule, ChatModule, HealthModule])
class AppModule:
    """Root module — imports all feature modules. Cross-cutting concerns
    (CORS, timing interceptor, logger) are configured in main.py via
    LaurenFactory.create() kwargs, not here."""
```

### CORS middleware (from lauren-eats)

Global middleware handling OPTIONS preflight and injecting CORS headers:

```python
from lauren import middleware
from lauren.types import CallNext, Request, Response

ALLOWED_ORIGINS = frozenset({"http://localhost:3000", "http://127.0.0.1:3000"})

@middleware()
class CorsMiddleware:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        origin = request.headers.get("origin", "")
        if request.method == "OPTIONS":
            return Response(b"", status=204).with_headers(_cors_headers(origin))
        response = await call_next(request)
        return response.with_headers(_cors_headers(origin))

def _cors_headers(origin: str) -> dict[str, str]:
    allowed = origin if origin in ALLOWED_ORIGINS else "*"
    return {
        "access-control-allow-origin": allowed,
        "access-control-allow-methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
        "access-control-allow-headers": "Content-Type, Authorization",
        "access-control-max-age": "86400",
    }

# Register globally:
app = LaurenFactory.create(AppModule, global_middlewares=[CorsMiddleware])
```

### Timing interceptor (from lauren-eats and lauren-ai-chatbot)

Global interceptor adding response time header:

```python
import time
from lauren import interceptor
from lauren.types import CallHandler, ExecutionContext, Response

@interceptor()
class TimingInterceptor:
    async def intercept(self, ctx: ExecutionContext, call_handler: CallHandler) -> Response:
        start = time.perf_counter()
        result = await call_handler.handle()  # always Response
        elapsed_ms = int((time.perf_counter() - start) * 1_000)
        return result.with_header("x-response-time", f"{elapsed_ms}ms")

# Register globally:
app = LaurenFactory.create(AppModule, global_interceptors=[TimingInterceptor])
```

### Exception handler with DI (from lauren-eats)

Controller-scoped error mapping with typed exceptions:

```python
from lauren import controller, post, exception_handler, use_exception_handlers, EventStream
from lauren.types import Request, Response

class ChatMessageError(ValueError):
    """Raised when a chat request is malformed (mapped to 400)."""

@exception_handler(ChatMessageError)
class ChatMessageErrorHandler:
    async def catch(self, exc: ChatMessageError, request: Request) -> Response:
        return Response.json({"success": False, "error": str(exc)}, status=400)

@controller("/api/chat", tags=["chat"])
@use_exception_handlers(ChatMessageErrorHandler)
class ChatController:
    def __init__(self, chat_service: ChatService) -> None:
        self._svc = chat_service

    @post("/")
    async def chat(self, body: Json[SendChatMessageRequest]) -> EventStream:
        if not body.message or not body.message.strip():
            raise ChatMessageError("`message` must be a non-empty string")
        # ... stream response
```

### WebSocket gateway with auth token (from lauren-ai-chatbot)

WebSocket gateway using query-string token authentication:

```python
from lauren import Query
from lauren.websockets import WebSocket, WebSocketDisconnect, on_connect, on_disconnect, ws_controller

@ws_controller("/ws/banking")
class BankingWsGateway:
    def __init__(self, forwarder: EventForwarder, token_service: WsTokenService) -> None:
        self._forwarder = forwarder
        self._token_service = token_service
        self._user_id: str | None = None

    @on_connect
    async def connect(self, ws: WebSocket, token: Query[str]) -> None:
        user_id = self._token_service.verify_token(token)
        if not user_id:
            await ws.close(code=4401, reason="invalid or expired token")
            raise WebSocketDisconnect("unauthorized", close_code=4401)
        self._user_id = user_id
        await self._forwarder.register(user_id, ws)

    @on_disconnect
    async def disconnect(self, ws: WebSocket) -> None:
        if self._user_id:
            await self._forwarder.unregister(self._user_id, ws)
```

### LaurenFactory.create() with all options (from examples)

Production-ready app creation combining multiple concerns:

```python
from lauren import LaurenFactory
from lauren.logging import default_logger
from lauren.serialization import MsgspecEncoder

app = LaurenFactory.create(
    AppModule,
    global_middlewares=[CorsMiddleware, LoggingMiddleware],
    global_interceptors=[TimingInterceptor],
    logger=default_logger(),
    json_encoder=MsgspecEncoder(),
    signals=signal_bus,
    docs_url="/docs",
    openapi_info={"title": "My API", "version": "1.0.0"},
)
```

---
name: config-hot-reload
description: Implements a DynamicConfigService that allows runtime configuration updates via an admin HTTP endpoint, with asyncio.Lock for thread safety. Use when you need to toggle feature settings without redeploying the application.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Dynamic Configuration Hot-Reload API

## Overview

`DynamicConfigService` stores a mutable config dict in memory. An admin
endpoint accepts `POST /admin/config` to update individual keys. An
`asyncio.Lock` serialises concurrent writes so updates are always atomic.
The service is a `SINGLETON` — all requests see the same config state.

## DynamicConfigService

```python
import asyncio
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class DynamicConfigService:
    def __init__(self) -> None:
        self._config: dict = {
            "maintenance_mode": False,
            "max_request_size": 10_485_760,   # 10 MB
            "allowed_origins": "*",
            "rate_limit_rps": 100,
        }
        self._lock = asyncio.Lock()

    async def update(self, key: str, value) -> None:
        async with self._lock:
            self._config[key] = value

    async def update_many(self, updates: dict) -> None:
        async with self._lock:
            self._config.update(updates)

    def get(self, key: str, default=None):
        return self._config.get(key, default)

    def all(self) -> dict:
        return dict(self._config)
```

## Pydantic request body

```python
from pydantic import BaseModel

class ConfigUpdateBody(BaseModel):
    key: str
    value: str | int | bool | float
```

## Controller

```python
from lauren import controller, get, post, module, Json

@controller("/admin/config")
class ConfigController:
    def __init__(self, cfg: DynamicConfigService) -> None:
        self._cfg = cfg

    @get("/")
    async def get_config(self) -> dict:
        return self._cfg.all()

    @post("/")
    async def update_config(self, body: Json[ConfigUpdateBody]) -> dict:
        await self._cfg.update(body.key, body.value)
        return {"updated": body.key, "value": body.value}

@module(
    controllers=[ConfigController],
    providers=[DynamicConfigService],
)
class ConfigModule:
    pass
```

## Reading config in middleware

```python
from lauren.types import Request, Response
from lauren import middleware

@middleware()
class MaintenanceModeMiddleware:
    def __init__(self, cfg: DynamicConfigService) -> None:
        self._cfg = cfg

    async def use(self, request: Request, next) -> Response:
        if self._cfg.get("maintenance_mode"):
            return Response.json({"error": "Service under maintenance"}, status=503)
        return await next(request)
```

## Persisting config changes

For durability across restarts, persist changes to a store on every write:

```python
import json, pathlib

class PersistentConfigService(DynamicConfigService):
    _PATH = pathlib.Path("/tmp/dynamic_config.json")

    def __init__(self) -> None:
        super().__init__()
        if self._PATH.exists():
            self._config.update(json.loads(self._PATH.read_text()))

    async def update(self, key: str, value) -> None:
        await super().update(key, value)
        self._PATH.write_text(json.dumps(self._config, indent=2))
```

## Audit logging

```python
from lauren.logging import Logger

@injectable(scope=Scope.SINGLETON)
class AuditedConfigService(DynamicConfigService):
    def __init__(self, log: Logger) -> None:
        super().__init__()
        self._log = log

    async def update(self, key: str, value) -> None:
        old = self._config.get(key)
        await super().update(key, value)
        self._log.log(f"Config changed: {key} {old!r} → {value!r}")
```

## Common mistakes

- Using `threading.Lock` instead of `asyncio.Lock` in async handlers — async
  handlers run in the event loop; `threading.Lock` in a coroutine will deadlock
  if another task tries to acquire the lock while it is held.
- Accessing `self._config` without the lock in a write path — even a simple
  dict assignment is not atomic under concurrent async tasks.
- Returning the internal dict directly from `all()` — return a copy
  (`dict(self._config)`) so callers cannot mutate it by reference.
- Storing arbitrary user-supplied keys without validation — accept only a
  known set of keys, or validate the key against a schema.

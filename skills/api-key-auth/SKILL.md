---
name: api-key-auth
description: Implements API key generation with per-key scope lists and a guard that enforces scope requirements in Lauren. Use when building machine-to-machine authentication where different callers need different permission scopes.
---

> Use `codemap find "use_guards"` to locate guard wiring before reading.

# API Key Generation & Scoped Permissions

## Overview

`ApiKeyService` generates SHA-256 derived keys and stores metadata (owner,
scopes) in an in-memory dict. `ApiKeyGuard` reads the `X-API-Key` header,
looks up the record, and checks that the required scope (from route metadata)
is present on the key.

## Core Pattern

```python
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from lauren import (
    ExecutionContext,
    Json,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
    set_metadata,
    use_guards,
)
from lauren.exceptions import ForbiddenError, UnauthorizedError
from pydantic import BaseModel

SCOPE_KEY = "required_scope"
API_KEY_HEADER = "x-api-key"


@dataclass
class ApiKeyRecord:
    key_hash: str
    owner: str
    scopes: list[str] = field(default_factory=list)


@injectable(scope=Scope.SINGLETON)
class ApiKeyService:
    """Creates and looks up hashed API keys."""

    def __init__(self) -> None:
        self._store: dict[str, ApiKeyRecord] = {}  # hash → record

    def create_key(self, owner: str, scopes: list[str]) -> str:
        raw = os.urandom(32).hex()
        key_hash = hashlib.sha256(raw.encode()).hexdigest()
        self._store[key_hash] = ApiKeyRecord(
            key_hash=key_hash, owner=owner, scopes=scopes
        )
        return raw  # return only once; hash is stored

    def lookup(self, raw_key: str) -> ApiKeyRecord | None:
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        return self._store.get(key_hash)

    def has_scope(self, record: ApiKeyRecord, scope: str) -> bool:
        return scope in record.scopes


@injectable(scope=Scope.SINGLETON)
class ApiKeyGuard:
    """Validates X-API-Key and enforces required_scope route metadata."""

    def __init__(self, svc: ApiKeyService) -> None:
        self._svc = svc

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        raw_key = ctx.request.headers.get(API_KEY_HEADER, "")
        if not raw_key:
            raise UnauthorizedError("Missing X-API-Key header")

        record = self._svc.lookup(raw_key)
        if record is None:
            raise UnauthorizedError("Invalid API key")

        required_scope = ctx.get_metadata(SCOPE_KEY, "")
        if required_scope and not self._svc.has_scope(record, required_scope):
            raise ForbiddenError(
                f"API key lacks scope '{required_scope}'",
                detail={"required": required_scope, "owner": record.owner},
            )

        ctx.request.state.api_key_owner = record.owner
        ctx.request.state.api_key_scopes = record.scopes
        return True


class CreateKeyBody(BaseModel):
    owner: str
    scopes: list[str]


@controller("/keys")
class KeyManagementController:
    def __init__(self, svc: ApiKeyService) -> None:
        self._svc = svc

    @post("/create")
    async def create(self, body: Json[CreateKeyBody]) -> dict:
        raw = self._svc.create_key(body.owner, body.scopes)
        return {"api_key": raw}


@use_guards(ApiKeyGuard)
@controller("/data")
class DataController:
    @get("/read")
    @set_metadata(SCOPE_KEY, "read")
    async def read_data(self, ctx: ExecutionContext) -> dict:
        return {"data": "some data", "owner": ctx.request.state.api_key_owner}

    @get("/write")
    @set_metadata(SCOPE_KEY, "write")
    async def write_data(self) -> dict:
        return {"status": "written"}


@module(
    controllers=[KeyManagementController, DataController],
    providers=[ApiKeyService, ApiKeyGuard],
)
class ApiKeyModule:
    pass
```

## Key Points

- Only the raw key is returned at creation time. The store holds only the SHA-256 hash.
- `scopes` is a simple list of strings. Design scope names as `resource:action` for clarity.
- For multi-worker deployments replace `self._store` with a Redis hash lookup.
- Rotate keys by creating a new key for the same owner and revoking the old hash.

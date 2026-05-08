---
name: jwt-tokens
description: Shows JWT access token generation and validation in Lauren. Use when building stateless authentication with Bearer tokens; covers JWTService, a guard that reads Authorization headers, and protected controllers.
---

> Use `codemap find "use_guards"` to locate guard wiring before reading.

# JWT Token Generation & Validation

## Overview

This skill provides stateless JWT authentication using the PyJWT library.
`JWTService` handles encoding/decoding. `JWTBearerGuard` validates tokens
on protected endpoints and stores the decoded `user_id` in request state.

## Dependencies

```
PyJWT
```

## Core Pattern

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from lauren import (
    ExecutionContext,
    Scope,
    controller,
    get,
    injectable,
    module,
    use_guards,
)
from lauren.exceptions import UnauthorizedError

SECRET = "test-secret-key"
ALGORITHM = "HS256"


@injectable(scope=Scope.SINGLETON)
class JWTService:
    """Creates and validates JWT access tokens."""

    def create_token(self, user_id: str, expire_minutes: int = 30) -> str:
        payload = {
            "sub": user_id,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=expire_minutes),
        }
        return pyjwt.encode(payload, SECRET, algorithm=ALGORITHM)

    def decode_token(self, token: str) -> dict:
        try:
            return pyjwt.decode(token, SECRET, algorithms=[ALGORITHM])
        except pyjwt.PyJWTError as exc:
            raise UnauthorizedError(str(exc)) from exc


@injectable(scope=Scope.SINGLETON)
class JWTBearerGuard:
    """Reads Authorization: Bearer <token>, validates it, stores user_id in state."""

    def __init__(self, jwt_svc: JWTService) -> None:
        self._svc = jwt_svc

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        auth = ctx.request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise UnauthorizedError("Missing Bearer token")
        token = auth[7:]
        payload = self._svc.decode_token(token)
        ctx.request.state.user_id = payload["sub"]
        return True


@use_guards(JWTBearerGuard)
@controller("/protected")
class ProtectedController:
    def __init__(self, jwt_svc: JWTService) -> None:
        self._svc = jwt_svc

    @get("/profile")
    async def profile(self, exec_ctx: ExecutionContext) -> dict:
        return {"user_id": exec_ctx.request.state.user_id}


@controller("/public")
class PublicController:
    def __init__(self, jwt_svc: JWTService) -> None:
        self._svc = jwt_svc

    @get("/token")
    async def issue_token(self) -> dict:
        token = self._svc.create_token("user-42")
        return {"access_token": token}


@module(
    controllers=[ProtectedController, PublicController],
    providers=[JWTService, JWTBearerGuard],
)
class JWTModule:
    pass
```

## Key Points

- `JWTBearerGuard` raises `UnauthorizedError` (HTTP 401) for missing or invalid tokens.
- When `can_activate` returns `False` (not raises), Lauren responds with 403.
- Raising the error gives the correct HTTP status and error payload automatically.
- `JWTService` is `SINGLETON` — shared across all requests, safe because it has no mutable state.
- Do **not** store the SECRET in source code; read it from an environment variable in production.

---
name: jwt-refresh-rotation
description: Implements JWT refresh token rotation with JTI-based blacklisting in Lauren. Use when you need short-lived access tokens (15 min) paired with longer-lived refresh tokens (7 days) that are rotated and invalidated on use.
---

> Use `codemap find "post_construct"` to locate lifecycle hooks before reading.

# JWT Refresh Token Rotation & Blacklisting

## Overview

Two token types are issued together:

| Token | TTL | Purpose |
|---|---|---|
| Access | 15 minutes | Authenticates API requests |
| Refresh | 7 days | Exchanges for a new token pair |

Each token carries a unique `jti` (JWT ID, `uuid4`). The blacklist is an
in-memory `set[str]` on the `TokenBlacklistService` singleton. On refresh,
the old refresh token's JTI is added to the blacklist; re-use returns 401.

## Dependencies

```
PyJWT
```

## Core Pattern

```python
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
from lauren import (
    ExecutionContext,
    Json,
    Scope,
    controller,
    injectable,
    module,
    post,
    use_guards,
)
from lauren.exceptions import UnauthorizedError
from pydantic import BaseModel

SECRET = "refresh-secret-key"
ALGORITHM = "HS256"


@injectable(scope=Scope.SINGLETON)
class TokenBlacklistService:
    """Tracks revoked JWT IDs to prevent refresh token reuse."""

    def __init__(self) -> None:
        self._blacklisted: set[str] = set()

    def revoke(self, jti: str) -> None:
        self._blacklisted.add(jti)

    def is_revoked(self, jti: str) -> bool:
        return jti in self._blacklisted


@injectable(scope=Scope.SINGLETON)
class TokenService:
    """Issues and validates access/refresh token pairs."""

    def __init__(self, blacklist: TokenBlacklistService) -> None:
        self._blacklist = blacklist

    def _encode(self, payload: dict) -> str:
        return pyjwt.encode(payload, SECRET, algorithm=ALGORITHM)

    def _decode(self, token: str) -> dict:
        try:
            return pyjwt.decode(token, SECRET, algorithms=[ALGORITHM])
        except pyjwt.PyJWTError as exc:
            raise UnauthorizedError(str(exc)) from exc

    def issue_pair(self, user_id: str) -> dict:
        now = datetime.now(timezone.utc)
        access = self._encode({
            "sub": user_id,
            "type": "access",
            "jti": str(uuid.uuid4()),
            "exp": now + timedelta(minutes=15),
        })
        refresh = self._encode({
            "sub": user_id,
            "type": "refresh",
            "jti": str(uuid.uuid4()),
            "exp": now + timedelta(days=7),
        })
        return {"access_token": access, "refresh_token": refresh}

    def rotate_refresh(self, refresh_token: str) -> dict:
        payload = self._decode(refresh_token)
        if payload.get("type") != "refresh":
            raise UnauthorizedError("Not a refresh token")
        jti = payload.get("jti", "")
        if self._blacklist.is_revoked(jti):
            raise UnauthorizedError("Refresh token already used")
        self._blacklist.revoke(jti)
        return self.issue_pair(payload["sub"])

    def validate_access(self, token: str) -> dict:
        payload = self._decode(token)
        if payload.get("type") != "access":
            raise UnauthorizedError("Not an access token")
        jti = payload.get("jti", "")
        if self._blacklist.is_revoked(jti):
            raise UnauthorizedError("Access token revoked")
        return payload


class RefreshRequest(BaseModel):
    refresh_token: str


class LoginRequest(BaseModel):
    user_id: str


@controller("/auth")
class AuthController:
    def __init__(self, tokens: TokenService) -> None:
        self._tokens = tokens

    @post("/login")
    async def login(self, body: Json[LoginRequest]) -> dict:
        return self._tokens.issue_pair(body.user_id)

    @post("/refresh")
    async def refresh(self, body: Json[RefreshRequest]) -> dict:
        return self._tokens.rotate_refresh(body.refresh_token)


@module(
    controllers=[AuthController],
    providers=[TokenBlacklistService, TokenService],
)
class RefreshModule:
    pass
```

## Key Points

- Blacklist is in-memory; restart clears it. For multi-worker production use Redis.
- Always check `type` claim to prevent access tokens being submitted to the refresh endpoint.
- Issue a *new* JTI on every rotation so each new refresh token can only be used once.
- Short access token TTL limits the window of exposure if a token is stolen.

---
name: oauth2-integration
description: Shows how to build an OAuth2 callback controller in Lauren. Use when integrating third-party OAuth2 providers (GitHub, Google, etc.) via authorization-code flow.
---

> Use `codemap find "controller"` to locate decorator definitions before reading.

# OAuth2 Provider Integration

## Overview

This skill implements the OAuth2 authorization-code flow using `httpx.AsyncClient`
for external provider calls. The service is a `SINGLETON` injectable that holds
provider credentials and performs the token exchange.

## Dependencies

```
httpx
```

## Core Pattern

```python
from __future__ import annotations

import httpx
from lauren import (
    Query,
    Scope,
    controller,
    get,
    injectable,
    module,
)
from lauren.types import Response


@injectable(scope=Scope.SINGLETON)
class OAuth2Service:
    """Handles OAuth2 token exchange with an external provider."""

    def __init__(self) -> None:
        self._client_id = "client-id"
        self._client_secret = "secret"
        self._token_url = "https://github.com/login/oauth/access_token"

    async def exchange_code(self, code: str) -> dict:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                self._token_url,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
            return r.json()


@controller("/auth")
class OAuth2Controller:
    def __init__(self, svc: OAuth2Service) -> None:
        self._svc = svc

    @get("/callback")
    async def callback(self, code: Query[str]) -> Response:
        token_data = await self._svc.exchange_code(code)
        access_token = token_data.get("access_token", "")
        return Response.redirect(
            f"/dashboard?token={access_token}", status=302
        )

    @get("/login")
    async def login(self) -> Response:
        authorize_url = (
            "https://github.com/login/oauth/authorize"
            f"?client_id={self._svc._client_id}"
            "&scope=read:user"
        )
        return Response.redirect(authorize_url, status=302)


@module(controllers=[OAuth2Controller], providers=[OAuth2Service])
class AuthModule:
    pass
```

## Testing

Mock `httpx.AsyncClient.post` with `unittest.mock.AsyncMock` to avoid
real network calls in tests.

```python
from unittest.mock import AsyncMock, patch, MagicMock

def test_callback_mocked():
    mock_response = MagicMock()
    mock_response.json.return_value = {"access_token": "gho_fake_token"}

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        client = build_app(AuthModule)
        r = client.get("/auth/callback?code=auth_code_abc", follow_redirects=False)
        assert r.status_code == 302
        assert "gho_fake_token" in r.headers["location"]
```

## Key Points

- `OAuth2Service` is `SINGLETON` — one `httpx.AsyncClient` context per request, not shared.
- Never store tokens in server-side state without encryption.
- For production use `lauren-guards` `session_cookie` to store the access token securely.
- Always validate the `state` parameter to prevent CSRF in real implementations.

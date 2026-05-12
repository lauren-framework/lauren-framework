"""Integration tests for the OAuth2 provider integration skill."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from lauren import LaurenFactory, Query, Scope, controller, get, injectable, module
from lauren.testing import TestClient


@injectable(scope=Scope.SINGLETON)
class OAuth2Service:
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
    async def callback(self, code: Query[str]) -> dict:
        """Return token data as JSON (easier to test than a redirect)."""
        token_data = await self._svc.exchange_code(code)
        return {"access_token": token_data.get("access_token", "")}

    @get("/login-url")
    async def login_url(self) -> dict:
        authorize_url = (
            f"https://github.com/login/oauth/authorize?client_id={self._svc._client_id}&scope=read:user"
        )
        return {"url": authorize_url}


@module(controllers=[OAuth2Controller], providers=[OAuth2Service])
class AuthModule:
    pass


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(AuthModule))


class TestOAuth2Integration:
    def test_login_url_contains_provider(self):
        client = build_app()
        r = client.get("/auth/login-url")
        assert r.status_code == 200
        assert "github.com/login/oauth/authorize" in r.json()["url"]

    def test_login_url_contains_client_id(self):
        client = build_app()
        r = client.get("/auth/login-url")
        assert "client_id=client-id" in r.json()["url"]

    def test_callback_exchanges_code_and_returns_token(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "gho_fake_token_abc"}

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            client = build_app()
            r = client.get("/auth/callback?code=auth_code_123")

        assert r.status_code == 200
        assert r.json()["access_token"] == "gho_fake_token_abc"

    def test_callback_missing_code_returns_422(self):
        client = build_app()
        r = client.get("/auth/callback")
        assert r.status_code == 422

    def test_callback_handles_provider_error(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"error": "bad_verification_code"}

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            client = build_app()
            r = client.get("/auth/callback?code=bad_code")

        assert r.status_code == 200
        assert r.json()["access_token"] == ""

    def test_exchange_code_called_with_correct_params(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "tok"}

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            client = build_app()
            client.get("/auth/callback?code=mycode")

        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert "mycode" in str(call_kwargs)

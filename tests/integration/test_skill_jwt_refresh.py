"""Integration tests for the JWT refresh token rotation skill."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt as pyjwt

from lauren import (
    Json,
    LaurenFactory,
    Scope,
    controller,
    injectable,
    module,
    post,
)
from lauren.exceptions import UnauthorizedError
from lauren.testing import TestClient
from pydantic import BaseModel

SECRET = "refresh-secret-key"
ALGORITHM = "HS256"


@injectable(scope=Scope.SINGLETON)
class TokenBlacklistService:
    def __init__(self) -> None:
        self._blacklisted: set[str] = set()

    def revoke(self, jti: str) -> None:
        self._blacklisted.add(jti)

    def is_revoked(self, jti: str) -> bool:
        return jti in self._blacklisted


@injectable(scope=Scope.SINGLETON)
class TokenService:
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
        access = self._encode(
            {
                "sub": user_id,
                "type": "access",
                "jti": str(uuid.uuid4()),
                "exp": now + timedelta(minutes=15),
            }
        )
        refresh = self._encode(
            {
                "sub": user_id,
                "type": "refresh",
                "jti": str(uuid.uuid4()),
                "exp": now + timedelta(days=7),
            }
        )
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


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(RefreshModule))


class TestJWTRefreshRotation:
    def test_login_issues_token_pair(self):
        client = build_app()
        r = client.post("/auth/login", json={"user_id": "alice"})
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert "refresh_token" in data

    def test_refresh_issues_new_token_pair(self):
        client = build_app()
        login_r = client.post("/auth/login", json={"user_id": "bob"})
        refresh_token = login_r.json()["refresh_token"]

        r = client.post("/auth/refresh", json={"refresh_token": refresh_token})
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert "refresh_token" in data

    def test_refresh_token_cannot_be_reused(self):
        client = build_app()
        login_r = client.post("/auth/login", json={"user_id": "carol"})
        refresh_token = login_r.json()["refresh_token"]

        # First use succeeds
        r1 = client.post("/auth/refresh", json={"refresh_token": refresh_token})
        assert r1.status_code == 200

        # Second use of the same token is rejected
        r2 = client.post("/auth/refresh", json={"refresh_token": refresh_token})
        assert r2.status_code == 401

    def test_access_token_rejected_as_refresh(self):
        client = build_app()
        login_r = client.post("/auth/login", json={"user_id": "dave"})
        access_token = login_r.json()["access_token"]

        r = client.post("/auth/refresh", json={"refresh_token": access_token})
        assert r.status_code == 401

    def test_garbage_token_rejected(self):
        client = build_app()
        r = client.post("/auth/refresh", json={"refresh_token": "not.a.valid.token"})
        assert r.status_code == 401

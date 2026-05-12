"""Integration tests for the JWT token generation and validation skill."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt as pyjwt

from lauren import (
    ExecutionContext,
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    module,
    use_guards,
)
from lauren.exceptions import UnauthorizedError
from lauren.testing import TestClient

SECRET = "test-secret-key"
ALGORITHM = "HS256"


@injectable(scope=Scope.SINGLETON)
class JWTService:
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
    @get("/profile")
    async def profile(self, exec_ctx: ExecutionContext) -> dict:
        return {"user_id": exec_ctx.request.state.user_id}


@module(
    controllers=[ProtectedController],
    providers=[JWTService, JWTBearerGuard],
)
class JWTModule:
    pass


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(JWTModule))


class TestJWTTokens:
    def test_valid_token_allows_access(self):
        svc = JWTService()
        token = svc.create_token("user-42")
        client = build_app()
        r = client.get("/protected/profile", headers={"authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json() == {"user_id": "user-42"}

    def test_missing_token_returns_401(self):
        client = build_app()
        r = client.get("/protected/profile")
        assert r.status_code == 401

    def test_invalid_token_returns_401(self):
        client = build_app()
        r = client.get(
            "/protected/profile",
            headers={"authorization": "Bearer this.is.garbage"},
        )
        assert r.status_code == 401

    def test_expired_token_returns_401(self):
        payload = {
            "sub": "old-user",
            "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
        }
        expired_token = pyjwt.encode(payload, SECRET, algorithm=ALGORITHM)
        client = build_app()
        r = client.get(
            "/protected/profile",
            headers={"authorization": f"Bearer {expired_token}"},
        )
        assert r.status_code == 401

    def test_wrong_secret_returns_401(self):
        payload = {
            "sub": "hacker",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
        }
        forged_token = pyjwt.encode(payload, "wrong-secret", algorithm=ALGORITHM)
        client = build_app()
        r = client.get(
            "/protected/profile",
            headers={"authorization": f"Bearer {forged_token}"},
        )
        assert r.status_code == 401

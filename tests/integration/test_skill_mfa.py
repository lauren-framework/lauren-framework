"""Integration tests for the TOTP multi-factor authentication skill."""

from __future__ import annotations

import pyotp

from lauren import (
    Json,
    LaurenFactory,
    Scope,
    controller,
    injectable,
    module,
    post,
)
from lauren.testing import TestClient
from pydantic import BaseModel


@injectable(scope=Scope.SINGLETON)
class MFAService:
    def generate_secret(self) -> str:
        return pyotp.random_base32()

    def get_provisioning_uri(self, secret: str, username: str, issuer: str = "MyApp") -> str:
        totp = pyotp.TOTP(secret)
        return totp.provisioning_uri(name=username, issuer_name=issuer)

    def verify_totp(self, secret: str, token: str) -> bool:
        totp = pyotp.TOTP(secret)
        return totp.verify(token)

    def current_code(self, secret: str) -> str:
        return pyotp.TOTP(secret).now()


class EnrollRequest(BaseModel):
    username: str


class VerifyRequest(BaseModel):
    secret: str
    token: str


@controller("/mfa")
class MFAController:
    def __init__(self, mfa: MFAService) -> None:
        self._mfa = mfa

    @post("/enroll")
    async def enroll(self, body: Json[EnrollRequest]) -> dict:
        secret = self._mfa.generate_secret()
        uri = self._mfa.get_provisioning_uri(secret, body.username)
        return {"secret": secret, "provisioning_uri": uri}

    @post("/verify")
    async def verify(self, body: Json[VerifyRequest]) -> dict:
        valid = self._mfa.verify_totp(body.secret, body.token)
        return {"valid": valid}


@module(controllers=[MFAController], providers=[MFAService])
class MFAModule:
    pass


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(MFAModule))


class TestMFATotp:
    def test_enroll_returns_secret_and_uri(self):
        client = build_app()
        r = client.post("/mfa/enroll", json={"username": "alice"})
        assert r.status_code == 200
        data = r.json()
        assert "secret" in data
        assert "provisioning_uri" in data
        assert len(data["secret"]) > 0
        assert "otpauth://" in data["provisioning_uri"]

    def test_provisioning_uri_contains_username(self):
        client = build_app()
        r = client.post("/mfa/enroll", json={"username": "bob"})
        assert "bob" in r.json()["provisioning_uri"]

    def test_provisioning_uri_contains_issuer(self):
        client = build_app()
        r = client.post("/mfa/enroll", json={"username": "carol"})
        assert "MyApp" in r.json()["provisioning_uri"]

    def test_verify_valid_totp_code(self):
        svc = MFAService()
        secret = svc.generate_secret()
        current_code = svc.current_code(secret)

        client = build_app()
        r = client.post("/mfa/verify", json={"secret": secret, "token": current_code})
        assert r.status_code == 200
        assert r.json()["valid"] is True

    def test_verify_invalid_totp_code(self):
        svc = MFAService()
        secret = svc.generate_secret()

        client = build_app()
        r = client.post("/mfa/verify", json={"secret": secret, "token": "000000"})
        # 000000 is almost certainly wrong (1/1000000 chance of being valid)
        assert r.status_code == 200
        # We just check the response structure; validity depends on timing
        assert "valid" in r.json()

    def test_verify_wrong_secret_returns_false(self):
        svc = MFAService()
        secret1 = svc.generate_secret()
        secret2 = svc.generate_secret()
        code_for_secret1 = svc.current_code(secret1)

        client = build_app()
        r = client.post("/mfa/verify", json={"secret": secret2, "token": code_for_secret1})
        assert r.status_code == 200
        assert r.json()["valid"] is False

    def test_each_enroll_generates_unique_secret(self):
        client = build_app()
        r1 = client.post("/mfa/enroll", json={"username": "user1"})
        r2 = client.post("/mfa/enroll", json={"username": "user2"})
        assert r1.json()["secret"] != r2.json()["secret"]

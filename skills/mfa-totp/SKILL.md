---
name: mfa-totp
description: Implements TOTP-based Multi-Factor Authentication using pyotp in Lauren. Use when adding a second factor (authenticator app) to an existing authentication flow.
---

> Use `codemap find "injectable"` to locate the DI decorator before reading.

# Multi-Factor Authentication (TOTP)

## Overview

`MFAService` wraps `pyotp` to generate Base32 secrets, produce provisioning
URIs for QR codes, and verify TOTP codes. The Lauren integration wires it as
a `SINGLETON` injectable consumed by HTTP endpoints.

## Dependencies

```
pyotp
```

## Core Pattern

```python
from __future__ import annotations

import pyotp
from lauren import Json, Scope, controller, injectable, module, post, get
from pydantic import BaseModel


@injectable(scope=Scope.SINGLETON)
class MFAService:
    """TOTP secret management and code verification."""

    def generate_secret(self) -> str:
        return pyotp.random_base32()

    def get_provisioning_uri(
        self, secret: str, username: str, issuer: str = "MyApp"
    ) -> str:
        totp = pyotp.TOTP(secret)
        return totp.provisioning_uri(name=username, issuer_name=issuer)

    def verify_totp(self, secret: str, token: str) -> bool:
        """Return True if token is valid within the default 30-second window."""
        totp = pyotp.TOTP(secret)
        return totp.verify(token)

    def current_code(self, secret: str) -> str:
        """Return the current valid code (useful for testing)."""
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
```

## Full Login Flow Integration

```
1. User logs in with password → server returns a "pending MFA" challenge.
2. Server stores `mfa_secret` in the user record (database).
3. User submits TOTP code → server calls `MFAService.verify_totp(secret, code)`.
4. On success, issue JWT / session.
```

## Key Points

- `pyotp.random_base32()` generates a cryptographically secure secret.
- `verify()` accepts codes from ±1 step (30 s window) to tolerate clock skew.
- Never expose the raw `secret` over an insecure channel; serve only the provisioning URI.
- Store secrets encrypted in the database; use `nacl.secret.SecretBox` or similar.
- `current_code(secret)` is provided for deterministic testing without mocking time.

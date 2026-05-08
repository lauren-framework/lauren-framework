---
name: input-sanitization
description: Provides SQL injection detection, XSS stripping, and CSRF token generation/validation as injectable services in a Lauren application. Use when accepting user-controlled input that is displayed in HTML or used in database queries, or when protecting state-mutating endpoints with CSRF tokens.
---

> Use `codemap find "InputSanitizer"` to locate any existing sanitization utilities before adding new ones.

# Input Sanitization (SQL Injection / XSS / CSRF)

## Design decisions

| Threat | Strategy | Why |
|---|---|---|
| **SQL injection** | *Detect and reject* — never escape | Escaping is fragile. Use parameterized queries; reject input that looks like SQL if you need belt-and-suspenders validation. |
| **XSS** | *Strip HTML tags + `html.escape`* | Removes active content; `html.escape` prevents injection into HTML attributes. |
| **CSRF** | *Double-submit cookie / HMAC token* | HMAC-signed token bound to a session ID; safe against forged cross-origin requests. |

## InputSanitizer

```python
from __future__ import annotations

import hashlib
import hmac
import html
import re
import secrets
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class InputSanitizer:
    """Stateless sanitization utilities."""

    _SQL_PATTERNS = [
        r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|EXEC|UNION)\b",
        r"(--|;|'|\"|`)",
        r"\bOR\b\s+\d+\s*=\s*\d+",
    ]
    _SQL_RE = re.compile("|".join(_SQL_PATTERNS), re.IGNORECASE)

    # ------------------------------------------------------------------
    # SQL injection detection
    # ------------------------------------------------------------------

    def detect_sql_injection(self, value: str) -> bool:
        """Return True if value contains SQL injection patterns.

        Use parameterized queries for actual DB calls; this is a last-line
        guard that should block obviously malicious input at the HTTP layer.
        """
        return bool(self._SQL_RE.search(value))

    # ------------------------------------------------------------------
    # XSS stripping
    # ------------------------------------------------------------------

    def strip_xss(self, value: str) -> str:
        """Remove <script> blocks and HTML tags; escape remaining special chars."""
        # Remove <script>...</script> blocks first (with content)
        cleaned = re.sub(
            r"<script[^>]*>.*?</script>", "", value,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Remove remaining HTML tags
        cleaned = re.sub(r"<[^>]+>", "", cleaned)
        # Escape &, <, >, ", '
        return html.escape(cleaned)

    # ------------------------------------------------------------------
    # CSRF
    # ------------------------------------------------------------------

    def generate_csrf_token(self, session_id: str, secret: str = "csrf-secret") -> str:
        """Generate a signed CSRF token bound to session_id.

        Token format: ``<random_hex>:<hmac_signature>``
        """
        raw = secrets.token_hex(16)
        sig = hmac.new(
            secret.encode(), f"{session_id}:{raw}".encode(), hashlib.sha256
        ).hexdigest()
        return f"{raw}:{sig}"

    def validate_csrf_token(
        self, token: str, session_id: str, secret: str = "csrf-secret"
    ) -> bool:
        """Return True if the token is valid for this session_id."""
        try:
            raw, sig = token.rsplit(":", 1)
            expected = hmac.new(
                secret.encode(),
                f"{session_id}:{raw}".encode(),
                hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(expected, sig)
        except (ValueError, AttributeError):
            return False
```

## Integrating with a handler

```python
from lauren.exceptions import ExtractorError, ForbiddenError
from lauren import Json
from pydantic import BaseModel

class CommentBody(BaseModel):
    text: str
    csrf_token: str

@controller("/comments")
class CommentController:
    def __init__(self, sanitizer: InputSanitizer) -> None:
        self._san = sanitizer

    @post("/")
    async def create(self, body: Json[CommentBody], request: Request) -> dict:
        session_id = request.cookies.get("session_id", "")
        if not self._san.validate_csrf_token(body.csrf_token, session_id):
            raise ForbiddenError("Invalid CSRF token")
        if self._san.detect_sql_injection(body.text):
            raise ExtractorError("Invalid input — potential SQL injection detected")
        clean = self._san.strip_xss(body.text)
        return {"saved": clean}
```

## Module wiring

```python
@module(controllers=[CommentController], providers=[InputSanitizer])
class AppModule:
    pass
```

## Testing

```python
def test_sql_injection_detected():
    san = InputSanitizer()
    assert san.detect_sql_injection("1' OR 1=1 --") is True
    assert san.detect_sql_injection("hello world") is False

def test_xss_stripped():
    san = InputSanitizer()
    result = san.strip_xss('<script>alert("xss")</script>hello')
    assert "<script>" not in result
    assert "hello" in result

def test_csrf_roundtrip():
    san = InputSanitizer()
    token = san.generate_csrf_token("session-abc")
    assert san.validate_csrf_token(token, "session-abc") is True
    assert san.validate_csrf_token(token, "different-session") is False
```

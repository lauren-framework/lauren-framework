"""Integration tests for input sanitization — SQL injection, XSS, CSRF (Skill 45)."""

from __future__ import annotations

import hashlib
import hmac
import html
import re
import secrets

from lauren import (
    Json,
    LaurenFactory,
    Scope,
    controller,
    injectable,
    module,
    post,
)
from lauren.exceptions import ExtractorError
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# InputSanitizer
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class InputSanitizer:
    _SQL_PATTERNS = [
        r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|EXEC|UNION)\b",
        r"(--|;|'|\"|`)",
        r"\bOR\b\s+\d+\s*=\s*\d+",
    ]
    _SQL_RE = re.compile("|".join(_SQL_PATTERNS), re.IGNORECASE)

    def detect_sql_injection(self, value: str) -> bool:
        return bool(self._SQL_RE.search(value))

    def strip_xss(self, value: str) -> str:
        cleaned = re.sub(
            r"<script[^>]*>.*?</script>", "", value, flags=re.DOTALL | re.IGNORECASE
        )
        cleaned = re.sub(r"<[^>]+>", "", cleaned)
        return html.escape(cleaned)

    def generate_csrf_token(self, session_id: str, secret: str = "csrf-secret") -> str:
        raw = secrets.token_hex(16)
        sig = hmac.new(
            secret.encode(), f"{session_id}:{raw}".encode(), hashlib.sha256
        ).hexdigest()
        return f"{raw}:{sig}"

    def validate_csrf_token(
        self, token: str, session_id: str, secret: str = "csrf-secret"
    ) -> bool:
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


# ---------------------------------------------------------------------------
# Controller that uses sanitizer
# ---------------------------------------------------------------------------

from pydantic import BaseModel


class TextBody(BaseModel):
    text: str


@controller("/sanitize")
class SanitizerController:
    def __init__(self, sanitizer: InputSanitizer) -> None:
        self._san = sanitizer

    @post("/check-sql")
    async def check_sql(self, body: Json[TextBody]) -> dict:
        if self._san.detect_sql_injection(body.text):
            raise ExtractorError("SQL injection detected")
        return {"safe": True, "text": body.text}

    @post("/strip-xss")
    async def strip_xss(self, body: Json[TextBody]) -> dict:
        clean = self._san.strip_xss(body.text)
        return {"clean": clean}


@module(controllers=[SanitizerController], providers=[InputSanitizer])
class SanitizerModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_client():
    return TestClient(LaurenFactory.create(SanitizerModule))


# ---------------------------------------------------------------------------
# Tests — SQL injection detection
# ---------------------------------------------------------------------------


class TestSqlInjectionDetection:
    def test_detects_or_equals_pattern(self):
        san = InputSanitizer()
        assert san.detect_sql_injection("1' OR 1=1 --") is True

    def test_detects_select_keyword(self):
        san = InputSanitizer()
        assert san.detect_sql_injection("SELECT * FROM users") is True

    def test_detects_drop_keyword(self):
        san = InputSanitizer()
        assert san.detect_sql_injection("DROP TABLE orders") is True

    def test_detects_union_keyword(self):
        san = InputSanitizer()
        assert san.detect_sql_injection("' UNION SELECT null--") is True

    def test_detects_single_quote(self):
        san = InputSanitizer()
        assert san.detect_sql_injection("O'Brien") is True

    def test_detects_double_dash(self):
        san = InputSanitizer()
        assert san.detect_sql_injection("admin'--") is True

    def test_safe_text_not_detected(self):
        san = InputSanitizer()
        assert san.detect_sql_injection("hello world") is False

    def test_normal_email_not_detected(self):
        san = InputSanitizer()
        assert san.detect_sql_injection("user@example.com") is False

    def test_api_blocks_sql_injection(self):
        client = build_client()
        r = client.post("/sanitize/check-sql", json={"text": "1' OR 1=1 --"})
        assert r.status_code == 422

    def test_api_allows_safe_text(self):
        client = build_client()
        r = client.post("/sanitize/check-sql", json={"text": "hello world"})
        assert r.status_code == 200
        assert r.json()["safe"] is True


# ---------------------------------------------------------------------------
# Tests — XSS stripping
# ---------------------------------------------------------------------------


class TestXssStripping:
    def test_strips_script_tag(self):
        san = InputSanitizer()
        result = san.strip_xss('<script>alert("xss")</script>hello')
        assert "script" not in result.lower()
        assert "hello" in result

    def test_strips_html_tags(self):
        san = InputSanitizer()
        result = san.strip_xss("<b>bold</b> text")
        assert "<b>" not in result
        assert "bold" in result
        assert "text" in result

    def test_escapes_ampersand(self):
        san = InputSanitizer()
        result = san.strip_xss("cats & dogs")
        assert "&amp;" in result

    def test_escapes_less_than(self):
        san = InputSanitizer()
        result = san.strip_xss("2 < 3")
        assert "&lt;" in result

    def test_plain_text_preserved(self):
        san = InputSanitizer()
        result = san.strip_xss("Hello, world!")
        assert "Hello, world!" in result

    def test_strips_inline_script_with_content(self):
        san = InputSanitizer()
        payload = "<script>document.cookie='stolen'</script>harmless"
        result = san.strip_xss(payload)
        assert "cookie" not in result
        assert "harmless" in result

    def test_api_xss_stripped(self):
        client = build_client()
        r = client.post("/sanitize/strip-xss", json={"text": "<b>hello</b>"})
        assert r.status_code == 200
        data = r.json()
        assert "<b>" not in data["clean"]
        assert "hello" in data["clean"]


# ---------------------------------------------------------------------------
# Tests — CSRF
# ---------------------------------------------------------------------------


class TestCsrf:
    def test_valid_token_validates(self):
        san = InputSanitizer()
        token = san.generate_csrf_token("session-123")
        assert san.validate_csrf_token(token, "session-123") is True

    def test_wrong_session_fails(self):
        san = InputSanitizer()
        token = san.generate_csrf_token("session-abc")
        assert san.validate_csrf_token(token, "session-xyz") is False

    def test_tampered_token_fails(self):
        san = InputSanitizer()
        token = san.generate_csrf_token("session-abc")
        tampered = token[:-4] + "xxxx"
        assert san.validate_csrf_token(tampered, "session-abc") is False

    def test_empty_token_fails(self):
        san = InputSanitizer()
        assert san.validate_csrf_token("", "session-abc") is False

    def test_malformed_token_fails(self):
        san = InputSanitizer()
        assert san.validate_csrf_token("notavalidtoken", "session-abc") is False

    def test_each_token_is_unique(self):
        san = InputSanitizer()
        t1 = san.generate_csrf_token("session-abc")
        t2 = san.generate_csrf_token("session-abc")
        assert t1 != t2

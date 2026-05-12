"""Integration tests for security headers and CORS middleware (Skill 47)."""

from __future__ import annotations

from lauren import LaurenFactory, Scope, controller, get, injectable, middleware, module
from lauren.testing import TestClient
from lauren.types import Request, Response


# ---------------------------------------------------------------------------
# Default headers
# ---------------------------------------------------------------------------

DEFAULT_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; object-src 'none'",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


# ---------------------------------------------------------------------------
# SecurityHeadersMiddleware
# ---------------------------------------------------------------------------


@middleware()
@injectable(scope=Scope.SINGLETON)
class SecurityHeadersMiddleware:
    def __init__(self, headers: dict | None = None) -> None:
        self._headers = headers or DEFAULT_SECURITY_HEADERS

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for key, value in self._headers.items():
            response = response.with_header(key, value)
        return response


# ---------------------------------------------------------------------------
# SimpleCorsMiddleware
# ---------------------------------------------------------------------------


@middleware()
@injectable(scope=Scope.SINGLETON)
class SimpleCorsMiddleware:
    def __init__(
        self,
        allow_origins: list[str] | None = None,
        allow_methods: list[str] | None = None,
        allow_headers: list[str] | None = None,
    ) -> None:
        self._allow_origins = set(allow_origins or ["*"])
        self._allow_methods = allow_methods or [
            "GET",
            "POST",
            "PUT",
            "DELETE",
            "OPTIONS",
        ]
        self._allow_headers = allow_headers or ["Content-Type", "Authorization"]

    async def dispatch(self, request: Request, call_next) -> Response:
        origin = request.headers.get("origin", "")
        if "*" in self._allow_origins or origin in self._allow_origins:
            response = await call_next(request)
            cors_origin = origin if origin else "*"
            response = response.with_header("Access-Control-Allow-Origin", cors_origin)
            response = response.with_header("Access-Control-Allow-Methods", ", ".join(self._allow_methods))
            response = response.with_header("Access-Control-Allow-Headers", ", ".join(self._allow_headers))
            return response
        return await call_next(request)


# ---------------------------------------------------------------------------
# Test controller
# ---------------------------------------------------------------------------


@controller("/api")
class ApiController:
    @get("/data")
    async def data(self) -> dict:
        return {"hello": "world"}


@module(
    controllers=[ApiController],
    providers=[SecurityHeadersMiddleware, SimpleCorsMiddleware],
)
class SecureAppModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_secure_client():
    app = LaurenFactory.create(
        SecureAppModule,
        global_middlewares=[SecurityHeadersMiddleware],
    )
    return TestClient(app)


def build_cors_client(allow_origins=None):
    # We need a fresh module with the CORS middleware configured.
    # Use a fresh injectable class to avoid decorator conflicts.
    @middleware()
    @injectable(scope=Scope.SINGLETON)
    class ConfiguredCors:
        def __init__(self) -> None:
            self._allow_origins = set(allow_origins or ["*"])
            self._allow_methods = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
            self._allow_headers = ["Content-Type", "Authorization"]

        async def dispatch(self, request: Request, call_next) -> Response:
            origin = request.headers.get("origin", "")
            if "*" in self._allow_origins or origin in self._allow_origins:
                response = await call_next(request)
                cors_origin = origin if origin else "*"
                response = response.with_header("Access-Control-Allow-Origin", cors_origin)
                response = response.with_header(
                    "Access-Control-Allow-Methods", ", ".join(self._allow_methods)
                )
                return response
            return await call_next(request)

    @module(controllers=[ApiController], providers=[ConfiguredCors])
    class CorsModule:
        pass

    app = LaurenFactory.create(CorsModule, global_middlewares=[ConfiguredCors])
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests — Security headers
# ---------------------------------------------------------------------------


class TestSecurityHeaders:
    def test_x_frame_options_deny(self):
        client = build_secure_client()
        r = client.get("/api/data")
        assert r.header("x-frame-options") == "DENY"

    def test_x_content_type_options_nosniff(self):
        client = build_secure_client()
        r = client.get("/api/data")
        assert r.header("x-content-type-options") == "nosniff"

    def test_x_xss_protection(self):
        client = build_secure_client()
        r = client.get("/api/data")
        assert r.header("x-xss-protection") == "1; mode=block"

    def test_strict_transport_security(self):
        client = build_secure_client()
        r = client.get("/api/data")
        hsts = r.header("strict-transport-security") or ""
        assert "max-age=31536000" in hsts

    def test_content_security_policy(self):
        client = build_secure_client()
        r = client.get("/api/data")
        csp = r.header("content-security-policy") or ""
        assert "default-src" in csp

    def test_referrer_policy(self):
        client = build_secure_client()
        r = client.get("/api/data")
        assert r.header("referrer-policy") is not None

    def test_permissions_policy(self):
        client = build_secure_client()
        r = client.get("/api/data")
        assert r.header("permissions-policy") is not None

    def test_response_body_not_affected(self):
        client = build_secure_client()
        r = client.get("/api/data")
        assert r.json() == {"hello": "world"}

    def test_custom_headers_override(self):
        @middleware()
        @injectable(scope=Scope.SINGLETON)
        class CustomHeadersMw:
            def __init__(self) -> None:
                self._headers = {"X-Custom": "custom-value"}

            async def dispatch(self, request: Request, call_next) -> Response:
                response = await call_next(request)
                for k, v in self._headers.items():
                    response = response.with_header(k, v)
                return response

        @module(controllers=[ApiController], providers=[CustomHeadersMw])
        class CustomModule:
            pass

        app = LaurenFactory.create(CustomModule, global_middlewares=[CustomHeadersMw])
        client = TestClient(app)
        r = client.get("/api/data")
        assert r.header("x-custom") == "custom-value"


# ---------------------------------------------------------------------------
# Tests — CORS
# ---------------------------------------------------------------------------


class TestCorsMiddleware:
    def test_wildcard_allows_any_origin(self):
        client = build_cors_client(allow_origins=["*"])
        r = client.get("/api/data", headers={"origin": "https://anywhere.com"})
        assert r.status_code == 200
        assert r.header("access-control-allow-origin") is not None

    def test_specific_origin_allowed(self):
        client = build_cors_client(allow_origins=["https://app.example.com"])
        r = client.get("/api/data", headers={"origin": "https://app.example.com"})
        acao = r.header("access-control-allow-origin") or ""
        assert "app.example.com" in acao

    def test_specific_origin_reflected_in_response(self):
        client = build_cors_client(allow_origins=["https://app.example.com"])
        r = client.get("/api/data", headers={"origin": "https://app.example.com"})
        assert r.header("access-control-allow-origin") == "https://app.example.com"

    def test_access_control_allow_methods_present(self):
        client = build_cors_client()
        r = client.get("/api/data", headers={"origin": "https://example.com"})
        methods = r.header("access-control-allow-methods") or ""
        assert "GET" in methods

    def test_no_origin_header_still_returns_200(self):
        client = build_cors_client()
        r = client.get("/api/data")
        assert r.status_code == 200

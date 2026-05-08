"""Integration tests for skill 25: API Versioning."""

from __future__ import annotations

from lauren import LaurenFactory, controller, get, module
from lauren.testing import TestClient
from lauren.types import Request, Response


# ---------------------------------------------------------------------------
# URL-prefix versioning
# ---------------------------------------------------------------------------


@controller("/api/v1/users")
class UsersV1Controller:
    @get("/")
    async def list(self) -> dict:
        return {"version": "v1", "users": [{"id": 1, "name": "Alice"}]}

    @get("/{user_id}")
    async def get_user(self, user_id: int) -> dict:
        return {"version": "v1", "id": user_id, "name": "Alice"}


@controller("/api/v2/users")
class UsersV2Controller:
    @get("/")
    async def list(self) -> dict:
        return {
            "version": "v2",
            "users": [{"id": 1, "name": "Alice", "email": "alice@example.com"}],
        }

    @get("/{user_id}")
    async def get_user(self, user_id: int) -> dict:
        return {
            "version": "v2",
            "id": user_id,
            "name": "Alice",
            "email": "alice@example.com",
        }


@module(controllers=[UsersV1Controller, UsersV2Controller])
class VersionedApiModule:
    pass


# ---------------------------------------------------------------------------
# Header versioning
# ---------------------------------------------------------------------------


@controller("/header/users")
class UsersHeaderController:
    @get("/")
    async def list(self, request: Request) -> dict:
        version = request.headers.get("accept-version", "v1")
        if version == "v2":
            return {
                "version": "v2",
                "users": [{"id": 1, "name": "Alice", "email": "alice@example.com"}],
            }
        return {"version": "v1", "users": [{"id": 1, "name": "Alice"}]}


@module(controllers=[UsersHeaderController])
class HeaderVersionedModule:
    pass


# ---------------------------------------------------------------------------
# Content negotiation versioning
# ---------------------------------------------------------------------------


@controller("/ct/users")
class UsersContentTypeController:
    @get("/")
    async def list(self, request: Request) -> Response:
        accept = request.headers.get("accept", "")
        if "application/vnd.myapp.v2+json" in accept:
            return Response.json(
                {
                    "version": "v2",
                    "users": [{"id": 1, "name": "Alice", "email": "alice@example.com"}],
                }
            )
        return Response.json({"version": "v1", "users": [{"id": 1, "name": "Alice"}]})


@module(controllers=[UsersContentTypeController])
class ContentNegotiatedModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_url_versioned() -> TestClient:
    return TestClient(LaurenFactory.create(VersionedApiModule))


def build_header_versioned() -> TestClient:
    return TestClient(LaurenFactory.create(HeaderVersionedModule))


def build_content_versioned() -> TestClient:
    return TestClient(LaurenFactory.create(ContentNegotiatedModule))


# ---------------------------------------------------------------------------
# Tests — URL prefix versioning
# ---------------------------------------------------------------------------


class TestUrlPrefixVersioning:
    def test_v1_list(self) -> None:
        client = build_url_versioned()
        r = client.get("/api/v1/users/")
        assert r.status_code == 200
        assert r.json()["version"] == "v1"

    def test_v2_list(self) -> None:
        client = build_url_versioned()
        r = client.get("/api/v2/users/")
        assert r.status_code == 200
        assert r.json()["version"] == "v2"

    def test_v1_response_has_no_email(self) -> None:
        client = build_url_versioned()
        users = client.get("/api/v1/users/").json()["users"]
        assert "email" not in users[0]

    def test_v2_response_has_email(self) -> None:
        client = build_url_versioned()
        users = client.get("/api/v2/users/").json()["users"]
        assert "email" in users[0]

    def test_v1_get_single_user(self) -> None:
        client = build_url_versioned()
        r = client.get("/api/v1/users/1")
        assert r.status_code == 200
        assert r.json()["version"] == "v1"
        assert r.json()["id"] == 1

    def test_v2_get_single_user_has_email(self) -> None:
        client = build_url_versioned()
        r = client.get("/api/v2/users/1")
        assert r.status_code == 200
        assert "email" in r.json()

    def test_v1_and_v2_coexist(self) -> None:
        client = build_url_versioned()
        r1 = client.get("/api/v1/users/")
        r2 = client.get("/api/v2/users/")
        assert r1.json()["version"] == "v1"
        assert r2.json()["version"] == "v2"


# ---------------------------------------------------------------------------
# Tests — header versioning
# ---------------------------------------------------------------------------


class TestHeaderVersioning:
    def test_default_version_is_v1(self) -> None:
        client = build_header_versioned()
        r = client.get("/header/users/")
        assert r.json()["version"] == "v1"

    def test_accept_version_v2_header(self) -> None:
        client = build_header_versioned()
        r = client.get("/header/users/", headers={"accept-version": "v2"})
        assert r.json()["version"] == "v2"
        assert "email" in r.json()["users"][0]

    def test_explicit_v1_header(self) -> None:
        client = build_header_versioned()
        r = client.get("/header/users/", headers={"accept-version": "v1"})
        assert r.json()["version"] == "v1"


# ---------------------------------------------------------------------------
# Tests — content negotiation versioning
# ---------------------------------------------------------------------------


class TestContentNegotiationVersioning:
    def test_default_accept_returns_v1(self) -> None:
        client = build_content_versioned()
        r = client.get("/ct/users/")
        assert r.json()["version"] == "v1"

    def test_v2_mime_type_returns_v2(self) -> None:
        client = build_content_versioned()
        r = client.get(
            "/ct/users/",
            headers={"accept": "application/vnd.myapp.v2+json"},
        )
        assert r.json()["version"] == "v2"
        assert "email" in r.json()["users"][0]

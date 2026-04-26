"""Unit tests for the radix-tree router."""

from __future__ import annotations

import pytest

from lauren._routing import Router
from lauren.exceptions import (
    MethodNotAllowedError,
    RouteNotFoundError,
    RouterConflictError,
)


def _noop():
    return None


class TestStaticRoutes:
    def test_single_static_route(self):
        r = Router()
        r.add_route("GET", "/users", _noop)
        entry, params = r.find("GET", "/users")
        assert entry.path_template == "/users"
        assert params == {}

    def test_root_route(self):
        r = Router()
        r.add_route("GET", "/", _noop)
        entry, params = r.find("GET", "/")
        assert entry.path_template == "/"
        assert params == {}

    def test_nested_static(self):
        r = Router()
        r.add_route("GET", "/api/v1/users", _noop)
        entry, _ = r.find("GET", "/api/v1/users")
        assert entry.path_template == "/api/v1/users"

    def test_trailing_slash_normalized(self):
        r = Router()
        r.add_route("GET", "/users/", _noop)
        entry, _ = r.find("GET", "/users")
        assert entry.path_template == "/users"


class TestParamRoutes:
    def test_single_param(self):
        r = Router()
        r.add_route("GET", "/users/{id}", _noop)
        entry, params = r.find("GET", "/users/42")
        assert params == {"id": "42"}
        assert entry.param_names == ("id",)

    def test_multiple_params(self):
        r = Router()
        r.add_route("GET", "/users/{uid}/posts/{pid}", _noop)
        entry, params = r.find("GET", "/users/alice/posts/7")
        assert params == {"uid": "alice", "pid": "7"}

    def test_static_preferred_over_param(self):
        r = Router()
        r.add_route("GET", "/users/me", _noop)
        r.add_route("GET", "/users/{id}", _noop)
        entry, params = r.find("GET", "/users/me")
        assert entry.path_template == "/users/me"
        assert params == {}

    def test_param_fallback_when_static_misses(self):
        r = Router()
        r.add_route("GET", "/users/me", _noop)
        r.add_route("GET", "/users/{id}", _noop)
        entry, params = r.find("GET", "/users/42")
        assert params == {"id": "42"}


class TestWildcardRoutes:
    def test_wildcard_captures_remainder(self):
        r = Router()
        r.add_route("GET", "/files/{*path}", _noop)
        entry, params = r.find("GET", "/files/a/b/c.txt")
        assert params == {"path": "a/b/c.txt"}

    def test_wildcard_empty(self):
        r = Router()
        r.add_route("GET", "/files/{*path}", _noop)
        entry, params = r.find("GET", "/files/")
        # root of /files with trailing slash normalized -> /files
        # doesn't match wildcard directly, try /files/ -> empty remainder
        entry, params = r.find("GET", "/files")
        assert params.get("path") == ""

    def test_wildcard_must_be_last(self):
        r = Router()
        with pytest.raises(RouterConflictError):
            r.add_route("GET", "/files/{*path}/more", _noop)


class TestConflictsAndErrors:
    def test_duplicate_route_conflict(self):
        r = Router()
        r.add_route("GET", "/x", _noop)
        with pytest.raises(RouterConflictError):
            r.add_route("GET", "/x", _noop)

    def test_param_name_conflict(self):
        r = Router()
        r.add_route("GET", "/users/{id}", _noop)
        with pytest.raises(RouterConflictError):
            r.add_route("GET", "/users/{uid}/extra", _noop)

    def test_not_found(self):
        r = Router()
        r.add_route("GET", "/x", _noop)
        with pytest.raises(RouteNotFoundError):
            r.find("GET", "/y")

    def test_method_not_allowed(self):
        r = Router()
        r.add_route("GET", "/x", _noop)
        r.add_route("POST", "/x", _noop)
        with pytest.raises(MethodNotAllowedError) as exc_info:
            r.find("DELETE", "/x")
        assert "GET" in exc_info.value.allow
        assert "POST" in exc_info.value.allow

    def test_unsupported_method(self):
        r = Router()
        with pytest.raises(ValueError):
            r.add_route("BREW", "/coffee", _noop)

    def test_frozen_router(self):
        r = Router()
        r.add_route("GET", "/x", _noop)
        r.freeze()
        with pytest.raises(RuntimeError):
            r.add_route("GET", "/y", _noop)


class TestMultipleMethods:
    def test_same_path_different_methods(self):
        r = Router()
        r.add_route("GET", "/users", _noop)
        r.add_route("POST", "/users", _noop)
        r.add_route("DELETE", "/users", _noop)
        assert r.find("GET", "/users")[0].method == "GET"
        assert r.find("POST", "/users")[0].method == "POST"
        assert r.find("DELETE", "/users")[0].method == "DELETE"

    def test_allowed_methods(self):
        r = Router()
        r.add_route("GET", "/x", _noop)
        r.add_route("POST", "/x", _noop)
        assert set(r.allowed_methods("/x")) == {"GET", "POST"}


class TestRoutesList:
    def test_routes_returns_all(self):
        r = Router()
        r.add_route("GET", "/a", _noop)
        r.add_route("POST", "/b", _noop)
        routes = r.routes()
        assert len(routes) == 2

    def test_deep_nesting(self):
        r = Router()
        r.add_route("GET", "/a/b/c/d/e/f/g", _noop)
        entry, _ = r.find("GET", "/a/b/c/d/e/f/g")
        assert entry.path_template == "/a/b/c/d/e/f/g"

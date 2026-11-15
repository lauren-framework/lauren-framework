"""Unit tests for the radix-tree router."""

from __future__ import annotations

import pytest

from lauren import RoutingConfig
from lauren._routing import RouteRedirect, Router, decode_segment, raw_routing_path
from lauren.exceptions import (
    BadRequestError,
    DecoratorUsageError,
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

    def test_param_names_belong_to_routes_not_tree_positions(self):
        r = Router()
        r.add_route("GET", "/users/{id}", _noop)
        r.add_route("GET", "/users/{uid}/extra", _noop)
        assert r.find("GET", "/users/7")[1] == {"id": "7"}
        assert r.find("GET", "/users/7/extra")[1] == {"uid": "7"}

    def test_templates_identical_up_to_param_names_conflict(self):
        r = Router()
        r.add_route("GET", "/users/{id}", _noop)
        with pytest.raises(RouterConflictError):
            r.add_route("GET", "/users/{uid}", _noop)

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
        assert set(r.allowed_methods("/x")) == {"GET", "HEAD", "POST"}


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


# ---------------------------------------------------------------------------
# HTTP semantics (RoutingConfig)
# ---------------------------------------------------------------------------


def _router(**config: object) -> Router:
    return Router(RoutingConfig(**config))  # type: ignore[arg-type]


class TestImplicitHead:
    def test_head_falls_back_to_get(self):
        r = _router()
        get_entry = r.add_route("GET", "/x/{id}", _noop)
        r.freeze()
        entry, params = r.find("HEAD", "/x/1")
        assert entry is get_entry
        assert params == {"id": "1"}

    def test_head_falls_back_to_get_on_the_static_fast_path(self):
        r = _router()
        get_entry = r.add_route("GET", "/health", _noop)
        r.freeze()
        assert r.find("HEAD", "/health")[0] is get_entry

    def test_explicit_head_wins(self):
        r = _router()
        r.add_route("GET", "/x", _noop)
        head_entry = r.add_route("HEAD", "/x", _noop)
        r.freeze()
        assert r.find("HEAD", "/x")[0] is head_entry

    def test_auto_head_off_restores_405(self):
        r = _router(auto_head=False)
        r.add_route("GET", "/x", _noop)
        r.freeze()
        with pytest.raises(MethodNotAllowedError) as exc_info:
            r.find("HEAD", "/x")
        assert exc_info.value.allow == ["GET"]

    def test_allow_lists_head_only_with_get(self):
        r = _router()
        r.add_route("POST", "/x", _noop)
        r.freeze()
        assert r.allowed_methods("/x") == ["POST"]


class TestImplicitOptions:
    def test_auto_options_adds_options_to_allow(self):
        r = _router(auto_options=True)
        r.add_route("GET", "/x/{id}", _noop)
        r.freeze()
        with pytest.raises(MethodNotAllowedError) as exc_info:
            r.find("OPTIONS", "/x/1")
        assert exc_info.value.allow == ["GET", "HEAD", "OPTIONS"]

    def test_explicit_options_route_wins(self):
        r = _router(auto_options=True)
        r.add_route("GET", "/x", _noop)
        options_entry = r.add_route("OPTIONS", "/x", _noop)
        r.freeze()
        assert r.find("OPTIONS", "/x")[0] is options_entry


class TestAnyMethod:
    def test_any_method_route_answers_every_method(self):
        r = _router()
        any_entry = r.add_route("*", "/proxy/{*rest}", _noop)
        r.freeze()
        for method in ("GET", "POST", "DELETE", "OPTIONS"):
            entry, params = r.find(method, "/proxy/a/b")
            assert entry is any_entry
            assert params == {"rest": "a/b"}

    def test_explicit_method_beats_any_method_at_a_node(self):
        r = _router()
        get_entry = r.add_route("GET", "/hook", _noop)
        any_entry = r.add_route("*", "/hook", _noop)
        r.freeze()
        assert r.find("GET", "/hook")[0] is get_entry
        assert r.find("HEAD", "/hook")[0] is get_entry
        assert r.find("PUT", "/hook")[0] is any_entry

    def test_any_method_node_never_answers_405(self):
        r = _router()
        r.add_route("*", "/hook", _noop)
        r.freeze()
        assert r.allowed_methods("/hook") == sorted(Router.HTTP_METHODS)
        assert r.static_route_count == 1

    def test_two_any_method_routes_conflict(self):
        r = _router()
        r.add_route("*", "/hook/{a}", _noop)
        with pytest.raises(RouterConflictError):
            r.add_route("*", "/hook/{b}", _noop)


class TestTrailingSlash:
    @pytest.mark.parametrize("path", ["/items/1", "/items/1/"])
    def test_ignore_matches_both_spellings(self, path):
        r = _router()
        r.add_route("GET", "/items/{id}", _noop)
        r.freeze()
        assert r.find("GET", path)[1] == {"id": "1"}

    def test_strict_keeps_spellings_apart(self):
        r = _router(trailing_slash="strict")
        bare = r.add_route("GET", "/x", _noop)
        slashed = r.add_route("GET", "/x/", _noop)
        r.freeze()
        assert r.find("GET", "/x")[0] is bare
        assert r.find("GET", "/x/")[0] is slashed
        assert slashed.path_template == "/x/"

    def test_strict_misses_the_other_spelling(self):
        r = _router(trailing_slash="strict")
        r.add_route("GET", "/items/{id}", _noop)
        r.freeze()
        with pytest.raises(RouteNotFoundError):
            r.find("GET", "/items/1/")

    @pytest.mark.parametrize(
        ("declared", "requested", "target"),
        [("/items/{id}", "/items/1/", "/items/1"), ("/dir/", "/dir", "/dir/")],
    )
    def test_redirect_points_at_the_declared_spelling(self, declared, requested, target):
        r = _router(trailing_slash="redirect")
        r.add_route("GET", declared, _noop)
        r.freeze()
        with pytest.raises(RouteRedirect) as exc_info:
            r.find("GET", requested)
        assert exc_info.value.path == target

    def test_redirect_mode_still_404s_unknown_paths(self):
        r = _router(trailing_slash="redirect")
        r.add_route("GET", "/x", _noop)
        r.freeze()
        with pytest.raises(RouteNotFoundError):
            r.find("GET", "/y/")

    def test_redirect_mode_root_is_canonical(self):
        r = _router(trailing_slash="redirect")
        r.add_route("GET", "/x", _noop)
        r.freeze()
        with pytest.raises(RouteNotFoundError):
            r.find("GET", "/")


class TestMergeSlashes:
    def test_merge_collapses_runs_of_slashes(self):
        r = _router(merge_slashes=True)
        r.add_route("GET", "/items/{id}", _noop)
        r.freeze()
        assert r.find("GET", "//items///1")[1] == {"id": "1"}

    def test_merge_with_redirect_redirects_to_the_merged_path(self):
        r = _router(merge_slashes=True, trailing_slash="redirect")
        r.add_route("GET", "/items/{id}", _noop)
        r.freeze()
        with pytest.raises(RouteRedirect) as exc_info:
            r.find("GET", "/items//1/")
        assert exc_info.value.path == "/items/1"

    def test_merge_with_redirect_404s_when_nothing_matches(self):
        r = _router(merge_slashes=True, trailing_slash="redirect")
        r.add_route("GET", "/items/{id}", _noop)
        r.freeze()
        with pytest.raises(RouteNotFoundError):
            r.find("GET", "/nothing//here")


class TestRawPath:
    def test_encoded_slash_stays_inside_its_segment(self):
        r = _router(match_raw_path=True)
        r.add_route("GET", "/files/{name}", _noop)
        r.freeze()
        assert r.find("GET", "/files/a%2Fb")[1] == {"name": "a/b"}

    def test_segments_are_decoded_as_utf8(self):
        r = _router(match_raw_path=True)
        r.add_route("GET", "/café/{q}", _noop)
        r.freeze()
        assert r.find("GET", "/caf%C3%A9/%E2%82%AC")[1] == {"q": "€"}

    @pytest.mark.parametrize("path", ["/files/%ZZ", "/files/%C3%28", "/files/%"])
    def test_malformed_encoding_is_a_400(self, path):
        r = _router(match_raw_path=True)
        r.add_route("GET", "/files/{name}", _noop)
        r.freeze()
        with pytest.raises(BadRequestError) as exc_info:
            r.find("GET", path)
        assert exc_info.value.detail["reason"] == "malformed_path_encoding"
        assert r.allowed_methods(path) == []

    def test_unencoded_paths_use_the_static_fast_path(self):
        r = _router(match_raw_path=True)
        entry = r.add_route("GET", "/health", _noop)
        r.freeze()
        assert r.find("GET", "/health")[0] is entry

    def test_non_latin1_segment_is_decoded_as_utf8(self):
        assert decode_segment("€%20x") == "€ x"

    @pytest.mark.parametrize(
        ("raw", "path", "expected"),
        [
            (b"/files/a%2Fb", "/files/a/b", "/files/a%2Fb"),
            (b"/api/files/a%2Fb?x=1", "/files/a/b", "/files/a%2Fb"),
            (b"/api/", "/", "/"),
            (b"/api", "/", "/"),
            (None, "/café x", "/caf%C3%A9%20x"),
            (b"/elsewhere", "/files/x", "/files/x"),
        ],
    )
    def test_raw_routing_path_strips_the_prefix_from_the_raw_form(self, raw, path, expected):
        assert raw_routing_path(raw, path) == expected


class TestHostRouting:
    def _router(self) -> Router:
        r = _router()
        r.add_route("GET", "/", _noop, host="{tenant}.example.com")
        r.add_route("GET", "/", _noop, host="admin.example.com")
        r.add_route("GET", "/", _noop, host="{a}.{b}.example.com")
        r.add_route("GET", "/", _noop)
        r.add_route("POST", "/only-plain", _noop)
        r.freeze()
        return r

    def test_exact_host_wins_over_parameterized(self):
        entry, _, host_params = self._router().find_host("GET", "/", "ADMIN.example.com:8443")
        assert entry.host == "admin.example.com"
        assert host_params == {}

    def test_parameterized_host_extracts_lower_cased_labels(self):
        entry, _, host_params = self._router().find_host("GET", "/", "Acme.example.com")
        assert entry.host == "{tenant}.example.com"
        assert host_params == {"tenant": "acme"}

    def test_fewer_parameters_first_then_more(self):
        entry, _, host_params = self._router().find_host("GET", "/", "x.y.example.com")
        assert entry.host == "{a}.{b}.example.com"
        assert host_params == {"a": "x", "b": "y"}

    @pytest.mark.parametrize("host", ["other.org", None, ""])
    def test_host_miss_falls_back_to_host_agnostic_routes(self, host):
        entry, _, host_params = self._router().find_host("GET", "/", host)
        assert entry.host is None
        assert host_params == {}

    def test_path_miss_in_a_host_router_falls_through(self):
        entry, _, _ = self._router().find_host("POST", "/only-plain", "acme.example.com")
        assert entry.path_template == "/only-plain"

    def test_405_unions_allow_across_candidates(self):
        with pytest.raises(MethodNotAllowedError) as exc_info:
            self._router().find_host("DELETE", "/", "acme.example.com")
        assert exc_info.value.allow == ["GET", "HEAD"]

    def test_unknown_path_is_a_404(self):
        with pytest.raises(RouteNotFoundError):
            self._router().find_host("GET", "/nope", "acme.example.com")

    def test_routes_lists_host_routed_entries(self):
        r = self._router()
        assert r.has_hosts
        assert len(r.routes()) == 5

    def test_same_host_method_and_path_conflict(self):
        r = _router()
        r.add_route("GET", "/", _noop, host="{t}.example.com")
        with pytest.raises(RouterConflictError):
            r.add_route("GET", "/", _noop, host="{t}.example.com")

    def test_host_param_shadowing_a_path_param_conflicts(self):
        r = _router()
        with pytest.raises(RouterConflictError) as exc_info:
            r.add_route("GET", "/{tenant}", _noop, host="{tenant}.example.com")
        assert exc_info.value.detail["param"] == "tenant"

    def test_malformed_host_is_a_decorator_usage_error(self):
        with pytest.raises(DecoratorUsageError):
            _router().add_route("GET", "/", _noop, host="example.com:80")

    def test_entry_builds_its_host(self):
        r = _router()
        entry = r.add_route("GET", "/", _noop, host="{tenant}.example.com")
        assert entry.build_host(tenant="acme") == "acme.example.com"
        assert entry.handler_key == ("GET", "{tenant}.example.com/")

    def test_redirect_policy_applies_across_host_candidates(self):
        r = _router(trailing_slash="redirect")
        r.add_route("GET", "/docs", _noop, host="{t}.example.com")
        r.freeze()
        with pytest.raises(RouteRedirect) as exc_info:
            r.find_host("GET", "/docs/", "acme.example.com")
        assert exc_info.value.path == "/docs"


class TestInstanceMethods:
    def test_methods_are_an_instance_property(self):
        before = Router.HTTP_METHODS
        ws = Router(methods=frozenset({"WEBSOCKET"}))
        ws.add_route("WEBSOCKET", "/ws", _noop)
        assert Router.HTTP_METHODS is before
        assert ws.methods == frozenset({"WEBSOCKET"})
        with pytest.raises(ValueError):
            Router().add_route("WEBSOCKET", "/ws", _noop)

    def test_concurrent_websocket_router_builds_never_touch_the_class(self):
        import threading

        before = Router.HTTP_METHODS
        errors: list[BaseException] = []

        def build() -> None:
            try:
                for i in range(200):
                    Router(methods={"WEBSOCKET"}).add_route("WEBSOCKET", f"/ws/{i}", _noop)
                    assert Router.HTTP_METHODS is before
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=build) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert Router.HTTP_METHODS is before

    def test_router_rejects_a_non_config(self):
        with pytest.raises(TypeError):
            Router("strict")  # type: ignore[arg-type]

    def test_config_property(self):
        config = RoutingConfig(trailing_slash="strict")
        assert Router(config).config is config


def test_deep_static_method_miss_falls_back_to_dynamic_sibling() -> None:
    router = Router()
    router.add_route("GET", "/items/special", _noop)
    dynamic = router.add_route("POST", "/items/{slug:slug}", _noop)
    router.freeze()

    entry, params = router.find("POST", "/items/special")

    assert entry is dynamic
    assert params == {"slug": "special"}


def test_dynamic_method_miss_falls_back_to_less_specific_sibling() -> None:
    router = Router()
    router.add_route("GET", "/items/{code:re([a-z]+)}", _noop)
    fallback = router.add_route("POST", "/items/{slug:slug}", _noop)
    router.freeze()

    entry, params = router.find("POST", "/items/blue-mug")

    assert entry is fallback
    assert params == {"slug": "blue-mug"}


def test_root_static_method_miss_falls_back_to_empty_wildcard() -> None:
    router = Router()
    router.add_route("GET", "/", _noop)
    fallback = router.add_route("POST", "/{*rest}", _noop)
    router.freeze()

    entry, params = router.find("POST", "/")

    assert entry is fallback
    assert params == {"rest": ""}


@pytest.mark.parametrize(
    ("headers", "host"),
    [
        ([(b"Host", b"Example.com")], "Example.com"),
        ([(b":authority", b"Example.com:443")], "Example.com:443"),
        ([(b"host", b"a.test"), (b":authority", b"b.test")], "a.test"),
        ([], None),
    ],
)
def test_websocket_scope_host_header(headers, host) -> None:
    from lauren._ws_runtime import _scope_host

    assert _scope_host({"headers": headers}) == host

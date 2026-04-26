"""Unit tests for :class:`lauren._routing.Router`'s static-prefix fast path.

These tests confirm:

* After :meth:`Router.freeze` every purely-static route is present in
  the flat ``_static_table`` keyed on the canonical path.
* Routes containing a ``{param}`` or ``{*wildcard}`` segment are
  excluded from the table \u2014 even the static prefix leading to them.
* Lookups for static paths hit the fast path and return the shared
  :data:`_EMPTY_PARAMS` sentinel rather than an allocated dict.
* Lookups for dynamic paths fall through cleanly to the radix walk.
* ``MethodNotAllowed`` behaviour is preserved, including the subtle
  case where a static path shares its URL with a dynamic sibling
  that defines a different method.
* Path normalisation (trailing slash, leading whitespace) is applied
  consistently across both code paths so the two paths are
  behaviourally indistinguishable to the caller.
"""

from __future__ import annotations

import pytest

from lauren._routing import Router
from lauren._routing import _EMPTY_PARAMS
from lauren.exceptions import MethodNotAllowedError, RouteNotFoundError


def _handler() -> None:  # placeholder handler, never invoked
    pass


# ---------------------------------------------------------------------------
# Fast-path population at freeze
# ---------------------------------------------------------------------------


def test_static_routes_land_in_fast_table() -> None:
    router = Router()
    router.add_route("GET", "/health", _handler)
    router.add_route("GET", "/metrics", _handler)
    router.add_route("POST", "/webhook", _handler)
    router.freeze()

    assert "/health" in router._static_table
    assert "/metrics" in router._static_table
    assert "/webhook" in router._static_table


def test_dynamic_routes_excluded_from_fast_table() -> None:
    router = Router()
    router.add_route("GET", "/users/{id}", _handler)
    router.add_route("GET", "/files/{*path}", _handler)
    router.freeze()

    # Param / wildcard routes are not keyed in the fast table.
    assert "/users/{id}" not in router._static_table
    assert "/users" not in router._static_table  # no handler at the prefix
    assert "/files/{*path}" not in router._static_table


def test_static_prefix_of_dynamic_route_is_not_included() -> None:
    """A static node that only exists as a stepping stone toward a
    ``{param}`` child must not leak into the fast table \u2014 no route
    has registered handlers at that node, so there's nothing to map.
    """
    router = Router()
    router.add_route("GET", "/users/{id}", _handler)
    router.freeze()

    # ``/users`` is traversed but has no handlers, so not keyed.
    assert "/users" not in router._static_table


def test_static_and_dynamic_at_same_depth_coexist() -> None:
    """``/api/ping`` (static) and ``/api/{name}`` (dynamic) must both
    work \u2014 the fast table picks up ``/api/ping`` while the radix
    walk serves the parametric branch."""
    router = Router()
    router.add_route("GET", "/api/ping", _handler)
    router.add_route("GET", "/api/{name}", _handler)
    router.freeze()

    assert "/api/ping" in router._static_table
    assert "/api/{name}" not in router._static_table


def test_static_route_count_matches_method_entries() -> None:
    router = Router()
    router.add_route("GET", "/a", _handler)
    router.add_route("POST", "/a", _handler)  # same path, diff method
    router.add_route("GET", "/b", _handler)
    router.add_route("GET", "/dynamic/{id}", _handler)
    router.freeze()

    # GET /a, POST /a, GET /b \u2014 three method entries across two paths.
    assert router.static_route_count == 3


# ---------------------------------------------------------------------------
# Fast-path lookup behaviour
# ---------------------------------------------------------------------------


def test_static_lookup_returns_shared_empty_params_sentinel() -> None:
    """The fast path returns the shared ``_EMPTY_PARAMS`` dict rather
    than allocating a new one per request. We assert identity (``is``)
    because the saving is proportional to traffic volume.
    """
    router = Router()
    router.add_route("GET", "/health", _handler)
    router.freeze()

    entry, params = router.find("GET", "/health")
    assert entry.path_template == "/health"
    assert params is _EMPTY_PARAMS


def test_static_lookup_after_freeze_returns_correct_entry() -> None:
    router = Router()
    h1, h2 = lambda: None, lambda: None
    router.add_route("GET", "/alpha", h1)
    router.add_route("GET", "/beta", h2)
    router.freeze()

    entry, _ = router.find("GET", "/alpha")
    assert entry.handler is h1
    entry, _ = router.find("GET", "/beta")
    assert entry.handler is h2


def test_static_lookup_honours_trailing_slash_normalisation() -> None:
    router = Router()
    router.add_route("GET", "/health", _handler)
    router.freeze()

    # The canonical form drops trailing slashes; both spellings must hit.
    entry1, _ = router.find("GET", "/health")
    entry2, _ = router.find("GET", "/health/")
    assert entry1 is entry2


def test_static_lookup_method_not_allowed_when_no_dynamic_routes() -> None:
    router = Router()
    router.add_route("GET", "/health", _handler)
    router.freeze()

    with pytest.raises(MethodNotAllowedError) as exc_info:
        router.find("POST", "/health")
    # The ``allow`` list must be populated so the dispatcher can emit
    # a correct ``Allow:`` header.
    assert exc_info.value.allow == ["GET"]


def test_static_lookup_falls_through_when_dynamic_sibling_exists() -> None:
    """If a static path matches but the method doesn't, and dynamic
    routes exist in the router, we must not raise ``MethodNotAllowed``
    eagerly \u2014 a ``{param}`` route elsewhere in the tree may legally
    pick up this path+method combo.
    """
    router = Router()
    router.add_route("GET", "/item", _handler)
    router.add_route("POST", "/{anything}", _handler)  # dynamic sibling
    router.freeze()

    # ``POST /item`` must resolve via the dynamic route, not fail.
    entry, params = router.find("POST", "/item")
    assert entry.path_template == "/{anything}"
    assert params == {"anything": "item"}


def test_static_lookup_returns_not_found_for_unknown_path() -> None:
    router = Router()
    router.add_route("GET", "/known", _handler)
    router.freeze()

    with pytest.raises(RouteNotFoundError):
        router.find("GET", "/unknown")


# ---------------------------------------------------------------------------
# Slow-path behaviour is preserved
# ---------------------------------------------------------------------------


def test_dynamic_route_still_works_after_freeze() -> None:
    router = Router()
    router.add_route("GET", "/users/{id}", _handler)
    router.freeze()

    entry, params = router.find("GET", "/users/42")
    assert entry.path_template == "/users/{id}"
    assert params == {"id": "42"}


def test_wildcard_route_still_works_after_freeze() -> None:
    router = Router()
    router.add_route("GET", "/files/{*path}", _handler)
    router.freeze()

    entry, params = router.find("GET", "/files/a/b/c.txt")
    assert entry.path_template == "/files/{*path}"
    assert params == {"path": "a/b/c.txt"}


def test_static_takes_priority_over_param_at_same_depth() -> None:
    """The static-first priority rule must hold via the fast path too.
    Registering ``/api/ping`` and ``/api/{name}`` means ``GET
    /api/ping`` hits the static entry, not the parametric one.
    """
    router = Router()
    static = router.add_route("GET", "/api/ping", _handler)
    router.add_route("GET", "/api/{name}", _handler)
    router.freeze()

    entry, params = router.find("GET", "/api/ping")
    assert entry is static
    assert params is _EMPTY_PARAMS


# ---------------------------------------------------------------------------
# has_dynamic_routes flag — correctness invariant
# ---------------------------------------------------------------------------


def test_has_dynamic_routes_false_for_all_static_router() -> None:
    router = Router()
    router.add_route("GET", "/a", _handler)
    router.add_route("GET", "/b", _handler)
    router.freeze()
    assert router._has_dynamic_routes is False


def test_has_dynamic_routes_true_with_param_route() -> None:
    router = Router()
    router.add_route("GET", "/a", _handler)
    router.add_route("GET", "/b/{id}", _handler)
    router.freeze()
    assert router._has_dynamic_routes is True


def test_has_dynamic_routes_true_with_wildcard_route() -> None:
    router = Router()
    router.add_route("GET", "/static", _handler)
    router.add_route("GET", "/files/{*path}", _handler)
    router.freeze()
    assert router._has_dynamic_routes is True


# ---------------------------------------------------------------------------
# Router without freeze() \u2014 fast path is inert, slow path still works
# ---------------------------------------------------------------------------


def test_lookup_without_freeze_still_works() -> None:
    """The fast table is populated in ``freeze``. Routers that skip
    freeze (a couple of test fixtures, plus any user that hand-rolls
    an in-process test) still route correctly via the radix walk.
    """
    router = Router()
    router.add_route("GET", "/alpha", _handler)
    # Deliberately do NOT freeze.
    entry, params = router.find("GET", "/alpha")
    assert entry.path_template == "/alpha"
    assert params == {}


def test_freeze_is_idempotent() -> None:
    router = Router()
    router.add_route("GET", "/x", _handler)
    router.freeze()
    table_before = dict(router._static_table)
    router.freeze()  # second call must be a no-op
    assert router._static_table == table_before


# ---------------------------------------------------------------------------
# allowed_methods uses the fast path too
# ---------------------------------------------------------------------------


def test_allowed_methods_hits_static_table() -> None:
    router = Router()
    router.add_route("GET", "/health", _handler)
    router.add_route("HEAD", "/health", _handler)
    router.add_route("POST", "/health", _handler)
    router.freeze()

    assert router.allowed_methods("/health") == ["GET", "HEAD", "POST"]


def test_allowed_methods_falls_back_to_radix_for_dynamic() -> None:
    router = Router()
    router.add_route("GET", "/users/{id}", _handler)
    router.freeze()

    assert router.allowed_methods("/users/42") == ["GET"]


def test_allowed_methods_returns_empty_for_unknown_path() -> None:
    router = Router()
    router.add_route("GET", "/known", _handler)
    router.freeze()

    assert router.allowed_methods("/unknown") == []

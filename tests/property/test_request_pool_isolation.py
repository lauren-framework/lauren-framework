"""Property tests for :meth:`lauren.types.Request.reset` pool-reuse hygiene.

Two invariants, both as properties:

* **Leakage** — for *any* attribute name a contributor might attach, reusing a
  pooled :class:`~lauren.types.Request` must never expose it.
* **Aliasing** — for *any* per-request payload, a container a caller retained
  from request N must not be mutated when request N+1 reuses the object. The
  accessors return their containers *by reference*, so ``reset`` has to allocate
  fresh ones rather than clear the previous request's dict in place.

Mirrors the arena's own "correctness invariant" testing style.
"""

from __future__ import annotations

from typing import Any

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import assume, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren.types import AppState, ClientInfo, Headers, Request, ServerInfo  # noqa: E402

#: Names that legitimately exist on a ``Request`` — the canonical fields
#: ``reset`` re-populates, plus every attribute the class itself defines
#: (properties, methods, ``__dict__``, dunders). These cannot be shadowed by
#: ``setattr`` and so are excluded from the generated name space.
_RESERVED = frozenset(
    {
        "_method",
        "_path",
        "_raw_query_string",
        "_headers",
        "_path_params",
        "_query_params",
        "_client",
        "_server",
        "_receive",
        "_body",
        "_body_consumed",
        "_state",
        "_app_state",
        "_max_body_size",
        "_matched_route",
        "_handler_class",
        "_handler_func",
        "_route_template",
        "_cookies",
    }
) | frozenset(dir(Request))


async def _noop_receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _make_request(
    *,
    raw_query_string: bytes = b"",
    cookie_header: str | None = None,
) -> Request:
    """Build a bare ``Request``; optional raw query / cookie header for the
    lazily-parsed ``query_params`` / ``cookies`` containers.
    """
    headers = Headers([("cookie", cookie_header)]) if cookie_header is not None else Headers()
    return Request(
        method="GET",
        path="/a",
        raw_query_string=raw_query_string,
        headers=headers,
        client=ClientInfo(None, None),
        server=ServerInfo(None, None),
        receive=_noop_receive,
        app_state=AppState(),
        max_body_size=1024,
    )


def _reset_kwargs() -> dict[str, Any]:
    return {
        "method": "GET",
        "path": "/b",
        "raw_query_string": b"",
        "headers": Headers(),
        "client": ClientInfo(None, None),
        "server": ServerInfo(None, None),
        "receive": _noop_receive,
        "app_state": AppState(),
        "max_body_size": 1024,
    }


@settings(max_examples=300, deadline=None)
@given(
    name=st.text(
        alphabet=st.characters(whitelist_categories=("L", "Nd", "Pc")),
        min_size=1,
        max_size=32,
    )
)
def test_no_arbitrary_attribute_survives_reset(name: str) -> None:
    assume(name not in _RESERVED)
    req = _make_request()
    setattr(req, name, "leaked")
    req.reset(**_reset_kwargs())
    assert not hasattr(req, name)


# -- Container aliasing --------------------------------------------------------
# ``reset`` must do more than hide the previous request's data: because the
# accessors hand out their containers *by reference* (``types.py:397`` returns
# ``self._path_params`` itself), a container a caller kept must survive intact.
# The failure mode here is not leakage but aliasing — request N+1 reusing the
# pooled object must not mutate a dict that request N's code still holds.

#: Keys/values mirror what a router extracts: identifier-ish text for the
#: parameter *name*, free text for the *value*.
_PARAM_NAME = st.text(
    alphabet=st.characters(whitelist_categories=("L", "Nd", "Pc")),
    min_size=1,
    max_size=12,
)
_PATH_PARAMS = st.dictionaries(_PARAM_NAME, st.text(max_size=12), max_size=6)

#: Small alphabets so generated payloads routinely collide with the fixed
#: request-N payloads below, which is where aliasing shows up most sharply.
_RAW_QUERY_STRING = st.text(alphabet="ab=&", max_size=12)
_COOKIE_HEADER = st.text(alphabet="abc ;=", max_size=12)


def _populate_path_params(req: Request, params: dict[str, str]) -> None:
    """Mirror the router's population step (``lauren/_asgi/__init__.py:1174``).

    The router fills ``_path_params`` *in place* (``clear`` + ``update``) instead
    of replacing it, so driving the property through the same two statements is
    what makes it representative of a real request.
    """
    req._path_params.clear()
    req._path_params.update(params)


@settings(max_examples=300, deadline=None)
@given(n_params=_PATH_PARAMS, next_params=_PATH_PARAMS)
def test_retained_path_params_are_not_mutated_by_a_later_reset(
    n_params: dict[str, str], next_params: dict[str, str]
) -> None:
    """Request N's ``path_params`` dict is untouched when N+1 reuses the object."""
    # Only *differing* payloads can reveal an in-place clobber: were the router
    # to populate the same params again, a mutating ``reset`` would still leave
    # the retained dict looking correct and the test would pass vacuously. Make
    # them differ by construction (the sentinel key contains ``\x00``, which the
    # letter/digit/connector alphabet of the key strategy above cannot produce)
    # rather than with an ``assume``, so no example is discarded on the way in.
    next_params = {**next_params, "\x00next-request": "1"}

    req = _make_request()
    _populate_path_params(req, n_params)
    held = req.path_params
    assert held == n_params

    req.reset(**_reset_kwargs())  # request N+1 takes the pooled instance over
    assert req.path_params is not held  # reset allocated a fresh dict...
    assert req.path_params == {}  # ...which it hands the router empty
    assert held == n_params  # N's dict was not emptied by reset

    _populate_path_params(req, next_params)  # the router fills the *new* dict
    assert req.path_params == next_params
    assert held == n_params  # ← the invariant: N's dict survived N+1 intact
    assert held is not req.path_params


@settings(max_examples=200, deadline=None)
@given(raw_query_string=_RAW_QUERY_STRING, cookie_header=_COOKIE_HEADER)
def test_retained_lazy_caches_are_not_mutated_by_a_later_reset(
    raw_query_string: str, cookie_header: str
) -> None:
    """Retained ``query_params`` / ``cookies`` dicts also survive a reset.

    Both are built on first access and cached, so they too are containers handed
    to user code by reference; an arbitrary request N+1 must not disturb them.
    """
    req = _make_request(
        raw_query_string=b"k=1&k=2",
        cookie_header="session=abc; theme=dark",
    )
    held_query_params = req.query_params
    held_cookies = req.cookies
    assert held_query_params == {"k": ["1", "2"]}
    assert held_cookies == {"session": "abc", "theme": "dark"}

    kwargs = _reset_kwargs()
    kwargs["raw_query_string"] = raw_query_string.encode("latin-1", "replace")
    kwargs["headers"] = Headers([("cookie", cookie_header)])
    req.reset(**kwargs)

    # Whatever N+1 parses, it must be a new object, never the retained one.
    assert req.query_params is not held_query_params
    assert req.cookies is not held_cookies
    assert held_query_params == {"k": ["1", "2"]}
    assert held_cookies == {"session": "abc", "theme": "dark"}

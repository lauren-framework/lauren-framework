"""Property test: no arbitrary attribute survives ``Request.reset``.

Mirrors the arena's own "correctness invariant" testing style, but as a
property: for *any* attribute name a contributor might attach, reusing a
pooled :class:`~lauren.types.Request` must never expose it.
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


def _make_request() -> Request:
    return Request(
        method="GET",
        path="/a",
        raw_query_string=b"",
        headers=Headers(),
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

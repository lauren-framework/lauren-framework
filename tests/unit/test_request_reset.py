"""Unit tests for :meth:`lauren.types.Request.reset` pool-reuse hygiene.

:class:`~lauren._arena.RequestArena` pools and reuses
:class:`~lauren.types.Request` instances. ``reset`` must therefore guarantee
that *no* attribute from the previous request — canonical or user-attached —
survives onto the next one, otherwise an unrelated request served by the same
pooled object could observe stale data (a silent cross-request data leak).
"""

from __future__ import annotations

from typing import Any

from lauren.types import AppState, ClientInfo, Headers, Request, ServerInfo

#: Every attribute ``Request.__init__`` / ``reset`` sets. Written as an explicit
#: literal so that adding a field to ``__init__`` without also setting it in
#: ``reset`` fails ``test_reset_attribute_set_is_exact`` below.
_EXPECTED_RESET_ATTRS = frozenset(
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
)


async def _noop_receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _reset_kwargs(path: str = "/second") -> dict[str, Any]:
    return {
        "method": "POST",
        "path": path,
        "raw_query_string": b"x=1",
        "headers": Headers([("x", "y")]),
        "client": ClientInfo("10.0.0.1", 80),
        "server": ServerInfo("example.com", 443),
        "receive": _noop_receive,
        "app_state": AppState(),
        "max_body_size": 1024,
    }


def _make_request(path: str = "/first") -> Request:
    return Request(
        method="GET",
        path=path,
        raw_query_string=b"a=1",
        headers=Headers([("cookie", "session=abc")]),
        client=ClientInfo(None, None),
        server=ServerInfo(None, None),
        receive=_noop_receive,
        app_state=AppState(),
        max_body_size=1_048_576,
    )


class TestResetClearsArbitraryAttributes:
    """The core invariant: user/extension attributes never survive a reset."""

    def test_reset_clears_arbitrary_attributes(self) -> None:
        req = _make_request()
        setattr(req, "custom", 1)
        setattr(req, "_private_cache", {"x": 1})
        req.reset(**_reset_kwargs())
        assert not hasattr(req, "custom")
        assert not hasattr(req, "_private_cache")

    def test_reset_clears_upload_cache(self) -> None:
        # The multipart parse cache the UploadFile extractor stashes.
        req = _make_request()
        setattr(req, "__lauren_upload_cache__", {"file": []})
        req.reset(**_reset_kwargs())
        assert not hasattr(req, "__lauren_upload_cache__")

    def test_reset_clears_any_dunder_ish_extension_attribute(self) -> None:
        req = _make_request()
        setattr(req, "__lauren_future_cache__", object())
        req.reset(**_reset_kwargs())
        assert not hasattr(req, "__lauren_future_cache__")


class TestResetRepopulatesCanonicalFields:
    def test_reset_repopulates_canonical_fields(self) -> None:
        req = _make_request()
        req.reset(**_reset_kwargs("/second"))
        assert req.method == "POST"
        assert req.path == "/second"
        assert req.headers.raw() == [("x", "y")]
        assert req.path_params == {}
        # The query-params cache must reflect *this* request's raw string
        # (``x=1``), never the previous request's (``a=1``).
        assert req.query_params == {"x": ["1"]}
        assert req.cookies == {}
        assert req.state.asdict() == {}
        assert req.get_matched_route() is None
        assert req.get_handler_class() is None
        assert req.get_route_handler_func() is None
        assert req.get_route_template() is None
        assert req._body is None
        assert req._body_consumed is False

    def test_reset_installs_fresh_state_instance(self) -> None:
        req = _make_request()
        old_state = req.state
        req.reset(**_reset_kwargs())
        assert req.state is not old_state
        assert req.state.asdict() == {}

    def test_reset_attribute_set_is_exact(self) -> None:
        """A field added to ``__init__`` but not ``reset`` fails this test."""
        req = _make_request()
        req.reset(**_reset_kwargs())
        assert set(req.__dict__) == _EXPECTED_RESET_ATTRS

    def test_reset_allocates_fresh_path_params(self) -> None:
        """A retained reference to ``path_params`` is not mutated by reset."""
        req = _make_request()
        held = req.path_params
        held["user_id"] = "42"
        req.reset(**_reset_kwargs())
        assert req.path_params is not held
        assert held == {"user_id": "42"}  # the retained dict is untouched
        assert req.path_params == {}

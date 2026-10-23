"""Unit tests for the development error page helpers in ``lauren._asgi._debug`` (F05)."""

from __future__ import annotations

from typing import Any

import pytest

from lauren import DebugConfig
from lauren._asgi import _debug
from lauren.types import ClientInfo, Headers, Request


def _request(host: str | None = "127.0.0.1", **kwargs: Any) -> Request:
    return Request(method="GET", path="/x", client=ClientInfo(host, 1234), **kwargs)


class _App:
    """The attributes the collector reads from a ``LaurenApp``."""

    error_format = "default"
    _handlers: dict[Any, Any] = {}


@pytest.mark.parametrize(
    "accept,expected",
    [
        (None, False),
        ("", False),
        ("*/*", False),
        ("application/json", False),
        ("text/html", True),
        ("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", True),
        ("application/xhtml+xml", True),
        ("text/html, application/json", False),
        ("text/html;q=0.5, application/json", False),
        ("application/json;q=0.5, text/html", True),
        ("application/problem+json, text/html;q=0.9", False),
        ("text/html;q=bogus", False),
        ("text/html;q=0", False),
    ],
)
def test_prefers_html_only_for_an_explicit_html_preference(accept: str | None, expected: bool) -> None:
    assert _debug.prefers_html(accept) is expected


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("127.8.9.10", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("fe80::1%eth0", False),
        ("::1%lo", True),
        ("10.0.0.1", False),
        ("203.0.113.5", False),
        ("::ffff:10.0.0.1", False),
        ("localhost", False),
        ("", False),
        (None, False),
    ],
)
def test_is_loopback(host: str | None, expected: bool) -> None:
    assert _debug.is_loopback(host) is expected


def test_client_allowed_respects_allow_remote() -> None:
    remote = _request("203.0.113.5")

    assert _debug.client_allowed(DebugConfig(), remote) is False
    assert _debug.client_allowed(DebugConfig(allow_remote=True), remote) is True


def test_resolve_config_turns_true_into_the_defaults() -> None:
    assert _debug.resolve_config(True) == DebugConfig()


def test_resolve_config_accepts_list_redaction_entries() -> None:
    config = DebugConfig(redact_headers=["x-secret"])  # type: ignore[arg-type]

    assert _debug.resolve_config(config) is config


def _raise_chain() -> BaseException:
    try:
        try:
            raise KeyError("inner")
        except KeyError:
            raise ValueError("middle")  # implicit __context__
    except ValueError as exc:
        try:
            raise RuntimeError("outer") from exc
        except RuntimeError as outer:
            return outer
    raise AssertionError("unreachable")


def test_chain_follows_cause_then_context() -> None:
    chain = _debug._chain(_raise_chain())

    assert [(c["relation"], c["type"]) for c in chain] == [("cause", "ValueError"), ("context", "KeyError")]
    assert chain[0]["location"].endswith("in _raise_chain")


def test_chain_respects_suppressed_context() -> None:
    try:
        try:
            raise KeyError("hidden")
        except KeyError:
            raise ValueError("shown") from None
    except ValueError as exc:
        assert _debug._chain(exc) == []


def test_chain_stops_on_a_cycle() -> None:
    first, second = ValueError("a"), KeyError("b")
    first.__cause__ = second
    second.__cause__ = first

    assert [c["type"] for c in _debug._chain(first)] == ["KeyError"]


def test_chain_is_bounded() -> None:
    head = current = ValueError("0")
    for index in range(1, 30):
        nxt = ValueError(str(index))
        current.__cause__ = nxt
        current = nxt

    assert len(_debug._chain(head)) == _debug._MAX_CHAIN


def test_text_survives_a_broken_str_and_truncates() -> None:
    class Broken:
        def __str__(self) -> str:
            raise RuntimeError("no")

    assert _debug._text(Broken()) == "<unprintable Broken>"
    assert _debug._text("x" * 10_000).endswith("…")


def test_safe_repr_survives_a_failing_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(value: object) -> str:
        raise RuntimeError("no")

    monkeypatch.setattr(_debug._repr, "repr", broken)

    assert _debug._safe_repr(object()) == "<unrepresentable object>"


def test_collect_without_a_traceback_or_route() -> None:
    info = _debug.collect(ValueError("x"), request=_request(), app=_App(), config=DebugConfig())

    assert info["frames"] == [] and info["route"] is None
    assert info["request"]["client"] == "127.0.0.1"


def test_collect_with_an_unknown_compiled_route() -> None:
    from lauren._routing import RouteEntry

    request = _request()
    request._matched_route = RouteEntry(method="GET", path_template="/gone", handler=lambda: None)

    info = _debug.collect(ValueError("x"), request=request, app=_App(), config=DebugConfig())

    assert info["route"] == {"method": "GET", "path_template": "/gone"}


def test_raised_at_without_a_traceback() -> None:
    assert _debug.raised_at(ValueError("x"), DebugConfig()) == {"raised_at": None, "traceback": []}


def test_render_html_handles_a_minimal_report() -> None:
    info = _debug.collect(ValueError("plain"), request=_request(None), app=_App(), config=DebugConfig())

    page = _debug.render_html(info)

    assert page.startswith("<!DOCTYPE html>")
    assert "did not match a route" in page
    assert "Exception chain" not in page and "Traceback" not in page


def test_request_summary_redacts_by_substring_case_insensitively() -> None:
    request = Request(
        method="GET",
        path="/x",
        raw_query_string=b"Access_Token=abc&page=2",
        headers=Headers([("X-Upstream-Authorization", "secret"), ("Accept", "*/*")]),
    )

    summary = _debug._request(request, DebugConfig())

    assert summary["query"] == {"Access_Token": ["[redacted]"], "page": ["2"]}
    assert ["x-upstream-authorization", "[redacted]"] in summary["headers"] or [
        "X-Upstream-Authorization",
        "[redacted]",
    ] in summary["headers"]


def test_production_profile_is_none_without_the_config_module(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = _debug.importlib.import_module

    def _import(name: str, *args: Any) -> Any:
        if name == "lauren.config":
            raise ImportError(name)
        return real_import(name, *args)

    monkeypatch.setattr(_debug.importlib, "import_module", _import)

    class _Container:
        def singletons(self) -> dict[Any, Any]:
            raise AssertionError("must not be consulted")

    assert _debug.production_profile(_Container()) is None


def test_qualname_of_none_is_empty() -> None:
    assert _debug._qualname(None) == ""


def test_frames_without_source_have_no_context() -> None:
    namespace: dict[str, Any] = {}
    exec(compile("def boom():\n    raise ValueError('no source')\n", "<generated>", "exec"), namespace)
    try:
        namespace["boom"]()
    except ValueError as exc:
        frames = _debug._frames(exc.__traceback__, DebugConfig())

    assert frames[-1]["file"] == "<generated>"
    assert frames[-1]["context"] == []


def test_frames_tolerate_a_missing_line_number(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        raise ValueError("x")
    except ValueError as exc:
        error = exc
    frame = error.__traceback__.tb_frame  # type: ignore[union-attr]
    monkeypatch.setattr(_debug.traceback, "walk_tb", lambda tb: iter([(frame, None)]))

    frames = _debug._frames(error.__traceback__, DebugConfig())

    assert frames[0]["line"] is None and frames[0]["context"] == []

"""Property tests: generated URLs route back to their route; the proxy resolver is safe."""

from __future__ import annotations

from urllib.parse import parse_qsl, unquote

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren._routing import Router  # noqa: E402
from lauren._urls._forwarded import compile_proxy, resolve_forwarded  # noqa: E402
from lauren._urls._template import UrlTemplate  # noqa: E402
from lauren.types import Headers  # noqa: E402
from lauren.urls import URL, ProxyConfig  # noqa: E402

TEMPLATES = (
    "/users/{id}",
    "/users/{id}/posts/{post}",
    "/files/{*path}",
    "/a/{x}/b/{*rest}",
    "/static",
)

#: Path-segment text a server can deliver intact: no "/", no whitespace or
#: control characters (the router trims surrounding whitespace).
_SEGMENT_CHARS = st.characters(blacklist_categories=("Cs", "Cc", "Zs", "Zl", "Zp"), blacklist_characters="/")
_segment = st.text(_SEGMENT_CHARS, min_size=1, max_size=12)
_wildcard = st.lists(_segment, min_size=0, max_size=4).map("/".join)


def _router() -> Router:
    router = Router()
    for template in TEMPLATES:
        router.add_route("GET", template, lambda: None)
    router.freeze()
    return router


ROUTER = _router()


@st.composite
def _route_and_params(draw: st.DrawFn) -> tuple[str, dict[str, str]]:
    template = draw(st.sampled_from(TEMPLATES))
    params = {}
    for name in sorted(UrlTemplate(template).param_names):
        params[name] = draw(_wildcard if name in ("path", "rest") else _segment)
    return template, params


@given(_route_and_params())
@settings(max_examples=150, deadline=None)
def test_decoded_generated_path_routes_back_with_the_original_values(
    case: tuple[str, dict[str, str]],
) -> None:
    template, params = case
    path = UrlTemplate(template).expand(params, label=template)

    entry, found = ROUTER.find("GET", unquote(path))  # what an ASGI server delivers

    assert entry.path_template == template
    assert found == params


@given(_route_and_params())
@settings(max_examples=100, deadline=None)
def test_raw_generated_path_routes_back_with_the_encoded_values(case: tuple[str, dict[str, str]]) -> None:
    template, params = case
    path = UrlTemplate(template).expand(params, label=template)

    entry, found = ROUTER.find("GET", path)

    assert entry.path_template == template
    assert {name: unquote(value) for name, value in found.items()} == params


_text = st.text(st.characters(blacklist_categories=("Cs",)), max_size=20)


@given(st.lists(st.tuples(_text.filter(bool), _text), max_size=6))
@settings(max_examples=100, deadline=None)
def test_with_query_round_trips_through_a_query_parser(pairs: list[tuple[str, str]]) -> None:
    url = URL("/s").with_query(pairs)

    expected: dict[str, list[str]] = {}
    for key, value in pairs:
        expected.setdefault(key, []).append(value)
    parsed: dict[str, list[str]] = {}
    for key, value in parse_qsl(url.query, keep_blank_values=True):
        parsed.setdefault(key, []).append(value)
    assert parsed == expected


PROXY = compile_proxy(ProxyConfig(trusted=["10.0.0.0/8"], headers="both", max_hops=3, trust_prefix=True))
_HEADER_NAMES = st.sampled_from(
    [
        "x-forwarded-for",
        "x-forwarded-proto",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-prefix",
        "forwarded",
    ]
)
_HEADER_VALUE = st.text(st.characters(min_codepoint=0x20, max_codepoint=0x7E), max_size=40)
_HEADERS = st.lists(st.tuples(_HEADER_NAMES, _HEADER_VALUE), max_size=6)
_UNTRUSTED = st.ip_addresses(v=4).filter(lambda ip: not ip.is_private).map(str)


@given(_UNTRUSTED, _HEADERS)
@settings(max_examples=100, deadline=None)
def test_untrusted_peers_never_influence_the_request(peer: str, headers: list[tuple[str, str]]) -> None:
    assert resolve_forwarded(PROXY, peer, Headers(headers)) is None


@given(st.sampled_from(["10.0.0.1", "10.255.0.7"]), _HEADERS, st.booleans())
@settings(max_examples=150, deadline=None)
def test_resolver_never_raises_and_only_reports_forwarded_values(
    peer: str, headers: list[tuple[str, str]], websocket: bool
) -> None:
    result = resolve_forwarded(PROXY, peer, Headers(headers), websocket=websocket)

    if result is None:
        return
    sent = " ".join(value for _, value in headers)
    if result.client is not None:
        assert result.client[0].strip("[]") in sent
    if result.scheme is not None:
        assert result.scheme in ({"ws", "wss"} if websocket else {"http", "https"})
    if result.prefix is not None:
        assert result.prefix.startswith("/") and not result.prefix.startswith("//")

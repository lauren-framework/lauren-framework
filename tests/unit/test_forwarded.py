"""``resolve_forwarded``: trust decisions and header parsing, table-driven."""

from __future__ import annotations

import pytest

from lauren._urls._forwarded import Forwarded, compile_proxy, resolve_forwarded
from lauren.exceptions import StartupError
from lauren.types import Headers
from lauren.urls import ProxyConfig

TRUSTED = ["10.0.0.0/8", "127.0.0.1"]


def _resolve(
    peer: str | None, sent: dict | list, /, *, websocket: bool = False, **config: object
) -> Forwarded | None:
    proxy = compile_proxy(ProxyConfig(trusted=config.pop("trusted", TRUSTED), **config))  # type: ignore[arg-type]
    items = list(sent.items()) if isinstance(sent, dict) else sent
    return resolve_forwarded(proxy, peer, Headers(items), websocket=websocket)


# ---------------------------------------------------------------------------
# Trust
# ---------------------------------------------------------------------------


def test_untrusted_peer_ignores_every_forwarding_header() -> None:
    headers = {
        "x-forwarded-for": "1.2.3.4",
        "x-forwarded-proto": "https",
        "x-forwarded-host": "evil.example",
        "x-forwarded-prefix": "/evil",
        "forwarded": "for=1.2.3.4;proto=https",
    }

    assert _resolve("203.0.113.5", headers, trust_prefix=True, headers="both") is None


def test_trusted_peer_without_forwarding_headers_resolves_nothing() -> None:
    assert _resolve("10.0.0.2", {}) is None


@pytest.mark.parametrize(
    ("peer", "trusted", "expected"),
    [
        ("10.1.2.3", ["10.0.0.0/8"], True),
        ("::ffff:10.1.2.3", ["10.0.0.0/8"], True),
        ("2001:db8::1", ["2001:db8::/32"], True),
        ("11.0.0.1", ["10.0.0.0/8"], False),
        ("testclient", ["10.0.0.0/8"], False),
        (None, ["unix"], True),
        (None, ["10.0.0.0/8"], False),
        ("8.8.8.8", ["*"], True),
    ],
)
def test_peer_trust(peer: str | None, trusted: list[str], expected: bool) -> None:
    assert compile_proxy(ProxyConfig(trusted=trusted)).is_trusted(peer) is expected


def test_a_single_string_is_one_trusted_entry() -> None:
    assert compile_proxy(ProxyConfig(trusted="10.0.0.0/8")).is_trusted("10.9.9.9")


# ---------------------------------------------------------------------------
# X-Forwarded-*
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("xff", "max_hops", "client"),
    [
        ("203.0.113.9", 1, "203.0.113.9"),
        ("1.1.1.1, 203.0.113.9", 1, "203.0.113.9"),  # leftmost is client-supplied
        ("203.0.113.9, 10.0.0.7", 1, "10.0.0.7"),  # one hop only
        ("203.0.113.9, 10.0.0.7", 2, "203.0.113.9"),  # trusted interior hop walked
        ("1.1.1.1, 203.0.113.9, 10.0.0.7", 3, "203.0.113.9"),  # stops at the first untrusted
        ("10.0.0.8, 10.0.0.7", 5, "10.0.0.8"),  # every hop trusted: the leftmost
        ("unknown, 10.0.0.7", 2, "unknown"),
        ("_hidden, 10.0.0.7", 2, "_hidden"),
    ],
)
def test_client_is_the_first_untrusted_hop_from_the_right(xff: str, max_hops: int, client: str) -> None:
    result = _resolve("10.0.0.1", {"x-forwarded-for": xff}, max_hops=max_hops)

    assert result is not None and result.client is not None
    assert result.client[0] == client


def test_repeated_header_lines_form_one_list() -> None:
    result = _resolve("10.0.0.1", [("x-forwarded-for", "1.1.1.1"), ("x-forwarded-for", "203.0.113.9")])

    assert result is not None and result.client == ("203.0.113.9", None)


@pytest.mark.parametrize(
    ("node", "client"),
    [
        ("203.0.113.9:4711", ("203.0.113.9", 4711)),
        ("[2001:db8::1]:4711", ("2001:db8::1", 4711)),
        ("2001:db8::1", ("2001:db8::1", None)),
        ("203.0.113.9:_obf", ("203.0.113.9", None)),
        ("[2001:db8::1", ("[2001:db8::1", None)),
    ],
)
def test_forwarded_for_nodes_with_ports(node: str, client: tuple) -> None:
    result = _resolve("10.0.0.1", {"x-forwarded-for": node})

    assert result is not None and result.client == client


def test_scheme_host_port_and_prefix_come_from_the_trusted_hop() -> None:
    result = _resolve(
        "10.0.0.1",
        {
            "x-forwarded-for": "203.0.113.9",
            "x-forwarded-proto": "http, https",
            "x-forwarded-host": "evil.example, api.example.com",
            "x-forwarded-port": "8443",
            "x-forwarded-prefix": "/backend/",
        },
        trust_prefix=True,
    )

    assert result == Forwarded(
        client=("203.0.113.9", None), scheme="https", host="api.example.com", port=8443, prefix="/backend"
    )


def test_values_for_an_interior_client_hop_are_picked_by_position() -> None:
    result = _resolve(
        "10.0.0.1",
        {"x-forwarded-for": "203.0.113.9, 10.0.0.7", "x-forwarded-proto": "https, http"},
        max_hops=2,
    )

    assert result is not None and result.scheme == "https"


def test_a_shorter_value_list_does_not_accept_client_supplied_metadata() -> None:
    result = _resolve(
        "10.0.0.1", {"x-forwarded-for": "203.0.113.9, 10.0.0.7", "x-forwarded-proto": "https"}, max_hops=2
    )

    assert result is not None and result.scheme is None


def test_prefix_is_ignored_unless_trusted() -> None:
    result = _resolve("10.0.0.1", {"x-forwarded-prefix": "/api", "x-forwarded-proto": "https"})

    assert result == Forwarded(scheme="https")


@pytest.mark.parametrize(
    ("header", "value"),
    [
        ("x-forwarded-proto", "javascript"),
        ("x-forwarded-host", "evil.example/path"),
        ("x-forwarded-host", "a b"),
        ("x-forwarded-port", "70000"),
        ("x-forwarded-port", "http"),
        ("x-forwarded-prefix", "api"),
        ("x-forwarded-prefix", "//evil.example"),
        ("x-forwarded-prefix", "/a/../b"),
        ("x-forwarded-prefix", "/a?b"),
        ("x-forwarded-prefix", "/a\tb"),
    ],
)
def test_invalid_values_are_ignored(header: str, value: str) -> None:
    assert _resolve("10.0.0.1", {header: value}, trust_prefix=True) is None


@pytest.mark.parametrize(
    ("proto", "scheme"), [("https", "wss"), ("http", "ws"), ("wss", "wss"), ("WS", "ws")]
)
def test_websocket_schemes_map_to_ws_and_wss(proto: str, scheme: str) -> None:
    result = _resolve("10.0.0.1", {"x-forwarded-proto": proto}, websocket=True)

    assert result is not None and result.scheme == scheme


def test_server_applied_keeps_client_and_scheme() -> None:
    result = _resolve(
        "10.0.0.1",
        {
            "x-forwarded-for": "203.0.113.9",
            "x-forwarded-proto": "https",
            "x-forwarded-host": "api.example.com",
        },
        server_applied=True,
    )

    assert result == Forwarded(host="api.example.com")


# ---------------------------------------------------------------------------
# RFC 7239 Forwarded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (
            "for=203.0.113.9;proto=https;host=api.example.com",
            Forwarded(("203.0.113.9", None), "https", "api.example.com"),
        ),
        ('for="[2001:db8::1]:4711";proto=https', Forwarded(("2001:db8::1", 4711), "https")),
        (
            'for=1.1.1.1, for=203.0.113.9;host="api.example.com"',
            Forwarded(("203.0.113.9", None), host="api.example.com"),
        ),
        ('for="a\\"b, c";proto=https, for=203.0.113.9', Forwarded(("203.0.113.9", None))),
        ("For=203.0.113.9;Proto=https", Forwarded(("203.0.113.9", None), "https")),
        ("for=unknown;proto=https", Forwarded(("unknown", None), "https")),
        ("proto=https", Forwarded(scheme="https")),
    ],
)
def test_forwarded_header(header: str, expected: Forwarded) -> None:
    assert _resolve("10.0.0.1", {"forwarded": header}, headers="forwarded") == expected


def test_forwarded_walk_uses_the_element_of_the_client_hop() -> None:
    header = "for=203.0.113.9;proto=https;host=api.example.com, for=10.0.0.7;proto=http"

    result = _resolve("10.0.0.1", {"forwarded": header}, headers="forwarded", max_hops=2)

    assert result == Forwarded(("203.0.113.9", None), "https", "api.example.com")


@pytest.mark.parametrize("header", ['for="unterminated', "for", "=x", "for=a;=b", "for=a b"])
def test_malformed_forwarded_header_is_ignored(header: str) -> None:
    assert _resolve("10.0.0.1", {"forwarded": header}, headers="forwarded") is None


def test_both_mode_prefers_forwarded_and_falls_back_to_x_forwarded() -> None:
    headers = {"forwarded": "for=203.0.113.9", "x-forwarded-for": "198.51.100.1"}

    assert _resolve("10.0.0.1", headers, headers="both").client == ("203.0.113.9", None)  # type: ignore[union-attr]
    assert _resolve("10.0.0.1", {"x-forwarded-for": "198.51.100.1"}, headers="both").client == (  # type: ignore[union-attr]
        "198.51.100.1",
        None,
    )


def test_forwarded_mode_ignores_x_forwarded_headers() -> None:
    assert _resolve("10.0.0.1", {"x-forwarded-for": "198.51.100.1"}, headers="forwarded") is None


def test_forwarded_mode_still_reads_a_trusted_prefix() -> None:
    result = _resolve("10.0.0.1", {"x-forwarded-prefix": "/api"}, headers="forwarded", trust_prefix=True)

    assert result == Forwarded(prefix="/api")


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "field"),
    [
        (ProxyConfig(trusted=[]), "trusted"),
        (ProxyConfig(trusted=["10.0.0.0/33"]), "trusted"),
        (ProxyConfig(trusted=["not-an-ip"]), "trusted"),
        (ProxyConfig(trusted=[10]), "trusted"),  # type: ignore[list-item]
        (ProxyConfig(trusted=["10.0.0.1"], headers="x-real-ip"), "headers"),  # type: ignore[arg-type]
        (ProxyConfig(trusted=["10.0.0.1"], max_hops=0), "max_hops"),
        (ProxyConfig(trusted=["10.0.0.1"], max_hops=True), "max_hops"),
        (ProxyConfig(trusted=["10.0.0.1"], trust_prefix="yes"), "trust_prefix"),  # type: ignore[arg-type]
    ],
)
def test_invalid_proxy_config_is_a_startup_error(config: ProxyConfig, field: str) -> None:
    with pytest.raises(StartupError) as info:
        compile_proxy(config)

    assert info.value.detail == {"reason": "invalid_proxy_config", "field": field}


def test_non_proxy_config_value_is_a_startup_error() -> None:
    with pytest.raises(StartupError) as info:
        compile_proxy({"trusted": ["10.0.0.1"]})

    assert info.value.detail["reason"] == "invalid_proxy_config"


def test_extremely_large_forwarded_port_is_ignored_not_a_server_error() -> None:
    result = _resolve("10.0.0.1", {"x-forwarded-for": "[2001:db8::1]:" + "9" * 5000})

    assert result is not None and result.client == ("2001:db8::1", None)


def test_a_malformed_forwarded_header_does_not_fall_back_to_other_headers() -> None:
    result = _resolve(
        "10.0.0.1",
        {
            "forwarded": 'for="unterminated',
            "x-forwarded-host": "evil.example",
            "x-forwarded-for": "203.0.113.9",
        },
        headers="both",
    )

    assert result is None


def test_unix_peer_string_is_trusted_only_when_explicitly_configured() -> None:
    assert compile_proxy(ProxyConfig(trusted=["unix"])).is_trusted("unix")
    assert not compile_proxy(ProxyConfig(trusted=["10.0.0.0/8"])).is_trusted("unix")
